from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.core import analytics
from app.models.incidents import Incident
from app.tools.registry import ToolContext, registry

_STR = {"type": "string"}
_BOOL = {"type": "boolean"}
_INT = {"type": "integer"}
_NUM = {"type": "number"}


def _incident_brief(inc: Incident, now: datetime) -> dict[str, Any]:
    return {
        "id": inc.id,
        "title": inc.title,
        "type": inc.work_item_type,
        "state": inc.state,
        "severity": inc.severity,
        "priority": inc.priority_label,
        "owner": inc.assigned_to,
        "age_days": round(inc.age_days(now), 1),
        "created_at": inc.created_at.isoformat(timespec="minutes"),
        "changed_at": inc.changed_at.isoformat(timespec="minutes"),
        "sla_due_at": inc.sla_due_at.isoformat(timespec="minutes") if inc.sla_due_at else None,
        "customer": inc.customer,
        "environment": inc.environment,
        "tags": inc.tags,
        "customer_impact": inc.impact,
    }


def _dump_list(items: list[Any]) -> list[dict[str, Any]]:
    return [i.model_dump(mode="json") for i in items]


# ---------------------------------------------------------------------------
# READ TOOLS
# ---------------------------------------------------------------------------


@registry.tool(
    "get_support_work_items",
    "List support work items with optional filters. Returns brief incident records.",
    {
        "state": _STR,
        "severity": _STR,
        "priority": _INT,
        "assigned_to": _STR,
        "unassigned_only": _BOOL,
        "min_age_days": _NUM,
        "changed_within_hours": _NUM,
        "stale_hours": _NUM,
        "environment": _STR,
        "customer": _STR,
        "customer_impact_only": _BOOL,
        "query": _STR,
        "limit": _INT,
    },
)
def get_support_work_items(
    ctx: ToolContext,
    state: str | None = None,
    severity: str | None = None,
    priority: int | None = None,
    assigned_to: str | None = None,
    unassigned_only: bool = False,
    min_age_days: float | None = None,
    changed_within_hours: float | None = None,
    stale_hours: float | None = None,
    environment: str | None = None,
    customer: str | None = None,
    customer_impact_only: bool = False,
    query: str | None = None,
    limit: int | None = 50,
) -> dict[str, Any]:
    items = analytics.filter_incidents(
        ctx.provider.list_incidents(),
        now=ctx.now,
        query=query,
        severity=severity,
        priority=priority,
        assigned_to=assigned_to,
        unassigned_only=unassigned_only,
        min_age_days=min_age_days,
        changed_within_hours=changed_within_hours,
        stale_hours=stale_hours,
        environment=environment,
        customer=customer,
        customer_impact_only=customer_impact_only,
        state=state,
        limit=limit,
    )
    return {"count": len(items), "incidents": [_incident_brief(i, ctx.now) for i in items]}


@registry.tool(
    "search_incidents",
    "Free-text search across incident id, title, description, tags, customer and impact.",
    {"query": _STR, "open_only": _BOOL, "limit": _INT},
)
def search_incidents(
    ctx: ToolContext, query: str = "", open_only: bool = False, limit: int = 50
) -> dict[str, Any]:
    items = analytics.filter_incidents(
        ctx.provider.list_incidents(), now=ctx.now, query=query, open_only=open_only, limit=limit
    )
    return {"count": len(items), "incidents": [_incident_brief(i, ctx.now) for i in items]}


@registry.tool(
    "get_incident",
    "Fetch one incident with full details: description, comments, relations, resolution.",
    {"incident_id": _STR},
)
def get_incident(ctx: ToolContext, incident_id: str) -> dict[str, Any]:
    inc = ctx.provider.find_by_id(incident_id)
    if inc is None:
        return {"found": False, "incident_id": incident_id}
    data = inc.model_dump(mode="json")
    data["found"] = True
    data["age_days"] = round(inc.age_days(ctx.now), 1)
    data["sla_risk"] = analytics.sla_risk(inc, ctx.now).risk.value
    return data


@registry.tool(
    "get_incident_history",
    "Return the comment/activity history for one incident.",
    {"incident_id": _STR},
)
def get_incident_history(ctx: ToolContext, incident_id: str) -> dict[str, Any]:
    inc = ctx.provider.find_by_id(incident_id)
    if inc is None:
        return {"found": False, "incident_id": incident_id}
    entries = sorted(inc.comments, key=lambda c: c.created_at)
    return {
        "found": True,
        "incident_id": inc.id,
        "created_at": inc.created_at.isoformat(timespec="minutes"),
        "changed_at": inc.changed_at.isoformat(timespec="minutes"),
        "resolved_at": inc.resolved_at.isoformat(timespec="minutes") if inc.resolved_at else None,
        "history": [
            {"at": c.created_at.isoformat(timespec="minutes"), "author": c.author, "text": c.text}
            for c in entries
        ],
    }


@registry.tool(
    "get_incident_comments",
    "Return comments only for one incident.",
    {"incident_id": _STR},
)
def get_incident_comments(ctx: ToolContext, incident_id: str) -> dict[str, Any]:
    comments = ctx.provider.get_comments(incident_id)
    return {
        "incident_id": incident_id,
        "count": len(comments),
        "comments": [
            {"author": c.author, "at": c.created_at.isoformat(timespec="minutes"), "text": c.text}
            for c in sorted(comments, key=lambda c: c.created_at)
        ],
    }


@registry.tool("get_open_incidents", "All incidents that are not resolved/closed.", {"limit": _INT})
def get_open_incidents(ctx: ToolContext, limit: int | None = None) -> dict[str, Any]:
    items = [i for i in ctx.provider.list_incidents() if i.is_open]
    items.sort(key=lambda i: i.age_hours(ctx.now), reverse=True)
    if limit:
        items = items[:limit]
    return {"count": len(items), "incidents": [_incident_brief(i, ctx.now) for i in items]}


@registry.tool(
    "get_critical_incidents",
    "Open incidents with Critical severity (falls back to P1 when severity is missing).",
    {"severity": _STR},
)
def get_critical_incidents(ctx: ToolContext, severity: str = "Critical") -> dict[str, Any]:
    items = [
        i
        for i in ctx.provider.list_incidents()
        if i.is_open and (i.severity or "").lower() == severity.lower()
    ]
    items.sort(key=lambda i: i.age_hours(ctx.now), reverse=True)
    return {"count": len(items), "incidents": [_incident_brief(i, ctx.now) for i in items]}


@registry.tool("get_unassigned_incidents", "Open incidents without an owner.", {})
def get_unassigned_incidents(ctx: ToolContext) -> dict[str, Any]:
    items = [i for i in ctx.provider.list_incidents() if i.is_open and not i.assigned_to]
    items.sort(key=lambda i: i.age_hours(ctx.now), reverse=True)
    return {"count": len(items), "incidents": [_incident_brief(i, ctx.now) for i in items]}


@registry.tool(
    "get_aging_incidents",
    "Open incidents older than a given number of days.",
    {"min_days": _NUM},
)
def get_aging_incidents(ctx: ToolContext, min_days: float = 3.0) -> dict[str, Any]:
    items = analytics.aging_incidents(ctx.provider.list_incidents(), ctx.now, min_days)
    return {
        "min_days": min_days,
        "count": len(items),
        "incidents": [_incident_brief(i, ctx.now) for i in items],
    }


@registry.tool(
    "get_sla_risk_incidents",
    "Open incidents classified as SLA risk (HIGH/MEDIUM by default). UNKNOWN when no SLA data exists.",
    {"include_low": _BOOL},
)
def get_sla_risk_incidents(ctx: ToolContext, include_low: bool = False) -> dict[str, Any]:
    items = analytics.sla_risk_incidents(
        ctx.provider.list_incidents(), ctx.now, include_low=include_low
    )
    return {"count": len(items), "risks": _dump_list(items)}


@registry.tool(
    "get_recently_updated_incidents",
    "Incidents whose state/fields changed within the given number of hours.",
    {"hours": _NUM},
)
def get_recently_updated_incidents(ctx: ToolContext, hours: float = 24.0) -> dict[str, Any]:
    cutoff = ctx.now - timedelta(hours=hours)
    items = [i for i in ctx.provider.list_incidents() if i.changed_at >= cutoff]
    items.sort(key=lambda i: i.changed_at, reverse=True)
    return {
        "hours": hours,
        "count": len(items),
        "incidents": [_incident_brief(i, ctx.now) for i in items],
    }


@registry.tool(
    "get_work_item_relations",
    "Related work items (links) for one incident.",
    {"incident_id": _STR},
)
def get_work_item_relations(ctx: ToolContext, incident_id: str) -> dict[str, Any]:
    inc = ctx.provider.find_by_id(incident_id)
    if inc is None:
        return {"found": False, "incident_id": incident_id}
    related = []
    for rel in inc.relations:
        other = ctx.provider.find_by_id(rel.target)
        related.append(
            {
                "type": rel.type,
                "target": rel.target,
                "title": other.title if other else None,
                "state": other.state if other else None,
            }
        )
    return {"found": True, "incident_id": inc.id, "relations": related}


@registry.tool(
    "get_support_status",
    "Today's overall support status: counts by severity, SLA risk, unassigned, top risks.",
    {},
)
def get_support_status(ctx: ToolContext) -> dict[str, Any]:
    status = analytics.support_status(ctx.provider.list_incidents(), ctx.now)
    return status.model_dump(mode="json")


@registry.tool(
    "get_support_standup",
    "Concise standup report: critical incidents, new/resolved, blockers, escalations.",
    {},
)
def get_support_standup(ctx: ToolContext) -> dict[str, Any]:
    return analytics.standup_report(ctx.provider.list_incidents(), ctx.now).model_dump(mode="json")


@registry.tool(
    "get_escalation_recommendations",
    "Recommended escalations with reasons (recommendation only - never executes an escalation).",
    {"min_band": _STR},
)
def get_escalation_recommendations(ctx: ToolContext, min_band: str = "MEDIUM") -> dict[str, Any]:
    from app.models.incidents import EscalationBand

    try:
        band = EscalationBand(min_band.upper())
    except ValueError:
        band = EscalationBand.MEDIUM
    items = analytics.escalation_recommendations(
        ctx.provider.list_incidents(), ctx.now, min_band=band
    )
    return {"count": len(items), "recommendations": _dump_list(items)}


@registry.tool(
    "get_recurring_issues",
    "AI-derived recurring incident clusters (keyword/tag based) for the given window.",
    {"window_days": _INT},
)
def get_recurring_issues(ctx: ToolContext, window_days: int = 30) -> dict[str, Any]:
    items = analytics.recurring_issues(ctx.provider.list_incidents(), ctx.now, window_days=window_days)
    return {"count": len(items), "recurring_issues": _dump_list(items)}


@registry.tool(
    "list_work_item_types",
    "Inspect the connected Azure DevOps project: configured work item types and mapping.",
    {},
)
def list_work_item_types(ctx: ToolContext) -> dict[str, Any]:
    from app.services.azure_devops import get_mapping

    mapping = get_mapping()
    return {
        "project_work_item_types": ctx.provider.list_work_item_types(),
        "mapped_incident_types": mapping.incident_types,
        "mapped_fields": mapping.fields,
        "open_states": mapping.open_states,
    }


@registry.tool(
    "get_daily_briefing",
    "Daily client-discussion briefing: critical incidents, SLA breach risk, "
    "escalations, stale follow-ups and unassigned items.",
    {},
)
def get_daily_briefing(ctx: ToolContext) -> dict[str, Any]:
    return analytics.daily_briefing(ctx.provider.list_incidents(), ctx.now)


@registry.tool(
    "get_weekly_report",
    "Weekly support report for the last 7 days: new vs resolved, open load, "
    "SLA risks, aging, UAT queue and recurring themes.",
    {},
)
def get_weekly_report(ctx: ToolContext) -> dict[str, Any]:
    return analytics.weekly_report(ctx.provider.list_incidents(), ctx.now)


@registry.tool(
    "get_uat_incidents",
    "Open incidents currently in the given state (default UAT).",
    {"state": _STR},
)
def get_uat_incidents(ctx: ToolContext, state: str = "UAT") -> dict[str, Any]:
    items = analytics.filter_incidents(
        ctx.provider.list_incidents(), now=ctx.now, open_only=True, state=state
    )
    return {
        "state": state,
        "count": len(items),
        "incidents": [_incident_brief(i, ctx.now) for i in items],
    }


@registry.tool(
    "get_followup_incidents",
    "Open incidents with no update for the given number of hours (need follow-up, shown in days).",
    {"stale_hours": _NUM},
)
def get_followup_incidents(ctx: ToolContext, stale_hours: float = 48.0) -> dict[str, Any]:
    items = analytics.filter_incidents(
        ctx.provider.list_incidents(), now=ctx.now, open_only=True, stale_hours=stale_hours
    )
    return {
        "stale_hours": stale_hours,
        "count": len(items),
        "incidents": [_incident_brief(i, ctx.now) for i in items],
    }


@registry.tool(
    "get_hold_incidents",
    "Open tickets on hold (tracked separately, never part of SLA risk).",
    {},
)
def get_hold_incidents(ctx: ToolContext) -> dict[str, Any]:
    items = analytics.hold_incidents(ctx.provider.list_incidents(), ctx.now)
    return {
        "count": len(items),
        "incidents": [_incident_brief(i, ctx.now) for i in items],
    }


READ_TOOL_COUNT = len(registry.read_tools())
