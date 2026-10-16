from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.models.incidents import Comment, Incident, Relation
from app.services.azure_devops.base import WorkItemProvider

DEMO_INCIDENTS_PATH = None  # resolved lazily by _demo_data_file()


def _demo_data_file(*parts: str):
    """Locate demo_data/ whether running from the repo or inside a container."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent.joinpath("demo_data", *parts)
        if candidate.exists():
            return candidate
    return here.parents[4].joinpath("demo_data", *parts)


def _hours_ago(now: datetime, hours: float | None) -> datetime | None:
    if hours is None:
        return None
    return now - timedelta(hours=float(hours))


def build_incident(entry: dict[str, Any], now: datetime) -> Incident:
    """Convert a demo_data incident entry (relative offsets) into an Incident."""
    tags_raw = entry.get("tags") or ""
    tags = [t.strip() for t in str(tags_raw).split(",") if t.strip()]

    comments = [
        Comment(
            author=c.get("author", "Unknown"),
            created_at=_hours_ago(now, c.get("hours_ago")) or now,
            text=c.get("text", ""),
        )
        for c in entry.get("comments") or []
    ]
    relations = [
        Relation(type=r.get("type", "related"), target=r["target"])
        for r in entry.get("relations") or []
    ]

    sla_due = entry.get("sla_due_hours")
    due = entry.get("due_date_hours")

    return Incident(
        id=entry["id"],
        numeric_id=entry.get("numeric_id"),
        title=entry["title"],
        description=entry.get("description"),
        work_item_type=entry.get("work_item_type", "Incident"),
        state=entry.get("state", "New"),
        reason=entry.get("reason"),
        priority=entry.get("priority"),
        severity=entry.get("severity"),
        assigned_to=entry.get("assigned_to"),
        assigned_to_email=entry.get("assigned_to_email"),
        created_at=_hours_ago(now, entry.get("created_hours_ago")) or now,
        changed_at=_hours_ago(now, entry.get("changed_hours_ago")) or now,
        last_update_at=_hours_ago(now, entry.get("last_update_hours_ago")),
        resolved_at=_hours_ago(now, entry.get("resolved_hours_ago")),
        closed_at=_hours_ago(now, entry.get("closed_hours_ago")),
        area_path=entry.get("area_path"),
        iteration_path=entry.get("iteration_path"),
        tags=tags,
        customer=entry.get("customer"),
        environment=entry.get("environment"),
        impact=entry.get("impact"),
        urgency=entry.get("urgency"),
        sla_due_at=_hours_ago(now, -float(sla_due)) if sla_due is not None else None,
        sla_target_hours=entry.get("sla_target_hours"),
        due_date=_hours_ago(now, -float(due)) if due is not None else None,
        resolution=entry.get("resolution"),
        root_cause=entry.get("root_cause"),
        blocked=bool(entry.get("blocked")),
        dependency=entry.get("dependency"),
        relations=relations,
        comments=comments,
        source="demo",
    )


class DemoProvider(WorkItemProvider):
    """In-memory support incidents loaded from demo_data/ (DEMO MODE, read-only)."""

    name = "demo"

    def __init__(self, data_path: Path | str | None = None, now: datetime | None = None):
        self.data_path = Path(data_path) if data_path else _demo_data_file(
            "incidents", "incidents.json"
        )
        self.now = now or datetime.now()
        self._items: dict[str, Incident] = {}
        self._reload()

    # --- setup -----------------------------------------------------------
    def _reload(self) -> None:
        import json

        raw = json.loads(self.data_path.read_text(encoding="utf-8"))
        self._items = {}
        for entry in raw.get("incidents", []):
            incident = build_incident(entry, self.now)
            self._items[incident.id] = incident

    def reset(self) -> None:
        self._reload()

    # --- READ ------------------------------------------------------------
    def list_work_item_types(self) -> list[str]:
        return sorted({i.work_item_type for i in self._items.values()})

    def list_incidents(self) -> list[Incident]:
        return list(self._items.values())

    def get_incident(self, incident_id: str) -> Incident | None:
        incident_id = incident_id.strip().upper()
        if incident_id in self._items:
            return self._items[incident_id]
        numeric = incident_id.lstrip("#")
        if numeric.isdigit():
            for inc in self._items.values():
                if str(inc.numeric_id) == numeric:
                    return inc
        return None

    def get_comments(self, incident_id: str) -> list[Comment]:
        inc = self.get_incident(incident_id)
        return list(inc.comments) if inc else []

    def _require(self, incident_id: str) -> Incident:
        inc = self.get_incident(incident_id)
        if inc is None:
            raise KeyError(f"Incident {incident_id} not found")
        return inc
