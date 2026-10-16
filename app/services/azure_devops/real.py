from __future__ import annotations

import base64
from datetime import datetime
from typing import Any

import httpx

from app.config import Settings
from app.models.incidents import Comment, Incident
from app.services.azure_devops.base import WorkItemProvider
from app.services.azure_devops.mapping import WorkItemMapping


class AzureDevOpsError(RuntimeError):
    pass


class AzureDevOpsProvider(WorkItemProvider):
    """Azure DevOps REST API, read-only (https://learn.microsoft.com/en-us/rest/api/azure/devops)."""

    name = "azure_devops"

    def __init__(self, settings: Settings, mapping: WorkItemMapping):
        if not settings.ado_configured:
            raise AzureDevOpsError(
                "Azure DevOps is not configured. Set ADO_ORG_URL, ADO_PROJECT and ADO_PAT."
            )
        self.settings = settings
        self.mapping = mapping
        org = (settings.ado_org_url or "").rstrip("/")
        project = (settings.ado_project or "").replace(" ", "%20")
        self._base = f"{org}/{project}/_apis"
        token = base64.b64encode(f":{settings.ado_pat}".encode()).decode()
        self._headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json-patch+json",
        }
        self._read_headers = {**self._headers, "Content-Type": "application/json"}
        self._client = httpx.Client(timeout=30.0)

    # --- HTTP helpers ----------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    def _translate(self, exc: Exception) -> AzureDevOpsError:
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if status in (401, 403):
                return AzureDevOpsError(
                    "Azure DevOps rejected the credentials ("
                    f"{status}). Check ADO_ORG_URL, ADO_PROJECT and ADO_PAT in .env "
                    "(the PAT may be expired or missing the Work Items scope)."
                )
            if status == 404:
                return AzureDevOpsError(
                    "Azure DevOps returned 404. Verify ADO_ORG_URL and ADO_PROJECT "
                    "in .env point at an existing project."
                )
            return AzureDevOpsError(f"Azure DevOps API error: HTTP {status}.")
        if isinstance(exc, httpx.HTTPError):
            return AzureDevOpsError(
                "Could not reach Azure DevOps: "
                f"{exc.__class__.__name__} ({exc}). Check network/proxy access to dev.azure.com."
            )
        return AzureDevOpsError(str(exc))

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        params = {**(params or {}), "api-version": self.settings.ado_api_version}
        try:
            resp = self._client.get(self._url(path), params=params, headers=self._read_headers)
            resp.raise_for_status()
        except (httpx.HTTPStatusError, httpx.HTTPError) as exc:
            raise self._translate(exc) from exc
        return resp.json()

    def _patch(self, path: str, patch: list[dict[str, Any]], params: dict[str, Any] | None = None) -> Any:
        params = {**(params or {}), "api-version": self.settings.ado_api_version}
        try:
            resp = self._client.patch(
                self._url(path), json=patch, params=params, headers=self._headers
            )
            resp.raise_for_status()
        except (httpx.HTTPStatusError, httpx.HTTPError) as exc:
            raise self._translate(exc) from exc
        return resp.json()

    def _post(self, path: str, payload: Any, params: dict[str, Any] | None = None) -> Any:
        params = {**(params or {}), "api-version": self.settings.ado_api_version}
        try:
            resp = self._client.post(
                self._url(path), json=payload, params=params, headers=self._read_headers
            )
            resp.raise_for_status()
        except (httpx.HTTPStatusError, httpx.HTTPError) as exc:
            raise self._translate(exc) from exc
        return resp.json()

    # --- READ ------------------------------------------------------------
    def list_work_item_types(self) -> list[str]:
        data = self._get("/wit/workitemtypes")
        return [t["name"] for t in data.get("value", [])]

    def _wiql_types(self) -> str:
        types = [t for t in self.mapping.incident_types if t not in self.mapping.excluded_types]
        return ", ".join(f"'{t}'" for t in types)

    def list_incidents(self) -> list[Incident]:
        wiql = (
            "SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.TeamProject] = @project AND [System.WorkItemType] IN ({self._wiql_types()}) "
            "ORDER BY [System.ChangedDate] DESC"
        )
        result = self._post("/wit/wiql", {"query": wiql})
        ids = [r["id"] for r in result.get("workItems", [])]
        if not ids:
            return []
        # NOTE: "$expand" cannot be combined with "fields" (400
        # ConflictingParametersException). "$expand=all" already returns all
        # fields plus relations. IDs are fetched in batches so large projects
        # do not exceed URL length limits.
        incidents: list[Incident] = []
        for start in range(0, min(len(ids), 2000), 50):
            chunk = ids[start : start + 50]
            id_param = ",".join(str(i) for i in chunk)
            data = self._get(
                "/wit/workitems",
                {"ids": id_param, "$expand": "all"},
            )
            incidents.extend(
                self.mapping.to_incident(w) for w in data.get("value", [])
            )
        return incidents

    def get_incident(self, incident_id: str) -> Incident | None:
        numeric = "".join(ch for ch in str(incident_id) if ch.isdigit())
        if not numeric:
            return None
        data = self._get(f"/wit/workitems/{numeric}", {"$expand": "all"})
        incident = self.mapping.to_incident(data, comments=self.get_comments(numeric))
        return incident

    def get_comments(self, incident_id: str) -> list[Comment]:
        numeric = "".join(ch for ch in str(incident_id) if ch.isdigit())
        if not numeric:
            return []
        try:
            data = self._get(f"/wit/workitems/{numeric}/comments")
        except AzureDevOpsError:
            return []
        from app.services.azure_devops.mapping import _display_name, _to_naive_utc

        comments: list[Comment] = []
        for c in data.get("comments", []) or data.get("value", []):
            published = _to_naive_utc(c.get("publishedDate") or c.get("createdDate"))
            text = str(c.get("text") or "")
            comments.append(
                Comment(
                    author=_display_name(c.get("createdBy")) or "Unknown",
                    created_at=published or datetime.now(),
                    text=text,
                )
            )
        return comments

    def close(self) -> None:
        self._client.close()
