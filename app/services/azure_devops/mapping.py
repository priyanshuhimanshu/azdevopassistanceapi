from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from app.models.incidents import Comment, Incident, Relation


def _to_naive_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, dict):  # OData style {"$": "..."}
        value = value.get("$")
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, dict):
        # Azure DevOps identity fields arrive as objects like
        # {"displayName": "Jane", ...}; OData style uses {"$": "..."}.
        for key in ("displayName", "display_name", "name", "$"):
            if value.get(key):
                value = value.get(key)
                break
        else:
            return None
        if value is None:
            return None
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    text = str(value).strip()
    return text or None


def _display_name(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    if "<" in text:
        return text.split("<", 1)[0].strip()
    return text


def _identity_parts(value: Any) -> tuple[str | None, str | None]:
    """Return (display_name, email) from an ADO identity value.

    Handles REST identity objects (displayName/uniqueName/mailAddress), OData
    ``{"$": "Name <email>"}`` strings and bare e-mail addresses.
    """
    if value is None:
        return None, None

    if isinstance(value, dict):
        display = None
        for key in ("displayName", "display_name", "name", "$"):
            if value.get(key):
                display = str(value.get(key)).strip()
                break
        email = None
        for key in ("uniqueName", "unique_name", "mailAddress", "email", "account", "id"):
            raw = value.get(key)
            if raw and "@" in str(raw):
                email = str(raw).strip()
                break
        if display and "<" in display:
            name, _, addr = display.partition("<")
            email = email or addr.strip().rstrip(">").strip() or None
            display = name.strip()
        return display or None, email or None

    text = str(value).strip()
    if not text:
        return None, None
    if "<" in text:
        name, _, addr = text.partition("<")
        addr = addr.strip().rstrip(">").strip()
        return name.strip() or None, addr or None
    if "@" in text:
        return None, text
    return text, None


class WorkItemMapping:
    """Organization-specific work item type/field mapping (work_item_mapping.yaml)."""

    def __init__(self, data: dict[str, Any]):
        self.raw = data
        self.incident_types: list[str] = list(data.get("incident_types", ["Incident"]))
        self.excluded_types: list[str] = list(data.get("excluded_types", []))
        self.open_states: list[str] = list(data.get("open_states", []))
        self.resolved_states: list[str] = list(data.get("resolved_states", []))
        self.severity_values: dict[str, str] = {
            str(k).lower(): v for k, v in (data.get("severity_values") or {}).items()
        }
        self.fields: dict[str, str] = dict(data.get("fields", {}))

    @classmethod
    def load(cls, path: Path | str) -> "WorkItemMapping":
        path = Path(path)
        if not path.exists():
            return cls({})
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return cls(data)

    def ref(self, logical: str) -> str | None:
        return self.fields.get(logical)

    # Well-known alternate reference names used by different process
    # templates (Agile uses System.Priority, Scrum/CMMI/customized projects
    # often use Microsoft.VSTS.Common.Priority). The yaml mapping stays
    # authoritative; these are only fallbacks when the mapped field is absent.
    _FALLBACK_REFS: dict[str, tuple[str, ...]] = {
        "priority": ("System.Priority", "Microsoft.VSTS.Common.Priority"),
        "severity": (
            "Custom.Severity",
            "Microsoft.VSTS.Common.Severity",
            "Custom.PriorityLabel",
        ),
    }

    def value(self, fields: dict[str, Any], logical: str, default: Any = None) -> Any:
        ref = self.ref(logical)
        if ref is not None and fields.get(ref) is not None:
            return fields.get(ref, default)
        for alt in self._FALLBACK_REFS.get(logical, ()):
            if fields.get(alt) is not None:
                return fields.get(alt)
        if ref is not None:
            return fields.get(ref, default)
        return default

    def normalize_severity(self, raw: Any) -> str | None:
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        # Custom.PriorityLabel style values look like "P3 - Medium" or
        # "P1 - Critical": the severity is the part after the dash.
        if "-" in text:
            candidate = text.rsplit("-", 1)[-1].strip()
            if candidate:
                mapped = self.severity_values.get(candidate.lower())
                if mapped:
                    return mapped
                if candidate.lower() in {"critical", "high", "medium", "low"}:
                    return candidate.capitalize()
        return self.severity_values.get(text.lower(), text)

    def to_incident(
        self,
        raw: dict[str, Any],
        *,
        comments: list[Comment] | None = None,
        source: str = "azure_devops",
    ) -> Incident:
        fields = raw.get("fields", {}) or {}
        numeric_id = raw.get("id")
        title = _text(self.value(fields, "title")) or f"Work item {numeric_id}"
        state = _text(self.value(fields, "state")) or "New"
        resolved = _to_naive_utc(self.value(fields, "resolved_date"))
        closed = _to_naive_utc(self.value(fields, "closed_date"))
        if resolved is None and state.lower() in {"resolved", "closed", "done", "removed"}:
            resolved = _to_naive_utc(self.value(fields, "changed_date"))
        if closed is None and state.lower() == "closed":
            closed = _to_naive_utc(self.value(fields, "changed_date"))

        tags_raw = self.value(fields, "tags")
        tags = [t.strip() for t in str(tags_raw or "").split(",") if t.strip()]

        priority_raw = self.value(fields, "priority")
        try:
            priority = int(priority_raw) if priority_raw is not None else None
        except (TypeError, ValueError):
            priority = None

        sla_target_raw = self.value(fields, "sla_target_hours")
        try:
            sla_target = float(sla_target_raw) if sla_target_raw is not None else None
        except (TypeError, ValueError):
            sla_target = None

        relations: list[Relation] = []
        for rel in raw.get("relations") or []:
            url = str(rel.get("url") or "")
            rel_type = str(rel.get("rel") or "related").rsplit("/", 1)[-1]
            target_id = url.rsplit("/", 1)[-1]
            if target_id.isdigit():
                relations.append(Relation(type=rel_type, target=f"#{target_id}"))

        created = _to_naive_utc(self.value(fields, "created_date")) or datetime.now()
        changed = _to_naive_utc(self.value(fields, "changed_date")) or created

        owner_display, owner_email = _identity_parts(self.value(fields, "assigned_to"))

        return Incident(
            id=f"#{numeric_id}" if numeric_id is not None else title,
            numeric_id=int(numeric_id) if numeric_id is not None else None,
            title=title,
            description=_text(self.value(fields, "description")),
            work_item_type=_text(self.value(fields, "work_item_type")) or "Incident",
            state=state,
            reason=_text(self.value(fields, "reason")),
            priority=priority,
            severity=self.normalize_severity(self.value(fields, "severity")),
            assigned_to=owner_display or owner_email,
            assigned_to_email=owner_email,
            created_at=created,
            changed_at=changed,
            last_update_at=changed,
            resolved_at=resolved,
            closed_at=closed,
            area_path=_text(self.value(fields, "area_path")),
            iteration_path=_text(self.value(fields, "iteration_path")),
            tags=tags,
            customer=_text(self.value(fields, "customer")),
            environment=_text(self.value(fields, "environment")),
            impact=_text(self.value(fields, "impact")),
            urgency=_text(self.value(fields, "urgency")),
            sla_due_at=_to_naive_utc(self.value(fields, "sla_due_date")),
            sla_target_hours=sla_target,
            due_date=_to_naive_utc(self.value(fields, "due_date")),
            resolution=_text(self.value(fields, "resolution")),
            root_cause=_text(self.value(fields, "root_cause")),
            blocked=state.lower() == "blocked",
            dependency=None,
            relations=relations,
            comments=comments or [],
            source=source,  # type: ignore[arg-type]
        )
