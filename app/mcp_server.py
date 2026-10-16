"""FastMCP server exposing the support-incident tools to MCP clients.

Run (stdio):     python -m app.mcp_server
Run (HTTP):      python -m app.mcp_server --http --port 8001

Read-only: all tools query Azure DevOps SUPPORT INCIDENTS. No write operations.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from fastmcp import FastMCP

from app.agents.support_agent import SupportIncidentAgent
from app.tools import incident_tools as _incident_tools  # noqa: F401
from app.tools.registry import ToolContext, registry

mcp = FastMCP(
    "azure-devops-support-assistant",
    instructions=(
        "Tools for querying Azure DevOps SUPPORT INCIDENTS (status, SLA risk, aging, "
        "escalation, standup, recurring issues). "
        "All data comes from Azure DevOps - never invent values. Read-only."
    ),
    version="0.1.0",
)


def _ctx() -> ToolContext:
    agent = SupportIncidentAgent()
    return agent.context()


# --------------------------------------------------------------------------- #
# READ tools
# --------------------------------------------------------------------------- #


@mcp.tool
def get_support_status() -> dict[str, Any]:
    """Today's support status: open/critical/high counts, SLA risk, unassigned, top risks."""
    return registry.call("get_support_status", {}, ctx=_ctx())


@mcp.tool
def get_sla_risk_incidents(include_low: bool = False) -> dict[str, Any]:
    """Incidents at risk of SLA breach with time remaining, reason and recommendation."""
    return registry.call("get_sla_risk_incidents", {"include_low": include_low}, ctx=_ctx())


@mcp.tool
def get_aging_incidents(min_days: float = 3.0) -> dict[str, Any]:
    """Open incidents older than min_days."""
    return registry.call("get_aging_incidents", {"min_days": min_days}, ctx=_ctx())


@mcp.tool
def get_critical_incidents(severity: str = "Critical") -> dict[str, Any]:
    """Open incidents with the given severity (default Critical)."""
    return registry.call("get_critical_incidents", {"severity": severity}, ctx=_ctx())


@mcp.tool
def get_unassigned_incidents() -> dict[str, Any]:
    """Open incidents without an owner."""
    return registry.call("get_unassigned_incidents", {}, ctx=_ctx())


@mcp.tool
def search_incidents(query: str, open_only: bool = False, limit: int = 50) -> dict[str, Any]:
    """Free-text incident search (id, title, description, tags, customer, impact)."""
    return registry.call(
        "search_incidents", {"query": query, "open_only": open_only, "limit": limit}, ctx=_ctx()
    )


@mcp.tool
def get_incident(incident_id: str) -> dict[str, Any]:
    """Full incident detail including comments, relations and SLA risk."""
    return registry.call("get_incident", {"incident_id": incident_id}, ctx=_ctx())


@mcp.tool
def get_incident_history(incident_id: str) -> dict[str, Any]:
    """Comment/activity history for one incident."""
    return registry.call("get_incident_history", {"incident_id": incident_id}, ctx=_ctx())


@mcp.tool
def get_escalation_recommendations(min_band: str = "MEDIUM") -> dict[str, Any]:
    """Recommended escalations with reasons (recommendation only - never escalates)."""
    return registry.call("get_escalation_recommendations", {"min_band": min_band}, ctx=_ctx())


@mcp.tool
def get_support_standup() -> dict[str, Any]:
    """Concise support standup: critical incidents, new/resolved, blockers, escalations."""
    return registry.call("get_support_standup", {}, ctx=_ctx())


@mcp.tool
def get_recurring_issues(window_days: int = 30) -> dict[str, Any]:
    """AI-derived recurring incident clusters (tags/keywords) for the window."""
    return registry.call("get_recurring_issues", {"window_days": window_days}, ctx=_ctx())


@mcp.tool
def list_work_item_types() -> dict[str, Any]:
    """Inspect the connected project: work item types and field mapping."""
    return registry.call("list_work_item_types", {}, ctx=_ctx())


@mcp.tool
def get_daily_briefing() -> dict[str, Any]:
    """Daily client-discussion briefing: critical, SLA risk, escalations, follow-ups."""
    return registry.call("get_daily_briefing", {}, ctx=_ctx())


@mcp.tool
def get_weekly_report() -> dict[str, Any]:
    """Weekly support report for the last 7 days."""
    return registry.call("get_weekly_report", {}, ctx=_ctx())


@mcp.tool
def get_uat_incidents(state: str = "UAT") -> dict[str, Any]:
    """Open incidents in the given state (default UAT)."""
    return registry.call("get_uat_incidents", {"state": state}, ctx=_ctx())


@mcp.tool
def get_followup_incidents(stale_hours: float = 48.0) -> dict[str, Any]:
    """Open incidents with no update for the given hours (need follow-up)."""
    return registry.call("get_followup_incidents", {"stale_hours": stale_hours}, ctx=_ctx())


@mcp.tool
def get_hold_incidents() -> dict[str, Any]:
    """Open tickets on hold (tracked separately from SLA risk)."""
    return registry.call("get_hold_incidents", {}, ctx=_ctx())


@mcp.tool
def answer_support_question(question: str) -> dict[str, Any]:
    """Ask the SupportIncidentAgent a natural-language question about support incidents."""
    response = SupportIncidentAgent().ask(question)
    return response.model_dump(mode="json")


def main() -> None:
    parser = argparse.ArgumentParser(description="Support AI assistant MCP server")
    parser.add_argument("--http", action="store_true", help="serve over HTTP instead of stdio")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    if args.http:
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        mcp.run()  # stdio


if __name__ == "__main__":
    sys.exit(main())
