from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.agents.support_agent import SupportIncidentAgent
from app.config import get_settings
from app.core import analytics
from app.models.incidents import ChatResponse, DashboardData
from app.services.audit import audit_logger
from app.services.azure_devops import get_mapping, get_provider
from app.tools.registry import registry

router = APIRouter(prefix="/api")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


def _mode() -> str:
    return "demo" if get_settings().demo_mode else "azure_devops"


def _now() -> datetime:
    return datetime.now()


def _naive_utc(value: datetime | None) -> datetime | None:
    """Query datetimes arrive timezone-aware (ISO +Z); stored datetimes are
    naive UTC. Convert so comparisons never mix aware and naive."""
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _ado_base() -> str | None:
    """Base URL for opening an ADO work item edit page in the portal."""
    from urllib.parse import quote

    settings = get_settings()
    if not (settings.ado_org_url and settings.ado_project):
        return None
    org = settings.ado_org_url.rstrip("/")
    project = quote(settings.ado_project.strip(), safe="")
    return f"{org}/{project}/_workitems/edit/"


def _agent() -> SupportIncidentAgent:
    return SupportIncidentAgent()


# --------------------------------------------------------------------------- #
# Meta / dashboard
# --------------------------------------------------------------------------- #


@router.get("/meta")
def meta() -> dict[str, Any]:
    settings = get_settings()
    provider = get_provider()
    mapping = get_mapping()
    return {
        "mode": "demo" if settings.demo_mode else "azure_devops",
        "demo_banner": "DEMO MODE" if settings.demo_mode else None,
        "llm_configured": settings.llm_configured,
        "llm_model": settings.llm_model if settings.llm_configured else None,
        "ado_configured": settings.ado_configured,
        "project_work_item_types": provider.list_work_item_types(),
        "mapped_incident_types": mapping.incident_types,
        "read_tools": registry.names() and sorted(t.name for t in registry.read_tools()),
    }


@router.get("/connection-test")
def connection_test() -> dict[str, Any]:
    """Step-by-step Azure DevOps connectivity diagnosis (no secrets are echoed)."""
    settings = get_settings()
    report: dict[str, Any] = {
        "mode": "demo" if settings.demo_mode else "azure_devops",
        "org_url": settings.ado_org_url,
        "project": settings.ado_project,
        "pat_present": bool(settings.ado_pat),
        "steps": [],
    }
    if settings.demo_mode:
        report["steps"].append(
            {"step": "demo_mode", "ok": True, "detail": "Running on bundled demo data."}
        )
        return report
    if not settings.ado_configured:
        report["steps"].append(
            {
                "step": "config",
                "ok": False,
                "detail": "ADO_ORG_URL, ADO_PROJECT and ADO_PAT must all be set in .env.",
            }
        )
        return report

    import httpx as _httpx

    org = (settings.ado_org_url or "").rstrip("/")
    import base64 as _b64

    token = _b64.b64encode(f":{settings.ado_pat}".encode()).decode()
    auth = {"Authorization": f"Basic {token}"}

    # 1. PAT validity - call the real org endpoint (org-scoped PATs get a 401
    #    from the profile endpoint even when they are perfectly valid).
    try:
        r = _httpx.get(
            f"{org}/_apis/projects?api-version=7.1",
            headers=auth,
            timeout=20,
            follow_redirects=True,
        )
        projects = []
        if r.status_code == 200:
            projects = [p["name"] for p in r.json().get("value", [])]
        report["steps"].append(
            {
                "step": "pat",
                "ok": r.status_code == 200,
                "detail": (
                    f"PAT accepted, {len(projects)} project(s) visible."
                    if r.status_code == 200
                    else f"Azure DevOps returned HTTP {r.status_code} for the PAT. "
                    "It is expired, revoked, or from another account - create a new one."
                ),
            }
        )
        report["projects"] = projects
    except _httpx.HTTPError as exc:
        report["steps"].append(
            {"step": "pat", "ok": False, "detail": f"Cannot reach Azure DevOps: {exc}"}
        )
        return report

    # 2. project + work item types (only when the PAT already works)
    report["project_visible"] = (settings.ado_project or "") in report.get("projects", [])
    if report["project_visible"]:
        try:
            from app.services.azure_devops import get_provider

            types = get_provider().list_work_item_types()
            report["steps"].append(
                {
                    "step": "project",
                    "ok": True,
                    "detail": f"Project reachable, {len(types)} work item types found.",
                }
            )
            report["work_item_types"] = types
        except Exception as exc:  # noqa: BLE001 - report, do not crash
            report["steps"].append({"step": "project", "ok": False, "detail": str(exc)})
    else:
        report["steps"].append(
            {
                "step": "project",
                "ok": False,
                "detail": f"ADO_PROJECT {settings.ado_project!r} is not in the visible "
                f"projects: {report.get('projects', [])}.",
            }
        )

    report["healthy"] = all(s["ok"] for s in report["steps"])
    return report


@router.get("/dashboard")
def dashboard() -> DashboardData:
    provider = get_provider()
    now = _now()
    incidents = provider.list_incidents()
    status = analytics.support_status(incidents, now)
    risks = analytics.sla_risk_incidents(incidents, now)
    critical = sorted(
        [i for i in incidents if i.is_open and not analytics.is_on_hold(i) and (i.severity or "").lower() == "critical"],
        key=lambda i: i.age_hours(now),
        reverse=True,
    )
    week_start, mon_label, fri_label, _ = analytics.current_ist_week(now)
    recent_all = sorted(
        [i for i in incidents if i.created_at >= week_start],
        key=lambda i: i.created_at,
        reverse=True,
    )
    recent = recent_all[:10]
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    monthly_total = sum(1 for i in incidents if i.created_at >= month_start)
    weekly_resolved = sum(1 for i in incidents if i.resolved_at and i.resolved_at >= week_start)
    monthly_resolved = sum(1 for i in incidents if i.resolved_at and i.resolved_at >= month_start)

    by_id = {i.id: i for i in incidents}
    pm_discussion: list[dict[str, Any]] = []
    seen_points: set[str] = set()
    for top in status.top_risks[:3]:
        inc = by_id.get(top.incident_id)
        if inc is None or analytics.is_on_hold(inc):
            continue
        fresh = [
            p
            for p in analytics.pm_discussion_points(inc, now)
            if p not in seen_points
        ]
        seen_points.update(fresh)
        if fresh:
            pm_discussion.append(
                {"incident_id": inc.id, "title": inc.title, "state": inc.state, "points": fresh[:3]}
            )
        if len(pm_discussion) >= 3:
            break

    return DashboardData(
        mode=_mode(),  # type: ignore[arg-type]
        generated_at=now,
        status=status,
        sla_risk=risks,
        critical_incidents=critical,
        on_hold_incidents=analytics.hold_incidents(incidents, now),
        recent_incidents=recent,
        recent_week_label=f"{mon_label} - {fri_label}",
        recent_total=len(recent_all),
        monthly_total=monthly_total,
        weekly_resolved=weekly_resolved,
        monthly_resolved=monthly_resolved,
        pm_discussion=pm_discussion,
    )


# --------------------------------------------------------------------------- #
# Incidents (READ)
# --------------------------------------------------------------------------- #


@router.get("/incidents")
def list_incidents(
    q: str | None = None,
    open_only: bool = False,
    closed_only: bool = False,
    severity: str | None = None,
    priority: int | None = None,
    assigned_to: str | None = None,
    assigned_to_in: str | None = None,
    unassigned: bool = False,
    min_age_days: float | None = None,
    max_age_days: float | None = None,
    changed_within_hours: float | None = None,
    stale_hours: float | None = None,
    environment: str | None = None,
    customer: str | None = None,
    customer_impact: bool = False,
    state: str | None = None,
    state_in: str | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    closed_after: datetime | None = None,
    closed_before: datetime | None = None,
    closed_created_after: datetime | None = None,
    limit: int = Query(default=100, le=500),
    include_comments: bool = False,
) -> dict[str, Any]:
    items = analytics.filter_incidents(
        get_provider().list_incidents(),
        now=_now(),
        query=q,
        open_only=open_only,
        closed_only=closed_only,
        severity=severity,
        priority=priority,
        assigned_to=assigned_to,
        assigned_to_any=[n for n in (assigned_to_in or "").split(",") if n.strip()] or None,
        unassigned_only=unassigned,
        min_age_days=min_age_days,
        max_age_days=max_age_days,
        changed_within_hours=changed_within_hours,
        stale_hours=stale_hours,
        environment=environment,
        customer=customer,
        customer_impact_only=customer_impact,
        state=state,
        state_in=[s for s in (state_in or "").split(",") if s.strip()] or None,
        created_after=_naive_utc(created_after),
        created_before=_naive_utc(created_before),
        closed_after=_naive_utc(closed_after),
        closed_before=_naive_utc(closed_before),
        closed_created_after=_naive_utc(closed_created_after),
        limit=limit,
    )
    if include_comments:
        # Comments live behind a per-ticket API call: fetch in parallel and
        # only for the (already limited) result page.
        from concurrent.futures import ThreadPoolExecutor

        provider = get_provider()

        def _load(incident_id: str) -> tuple[str, list]:
            try:
                return incident_id, provider.get_comments(incident_id)
            except Exception:
                return incident_id, []

        with ThreadPoolExecutor(max_workers=10) as pool:
            for incident_id, comments in pool.map(_load, [i.id for i in items]):
                next((i for i in items if i.id == incident_id), None).comments = comments  # type: ignore[union-attr]
    return {"count": len(items), "incidents": [i.model_dump(mode="json") for i in items]}


@router.get("/incidents/{incident_id:path}/comments")
def incident_comments(incident_id: str) -> dict[str, Any]:
    provider = get_provider()
    inc = provider.find_by_id(incident_id)
    if inc is None:
        raise HTTPException(status_code=404, detail=f"Incident {incident_id} not found")
    return {
        "incident_id": inc.id,
        "comments": [
            {"author": c.author, "at": c.created_at.isoformat(), "text": c.text}
            for c in sorted(inc.comments, key=lambda c: c.created_at)
        ],
    }


@router.get("/incidents/{incident_id:path}")
def get_incident(incident_id: str) -> dict[str, Any]:
    provider = get_provider()
    inc = provider.find_by_id(incident_id)
    if inc is None:
        raise HTTPException(status_code=404, detail=f"Incident {incident_id} not found")
    now = _now()
    summary = analytics.summarize_incident(inc, now)
    risk = analytics.sla_risk(inc, now)
    return {
        "incident": inc.model_dump(mode="json"),
        "summary": summary.model_dump(mode="json"),
        "sla_risk": risk.model_dump(mode="json"),
        "pm_discussion": analytics.pm_discussion_points(inc, now),
        "ado_base": _ado_base(),
    }


# --------------------------------------------------------------------------- #
# Capability endpoints
# --------------------------------------------------------------------------- #


@router.get("/status")
def support_status() -> dict[str, Any]:
    return analytics.support_status(get_provider().list_incidents(), _now()).model_dump(mode="json")


@router.get("/sla-risk")
def sla_risk(include_low: bool = False) -> dict[str, Any]:
    now = _now()
    incidents = get_provider().list_incidents()
    risks = analytics.sla_risk_incidents(incidents, now, include_low=include_low)
    unknown = [
        i.id
        for i in incidents
        if i.is_open and analytics.sla_risk(i, now).risk.value == "UNKNOWN"
    ]
    return {
        "count": len(risks),
        "risks": [r.model_dump(mode="json") for r in risks],
        "unknown_sla_ids": unknown,
    }


@router.get("/aging")
def aging(days: float = 3.0) -> dict[str, Any]:
    now = _now()
    items = analytics.aging_incidents(get_provider().list_incidents(), now, days)
    return {
        "min_days": days,
        "count": len(items),
        "incidents": [i.model_dump(mode="json") for i in items],
    }


@router.get("/escalations")
def escalations(min_band: str = "MEDIUM") -> dict[str, Any]:
    from app.models.incidents import EscalationBand

    try:
        band = EscalationBand(min_band.upper())
    except ValueError:
        band = EscalationBand.MEDIUM
    items = analytics.escalation_recommendations(
        get_provider().list_incidents(), _now(), min_band=band
    )
    return {"count": len(items), "recommendations": [i.model_dump(mode="json") for i in items]}


@router.get("/standup")
def standup() -> dict[str, Any]:
    return analytics.standup_report(get_provider().list_incidents(), _now()).model_dump(mode="json")


@router.get("/recurring")
def recurring(window_days: int = 30) -> dict[str, Any]:
    items = analytics.recurring_issues(get_provider().list_incidents(), _now(), window_days=window_days)
    return {"count": len(items), "issues": [i.model_dump(mode="json") for i in items]}


@router.get("/hold")
def hold() -> dict[str, Any]:
    """Open tickets on hold (tracked separately from SLA risk)."""
    items = analytics.hold_incidents(get_provider().list_incidents(), _now())
    return {"count": len(items), "incidents": [i.model_dump(mode="json") for i in items]}


@router.get("/biweekly")
def biweekly() -> dict[str, Any]:
    """Bi-weekly client report data (previous Monday -> current Friday)."""
    settings = get_settings()
    return {
        "project": settings.project_label,
        "ado_base": _ado_base(),
        "report": analytics.weekly_report(
            get_provider().list_incidents(), _now(), window_days=14
        ),
    }


@router.get("/briefing")
def briefing() -> dict[str, Any]:
    """Daily client-discussion briefing: critical, SLA risk, escalations, follow-ups."""
    return analytics.daily_briefing(get_provider().list_incidents(), _now())


@router.get("/weekly")
def weekly() -> dict[str, Any]:
    """Weekly support report for the last 7 days."""
    return analytics.weekly_report(get_provider().list_incidents(), _now())


# --------------------------------------------------------------------------- #
# Chat (AI agent)
# --------------------------------------------------------------------------- #


@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    agent = _agent()
    response = agent.ask(req.message)
    audit_logger.record(
        "agent.chat",
        decision=response.intent,
        details={"message": req.message[:200], "used_llm": response.used_llm},
    )
    return response


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


@router.get("/audit")
def audit(limit: int = 100) -> dict[str, Any]:
    entries = audit_logger.recent(limit)
    return {"count": len(entries), "entries": entries}
