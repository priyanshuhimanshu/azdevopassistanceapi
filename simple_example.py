"""ONE-FILE mini version of the backend: dashboard + incident + agent + llm + mcp + api.

Self-contained: only Python standard library, no installs, no server to set up,
no Azure DevOps, no LLM key. Run it with any Python 3:

    python simple_example.py
"""

from datetime import datetime, timedelta

NOW = datetime(2026, 10, 9, 12, 0, 0)

# --- hardcoded tickets (plain dicts, like simplified work items) -------------
TICKETS = [
    {"id": "#101", "title": "Payment API failure", "state": "Investigating",
     "owner": "John", "severity": "High", "created": NOW - timedelta(days=2),
     "sla_due": NOW + timedelta(days=1)},
    {"id": "#102", "title": "Login page slow", "state": "New",
     "owner": None, "severity": "Medium", "created": NOW - timedelta(days=5),
     "sla_due": None},
    {"id": "#103", "title": "Old printer bug", "state": "Closed",
     "owner": "John", "severity": "Low", "created": NOW - timedelta(days=20),
     "sla_due": NOW - timedelta(days=10)},
]

CLOSED = {"resolved", "closed", "done"}


def is_open(t):
    return t["state"].strip().lower() not in CLOSED


def age_days(t):
    return round((NOW - t["created"]).total_seconds() / 86400, 1)


# --- 1. DASHBOARD: counts + health from the ticket list -----------------------
def dashboard(tickets):
    open_t = [t for t in tickets if is_open(t)]
    critical = [t for t in open_t if t["severity"] == "Critical"]
    unassigned = [t for t in open_t if not t["owner"]]
    health = "GREEN"
    if critical or unassigned:
        health = "AMBER"
    if len(critical) >= 3:
        health = "RED"
    return {
        "health": health,
        "open": len(open_t),
        "critical": len(critical),
        "unassigned": len(unassigned),
        "top_risk": max(open_t, key=age_days)["id"] if open_t else None,
    }


# --- 2. INCIDENT: summarize a single ticket -----------------------------------
def summarize(ticket):
    if ticket["sla_due"] is None:
        sla = "UNKNOWN (no due date)"
    elif ticket["sla_due"] <= NOW:
        sla = "HIGH RISK (past due)"
    else:
        sla = "ok (%s left)" % (ticket["sla_due"] - NOW)
    return {
        "id": ticket["id"], "title": ticket["title"],
        "owner": ticket["owner"] or "Unassigned",
        "age_days": age_days(ticket), "sla": sla,
    }


# --- 3. AGENT: tiny router — English in, answer out ---------------------------
def ask(question):
    q = question.lower()
    if "sla" in q or "risk" in q:                      # intent: sla_risk
        rows = ["%s %s (due %s)" % (t["id"], t["title"], t["sla_due"])
                for t in TICKETS if is_open(t) and t["sla_due"] and t["sla_due"] <= NOW]
        return "SLA RISK\n" + ("\n".join(rows) if rows else "None.")
    if "open" in q:                                     # intent: search
        rows = ["%s %s [%s, %s]" % (t["id"], t["title"], t["state"], t["owner"] or "Unassigned")
                for t in TICKETS if is_open(t)]
        return "OPEN TICKETS (%d)\n%s" % (len(rows), "\n".join(rows))
    if "status" in q:                                   # intent: support_status
        d = dashboard(TICKETS)
        return "STATUS: health=%s open=%d critical=%d unassigned=%d top=%s" % (
            d["health"], d["open"], d["critical"], d["unassigned"], d["top_risk"])
    return "Sorry, try: 'status', 'open tickets', or 'sla risk'."


# --- 4. LLM: the tool-calling loop (simulated, no API key) --------------------
# Real flow: app sends messages + tool schemas to the model -> model asks for a
# tool by name -> OUR code runs it -> result goes back -> model writes the answer.
# FakeLLM below plays the model's role with one hardcoded rule so you can see
# the loop shape (the real loop allows up to 6 rounds).
LLM_TOOLS = {
    "get_open_count": {
        "description": "Count currently open tickets",
        "run": lambda: sum(1 for t in TICKETS if is_open(t)),
    },
}


class FakeLLM:
    def chat(self, messages):
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        if tool_msgs:  # we already ran a tool: compose the final answer
            return {"text": "There are %s open tickets." % tool_msgs[-1]["content"]}
        question = messages[-1]["content"].lower()
        if "how many" in question and "open" in question:
            return {"tool_calls": [{"name": "get_open_count", "args": {}}]}
        return {"text": "I only know how to count open tickets."}


def ask_with_llm(question):
    messages = [
        {"role": "system", "content": "You answer using tools only. Never invent numbers."},
        {"role": "user", "content": question},
    ]
    for _ in range(3):  # max rounds
        reply = FakeLLM().chat(messages)
        if "tool_calls" not in reply:
            return reply["text"]
        for call in reply["tool_calls"]:
            result = LLM_TOOLS[call["name"]]["run"]()  # OUR code runs the tool
            messages.append({"role": "tool", "content": result})
    return "Gave up after 3 rounds."


# --- 5. MCP SERVER: the FastMCP pattern (no package needed) -------------------
# Real FastMCP: @mcp.tool registers a function; clients call it by name with JSON
# args over stdio/HTTP. Below is the same idea in 10 lines: a decorator that
# files functions into a dict, plus a dispatch function (like JSON-RPC).
MCP_TOOLS = {}


def mcp_tool(fn):
    MCP_TOOLS[fn.__name__] = {"run": fn, "help": fn.__doc__}
    return fn


@mcp_tool
def get_open_tickets():
    """List open support tickets."""
    return [t["id"] for t in TICKETS if is_open(t)]


@mcp_tool
def get_ticket(ticket_id):
    """Full detail of one ticket."""
    return next((t for t in TICKETS if t["id"] == ticket_id), None)


def mcp_list_tools():
    return sorted(MCP_TOOLS)


def mcp_call(name, args):
    return {"ok": True, "result": MCP_TOOLS[name]["run"](**args)}


# --- 6. API: UI talks to backend over HTTP (real round-trip, stdlib only) -----
# Real app: browser fetch("/api/status") -> FastAPI -> JSON -> React renders.
# Here: a tiny HTTP server serves dashboard() as JSON; urllib (the "browser")
# fetches it; then we pick KPIs out of the JSON exactly like Dashboard.tsx does.
import json as _json
import threading as _threading
import urllib.request as _url
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/status":
            body = _json.dumps(dashboard(TICKETS)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass  # keep the demo output clean


def api_demo():
    server = HTTPServer(("127.0.0.1", 0), Handler)  # port 0 = any free port
    port = server.server_address[1]
    _threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with _url.urlopen("http://127.0.0.1:%d/api/status" % port, timeout=10) as r:
            data = _json.load(r)  # UI side: JSON -> KPI numbers
    finally:
        server.shutdown()
    return ("UI shows: Open=%(open)s Critical=%(critical)s "
            "Unassigned=%(unassigned)s Health=%(health)s" % data)


# --- run it ------------------------------------------------------------------
if __name__ == "__main__":
    print("1. DASHBOARD:", dashboard(TICKETS))
    print("2. INCIDENT :", summarize(TICKETS[0]))
    print("3. AGENT    :\n" + ask("show open tickets"))
    print("4. LLM LOOP :\n" + ask_with_llm("how many tickets are open?"))
    print("5. MCP      : tools =", mcp_list_tools())
    print("   call get_open_tickets ->", mcp_call("get_open_tickets", {}))
    print("6. API      :", api_demo())
