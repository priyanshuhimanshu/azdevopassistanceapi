from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Iterable

from app.models.incidents import (
    EscalationBand,
    EscalationItem,
    Incident,
    IncidentRisk,
    IncidentSummary,
    RecurringIssue,
    ResolutionSummary,
    RiskLevel,
    StandupIncident,
    StandupReport,
    SupportStatus,
    TopRisk,
)

SEVERITY_ORDER = ["Critical", "High", "Medium", "Low", "Unknown"]

# ---------------------------------------------------------------------------
# SLA risk
# ---------------------------------------------------------------------------

# Estimation basis: 8 working hours per weekday (Monday-Friday).
WORKING_HOURS_PER_DAY = 8.0
HIGH_RISK_REMAINING_HOURS = 8.0  # <= 1 working day left
MEDIUM_RISK_REMAINING_HOURS = 24.0  # <= 3 working days left
HIGH_RISK_RATIO = 0.10
MEDIUM_RISK_RATIO = 0.25


def working_hours_between(start: datetime, end: datetime) -> float:
    """Working hours between two datetimes: only Monday-Friday counts, each
    weekday contributing 8 working hours prorated across the day."""
    if end <= start:
        return 0.0
    total = 0.0
    cursor = start
    while cursor < end:
        day_end = datetime(cursor.year, cursor.month, cursor.day) + timedelta(days=1)
        chunk_end = min(day_end, end)
        if cursor.weekday() < 5:  # Monday-Friday
            total += (chunk_end - cursor).total_seconds() / 3600.0
        cursor = chunk_end
    return round(total * WORKING_HOURS_PER_DAY / 24.0, 1)


def sla_risk(incident: Incident, now: datetime) -> IncidentRisk:
    """Classify SLA breach risk. Never fabricates missing SLA data."""
    age_hours = round(incident.age_hours(now), 1)
    base = dict(
        incident_id=incident.id,
        title=incident.title,
        severity=incident.severity,
        priority=incident.priority_label,
        state=incident.state,
        owner=incident.assigned_to,
        age_hours=age_hours,
        sla_target_hours=incident.sla_target_hours,
    )

    if not incident.is_open:
        return IncidentRisk(
            **base,
            time_remaining_hours=None,
            risk=RiskLevel.NONE,
            reason="Incident is resolved/closed - no SLA exposure.",
            recommendation="No action required.",
        )

    # End date: SLA due date first, otherwise the work-item due date.
    end_date = incident.sla_due_at or incident.due_date
    if end_date is None:
        return IncidentRisk(
            **base,
            time_remaining_hours=None,
            risk=RiskLevel.UNKNOWN,
            reason="No end date (SLA due date / due date) is available in the Azure DevOps data.",
            recommendation="Configure an SLA due date or due date if this incident type should be tracked.",
        )

    if end_date <= now:
        remaining = -working_hours_between(end_date, now)
    else:
        remaining = working_hours_between(now, end_date)
    target = incident.sla_target_hours
    if target is None:
        target = working_hours_between(incident.created_at, end_date)
    ratio = (remaining / target) if target and target > 0 else None

    if remaining <= 0:
        risk = RiskLevel.HIGH
        reason = f"SLA deadline passed {format_working_days(abs(remaining))} ago (working time)."
        recommendation = "Escalate immediately and notify the incident owner."
    elif remaining <= HIGH_RISK_REMAINING_HOURS or (ratio is not None and ratio <= HIGH_RISK_RATIO):
        risk = RiskLevel.HIGH
        reason = "Incident is approaching SLA deadline (working time)."
        recommendation = "Escalate immediately."
    elif remaining <= MEDIUM_RISK_REMAINING_HOURS or (ratio is not None and ratio <= MEDIUM_RISK_RATIO):
        risk = RiskLevel.MEDIUM
        reason = f"{format_working_days(remaining)} left against a {format_working_days(target)} SLA target (8 working hrs/day)."
        recommendation = "Confirm progress with the owner and prepare a mitigation plan."
    else:
        risk = RiskLevel.LOW
        reason = f"{format_working_days(remaining)} remaining against a {format_working_days(target)} SLA target (8 working hrs/day)."
        recommendation = "Monitor."

    if (incident.state or "").strip().lower() == "uat" and risk == RiskLevel.HIGH:
        # UAT is the last step before closure: cap at MEDIUM even when the
        # SLA deadline has passed, so near-done tickets don't top risk lists.
        risk = RiskLevel.MEDIUM
        reason = f"{reason} Ticket is in UAT (near closure), so risk is capped at medium."
        recommendation = "Confirm user acceptance with the owner and close the ticket."

    return IncidentRisk(
        **base,
        time_remaining_hours=round(remaining, 1),
        risk=risk,
        reason=reason,
        recommendation=recommendation,
    )


def is_on_hold(incident: Incident) -> bool:
    return incident.is_open and incident.state.strip().lower() == "on hold"


def is_uat(incident: Incident) -> bool:
    return incident.is_open and (incident.state or "").strip().lower() == "uat"


def hold_incidents(incidents: Iterable[Incident], now: datetime) -> list[Incident]:
    """Open tickets on hold - tracked separately, never part of SLA risk."""
    return sorted(
        [i for i in incidents if is_on_hold(i)],
        key=lambda i: i.age_hours(now),
        reverse=True,
    )


def sla_risk_incidents(
    incidents: Iterable[Incident], now: datetime, *, include_low: bool = False
) -> list[IncidentRisk]:
    risks = [
        sla_risk(i, now)
        for i in incidents
        if i.is_open and not is_on_hold(i) and not is_uat(i)
    ]
    keep = {RiskLevel.HIGH, RiskLevel.MEDIUM}
    if include_low:
        keep.add(RiskLevel.LOW)
    return sorted(
        [r for r in risks if r.risk in keep],
        key=lambda r: (r.time_remaining_hours is None, r.time_remaining_hours or 0),
    )


# ---------------------------------------------------------------------------
# Aging
# ---------------------------------------------------------------------------


def aging_incidents(
    incidents: Iterable[Incident], now: datetime, min_days: float
) -> list[Incident]:
    return sorted(
        [i for i in incidents if i.is_open and not is_on_hold(i) and i.age_days(now) > min_days],
        key=lambda i: i.age_hours(now),
        reverse=True,
    )


# ---------------------------------------------------------------------------
# Escalation recommendation
# ---------------------------------------------------------------------------

SEVERITY_SCORE = {"Critical": 3, "High": 2, "Medium": 1, "Low": 0}
SLA_SCORE = {RiskLevel.HIGH: 3, RiskLevel.MEDIUM: 2, RiskLevel.LOW: 0, RiskLevel.UNKNOWN: 0, RiskLevel.NONE: 0}
BAND_HIGH = 8
BAND_MEDIUM = 6


def escalation_score(incident: Incident, now: datetime) -> tuple[int, list[str]]:
    # On Hold is paused by customer/priority decision - never escalate.
    if is_on_hold(incident):
        return 0, []
    score = 0
    reasons: list[str] = []

    sev = incident.severity or "Unknown"
    sev_points = SEVERITY_SCORE.get(sev, 0)
    if sev_points:
        score += sev_points
        reasons.append(f"{sev} severity")

    if incident.priority is not None and incident.priority <= 1:
        score += 1
        reasons.append("P1 priority")

    risk = sla_risk(incident, now)
    if risk.risk in (RiskLevel.HIGH, RiskLevel.MEDIUM):
        score += SLA_SCORE[risk.risk]
        reasons.append(f"SLA {risk.risk.value}")

    if not incident.is_open:
        return 0, []

    age_days = incident.age_days(now)
    if age_days > 7:
        score += 2
        reasons.append(f"Open for {age_days:.0f} days")
    elif age_days > 3:
        score += 1
        reasons.append(f"Open for {age_days:.0f} days")

    if not incident.assigned_to:
        score += 2
        reasons.append("No owner assigned")

    since_update = incident.hours_since_update(now)
    if since_update is not None and since_update >= 12:
        score += 2
        reasons.append(f"No update for {format_stale(since_update)}")

    if incident.blocked or incident.state == "Blocked":
        score += 2
        reasons.append("Blocked" + (f" on {incident.dependency}" if incident.dependency else ""))

    if incident.dependency and not incident.blocked:
        score += 1
        reasons.append(f"Unresolved dependency: {incident.dependency}")

    if incident.has_customer_impact:
        score += 1
        reasons.append("Customer impact reported")

    return score, reasons


def escalation_recommendations(
    incidents: Iterable[Incident], now: datetime, *, min_band: EscalationBand = EscalationBand.MEDIUM
) -> list[EscalationItem]:
    order = {EscalationBand.HIGH: 0, EscalationBand.MEDIUM: 1, EscalationBand.LOW: 2}
    items: list[EscalationItem] = []
    for incident in incidents:
        if is_on_hold(incident):
            continue  # paused - tracked in On Hold panel only
        score, reasons = escalation_score(incident, now)
        if score >= BAND_HIGH:
            band = EscalationBand.HIGH
        elif score >= BAND_MEDIUM:
            band = EscalationBand.MEDIUM
        elif score > 0:
            band = EscalationBand.LOW
        else:
            continue
        if order[band] <= order[min_band]:
            if (incident.state or "").strip().lower() == "uat" and band == EscalationBand.HIGH:
                # Same near-closure rule as SLA risk: UAT never escalates as HIGH.
                band = EscalationBand.MEDIUM
                reasons = reasons + ["UAT (near closure) - capped at medium"]
            items.append(
                EscalationItem(
                    incident_id=incident.id,
                    title=incident.title,
                    band=band,
                    score=score,
                    reasons=reasons,
                )
            )
    return sorted(items, key=lambda e: (order[e.band], -e.score))


# ---------------------------------------------------------------------------
# Daily support status
# ---------------------------------------------------------------------------


def _severity_bucket(incident: Incident) -> str:
    sev = (incident.severity or "").strip().lower()
    if sev in {"critical", "crit", "1"}:
        return "Critical"
    if sev in {"high", "2"}:
        return "High"
    if sev in {"medium", "med", "3"}:
        return "Medium"
    if sev in {"low", "4"}:
        return "Low"
    # Fall back to priority when severity is missing (P1 -> Critical, ...)
    if incident.priority == 1:
        return "Critical"
    if incident.priority == 2:
        return "High"
    if incident.priority == 3:
        return "Medium"
    if incident.priority == 4:
        return "Low"
    return "Unknown"


def _start_of_day(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def support_status(incidents: list[Incident], now: datetime) -> SupportStatus:
    # Every KPI counts all not-closed tickets (including On Hold) so the
    # numbers match the Open tab on the incidents page.
    open_items = [i for i in incidents if i.is_open]
    counts = defaultdict(int)
    for inc in open_items:
        counts[_severity_bucket(inc)] += 1

    today = _start_of_day(now)
    new_today = sum(1 for i in incidents if i.created_at >= today)
    resolved_today = sum(
        1 for i in incidents if i.resolved_at and i.resolved_at >= today
    )

    risks = sla_risk_incidents(open_items, now)
    at_risk = [r for r in risks if r.risk in (RiskLevel.HIGH, RiskLevel.MEDIUM)]
    unassigned = [i for i in open_items if not i.assigned_to]
    escalations = escalation_recommendations(open_items, now)
    escalations_now = [e for e in escalations if e.band == EscalationBand.HIGH]

    oldest = max(open_items, key=lambda i: i.age_hours(now)) if open_items else None

    # Overall health: every conclusion traces back to counted data.
    health = "GREEN"
    if counts["Critical"] or at_risk or unassigned or (
        oldest and oldest.age_days(now) > 7
    ):
        health = "AMBER"
    if counts["Critical"] >= 3 or len([r for r in at_risk if r.risk == RiskLevel.HIGH]) >= 3:
        health = "RED"

    top_risks = _top_risks(open_items, now, risks, escalations)

    recommendations: list[str] = []
    critical_high_risk = [r for r in at_risk if r.risk == RiskLevel.HIGH]
    if critical_high_risk:
        recommendations.append(
            "Escalate " + ", ".join(r.incident_id for r in critical_high_risk) + " immediately (SLA at risk)."
        )
    if unassigned:
        recommendations.append(
            f"Assign owners to {len(unassigned)} unassigned incident(s): "
            + ", ".join(i.id for i in unassigned[:5])
            + "."
        )
    aging = aging_incidents(open_items, now, 7)
    if aging:
        shown = aging if len(aging) <= 8 else aging[:8]
        more = "" if len(aging) <= 8 else f" and {len(aging) - 8} more"
        recommendations.append(
            f"Review {len(aging)} incident(s) open for more than 7 days with the support manager: "
            + ", ".join(i.id for i in shown)
            + more
            + "."
        )
    blocked = [i for i in open_items if i.blocked or i.state == "Blocked"]
    if blocked:
        recommendations.append(
            "Clear blockers on " + ", ".join(i.id for i in blocked) + "."
        )
    escalation_ids = [
        e.incident_id
        for e in escalations_now
        if e.incident_id not in {r.incident_id for r in critical_high_risk}
    ]
    if escalation_ids:
        recommendations.append(
            "Escalation review needed: " + ", ".join(escalation_ids) + "."
        )
    if not recommendations:
        recommendations.append("No immediate actions required - keep monitoring.")

    return SupportStatus(
        generated_at=now,
        overall_health=health,
        # Matches the Open tab on the incidents page (open_only): every
        # not-closed ticket, including On Hold.
        open_incidents=len(open_items),
        critical=counts["Critical"],
        high=counts["High"],
        medium=counts["Medium"],
        low=counts["Low"],
        new_today=new_today,
        resolved_today=resolved_today,
        sla_at_risk=len(at_risk),
        oldest_open_id=oldest.id if oldest else None,
        oldest_open_age_days=round(oldest.age_days(now), 1) if oldest else None,
        unassigned=len(unassigned),
        escalation_required=len(escalations_now),
        top_risks=top_risks,
        recommendations=recommendations,
    )


def _top_risks(
    open_items: list[Incident],
    now: datetime,
    risks: list[IncidentRisk],
    escalations: list[EscalationItem],
) -> list[TopRisk]:
    risk_by_id = {r.incident_id: r for r in risks}
    esc_by_id = {e.incident_id: e for e in escalations}

    scored: list[tuple[float, Incident, list[str]]] = []
    for inc in open_items:
        if is_on_hold(inc):
            continue  # paused - shown in On Hold panel only, never a top risk
        reasons: list[str] = []
        score = 0.0
        sev = _severity_bucket(inc)
        if sev == "Critical":
            score += 5
            reasons.append("Critical")
        elif sev == "High":
            score += 3
            reasons.append("High")

        risk = risk_by_id.get(inc.id) or sla_risk(inc, now)
        if risk.risk == RiskLevel.HIGH:
            score += 6
            reasons.append("SLA breach risk")
        elif risk.risk == RiskLevel.MEDIUM:
            score += 3
            reasons.append("SLA at risk")

        if inc.age_days(now) >= 3:
            score += 2
            reasons.append(f"Open for {inc.age_days(now):.0f} days")
        if not inc.assigned_to:
            score += 3
            reasons.append("No owner assigned")
        if inc.has_customer_impact:
            score += 2
            reasons.append("Customer impact reported")
        if inc.blocked or inc.state == "Blocked":
            score += 2
            reasons.append("Blocked")

        if score > 0:
            scored.append((score, inc, reasons))

    scored.sort(key=lambda t: t[0], reverse=True)
    return [
        TopRisk(
            incident_id=inc.id,
            title=inc.title,
            severity=_severity_bucket(inc),
            priority=inc.priority_label,
            status=inc.state,
            age_days=round(inc.age_days(now), 1),
            reasons=reasons,
        )
        for _, inc, reasons in scored[:5]
    ]


# ---------------------------------------------------------------------------
# Standup
# ---------------------------------------------------------------------------


def _format_sla_risk_label(r: IncidentRisk) -> str:
    """Human-readable SLA risk: 'HIGH RISK (overdue by 2.5 working days)' or
    'MEDIUM RISK (1.1 working days left)'. Never emits confusing
    negative '(-X days left)' strings."""
    if r.time_remaining_hours is None:
        return r.risk.value
    if r.time_remaining_hours < 0:
        return f"{r.risk.value} (overdue by {format_working_days(abs(r.time_remaining_hours))})"
    return f"{r.risk.value} ({format_working_days(r.time_remaining_hours)} left)"


def standup_report(incidents: list[Incident], now: datetime) -> StandupReport:
    open_items = [i for i in incidents if i.is_open]
    active = [i for i in open_items if not is_on_hold(i)]
    yesterday = now - timedelta(days=1)

    critical = [
        StandupIncident(
            incident_id=i.id,
            title=i.title,
            owner=i.owner_display,
            status=i.state,
            risk=_incident_risk_label(i, now),
        )
        for i in sorted(
            [x for x in active if _severity_bucket(x) == "Critical"],
            key=lambda x: x.age_hours(now),
            reverse=True,
        )
    ]

    blockers = [
        StandupIncident(
            incident_id=i.id,
            title=i.title,
            owner=i.owner_display,
            status=i.state,
            risk=i.dependency or "Blocked",
        )
        for i in active
        if i.blocked or i.state == "Blocked"
    ]

    risks = [
        StandupIncident(
            incident_id=r.incident_id,
            title=r.title,
            owner=r.owner or "Unassigned",
            status=r.state or "",
            risk=_format_sla_risk_label(r),
        )
        for r in sla_risk_incidents(active, now)
        if r.risk in (RiskLevel.HIGH, RiskLevel.MEDIUM)
    ]

    escalations = escalation_recommendations(active, now)
    escalations_high = [e for e in escalations if e.band == EscalationBand.HIGH]

    day_start = _start_of_day(now)
    new_items = [i for i in incidents if i.created_at >= day_start]
    resolved_items = [
        i for i in incidents if i.resolved_at and yesterday <= i.resolved_at < now
    ]
    unassigned_items = [i for i in active if not i.assigned_to]

    def _to_standup(i: Incident) -> StandupIncident:
        return StandupIncident(
            incident_id=i.id,
            title=i.title,
            owner=i.owner_display,
            status=i.state,
            risk=_incident_risk_label(i, now),
        )

    return StandupReport(
        generated_at=now,
        critical_incidents=critical,
        new_today=len(new_items),
        new_incidents=[_to_standup(i) for i in sorted(new_items, key=lambda x: x.created_at, reverse=True)[:10]],
        resolved_yesterday=len(resolved_items),
        resolved_incidents=[_to_standup(i) for i in sorted(resolved_items, key=lambda x: x.resolved_at or now, reverse=True)[:10]],
        blockers=blockers,
        escalations_recommended=len(escalations_high),
        escalations=escalations_high[:5],
        sla_at_risk=risks,
        unassigned=len(unassigned_items),
        unassigned_incidents=[_to_standup(i) for i in sorted(unassigned_items, key=lambda x: x.age_hours(now), reverse=True)[:8]],
    )


def _incident_risk_label(incident: Incident, now: datetime) -> str:
    risk = sla_risk(incident, now)
    if risk.risk in (RiskLevel.HIGH, RiskLevel.MEDIUM, RiskLevel.UNKNOWN):
        return risk.risk.value
    if not incident.assigned_to:
        return "No owner"
    return "Monitoring"


# ---------------------------------------------------------------------------
# Recurring issues (AI-derived clustering via tags/keywords)
# ---------------------------------------------------------------------------

CLUSTER_KEYWORDS = {
    "Database connection failures": [
        "database",
        "connection",
        "db-connection",
        "pool",
        "timeout",
        "sql",
    ],
    "Authentication timeouts": [
        "authentication",
        "auth",
        "sso",
        "token",
        "login",
        "password",
        "401",
    ],
    "Network / gateway instability": [
        "gateway",
        "network",
        "load balancer",
        "health probe",
        "vpn",
        "502",
    ],
    "Email delivery problems": ["email", "notification", "smtp", "dmarc", "mail"],
}


def recurring_issues(incidents: list[Incident], now: datetime, *, window_days: int = 30) -> list[RecurringIssue]:
    cutoff = now - timedelta(days=window_days)
    in_window = [i for i in incidents if i.created_at >= cutoff]
    split = now - timedelta(days=min(window_days, 7))

    results: list[RecurringIssue] = []
    for label, keywords in CLUSTER_KEYWORDS.items():
        matched = [i for i in in_window if _matches_cluster(i, keywords)]
        if len(matched) < 2:
            continue
        recent = [i for i in matched if i.created_at >= split]
        earlier = [i for i in matched if cutoff <= i.created_at < split]
        if len(recent) > len(earlier):
            trend = "Increasing"
        elif len(recent) < len(earlier):
            trend = "Decreasing"
        else:
            trend = "Stable"

        sev_scores = [_severity_bucket(i) for i in matched]
        impact = "High" if "Critical" in sev_scores or sev_scores.count("High") >= 2 else (
            "Medium" if "High" in sev_scores or sev_scores.count("Medium") >= 2 else "Low"
        )

        results.append(
            RecurringIssue(
                label=label,
                incident_ids=[i.id for i in matched],
                count=len(matched),
                trend=trend,
                impact=impact,
                ai_derived=True,
                evidence=sorted({k for i in matched for k in i.tags if k in keywords})
                or [keywords[0]],
            )
        )
    return sorted(results, key=lambda r: r.count, reverse=True)


def _matches_cluster(incident: Incident, keywords: list[str]) -> bool:
    """Cluster membership: an explicit tag match, or >= 2 keyword hits in the text."""
    kws = [k.lower() for k in keywords]
    tags = " ".join(incident.tags).lower()
    if any(k in tags for k in kws):
        return True
    text = " ".join(
        [
            incident.title,
            incident.description or "",
            incident.impact or "",
            incident.root_cause or "",
            incident.resolution or "",
        ]
    ).lower()
    return sum(1 for k in kws if k in text) >= 2


def _matches_keywords(incident: Incident, keywords: list[str]) -> bool:
    return _matches_cluster(incident, keywords)


# ---------------------------------------------------------------------------
# Incident summary + resolution summary
# ---------------------------------------------------------------------------


def summarize_incident(incident: Incident, now: datetime) -> IncidentSummary:
    comments_sorted = sorted(incident.comments, key=lambda c: c.created_at)
    recent = [
        f"{c.created_at.strftime('%H:%M')} - {c.text}"
        for c in comments_sorted[-5:]
    ]
    latest = incident.latest_comment()

    missing: list[str] = []
    for field_name, value in [
        ("Severity", incident.severity),
        ("Priority", incident.priority),
        ("Assigned To", incident.assigned_to),
        ("SLA", incident.sla_due_at),
        ("Customer", incident.customer),
        ("Environment", incident.environment),
    ]:
        if value is None:
            missing.append(field_name)

    risk = sla_risk(incident, now)
    if not incident.is_open:
        escalation = "Not required (resolved/closed)"
    elif is_on_hold(incident):
        escalation = "Not required (on hold - paused)"
    elif risk.risk == RiskLevel.HIGH:
        escalation = "Required"
    else:
        esc = escalation_recommendations([incident], now)
        escalation = "Required" if esc and esc[0].band in (EscalationBand.HIGH, EscalationBand.MEDIUM) else "Not required"

    if not incident.is_open:
        next_action = "Resolved - no further action needed."
    elif is_on_hold(incident):
        next_action = "On hold - paused; confirm resume date with the customer when ready."
    elif not incident.assigned_to:
        next_action = "Recommend assigning an owner so this stays on track."
    elif risk.risk == RiskLevel.HIGH:
        next_action = "Kindly prioritize with the owner and align on a mitigation plan."
    elif latest and incident.age_hours(now) > 48:
        next_action = "Suggest a gentle review with the support team to keep up momentum."
    elif latest:
        next_action = f"Share a courteous status check with {incident.assigned_to}."
    else:
        next_action = "Invite the owner to share an initial update."

    situation = None
    if latest:
        situation = f"{latest.author} ({latest.created_at.strftime('%Y-%m-%d %H:%M')}): {latest.text}"
    elif incident.description:
        situation = incident.description

    return IncidentSummary(
        incident_id=incident.id,
        title=incident.title,
        status=incident.state,
        severity=incident.severity,
        priority=incident.priority_label,
        owner=incident.assigned_to,
        age_hours=round(incident.age_hours(now), 1),
        age_days=round(incident.age_days(now), 1),
        customer_impact=incident.impact,
        current_situation=situation,
        recent_activity=recent,
        tags=incident.tags,
        environment=incident.environment,
        customer=incident.customer,
        resolution=incident.resolution,
        root_cause=incident.root_cause,
        related_incidents=[r.target for r in incident.relations],
        next_recommended_action=next_action,
        escalation=escalation,
        sla_risk=risk.risk,
        missing_fields=missing,
    )


def pm_discussion_points(incident: Incident, now: datetime) -> list[str]:
    """What the project manager should discuss for this ticket: derived from
    owner, SLA working time, blockers, hold/UAT state, staleness and impact."""
    points: list[str] = []
    if not incident.is_open:
        return ["Closed - kindly confirm the customer is happy with the resolution."]
    if not incident.assigned_to:
        points.append("Recommend assigning an owner so accountability stays clear.")
    risk = sla_risk(incident, now)
    if risk.risk == RiskLevel.HIGH:
        remaining = format_working_days(risk.time_remaining_hours)
        points.append(f"Kindly expedite - around {remaining} of working time remains before the end date.")
    elif risk.risk == RiskLevel.MEDIUM:
        points.append("Review the timeline with the owner and agree a comfortable way forward.")
    elif risk.risk == RiskLevel.UNKNOWN:
        points.append("Check with the customer whether a target date should be set for this ticket.")
    if incident.blocked or incident.state.strip().lower() == "blocked":
        points.append(
            "Help unblock progress"
            + (f" - currently awaiting {incident.dependency}." if incident.dependency else ".")
        )
    if incident.state.strip().lower() == "on hold":
        points.append("Confirm what is awaited with the customer and agree a resume date.")
    if incident.state.strip().lower() == "uat":
        points.append("Invite the customer to confirm acceptance so this can be closed out smoothly.")
    stale = incident.hours_since_update(now) or 0
    if stale >= 48:
        points.append(f"No update for {format_stale(stale)} - a gentle nudge to the owner for a status refresh would help.")
    if incident.has_customer_impact:
        points.append(f"Share a reassuring progress update and ETA with {incident.customer}.")
    if not incident.root_cause and incident.age_days(now) > 7:
        points.append("Capture the root cause when convenient, so future occurrences are easier to prevent.")
    return points[:6]


def resolution_summary(incident: Incident) -> ResolutionSummary:
    missing: list[str] = []
    problem = incident.description
    impact = incident.impact
    root_cause = incident.root_cause
    resolution = incident.resolution

    if not root_cause:
        missing.append("Root cause (not present in the work item)")
    if not resolution:
        missing.append("Resolution (not present in the work item)")

    validation = None
    if resolution:
        last = incident.latest_comment()
        if last and not incident.is_open:
            validation = f"Latest work item update: {last.text}"
        elif last:
            validation = f"Latest work item update: {last.text}"

    preventive = None
    if root_cause and resolution:
        preventive = "Not recorded in the work item - recommend adding a preventive action."

    return ResolutionSummary(
        incident_id=incident.id,
        title=incident.title,
        problem=problem,
        impact=impact,
        root_cause=root_cause,
        resolution=resolution,
        validation=validation,
        preventive_action=preventive,
        missing_information=missing,
    )


# ---------------------------------------------------------------------------
# Natural-language incident search filters
# ---------------------------------------------------------------------------

_INCIDENT_ID = re.compile(r"\b(INC|BUG|WI|TASK)[-\s]?(\d+)\b", re.IGNORECASE)


def find_incident_id(text: str) -> str | None:
    m = _INCIDENT_ID.search(text or "")
    if not m:
        return None
    return f"{m.group(1).upper()}-{m.group(2)}"


def filter_incidents(
    incidents: Iterable[Incident],
    *,
    now: datetime,
    query: str | None = None,
    open_only: bool = False,
    closed_only: bool = False,
    severity: str | None = None,
    priority: int | None = None,
    assigned_to: str | None = None,
    assigned_to_any: Iterable[str] | None = None,
    unassigned_only: bool = False,
    min_age_days: float | None = None,
    max_age_days: float | None = None,
    changed_within_hours: float | None = None,
    stale_hours: float | None = None,
    environment: str | None = None,
    customer: str | None = None,
    customer_impact_only: bool = False,
    state: str | None = None,
    state_in: Iterable[str] | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    closed_after: datetime | None = None,
    closed_before: datetime | None = None,
    closed_created_after: datetime | None = None,
    limit: int | None = None,
) -> list[Incident]:
    items = list(incidents)
    if open_only:
        items = [i for i in items if i.is_open]
    if closed_only:
        items = [i for i in items if not i.is_open]
    if severity:
        wanted = severity.strip().lower()
        items = [i for i in items if (i.severity or "").strip().lower() == wanted]
    if priority is not None:
        items = [i for i in items if i.priority == priority]
    if assigned_to:
        # Match on name/email parts so "Sujit Mishra" finds "Sujit Kumar Mishra"
        # and either a display name or an e-mail address can be queried.
        needles = [t for t in re.split(r"[^a-z0-9]+", assigned_to.strip().lower()) if t]
        if needles:
            def _owner_matches(inc: Incident) -> bool:
                hay = f"{inc.assigned_to or ''} {inc.assigned_to_email or ''}".lower()
                return all(n in hay for n in needles)

            items = [i for i in items if _owner_matches(i)]
    if assigned_to_any:
        needles = [n.strip().lower() for n in assigned_to_any if n.strip()]
        items = [
            i
            for i in items
            if any(n in (i.assigned_to or "").lower() for n in needles)
        ]
    if unassigned_only:
        items = [i for i in items if not i.assigned_to]
    if min_age_days is not None:
        items = [i for i in items if i.age_days(now) > min_age_days]
    if max_age_days is not None:
        items = [i for i in items if i.age_days(now) <= max_age_days]
    if changed_within_hours is not None:
        items = [
            i
            for i in items
            if (now - i.changed_at).total_seconds() / 3600.0 <= changed_within_hours
        ]
    if stale_hours is not None:
        items = [
            i
            for i in items
            if (i.hours_since_update(now) or 0) >= stale_hours
        ]
    if environment:
        items = [i for i in items if (i.environment or "").lower() == environment.lower()]
    if customer:
        items = [i for i in items if customer.lower() in (i.customer or "").lower()]
    if customer_impact_only:
        items = [i for i in items if i.impact]
    if state:
        items = [i for i in items if i.state.lower() == state.lower()]
    if state_in:
        wanted = {s.strip().lower() for s in state_in if s.strip()}
        items = [i for i in items if (i.state or "").strip().lower() in wanted]
    if created_after is not None:
        items = [i for i in items if i.created_at >= created_after]
    if created_before is not None:
        items = [i for i in items if i.created_at <= created_before]
    if closed_after is not None or closed_before is not None:
        # Closed-date bounds apply to closed items only; open items always pass.
        def _in_closed_window(i: Incident) -> bool:
            if i.is_open:
                return True
            closed_at = i.resolved_at or i.closed_at
            if closed_at is None:
                return False
            if closed_after is not None and closed_at < closed_after:
                return False
            if closed_before is not None and closed_at > closed_before:
                return False
            return True

        items = [i for i in items if _in_closed_window(i)]
    if closed_created_after is not None:
        # Closed tickets must also have been created on/after the boundary
        # (tickets created in a prior month but closed this month are hidden);
        # open tickets always pass.
        items = [
            i
            for i in items
            if i.is_open or i.created_at >= closed_created_after
        ]
    if query:
        items = [i for i in items if _text_match(i, query)]

    items.sort(key=lambda i: i.age_hours(now), reverse=True)
    return items[:limit] if limit else items


# ---------------------------------------------------------------------------
# Display helpers
# --------------------------------------------------------------------------- #


def format_stale(hours: float) -> str:
    """User-facing staleness duration: hours below a day, days otherwise."""
    if hours < 24:
        return f"{hours:.0f} hours"
    days = hours / 24.0
    return f"{days:.1f} days" if days < 10 else f"{days:.0f} days"


def format_days(hours: float | None) -> str:
    """User-facing SLA durations, always in days (None -> 'Not available')."""
    if hours is None:
        return "Not available"
    days = hours / 24.0
    return f"{days:.1f} days" if days < 10 else f"{days:.0f} days"


def format_working_days(working_hours: float | None) -> str:
    """User-facing working-time durations in working days
    (8 working hours per weekday, None -> 'Not available')."""
    if working_hours is None:
        return "Not available"
    days = working_hours / WORKING_HOURS_PER_DAY
    return f"{days:.1f} working days" if days < 10 else f"{days:.0f} working days"


# ---------------------------------------------------------------------------
# Daily client-discussion briefing + weekly report (JSON-safe dicts)
# --------------------------------------------------------------------------- #


OWNER_TEAM = {
    "suthakaran venkatachalapathi": "Data Engineering",
    "ryme majumdar": "Data Engineering",
    "lakshmi priya kirla": "Data Engineering",
    "sujit kumar mishra": "FSD",
    "manoranjan kumar saha": "FSD",
    "saurabh more": "Mobile",
    "sanket anil shirode": "DevOps",
}


def team_of(owner: str | None) -> str:
    """Team/area label for an owner (roster mirrors the frontend team tabs)."""
    if not owner:
        return "-"
    return OWNER_TEAM.get(owner.strip().lower(), "-")


def _brief_item(incident: Incident, now: datetime) -> dict:
    return {
        "incident_id": incident.id,
        "title": incident.title,
        "severity": incident.severity,
        "priority": incident.priority_label,
        "state": incident.state,
        "owner": incident.owner_display,
        "area": team_of(incident.assigned_to),
        "age_days": round(incident.age_days(now), 1),
    }


def daily_briefing(incidents: list[Incident], now: datetime) -> dict:
    """What to discuss with the client today: critical, SLA risk,
    escalations, stale follow-ups and unassigned items.
    On Hold is paused - listed separately, never in action lists."""
    open_items = [i for i in incidents if i.is_open]
    active = [i for i in open_items if not is_on_hold(i)]
    critical = sorted(
        [i for i in active if (i.severity or "").lower() == "critical"],
        key=lambda i: i.age_hours(now),
        reverse=True,
    )
    sla_high = [
        r for r in sla_risk_incidents(active, now) if r.risk == RiskLevel.HIGH
    ]
    esc_high = [
        e
        for e in escalation_recommendations(active, now)
        if e.band == EscalationBand.HIGH
    ]
    stale = [i for i in filter_incidents(active, now=now, stale_hours=48.0)[:10]]
    unassigned = [i for i in active if not i.assigned_to]
    hold = hold_incidents(incidents, now)
    return {
        "generated_at": now.isoformat(timespec="minutes"),
        "open_count": len(active),
        "on_hold": [_brief_item(i, now) for i in hold],
        "critical": [_brief_item(i, now) for i in critical],
        "sla_high_risk": [r.model_dump(mode="json") for r in sla_high],
        "escalations_high": [e.model_dump(mode="json") for e in esc_high],
        "stale_followups": [_brief_item(i, now) for i in stale],
        "unassigned": [_brief_item(i, now) for i in unassigned],
    }


# IST has no daylight saving, so a fixed offset is exact and needs no tz database.
IST_OFFSET = timedelta(hours=5, minutes=30)


def current_ist_week(now: datetime) -> tuple[datetime, str, str, int]:
    """Monday 00:00 IST of the week containing `now`.

    Stored datetimes are naive UTC, so the IST midnight is converted back to
    the same naive frame for comparison. Returns (week_start, monday_label,
    friday_label, days_elapsed_in_week).
    """
    ist_now = now + IST_OFFSET
    monday = (ist_now - timedelta(days=ist_now.weekday())).date()
    friday = monday + timedelta(days=4)
    week_start = datetime(monday.year, monday.month, monday.day) - IST_OFFSET
    days_elapsed = max(1, (ist_now.date() - monday).days + 1)
    fmt = lambda d: f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"
    return week_start, fmt(monday), fmt(friday), days_elapsed


_MONTH_NAMES = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_MONTH_LABELS = {v: k[:3].title() for k, v in _MONTH_NAMES.items()}
_MONTH_ALT = "|".join(sorted(_MONTH_NAMES, key=len, reverse=True))
_AMBIGUOUS_MONTHS = {"may", "march", "mar"}

# "1-30th", "1st to 30th", "3 through 8" etc.
_DAY_RANGE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s*(?:-|--|to|through|thru|till|until)\s*"
    r"(\d{1,2})(?:st|nd|rd|th)?\b",
    re.IGNORECASE,
)
_YEAR = re.compile(r"\b(20\d{2})\b")


def _month_last_day(year: int, month: int) -> int:
    first_of_next = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return (first_of_next - timedelta(days=1)).day


def _ist_day_start(d: date) -> datetime:
    """00:00 IST for `d`, expressed in the naive-UTC frame used for storage."""
    return datetime(d.year, d.month, d.day) - IST_OFFSET


def parse_date_window(
    text: str, now: datetime
) -> tuple[datetime, datetime, str] | None:
    """Detect a date window in free text.

    Understands day ("today"), explicit day ranges ("1-30th oct,2026"), month
    names with an optional year, relative weeks/months/years ("this week",
    "last month", "this year") and rolling windows ("past week"/"last 7 days").
    Returns (start, end_inclusive, label) in the naive-UTC frame, or None when
    no date window is present.
    """
    low = text.lower()
    ist_today = (now + IST_OFFSET).date()

    year_hit = _YEAR.search(low)
    year = int(year_hit.group(1)) if year_hit else None

    # ---- explicit day / today -------------------------------------------
    if re.search(r"\btoday\b", low) and not re.search(r"\b(week|month|year)\b", low):
        start = _ist_day_start(ist_today)
        end = start + timedelta(days=1) - timedelta(microseconds=1)
        return start, end, ist_today.strftime("%d %b %Y")

    # ---- explicit day range ("1-30th oct,2026") -------------------------
    day_range = _DAY_RANGE.search(low)

    # ---- month name -----------------------------------------------------
    month: int | None = None
    month_hit = re.search(rf"\b({_MONTH_ALT})\b", low)
    if month_hit:
        name = month_hit.group(1)
        if name in _AMBIGUOUS_MONTHS:
            # "may"/"march" are also common verbs; only treat as a month when
            # clearly qualified by a year, a day range, or a leading preposition.
            qualified = bool(year_hit) or bool(day_range) or bool(
                re.search(
                    rf"\b(?:in|during|for|of|from|since|until|through)\s+{name}\b", low
                )
            )
            if not qualified:
                month_hit = None
        if month_hit:
            month = _MONTH_NAMES[name]

    # ---- relative week / month / year -----------------------------------
    week_rel = None
    if re.search(r"\b(last|previous|prior)\s+week\b", low):
        week_rel = "last"
    elif (
        re.search(r"\b(this|current)\s+week\b", low)
        or re.search(r"\bweek\s+to\s+date\b|\bwtd\b", low)
        or (re.search(r"\bweekly\b", low) and "biweekly" not in low and "bi-weekly" not in low)
    ):
        week_rel = "this"

    month_rel = None
    if re.search(r"\b(last|previous|prior)\s+month\b", low):
        month_rel = "last"
    elif (
        re.search(r"\b(this|current)\s+month\b", low)
        or "month to date" in low
        or re.search(r"\bmtd\b", low)
        or re.search(r"\bmonthly\b", low)
    ):
        month_rel = "this"

    year_rel = None
    if re.search(r"\b(this|current)\s+year\b", low) or re.search(r"\byear\s+to\s+date\b|\bytd\b", low):
        year_rel = "this"
    elif re.search(r"\b(last|previous|prior)\s+year\b", low):
        year_rel = "last"

    rolling = re.search(r"\b(?:past|last|previous)\s+7\s+days\b", low) or re.search(
        r"\bpast\s+week\b", low
    )

    if (
        month is None
        and month_rel is None
        and week_rel is None
        and year_rel is None
        and day_range is None
        and not rolling
    ):
        return None

    # ---- week window (full Mon-Sun calendar week in IST) ----------------
    if rolling:
        start_day = ist_today - timedelta(days=6)
        start = _ist_day_start(start_day)
        end = _ist_day_start(ist_today) + timedelta(days=1) - timedelta(microseconds=1)
        label = f"last 7 days to {ist_today.strftime('%d %b %Y')}"
        return start, end, label

    if week_rel is not None:
        monday = ist_today - timedelta(days=ist_today.weekday())
        if week_rel == "last":
            monday -= timedelta(days=7)
        sunday = monday + timedelta(days=6)
        start = _ist_day_start(monday)
        end = _ist_day_start(sunday) + timedelta(days=1) - timedelta(microseconds=1)
        label = f"{monday.strftime('%d %b')} - {sunday.strftime('%d %b %Y')}"
        return start, end, label

    # ---- year window ----------------------------------------------------
    if year_rel is not None and month is None and day_range is None:
        y = ist_today.year - (1 if year_rel == "last" else 0)
        start = _ist_day_start(date(y, 1, 1))
        end = _ist_day_start(date(y + 1, 1, 1)) - timedelta(microseconds=1)
        return start, end, str(y)

    # ---- month window ---------------------------------------------------
    if month_rel == "last":
        first_this = ist_today.replace(day=1)
        prev = first_this - timedelta(days=1)
        month, year = prev.month, prev.year
    elif month_rel == "this":
        month, year = ist_today.month, ist_today.year
    else:
        month = month or ist_today.month
        year = year or ist_today.year

    last_day = _month_last_day(year, month)
    start_day, end_day = 1, last_day
    if day_range:
        lo, hi = int(day_range.group(1)), int(day_range.group(2))
        if lo > hi:
            lo, hi = hi, lo
        start_day = max(1, min(lo, last_day))
        end_day = max(1, min(hi, last_day))

    start = _ist_day_start(date(year, month, start_day))
    end = _ist_day_start(date(year, month, end_day) + timedelta(days=1)) - timedelta(
        microseconds=1
    )
    label = f"{start_day}-{end_day} {_MONTH_LABELS[month]} {year}"
    return start, end, label


def weekly_report(
    incidents: list[Incident], now: datetime, *, window_days: int | None = None
) -> dict:
    """Current IST week (Monday-Friday) report, or a rolling last-N-days
    report when window_days is given (e.g. 14 for bi-weekly): new vs
    resolved, current open load, SLA risk, aging, UAT queue, recurring themes."""
    if window_days is None:
        week_start, mon_label, fri_label, days_elapsed = current_ist_week(now)
        week_label = f"{mon_label} - {fri_label} (IST)"
    elif window_days == 14:
        # Bi-weekly report runs twice a week: Monday->Tuesday, then
        # Monday->Friday (week-to-date, capped at Friday).
        week_start, mon_label, _, _ = current_ist_week(now)
        ist_today = (now + IST_OFFSET).date()
        friday = (week_start + IST_OFFSET + timedelta(days=4)).date()
        end_day = min(ist_today, friday)
        days_elapsed = max(1, (end_day - (week_start + IST_OFFSET).date()).days + 1)
        fmt = lambda d: f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"
        monday = (week_start + IST_OFFSET).date()
        week_label = f"{fmt(monday)} - {fmt(end_day)}, {end_day.year}"
    else:
        week_start = now - timedelta(days=window_days)
        days_elapsed = window_days
        week_label = f"Last {window_days} days"
    open_items = [i for i in incidents if i.is_open]
    active = [i for i in open_items if not is_on_hold(i)]
    new_week = sorted(
        [i for i in incidents if i.created_at >= week_start],
        key=lambda i: i.created_at,
        reverse=True,
    )
    resolved_week = sorted(
        [i for i in incidents if i.resolved_at and i.resolved_at >= week_start],
        key=lambda i: i.resolved_at or now,
        reverse=True,
    )
    by_severity: dict[str, int] = {}
    for i in active:
        by_severity[i.severity or "Unknown"] = by_severity.get(i.severity or "Unknown", 0) + 1
    risks = sla_risk_incidents(active, now)
    aging = aging_incidents(active, now, 7.0)
    uat = [i for i in active if i.state.strip().lower() == "uat"]
    testing = [i for i in active if i.state.strip().lower() == "testing"]
    in_progress = [
        i for i in active if i.state.strip().lower() in {"in progress", "triage"}
    ]
    new_triage = [i for i in active if i.state.strip().lower() == "new"]
    on_hold = hold_incidents(incidents, now)
    recurring = recurring_issues(incidents, now, window_days=days_elapsed)
    return {
        "generated_at": now.isoformat(timespec="minutes"),
        "title": "BI-WEEKLY SUPPORT REPORT" if window_days == 14 else "WEEKLY SUPPORT REPORT",
        "week": week_label,
        "week_start": week_start.isoformat(timespec="minutes"),
        "window_days": days_elapsed,
        "new_count": len(new_week),
        "resolved_count": len(resolved_week),
        "open_count": len(active),
        "open_by_severity": by_severity,
        "new_incidents": [_brief_item(i, now) for i in new_week],
        "resolved_incidents": [_brief_item(i, now) for i in resolved_week],
        "sla_risks": [r.model_dump(mode="json") for r in risks],
        "aging_over_7_days": [_brief_item(i, now) for i in aging],
        "uat_open": [_brief_item(i, now) for i in uat],
        "testing_open": [_brief_item(i, now) for i in testing],
        "in_progress_open": [_brief_item(i, now) for i in in_progress],
        "new_open": [_brief_item(i, now) for i in new_triage],
        "on_hold_open": [_brief_item(i, now) for i in on_hold],
        "recurring": [r.model_dump(mode="json") for r in recurring],
    }


def _text_match(incident: Incident, query: str) -> bool:
    haystack = " ".join(
        [
            incident.id,
            incident.title,
            incident.description or "",
            " ".join(incident.tags),
            incident.customer or "",
            incident.environment or "",
            incident.impact or "",
            incident.assigned_to or "",
            incident.assigned_to_email or "",
            incident.state,
        ]
    ).lower()
    tokens = [t for t in re.split(r"\W+", query.lower()) if len(t) > 2]
    if not tokens:
        return True
    return sum(1 for t in tokens if t in haystack) >= max(1, len(tokens) // 2)
