from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from app.config import get_settings
from app.core import analytics
from app.models.incidents import (
    ChatResponse,
    EscalationBand,
    EscalationItem,
    Incident,
    RiskLevel,
)
from app.services.llm.client import LLMClient
from app.tools.registry import ToolContext, registry

# make sure read tools are registered on import
from app.tools import incident_tools as _incident_tools  # noqa: F401

_CONTROL_TOKENS = {
    "show", "give", "list", "me", "all", "the", "which", "are", "of", "for", "more",
    "than", "open", "older", "incident", "incidents", "with", "without", "an", "a",
    "to", "today", "please", "needs", "need", "should", "what", "is", "and", "any",
    "that", "this", "these", "those", "has", "have", "been", "get", "find", "report",
    # ticket-listing phrasing ("working on which tickets currently")
    "ticket", "tickets", "working", "currently", "on", "in", "or", "not",
    # week-scoped activity ("comments ... during the current week")
    "week", "weekly", "current", "during",
    # state words (consumed by the STATE_PHRASES filter below, not free text)
    "progress", "testing", "triage", "blocked", "uat", "hold", "holds",
    "waiting", "investigating", "wip", "work", "done", "closed", "started",
    # generic request framing ("share an update ... to ensure accurate tracking")
    "share", "team", "teams", "specific", "specifically", "ensure",
    "accurate", "tracking", "ongoing", "activities", "activity",
    "provide", "overview", "detailed", "details",
    # comment recency (consumed by the today-filter above, not free text)
    "received", "comment", "comments", "commented", "created",
    # words consumed by structured filters (kept out of the free-text query)
    "owner", "owners", "assigned", "assign", "unassigned", "recent", "update",
    "updates", "changed", "change", "critical", "high", "medium", "low", "priority",
    "resolved", "resolve", "closed", "close", "customer", "impact", "affected",
    "affecting", "unresolved", "active", "stale", "reaching", "affects",
    # date-window framing (consumed by the month/date parser, not free text)
    "month", "months", "monthly", "year", "years", "mtd", "date", "dates",
    "between", "from", "since", "until", "till", "through", "thru", "of",
    "jan", "january", "feb", "february", "mar", "march", "apr", "april", "may",
    "jun", "june", "jul", "july", "aug", "august", "sep", "sept", "september",
    "oct", "october", "nov", "november", "dec", "december",
}

DAY_WORDS = re.compile(r"(\d+)\s*day", re.IGNORECASE)
PRIORITY = re.compile(r"\bp([1-4])\b", re.IGNORECASE)
# Assignee can be given as a display name ("assigned to Sujit Mishra") or an
# e-mail address ("assigned to sujit.mishra@contoso.com").
ASSIGNED_EMAIL = re.compile(
    r"assigned to\s+([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})",
    re.IGNORECASE,
)
ASSIGNED_NAME = re.compile(
    r"assigned to ([A-Za-z][A-Za-z .'-]{0,40}?)"
    r"(?=\s*(?:[.,;]|$|\bfor\b|\bon\b|\bin\b|\bduring\b|\bwho\b|\bwhose\b|"
    r"\bfrom\b|\bbetween\b|\bthis\b|\bcurrent\b|\bpast\b|\blast\b|\bopen\b|"
    r"\bwith\b|\bthat\b|\bcreated\b|\bupdated\b|\bresolved\b|\bclosed\b|"
    r"\bmonth\b|\bmonthly\b|\bweek\b|\bweekly\b|\byear\b|\byearly\b|\btoday\b|"
    r"\bdaily\b))",
    re.IGNORECASE,
)

# Filter questions that carry a time scope ("assigned to X this week/month")
# should produce a filtered list, not the weekly/open status report.
_FILTER_SIGNAL = re.compile(
    r"assigned to|\bassigned\b|\bunassigned\b|\bowner|\bpriority\b|\bp[1-4]\b|\blist\b",
    re.IGNORECASE,
)
_WEEK_SCOPE = re.compile(
    r"\bweek(ly)?\b|\bmonthly\b|last 7 days|past week|this week|current week|"
    r"last week|previous week",
    re.IGNORECASE,
)
_MONTH_SCOPE = re.compile(r"\bmonths?\b|\bmonthly\b|\bmtd\b|month to date", re.IGNORECASE)
# Activity/date-field words: when present the month/week scope targets that field
# (handled by the created/closed/comment logic), so the generic created-date
# window must not be applied.
_ACTIVITY_WORDS = re.compile(
    r"\b(created|resolved|closed|comment|comments|commented|update|updates|"
    r"updated|changed)\b",
    re.IGNORECASE,
)

# Natural-language ticket states ("in progress or testing", "on hold", "in UAT").
# Matched phrases become a structured state_in filter in _intent_search.
# A phrase maps to every ADO state it covers ("wip" spans the active states).
STATE_PHRASES: list[tuple[str, list[str]]] = [
    ("work in progress", ["In Progress", "Testing", "Triage"]),
    ("wip", ["In Progress", "Testing", "Triage"]),
    ("not started", ["New"]),
    ("on hold", ["On Hold"]),
    ("on-hold", ["On Hold"]),
    ("in progress", ["In Progress"]),
    ("testing", ["Testing"]),
    ("user acceptance", ["UAT"]),
    ("uat", ["UAT"]),
    ("triage", ["Triage"]),
    ("blocked", ["Blocked"]),
    ("waiting", ["Waiting"]),
    ("investigating", ["Investigating"]),
    ("done", ["Closed", "Resolved"]),
    ("closed", ["Closed"]),
]

SYSTEM_PROMPT = (
    "You are SupportIncidentAgent, an AI assistant that answers questions about "
    "Azure DevOps SUPPORT INCIDENTS (not a software development backlog).\n"
    "Rules:\n"
    "- Use the provided tools to fetch every fact. Never invent incident data, "
    "SLA values, dates, owners or counts.\n"
    "- If information is missing in Azure DevOps, say it is not available.\n"
    "- Output concise, structured answers (severity, priority, age, risk, owner).\n"
    "- When listing tickets, keep tables narrow: at most ID, Title, State, Owner. "
    "Put extra fields (severity, priority, age, SLA due) on short detail lines "
    "under each ticket - never emit tables wider than 4 columns.\n"
    "- This assistant is read-only. It never creates, updates or closes work items.\n"
    "- Core questions: what is open, critical, aging, breaching SLA, unowned, "
    "needing escalation, blocked, recurring, UAT queue, follow-ups, daily client "
    "discussion and weekly report.\n"
    "Ticket state vocabulary (use it when interpreting questions):\n"
    "- 'done' means Closed (Resolved counts as done too).\n"
    "- 'work in progress' / 'WIP' means actively being worked: In Progress, "
    "Testing and Triage states.\n"
    "- 'UAT' means User Acceptance Testing (customer validation, last step "
    "before closure).\n"
    "- 'New' means open but work has not started yet ('not started').\n"
)


class SupportIncidentAgent:
    """Single primary agent with specialized tools (no multi-agent complexity)."""

    def __init__(
        self,
        provider: Any = None,
        llm: LLMClient | None = None,
        now: datetime | None = None,
    ):
        from app.services.azure_devops import get_provider

        self.provider = provider or get_provider()
        self.llm = llm or LLMClient()
        self._now = now

    # ------------------------------------------------------------------ #
    @property
    def mode(self) -> str:
        return "demo" if getattr(self.provider, "name", "") == "demo" else "azure_devops"

    @property
    def now(self) -> datetime:
        return self._now or datetime.now()

    def context(self) -> ToolContext:
        return ToolContext(
            provider=self.provider,
            now=self.now,
        )

    # ------------------------------------------------------------------ #
    def ask(self, message: str) -> ChatResponse:
        intent, payload, answer = self._route(message)
        if intent != "unknown":
            return ChatResponse(
                answer=answer, intent=intent, mode=self.mode, used_llm=False, data=payload
            )
        if self.llm.configured:
            try:
                answer, data = self._ask_llm(message)
            except Exception as exc:  # LLM outage/rate-limit: degrade gracefully, never 500
                return ChatResponse(
                    answer=(
                        "The AI model is temporarily unavailable "
                        f"({exc}). I ran a local search instead:\n\n"
                        + self._fallback_search(message)[2]
                    ),
                    intent="llm_unavailable",
                    mode=self.mode,
                    used_llm=False,
                    data={"error": str(exc)},
                )
            return ChatResponse(
                answer=answer, intent="llm_tools", mode=self.mode, used_llm=True, data=data
            )
        intent, payload, answer = self._fallback_search(message)
        return ChatResponse(
            answer=answer, intent=intent, mode=self.mode, used_llm=False, data=payload
        )

    # ------------------------------------------------------------------ #
    # Deterministic intent router (works with zero configuration / no LLM)
    # ------------------------------------------------------------------ #
    def _route(self, message: str) -> tuple[str, dict[str, Any], str]:
        text = message.strip()
        low = text.lower()
        ctx = self.context()

        # comment/update activity questions ("received comments this week") are an
        # activity search, not the weekly/bi-weekly status report
        activity_q = "comment" in low or "update" in low
        # "list of tickets assigned to X this week/month" is a filtered list, not
        # the weekly/open status report: a filter signal + time scope falls through
        # to the search intent which applies the date window itself.
        filtered_time_q = bool(_FILTER_SIGNAL.search(low)) and bool(
            _WEEK_SCOPE.search(low) or _MONTH_SCOPE.search(low)
        )
        if re.search(r"bi.?weekly|fortnight|last 14 days|past 14 days|last two weeks|past two weeks", low) and not activity_q and not filtered_time_q:
            return self._intent_biweekly()
        if re.search(r"\bweek(ly)?\b|last 7 days|past week|this week|current week", low) and not activity_q and not filtered_time_q:
            return self._intent_weekly()
        if re.search(r"\buat\b|user acceptance", low):
            return self._intent_uat()
        if re.search(r"follow[\s-]?ups?|stale|no updates?|awaiting", low):
            return self._intent_followup()
        if re.search(r"discuss|talking points|daily discussion|client today", low):
            return self._intent_briefing()
        if "standup" in low or "stand-up" in low or "daily report" in low:
            return self._intent_standup()
        if "escalat" in low:
            return self._intent_escalation()
        if "recurring" in low or "repeating" in low or "common problem" in low or "trend" in low:
            return self._intent_recurring()
        if "resolution summary" in low:
            inc_id = analytics.find_incident_id(text)
            if inc_id:
                return self._intent_resolution(inc_id)

        inc_id = analytics.find_incident_id(text)
        if inc_id and ("summar" in low or "tell me about" in low or low.startswith("inc")):
            return self._intent_incident_summary(inc_id)

        if "sla" in low or "at risk" in low or "risk of breach" in low or "breach" in low:
            return self._intent_sla_risk()
        if ("status" in low) or ("today" in low and "support" in low) or low.strip() in {"status", "overview"}:
            return self._intent_status()

        # aging
        if re.search(r"\b(older than|open for|aged|aging|age)\b", low) or (
            DAY_WORDS.search(low) and re.search(r"\b(days?)\b", low) and re.search(r"\b(more than|>|over|older)\b", low)
        ):
            days = 3.0
            m = DAY_WORDS.search(low)
            if m:
                days = float(m.group(1))
            return self._intent_aging(days)

        search_hint = re.search(
            r"\bp[1-4]\b|\b(incident|incidents|ticket|tickets|working|wip|triage|done|comment|comments|commented|critical|assigned|unassigned|owner|owners|"
            r"production|authentication|customer|changed|resolved|unresolved|show|list|find|"
            r"recent|stale|blocked|severity|open|priority)\b",
            low,
        )
        if search_hint:
            return self._intent_search(text)
        return "unknown", {}, ""

    # --------------------------- intents ------------------------------ #
    def _intent_status(self) -> tuple[str, dict[str, Any], str]:
        status = analytics.support_status(self.provider.list_incidents(), self.now)
        return "support_status", status.model_dump(mode="json"), render_status(status)

    def _intent_sla_risk(self) -> tuple[str, dict[str, Any], str]:
        incidents = self.provider.list_incidents()
        risks = analytics.sla_risk_incidents(incidents, self.now)
        unknown = [
            i
            for i in incidents
            if i.is_open
            and not analytics.is_on_hold(i)
            and analytics.sla_risk(i, self.now).risk == RiskLevel.UNKNOWN
        ]
        hold = analytics.hold_incidents(incidents, self.now)
        payload = {
            "risks": [r.model_dump(mode="json") for r in risks],
            "unknown_sla_count": len(unknown),
            "unknown_sla_ids": [i.id for i in unknown],
            "on_hold": [
                {
                    "incident_id": i.id,
                    "title": i.title,
                    "owner": i.owner_display,
                    "age_days": round(i.age_days(self.now), 1),
                }
                for i in hold
            ],
        }
        return "sla_risk", payload, render_sla_risk(risks, unknown, hold, self.now)

    def _intent_aging(self, days: float) -> tuple[str, dict[str, Any], str]:
        items = analytics.aging_incidents(self.provider.list_incidents(), self.now, days)
        payload = {"min_days": days, "count": len(items), "ids": [i.id for i in items]}
        return "aging", payload, render_aging(items, days, self.now)

    def _intent_incident_summary(self, incident_id: str) -> tuple[str, dict[str, Any], str]:
        inc = self.provider.find_by_id(incident_id)
        if inc is None:
            return "incident_summary", {"found": False, "incident_id": incident_id}, (
                f"{incident_id} was not found in the connected Azure DevOps project."
            )
        summary = analytics.summarize_incident(inc, self.now)
        return "incident_summary", summary.model_dump(mode="json"), render_summary(summary)

    def _intent_resolution(self, incident_id: str) -> tuple[str, dict[str, Any], str]:
        inc = self.provider.find_by_id(incident_id)
        if inc is None:
            return "resolution_summary", {"found": False}, (
                f"{incident_id} was not found in the connected Azure DevOps project."
            )
        res = analytics.resolution_summary(inc)
        return "resolution_summary", res.model_dump(mode="json"), render_resolution(res)

    def _intent_escalation(self) -> tuple[str, dict[str, Any], str]:
        items = analytics.escalation_recommendations(
            self.provider.list_incidents(), self.now, min_band=EscalationBand.MEDIUM
        )
        payload = [i.model_dump(mode="json") for i in items]
        return "escalation", {"recommendations": payload}, render_escalations(items)

    def _intent_standup(self) -> tuple[str, dict[str, Any], str]:
        report = analytics.standup_report(self.provider.list_incidents(), self.now)
        return "standup", report.model_dump(mode="json"), render_standup(report)

    def _intent_briefing(self) -> tuple[str, dict[str, Any], str]:
        data = analytics.daily_briefing(self.provider.list_incidents(), self.now)
        return "daily_briefing", data, render_briefing(data)

    def _intent_weekly(self) -> tuple[str, dict[str, Any], str]:
        data = analytics.weekly_report(self.provider.list_incidents(), self.now)
        return "weekly_report", data, render_weekly(data)

    def _intent_biweekly(self) -> tuple[str, dict[str, Any], str]:
        from app.config import get_settings

        data = analytics.weekly_report(self.provider.list_incidents(), self.now, window_days=14)
        return (
            "biweekly_report",
            data,
            render_biweekly(data, get_settings().project_label),
        )

    def _intent_uat(self) -> tuple[str, dict[str, Any], str]:
        items = analytics.filter_incidents(
            self.provider.list_incidents(), now=self.now, open_only=True, state="UAT"
        )
        payload = {"count": len(items), "ids": [i.id for i in items]}
        if not items:
            return "uat", payload, "UAT queue is empty - no open incidents are in UAT."
        return "uat", payload, render_incident_table(items, self.now, title="UAT QUEUE")

    def _intent_followup(self) -> tuple[str, dict[str, Any], str]:
        items = analytics.filter_incidents(
            self.provider.list_incidents(), now=self.now, open_only=True, stale_hours=48.0
        )
        payload = {"count": len(items), "ids": [i.id for i in items]}
        if not items:
            return "followup", payload, "Nothing needs follow-up - every open incident was updated in the last 2 days."
        return (
            "followup",
            payload,
            render_incident_table(items, self.now, title="NEEDS FOLLOW-UP (no update in 2 days)"),
        )

    def _intent_recurring(self) -> tuple[str, dict[str, Any], str]:
        issues = analytics.recurring_issues(self.provider.list_incidents(), self.now)
        return "recurring", {"issues": [i.model_dump(mode="json") for i in issues]}, render_recurring(issues)

    def _intent_search(self, message: str) -> tuple[str, dict[str, Any], str]:
        low = message.lower()
        kwargs: dict[str, Any] = {"now": self.now}

        m = PRIORITY.search(low)
        if m:
            kwargs["priority"] = int(m.group(1))
        if "critical" in low:
            kwargs["severity"] = "Critical"
        if "unassigned" in low or "without an owner" in low or "no owner" in low or "without owners" in low:
            kwargs["unassigned_only"] = True
        m = ASSIGNED_EMAIL.search(message) or ASSIGNED_NAME.search(message)
        if m and not kwargs.get("unassigned_only"):
            kwargs["assigned_to"] = m.group(1).strip()
        # natural-language states ("those in In Progress or Testing") -> state filter
        state_hits: list[str] = []
        for phrase, canons in STATE_PHRASES:
            if re.search(rf"\b{re.escape(phrase)}\b", low):
                state_hits.extend(c for c in canons if c not in state_hits)
        if state_hits and not kwargs.get("state") and not kwargs.get("state_in"):
            kwargs["state_in"] = state_hits
        if "customer impact" in low:
            kwargs["customer_impact_only"] = True
        # "comments today / this week" needs REAL comment dates: a state change
        # also bumps ChangedDate, so recency alone admits stale-comment tickets.
        comment_window_start = None
        if "today" in low or "week" in low:
            # "comments today" / "updated today" / "created today" / "resolved today",
            # or the same activity scoped to the current week (week window wins: it
            # covers today).
            week_q = bool(re.search(r"\bweek(ly)?\b|current week|this week|past week", low))
            midnight = self.now.replace(hour=0, minute=0, second=0, microsecond=0)
            week_start, _, _, _ = analytics.current_ist_week(self.now)
            if "created" in low and "comment" not in low and "update" not in low:
                kwargs["created_after"] = week_start if (week_q and "today" not in low) else midnight
            elif "resolved" in low or "closed" in low:
                kwargs["closed_only"] = True
                kwargs["closed_after"] = week_start if (week_q and "today" not in low) else midnight
            elif "comment" in low or "update" in low or "changed" in low:
                if week_q:
                    if "comment" in low:
                        comment_window_start = week_start
                    kwargs["changed_within_hours"] = max(
                        (self.now - week_start).total_seconds() / 3600.0, 1.0
                    )
                else:
                    if "comment" in low:
                        comment_window_start = midnight
                    kwargs["changed_within_hours"] = max(
                        (self.now - midnight).total_seconds() / 3600.0, 1.0
                    )
        if "no recent update" in low or "without recent update" in low or "stale" in low:
            kwargs["stale_hours"] = 12.0
        if "resolved" in low or "closed" in low:
            pass  # keep default (all) unless open_only requested
        if re.search(r"\b(open|unresolved|active)\b", low):
            kwargs["open_only"] = True

        # month / week / explicit date-range scoping ("for the current month",
        # "assigned to X this week", "1-30th oct,2026"). The window is applied to
        # the created date. Queries naming an activity field (created/resolved/
        # closed/comments/updates) are handled above and never overridden.
        month_label = None
        if comment_window_start is None and not _ACTIVITY_WORDS.search(low):
            window = analytics.parse_date_window(message, self.now)
            if window:
                start, end, month_label = window
                kwargs.setdefault("created_after", start)
                kwargs.setdefault("created_before", end)

        query = _search_query(message)
        if query:
            kwargs["query"] = query

        items = analytics.filter_incidents(self.provider.list_incidents(), **kwargs)

        # "high-priority" phrase handling (severity High OR priority <= 2)
        if "high-priority" in low or "high priority" in low:
            items = [
                i
                for i in items
                if (i.severity or "").lower() == "high" or (i.priority is not None and i.priority <= 2)
            ]

        # On Hold is paused: keep it out of action lists unless explicitly asked.
        if "hold" not in low and "On Hold" not in (kwargs.get("state_in") or []):
            items = [i for i in items if not analytics.is_on_hold(i)]

        table_title = "MATCHING INCIDENTS"
        if comment_window_start is not None:
            # "received comments ..." must mean real comments: drop tickets whose
            # ChangedDate moved for other reasons (e.g. bulk state changes).
            from concurrent.futures import ThreadPoolExecutor

            provider = self.provider

            def _has_recent_comment(incident_id: str) -> bool:
                try:
                    comments = provider.get_comments(incident_id)
                except Exception:
                    return True  # comment history unavailable: keep on ChangedDate basis
                return any(c.created_at >= comment_window_start for c in comments)

            with ThreadPoolExecutor(max_workers=10) as pool:
                flags = list(pool.map(_has_recent_comment, [i.id for i in items]))
            items = [i for i, keep in zip(items, flags) if keep]
            table_title = (
                "TICKETS WITH COMMENTS THIS WEEK"
                if comment_window_start < self.now.replace(hour=0, minute=0, second=0, microsecond=0)
                else "TICKETS WITH COMMENTS TODAY"
            )
        elif month_label:
            table_title = f"MATCHING INCIDENTS - {month_label}"

        payload = {"count": len(items), "ids": [i.id for i in items], "filters": kwargs_str(kwargs)}
        if not items:
            answer = "No incidents matched those filters."
            if not self.llm.configured:
                answer += (
                    "\n\nTip: try e.g. 'Show P1 incidents', 'Show incidents without owners' "
                    "or 'Summarize INC-1234'."
                )
            return "search", payload, answer
        return "search", payload, render_incident_table(items, self.now, title=table_title)

    def _fallback_search(self, message: str) -> tuple[str, dict[str, Any], str]:
        intent, payload, answer = self._intent_search(message)
        return "search_fallback", payload, answer

    # ------------------------------------------------------------------ #
    # LLM tool-calling path (read-only tools only)
    # ------------------------------------------------------------------ #
    def _ask_llm(self, message: str) -> tuple[str, dict[str, Any]]:
        ctx = self.context()
        tools = registry.openai_schemas()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ]
        collected: dict[str, Any] = {}
        for _ in range(6):
            response = self.llm.chat(messages, tools=tools)
            choice = (response.get("choices") or [{}])[0]
            msg = choice.get("message", {})
            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                return msg.get("content", ""), collected
            messages.append(
                {
                    "role": "assistant",
                    "content": msg.get("content"),
                    "tool_calls": tool_calls,
                }
            )
            for call in tool_calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                try:
                    tool = registry.get(name)
                    if tool is None or tool.mutates:
                        raise PermissionError("Only read-only tools are available to the agent.")
                    result = registry.call(name, args, ctx=ctx)
                except Exception as exc:
                    result = {"error": str(exc)}
                collected[name] = result
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id"),
                        "content": json.dumps(result, default=str)[:12000],
                    }
                )
        return "I could not complete the analysis within the tool budget.", collected


# --------------------------------------------------------------------------- #
# Renderers - every number shown here comes from Azure DevOps data
# --------------------------------------------------------------------------- #

SEV_ORDER = ["Critical", "High", "Medium", "Low"]


def _sev_counts(status) -> dict[str, int]:
    return {s: getattr(status, s.lower()) for s in SEV_ORDER}


def render_status(status) -> str:
    lines = [
        "SUPPORT STATUS",
        "==============",
        "",
        f"Overall Health: {status.overall_health}",
        "",
        f"Open Incidents: {status.open_incidents}",
    ]
    for sev, count in _sev_counts(status).items():
        lines.append(f"{sev}: {count}")
    lines += [
        "",
        f"New Today: {status.new_today}",
        f"Resolved Today: {status.resolved_today}",
        "",
        f"SLA At Risk: {status.sla_at_risk}",
        "",
    ]
    if status.oldest_open_id:
        lines += [
            "Oldest Open Incident:",
            f"{status.oldest_open_id} - {status.oldest_open_age_days} days",
            "",
        ]
    lines.append(f"Unassigned: {status.unassigned}")
    lines.append("")
    lines.append(f"Escalation Required: {status.escalation_required}")
    lines += ["", "Top Risks", ""]
    if not status.top_risks:
        lines.append("No significant risks detected.")
    for idx, risk in enumerate(status.top_risks, start=1):
        lines.append(f"{idx}. {risk.incident_id}")
        if risk.severity:
            lines.append(f"   {risk.severity}")
        if getattr(risk, "status", None):
            lines.append(f"   Status: {risk.status}")
        lines.append(f"   Open for {risk.age_days} days")
        for reason in risk.reasons[1:]:
            if reason.lower().startswith("open for"):
                continue  # age is already shown above
            lines.append(f"   {reason}")
        lines.append("")
    lines.append("Recommendations")
    for rec in status.recommendations:
        lines.append(f"- {rec}")
    return "\n".join(lines).strip()


def render_sla_risk(risks, unknown, hold=None, now=None) -> str:
    lines = ["SLA RISK ANALYSIS", "=================", ""]
    if not risks:
        lines.append("No open incident is currently inside its SLA window.")
        lines.append("")
    for level in (RiskLevel.HIGH, RiskLevel.MEDIUM, RiskLevel.LOW):
        group = [r for r in risks if r.risk == level]
        if not group:
            continue
        lines += [level.value, ""]
        for r in group:
            state = r.state or "Not available"
            if state.strip().lower() == "on hold":
                state += " (SLA clock still running)"
            lines += [
                r.incident_id,
                f"Severity: {r.severity or 'Not available'}",
                f"Priority: {r.priority or 'Not available'}",
                f"Status: {state}",
                f"Age: {analytics.format_days(r.age_hours)}",
                f"SLA Target: {analytics.format_working_days(r.sla_target_hours)}",
                f"Time Remaining: {analytics.format_working_days(r.time_remaining_hours)}",
                "",
                "Reason:",
                r.reason,
                "",
                "Recommendation:",
                r.recommendation,
                "",
            ]
    if unknown:
        lines += [
            "SLA Risk: UNKNOWN",
            "",
            f"{len(unknown)} open incident(s) have no end date (SLA due date / due date): "
            + ", ".join(i.id for i in unknown[:8])
            + (" ..." if len(unknown) > 8 else ""),
            "",
            "Reason:",
            "No end date is available in the Azure DevOps data.",
            "",
        ]
    hold = hold or []
    lines += ["ON HOLD (tracked separately, not in SLA risk)", "", ""]
    if not hold:
        lines.append("No tickets on hold.")
    for i in hold:
        age = f"{i.age_days(now):.1f} days" if now is not None else ""
        lines.append(f"{i.id} - {i.title} ({i.owner_display}, {age})".rstrip())
    return "\n".join(lines).strip()


def render_aging(items, min_days, now) -> str:
    lines = [f"INCIDENTS OPEN FOR MORE THAN {min_days:g} DAYS", "=" * 46, ""]
    if not items:
        lines.append("No open incidents match this aging filter.")
        return "\n".join(lines)
    lines += [
        f"{'ID':<10} {'Priority':<9} {'Age':<9} {'Owner':<14} {'Status'}",
        "-" * 60,
    ]
    for inc in items:
        age = f"{inc.age_days(now):.0f} days"
        prio = inc.priority_label or "-"
        lines.append(
            f"{inc.id:<10} {prio:<9} {age:<9} {(inc.assigned_to or '-')[:13]:<14} {inc.state}"
        )
    lines += ["", f"Total: {len(items)} incident(s)."]
    return "\n".join(lines)


def render_summary(s) -> str:
    lines = [f"{s.incident_id} - {s.title}", ""]
    lines.append(f"Status:\n{s.status}")
    lines.append("")
    lines.append(f"Severity:\n{s.severity or 'Not available'}")
    lines.append("")
    lines.append(f"Priority:\n{s.priority or 'Not available'}")
    lines.append("")
    lines.append(f"Owner:\n{s.owner or 'Unassigned'}")
    lines.append("")
    lines.append(f"Age:\n{s.age_days} days")
    lines.append("")
    lines.append(f"Customer Impact:\n{s.customer_impact or 'Not recorded'}")
    lines.append("")
    if s.customer:
        lines += [f"Customer: {s.customer}", ""]
    if s.environment:
        lines += [f"Environment: {s.environment}", ""]
    lines.append(f"Current Situation:\n{s.current_situation or 'No updates recorded.'}")
    if s.recent_activity:
        lines += ["", "Recent Activity:"]
        lines += [f"  {a}" for a in s.recent_activity]
    if s.related_incidents:
        lines += ["", "Related: " + ", ".join(s.related_incidents)]
    if s.resolution:
        lines += ["", f"Resolution:\n{s.resolution}"]
    lines += ["", f"Next Recommended Action:\n{s.next_recommended_action}"]
    lines += ["", f"Escalation:\n{s.escalation}"]
    lines += ["", f"SLA Risk:\n{s.sla_risk.value}"]
    if s.missing_fields:
        lines += ["", "Data not available in Azure DevOps: " + ", ".join(s.missing_fields)]
    return "\n".join(lines)


def render_escalations(items) -> str:
    lines = ["RECOMMENDED ESCALATIONS", "=======================", ""]
    if not items:
        lines.append("No escalation is recommended right now.")
        return "\n".join(lines)
    lines.append("Recommendation only - nothing is escalated automatically.")
    lines.append("")
    for band in (EscalationBand.HIGH, EscalationBand.MEDIUM):
        group = [i for i in items if i.band == band]
        if not group:
            continue
        lines += [band.value, ""]
        for item in group:
            lines += [item.incident_id, "Reason:", "; ".join(item.reasons) + ".", ""]
    return "\n".join(lines).strip()


def render_briefing(data: dict[str, Any]) -> str:
    lines = ["DAILY CLIENT DISCUSSION", "=======================", ""]
    lines.append(f"Open incidents: {data['open_count']}")
    lines.append("")

    def _fmt(items: list[dict[str, Any]]) -> list[str]:
        return [f"- {i['incident_id']}: {i['title']} ({i['owner']}, {i['age_days']}d)" for i in items]

    lines.append(f"1. Critical right now ({len(data['critical'])})")
    lines += _fmt(data["critical"]) or ["   None."]
    lines.append("")
    lines.append(f"2. SLA breach risk ({len(data['sla_high_risk'])})")
    for r in data["sla_high_risk"]:
        left = analytics.format_working_days(r.get("time_remaining_hours")) + " left" if r.get("time_remaining_hours") is not None else r["risk"]
        lines.append(f"- {r['incident_id']}: {r['title']} ({left})")
    if not data["sla_high_risk"]:
        lines.append("   None.")
    lines.append("")
    lines.append(f"3. Recommended escalations ({len(data['escalations_high'])})")
    for e in data["escalations_high"]:
        lines.append(f"- {e['incident_id']}: {e['title']} ({'; '.join(e['reasons'][:2])})")
    if not data["escalations_high"]:
        lines.append("   None.")
    lines.append("")
    lines.append(f"4. Stale - needs follow-up, no update in 2 days ({len(data['stale_followups'])})")
    lines += _fmt(data["stale_followups"][:8]) or ["   None."]
    lines.append("")
    lines.append(f"5. Unassigned ({len(data['unassigned'])})")
    lines += _fmt(data["unassigned"][:8]) or ["   None."]
    lines.append("")
    lines.append(f"6. On hold ({len(data.get('on_hold', []))})")
    lines += _fmt(data.get("on_hold", [])[:8]) or ["   None."]
    return "\n".join(lines).strip()


def render_weekly(data: dict[str, Any]) -> str:
    title = data.get("title", "WEEKLY SUPPORT REPORT")
    lines = [f"{title} ({data.get('week', 'this week')})", "===================================", ""]
    lines.append(f"New: {data['new_count']}   Resolved: {data['resolved_count']}   Current backlog: {data['open_count']}")
    lines.append("")
    lines.append("Open by severity: " + ", ".join(f"{k} {v}" for k, v in data["open_by_severity"].items()))
    lines.append("")
    lines.append(f"New this week ({data['new_count']})")
    for i in data["new_incidents"][:10]:
        lines.append(f"- {i['incident_id']}: {i['title']} ({i['state']}, {i['owner']})")
    if not data["new_incidents"]:
        lines.append("   None.")
    lines.append("")
    lines.append(f"Resolved this week ({data['resolved_count']})")
    for i in data["resolved_incidents"][:10]:
        lines.append(f"- {i['incident_id']}: {i['title']}")
    if not data["resolved_incidents"]:
        lines.append("   None.")
    lines.append("")
    lines.append(f"SLA risks open ({len(data['sla_risks'])})")
    for r in data["sla_risks"]:
        lines.append(f"- {r['incident_id']}: {r['risk']} - {r['title']}")
    if not data["sla_risks"]:
        lines.append("   None.")
    lines.append("")
    lines.append(f"Open more than 7 days ({len(data['aging_over_7_days'])})")
    for i in data["aging_over_7_days"][:10]:
        lines.append(f"- {i['incident_id']}: {i['title']} ({i['age_days']}d)")
    if not data["aging_over_7_days"]:
        lines.append("   None.")
    lines.append("")
    lines.append(f"In UAT ({len(data['uat_open'])})")
    for i in data["uat_open"]:
        lines.append(f"- {i['incident_id']}: {i['title']} ({i['owner']})")
    if not data["uat_open"]:
        lines.append("   None.")
    lines.append("")
    lines.append(f"Recurring themes ({len(data['recurring'])})")
    for r in data["recurring"]:
        lines.append(f"- {r['label']}: {r['count']} incidents ({', '.join(r['incident_ids'][:5])})")
    if not data["recurring"]:
        lines.append("   None found.")
    return "\n".join(lines).strip()


def render_biweekly(data: dict[str, Any], project: str) -> str:
    """Client-ready bi-weekly status report in the agreed template."""
    uat = data.get("uat_open", [])
    testing = data.get("testing_open", [])
    in_progress = data.get("in_progress_open", [])
    new = data.get("new_open", [])
    hold = data.get("on_hold_open", [])
    backlog_total = len(uat) + len(testing) + len(in_progress) + len(new) + len(hold)

    # Global serial numbers across the backlog, in display order.
    serial, order = {}, []
    for group in (uat, testing, in_progress, new, hold):
        for item in group:
            serial[item["incident_id"]] = len(order) + 1
            order.append(item)

    def _age(item: dict[str, Any]) -> str:
        days = item.get("age_days") or 0
        return f"{days:.0f} days" if days >= 1 else "Today"

    backlog_rows = [
        ("UAT", uat, None),
        ("Testing", testing, None),
        ("In Progress", in_progress, None),
        ("New / Triage", new, None),
        ("On Hold", hold, lambda i: f"({_age(i)})"),
    ]
    status_w = max([len("Status")] + [len(label) for label, _, _ in backlog_rows])

    def _tickets(items: list[dict[str, Any]], suffix=None) -> str:
        if not items:
            return "-"
        return ", ".join(
            i["incident_id"] + (f" {suffix(i)}" if suffix else "") for i in items
        )

    lines = [
        f"Bi-Weekly Status Report - {project}",
        "",
        f"Reporting Period: {data.get('week', '')}",
        "",
        f"1. Current Backlog ({backlog_total})",
        "",
        f"{'Status':<{status_w}}   {'Count':<5}   Tickets",
        "-" * 60,
    ]
    for label, items, suffix in backlog_rows:
        lines.append(
            f"{label:<{status_w}}   {len(items):<5}   {_tickets(items, suffix)}"
        )
    lines += [
        "",
        "2. Accomplishments",
        "",
        f"{data.get('resolved_count', 0)} tickets have been closed during the reporting period.",
    ]
    if uat:
        lines.append(
            f"UAT activities have progressed, with {len(uat)} ticket(s) currently "
            "awaiting final UAT confirmation/closure."
        )
    if testing:
        lines.append(
            f"Development is complete for {len(testing)} item(s) currently in testing, "
            "ready for the next validation cycle."
        )
    if data.get("new_count"):
        lines.append(f"{data['new_count']} new ticket(s) were raised during the period.")
    lines += ["", f"3. UAT - Awaiting Confirmation / Closure ({len(uat)} Tickets)", ""]
    if uat:
        area_w = max([len("Area")] + [len(i.get("area", "-")) for i in uat])
        lines += [
            f"{'#':<3}   {'Ticket':<10}   {'Area':<{area_w}}   Description"
            + " " * 40
            + "Age",
            "-" * 90,
        ]
        for item in uat:
            lines.append(
                f"{serial[item['incident_id']]:<3}   {item['incident_id']:<10}   "
                f"{item.get('area', '-'):<{area_w}}   {item['title'][:50]:<50}   {_age(item)}"
            )
    else:
        lines.append("No tickets in UAT.")
    lines += ["", "4. Testing - Development Completed", ""]
    if testing:
        lines += [
            "The following items have completed development and are currently in testing:",
            "",
            f"{'Ticket':<10}   {'Area':<16}   Description" + " " * 39 + "Status",
            "-" * 100,
        ]
        for item in testing:
            lines.append(
                f"{item['incident_id']:<10}   {item.get('area', '-'):<16}   "
                f"{item['title'][:40]:<40}   {item['state']}"
            )
        lines += [
            "",
            "Next step: Once testing is completed, the items will be moved to UAT/feedback as applicable.",
        ]
    else:
        lines.append("No tickets in testing.")
    lines += ["", "5. Key Focus for Next", ""]
    if uat:
        lines.append(
            "Obtain UAT confirmation and close the remaining "
            f"{len(uat)} UAT ticket(s) ({', '.join(i['incident_id'] for i in uat)})."
        )
    stale_hold = [i for i in hold if (i.get("age_days") or 0) >= 14]
    if stale_hold:
        aged = ", ".join(f"{i['incident_id']} ({_age(i)})" for i in stale_hold)
        lines.append(
            "Review the long-pending On Hold items "
            f"({aged}) "
            "and confirm the required action/next steps where client input is needed."
        )
    sla_high = [r for r in data.get("sla_risks", []) if r.get("risk") == "HIGH RISK"]
    if sla_high:
        lines.append(
            "Escalate tickets at SLA risk: "
            f"{', '.join(r['incident_id'] for r in sla_high)}."
        )
    if not uat and not stale_hold and not sla_high:
        lines.append("No critical focus items - keep monitoring.")
    return "\n".join(lines).strip()


def render_standup(report) -> str:
    def _fmt(inc) -> str:
        return f"- {inc.incident_id}: {inc.title} (Owner: {inc.owner}, Status: {inc.status}) - {inc.risk}"

    lines = ["SUPPORT STANDUP", "===============", ""]
    lines += [f"Critical Incidents ({len(report.critical_incidents)})", "------------------"]
    if not report.critical_incidents:
        lines.append("None.")
    for inc in report.critical_incidents:
        lines.append(_fmt(inc))
    lines += [
        "",
        f"New Incidents ({report.new_today})",
        "-------------",
    ]
    new_items = getattr(report, "new_incidents", []) or []
    if not new_items:
        lines.append(f"{report.new_today} new incidents today" if report.new_today == 0 else "See incident list for details.")
        if report.new_today > 0 and not new_items:
            pass  # count-only fallback (old payloads)
    else:
        for inc in new_items:
            lines.append(_fmt(inc))
    lines += [
        "",
        f"Resolved ({report.resolved_yesterday} in the last day)",
        "--------",
    ]
    resolved_items = getattr(report, "resolved_incidents", []) or []
    if not resolved_items:
        lines.append("None." if report.resolved_yesterday == 0 else f"{report.resolved_yesterday} resolved in the last day - see backlog for details.")
    else:
        for inc in resolved_items:
            lines.append(_fmt(inc))
    lines += [
        "",
        f"SLA At Risk ({len(report.sla_at_risk)})",
        "-----------",
    ]
    if report.sla_at_risk:
        for r in report.sla_at_risk:
            lines.append(_fmt(r))
    else:
        lines.append("None.")
    lines += ["", f"Blockers ({len(report.blockers)})", "--------"]
    if report.blockers:
        for b in report.blockers:
            blocked_by = b.risk if b.risk and b.risk != "Blocked" else "Blocked"
            lines.append(f"- {b.incident_id}: {b.title} (Owner: {b.owner}, Status: {b.status}) - Blocked by: {blocked_by}")
    else:
        lines.append("None.")
    lines += [
        "",
        f"Escalations ({report.escalations_recommended} recommended)",
        "-----------",
    ]
    escalations = getattr(report, "escalations", []) or []
    if not escalations:
        lines.append("None recommended." if report.escalations_recommended == 0 else f"{report.escalations_recommended} recommended - ask 'Which incidents need escalation?' for reasons.")
    else:
        for e in escalations:
            reasons = "; ".join(e.reasons) if getattr(e, "reasons", None) else ""
            lines.append(f"- {e.incident_id}: {e.title} [{e.band.value if hasattr(e.band, 'value') else e.band}] - {reasons}".rstrip(" -"))
    unassigned_items = getattr(report, "unassigned_incidents", []) or []
    lines += ["", f"Unassigned incidents: {report.unassigned}", ""]
    if unassigned_items:
        for u in unassigned_items:
            lines.append(_fmt(u))
    return "\n".join(lines)


def render_recurring(issues) -> str:
    lines = ["RECURRING ISSUES", "================", ""]
    if not issues:
        lines.append("No recurring patterns found in the last 30 days.")
        return "\n".join(lines)
    for idx, issue in enumerate(issues, start=1):
        lines += [
            f"{idx}. {issue.label}",
            f"   Incidents: {issue.count}",
            f"   Trend: {issue.trend}",
            f"   Impact: {issue.impact}",
            "   " + ", ".join(issue.incident_ids),
            "",
        ]
    lines.append(
        "Note: these clusters are AI-derived patterns (tags/keywords), "
        "not explicit Azure DevOps categories."
    )
    return "\n".join(lines).strip()


def render_resolution(res) -> str:
    lines = [
        "INCIDENT RESOLUTION SUMMARY",
        "===========================",
        "",
        f"Incident: {res.incident_id} - {res.title}",
        "",
        "Problem:",
        res.problem or "Not available in the work item.",
        "",
        "Impact:",
        res.impact or "Not available in the work item.",
        "",
        "Root Cause:",
        res.root_cause or "NOT RECORDED - cannot be inferred safely.",
        "",
        "Resolution:",
        res.resolution or "NOT RECORDED - cannot be inferred safely.",
        "",
        "Validation:",
        res.validation or "Not available in the work item.",
        "",
        "Preventive Action:",
        res.preventive_action or "Not recorded.",
    ]
    if res.missing_information:
        lines += ["", "Missing information:"]
        lines += [f"- {m}" for m in res.missing_information]
    return "\n".join(lines)


def render_incident_table(items: list[Incident], now: datetime, title: str = "INCIDENTS") -> str:
    lines = [title, "=" * len(title), ""]
    lines += [
        f"{'ID':<10} {'Sev':<9} {'Pri':<4} {'Age':<9} {'Owner':<14} {'Status':<15} Title",
        "-" * 90,
    ]
    for inc in items:
        lines.append(
            f"{inc.id:<10} {(inc.severity or '-'):<9} {(inc.priority_label or '-'):<4} "
            f"{inc.age_days(now):<9.1f} {(inc.assigned_to or 'Unassigned')[:13]:<14} "
            f"{inc.state:<15} {inc.title[:40]}"
        )
    lines += ["", f"Total: {len(items)} incident(s)."]
    return "\n".join(lines)


def _search_query(message: str) -> str:
    tokens = [
        t
        for t in re.split(r"\W+", message.lower())
        if t
        and t not in _CONTROL_TOKENS
        and not re.match(r"^\d", t)
        and not re.fullmatch(r"p[1-4]", t)
    ]
    return " ".join(tokens)


def kwargs_str(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {
        k: (v.isoformat() if isinstance(v, datetime) else v)
        for k, v in kwargs.items()
        if k != "now"
    }
