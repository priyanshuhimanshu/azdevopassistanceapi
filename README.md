# Azure DevOps Support AI — Backend

FastAPI service for the **Azure DevOps Support & Incident AI Assistant**. Answers natural-language questions about support incidents/issues: status, SLA risk, aging, escalation, standup, daily/weekly reports.

## Tech Stack

- Python 3.13 + FastAPI
- Uvicorn (ASGI server)
- Pydantic / pydantic-settings
- httpx, PyYAML, python-dotenv
- fastmcp (MCP tool exposure)

## Requirements

- Python 3.13

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

- API root: http://127.0.0.1:8000/
- Interactive docs: http://127.0.0.1:8000/docs
- Health: http://127.0.0.1:8000/health

## Configuration

Read from environment variables (or a `.env` file in this folder — copy `.env.example` to
`.env` and fill it in; it is git-ignored).

| Variable | Default | Description |
| --- | --- | --- |
| `DEMO_MODE` | `true` when ADO unset | Serve sample in-memory incidents instead of Azure DevOps |
| `ADO_ORG_URL` | — | Azure DevOps organization URL |
| `ADO_PROJECT` | — | Azure DevOps project name |
| `ADO_PAT` | — | Azure DevOps personal access token |
| `ADO_API_VERSION` | `7.1-preview` | ADO REST API version |
| `LLM_API_KEY` / `OPENAI_API_KEY` | — | Enables AI chat (`/api/chat`) |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint |
| `LLM_MODEL` | `gpt-4o-mini` | Chat model |
| `LLM_TEMPERATURE` | `0.1` | Sampling temperature |
| `LLM_MAX_TOKENS` | `2048` | Max response tokens |
| `CORS_ORIGINS` | `http://localhost:5173,...` | Comma-separated allowed origins |
| `PORT` | `8000` | Listen port (set by Render) |
| `HOST` | `127.0.0.1` | Bind host |
| `REPORT_PROJECT_NAME` | — | Display name used in reports |
| `WORK_ITEM_MAPPING` | `config/work_item_mapping.yaml` | Work item type/field mapping |
| `AUDIT_LOG` | `backend/logs/audit.jsonl` | Audit log path |
| `AUDIT_LOG_ENABLED` | `false` | Enable audit logging |

### Modes

- **Azure DevOps mode**: set `ADO_ORG_URL`, `ADO_PROJECT`, `ADO_PAT` → `DEMO_MODE=false` automatically.
- **Demo mode**: ADO unset (or `DEMO_MODE=true`) → reads `demo_data/incidents/incidents.json`.
  No demo data ships with this repo, so configure `ADO_*` (or set `DEMO_MODE=false`) for a working deploy.

> `config/work_item_mapping.yaml` ships in this repo. The work-item mapping must exist for
> `/api/*` to load — it maps incident types and field reference names for your project.

## API Endpoints

Prefix `/api` unless noted.

| Method | Path | Description |
| --- | --- | --- |
| GET | `/health` | Liveness probe |
| GET | `/` | Service info + mode |
| GET | `/docs` | OpenAPI (Swagger) UI |
| GET | `/api/meta` | Modes, mapping, feature flags |
| GET | `/api/connection-test` | ADO connectivity check |
| GET | `/api/dashboard` | Overview counters |
| GET | `/api/incidents` | Incident list (filterable) |
| GET | `/api/incidents/{id}` | Incident detail |
| GET | `/api/incidents/{id}/comments` | Incident comments |
| GET | `/api/status` | Support status summary |
| GET | `/api/sla-risk` | SLA-at-risk incidents |
| GET | `/api/aging` | Aging incidents |
| GET | `/api/escalations` | Escalation candidates |
| GET | `/api/standup` | Standup report |
| GET | `/api/recurring` | Recurring issues |
| GET | `/api/hold` | On-hold items |
| GET | `/api/biweekly` | Biweekly report |
| GET | `/api/briefing` | Daily briefing |
| GET | `/api/weekly` | Weekly report |
| POST | `/api/chat` | AI chat (`{"message": "..."}`) |
| GET | `/api/audit` | Audit log entries |

## Run with Docker

The `Dockerfile` expects the **monorepo root** as build context (it copies `backend/`,
`backend/requirements.txt` and `backend/config/`):

```bash
# from the monorepo root
docker build -f backend/Dockerfile -t azdevops-support-ai-backend .
docker run -p 8000:8000 -e DEMO_MODE=true azdevops-support-ai-backend
```

Or with Compose from the monorepo root: `docker compose up backend`.

## Deploy on Render

Use a **Web Service** with Docker:

- Health check path: `/health`
- Port: Render injects `$PORT` — ensure the start command uses it
- Set `CORS_ORIGINS` to your frontend origin (e.g. `https://priyanshuhimanshu.github.io`)

## Project Structure

```
backend/
  app/
    main.py            # FastAPI app, CORS, /health
    config.py          # Settings from env / .env
    api/routes.py      # All /api/* endpoints
    agents/            # SupportIncidentAgent (NLP + analytics orchestration)
    core/analytics.py  # Reports, SLA, aging, standup computations
    models/            # Incident, ChatResponse, DashboardData
    services/
      azure_devops/    # Real ADO provider + demo provider + mapping
      llm/             # OpenAI-compatible chat client
      audit.py         # Optional JSONL audit logging
    tools/             # MCP tool registry
  config/
    work_item_mapping.yaml   # Work item type / field mapping
  .env.example         # Environment template (copy to .env)
  .env                 # Your credentials (git-ignored)
  Dockerfile
  requirements.txt
  README.md
  simple_example.py    # Minimal API usage example
```
