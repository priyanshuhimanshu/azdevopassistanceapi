from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Comment(BaseModel):
    author: str
    created_at: datetime
    text: str


class Relation(BaseModel):
    type: str = "related"
    target: str


class Incident(BaseModel):
    """A support incident / issue as stored in Azure DevOps work items."""

    id: str
    numeric_id: int | None = None
    title: str
    description: str | None = None
    work_item_type: str = "Incident"
    state: str = "New"
    reason: str | None = None
    priority: int | None = None
    severity: str | None = None
    assigned_to: str | None = None
    assigned_to_email: str | None = None
    created_at: datetime
    changed_at: datetime
    last_update_at: datetime | None = None
    resolved_at: datetime | None = None
    closed_at: datetime | None = None
    area_path: str | None = None
    iteration_path: str | None = None
    tags: list[str] = Field(default_factory=list)
    customer: str | None = None
    environment: str | None = None
    impact: str | None = None
    urgency: str | None = None
    sla_due_at: datetime | None = None
    sla_target_hours: float | None = None
    due_date: datetime | None = None
    resolution: str | None = None
    root_cause: str | None = None
    blocked: bool = False
    dependency: str | None = None
    relations: list[Relation] = Field(default_factory=list)
    comments: list[Comment] = Field(default_factory=list)
    source: Literal["demo", "azure_devops"] = "demo"

    # --- derived helpers -------------------------------------------------
    @property
    def priority_label(self) -> str | None:
        if self.priority is None:
            return None
        return f"P{self.priority}"

    @property
    def owner(self) -> str | None:
        return self.assigned_to

    @property
    def owner_display(self) -> str:
        return self.assigned_to or "Unassigned"

    @property
    def is_open(self) -> bool:
        if self.resolved_at or self.closed_at:
            return False
        return self.state.strip().lower() not in {
            "resolved", "closed", "done", "removed", "completed", "verified",
        }

    @property
    def has_customer_impact(self) -> bool:
        return bool(self.impact and self.customer)

    def age(self, now: datetime) -> timedelta:
        return max(now - self.created_at, timedelta(0))

    def age_hours(self, now: datetime) -> float:
        return self.age(now).total_seconds() / 3600.0

    def age_days(self, now: datetime) -> float:
        return self.age_hours(now) / 24.0

    def hours_since_update(self, now: datetime) -> float | None:
        ref = self.last_update_at or self.changed_at
        if ref is None:
            return None
        return max((now - ref).total_seconds() / 3600.0, 0.0)

    def latest_comment(self) -> Comment | None:
        if not self.comments:
            return None
        return max(self.comments, key=lambda c: c.created_at)


class RiskLevel(str, Enum):
    HIGH = "HIGH RISK"
    MEDIUM = "MEDIUM RISK"
    LOW = "LOW RISK"
    UNKNOWN = "UNKNOWN"
    NONE = "NO RISK"


class EscalationBand(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class IncidentRisk(BaseModel):
    incident_id: str
    title: str
    severity: str | None = None
    priority: str | None = None
    state: str | None = None
    owner: str | None = None
    age_hours: float
    sla_target_hours: float | None = None
    time_remaining_hours: float | None = None
    risk: RiskLevel
    reason: str
    recommendation: str


class TopRisk(BaseModel):
    incident_id: str
    title: str
    severity: str | None = None
    priority: str | None = None
    status: str | None = None
    age_days: float
    reasons: list[str] = Field(default_factory=list)


class SupportStatus(BaseModel):
    generated_at: datetime
    overall_health: str
    open_incidents: int
    critical: int
    high: int
    medium: int
    low: int
    new_today: int
    resolved_today: int
    sla_at_risk: int
    oldest_open_id: str | None = None
    oldest_open_age_days: float | None = None
    unassigned: int
    escalation_required: int
    top_risks: list[TopRisk] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class IncidentSummary(BaseModel):
    incident_id: str
    title: str
    status: str
    severity: str | None = None
    priority: str | None = None
    owner: str | None = None
    age_hours: float
    age_days: float
    customer_impact: str | None = None
    current_situation: str | None = None
    recent_activity: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    environment: str | None = None
    customer: str | None = None
    resolution: str | None = None
    root_cause: str | None = None
    related_incidents: list[str] = Field(default_factory=list)
    next_recommended_action: str | None = None
    escalation: str = "Not required"
    sla_risk: RiskLevel = RiskLevel.UNKNOWN
    missing_fields: list[str] = Field(default_factory=list)


class EscalationItem(BaseModel):
    incident_id: str
    title: str
    band: EscalationBand
    score: int
    reasons: list[str]


class StandupIncident(BaseModel):
    incident_id: str
    title: str
    owner: str
    status: str
    risk: str


class StandupReport(BaseModel):
    generated_at: datetime
    critical_incidents: list[StandupIncident] = Field(default_factory=list)
    new_today: int = 0
    new_incidents: list[StandupIncident] = Field(default_factory=list)
    resolved_yesterday: int = 0
    resolved_incidents: list[StandupIncident] = Field(default_factory=list)
    blockers: list[StandupIncident] = Field(default_factory=list)
    escalations_recommended: int = 0
    escalations: list[EscalationItem] = Field(default_factory=list)
    sla_at_risk: list[StandupIncident] = Field(default_factory=list)
    unassigned: int = 0
    unassigned_incidents: list[StandupIncident] = Field(default_factory=list)


class RecurringIssue(BaseModel):
    label: str
    incident_ids: list[str]
    count: int
    trend: str
    impact: str
    ai_derived: bool = False
    evidence: list[str] = Field(default_factory=list)


class ResolutionSummary(BaseModel):
    incident_id: str
    title: str
    problem: str | None = None
    impact: str | None = None
    root_cause: str | None = None
    resolution: str | None = None
    validation: str | None = None
    preventive_action: str | None = None
    missing_information: list[str] = Field(default_factory=list)


class ChatResponse(BaseModel):
    answer: str
    intent: str
    mode: Literal["demo", "azure_devops"]
    used_llm: bool = False
    data: Any | None = None


class DashboardData(BaseModel):
    mode: Literal["demo", "azure_devops"]
    generated_at: datetime
    status: SupportStatus
    sla_risk: list[IncidentRisk] = Field(default_factory=list)
    critical_incidents: list[Incident] = Field(default_factory=list)
    on_hold_incidents: list[Incident] = Field(default_factory=list)
    recent_incidents: list[Incident] = Field(default_factory=list)
    recent_week_label: str = ""
    recent_total: int = 0
    monthly_total: int = 0
    weekly_resolved: int = 0
    monthly_resolved: int = 0
    pm_discussion: list[dict[str, Any]] = Field(default_factory=list)
