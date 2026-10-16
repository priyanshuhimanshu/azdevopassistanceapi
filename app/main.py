from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.config import get_settings
from app.services.azure_devops.real import AzureDevOpsError
from app.tools import incident_tools as _incident_tools  # noqa: F401

settings = get_settings()

app = FastAPI(
    title="Azure DevOps Support & Incident AI Assistant",
    description=(
        "AI assistant that answers natural-language questions about Azure DevOps "
        "support incidents/issues (status, SLA risk, aging, escalation, standup)."
    ),
    version="0.1.0",
)

# The deployed frontend and local dev server are always allowed, in addition
# to whatever CORS_ORIGINS is configured in the environment.
ALWAYS_ALLOWED_ORIGINS = (
    "https://priyanshuhimanshu.github.io",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted({*settings.cors_origins, *ALWAYS_ALLOWED_ORIGINS}),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)


@app.exception_handler(AzureDevOpsError)
async def ado_error_handler(request: Request, exc: AzureDevOpsError) -> JSONResponse:
    """Surface Azure DevOps connectivity/auth problems as a readable 502."""
    return JSONResponse(
        status_code=502,
        content={
            "error": "azure_devops_unavailable",
            "detail": str(exc),
            "hint": "Check ADO_ORG_URL, ADO_PROJECT and ADO_PAT in .env (the PAT may be expired or missing the Work Items scope).",
        },
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def root() -> dict[str, str]:
    return {
        "name": "Azure DevOps Support & Incident AI Assistant",
        "docs": "/docs",
        "mode": "demo" if settings.demo_mode else "azure_devops",
    }
