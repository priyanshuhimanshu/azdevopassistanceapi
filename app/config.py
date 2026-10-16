from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

def _find_root() -> Path:
    """Project root whether running from the repo checkout or inside a container."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config" / "work_item_mapping.yaml").exists() or (
            parent / "demo_data"
        ).exists():
            return parent
    return here.parents[2]


ROOT = _find_root()
BACKEND_DIR = Path(__file__).resolve().parents[1]  # folder that contains app/
BACKEND_ROOT = BACKEND_DIR
DEMO_DATA_DIR = ROOT / "demo_data"
CONFIG_DIR = BACKEND_DIR / "config"
DOCS_DIR = ROOT / "docs"


def _load_dotenv() -> None:
    """Load .env from the backend folder first, then the project root/cwd."""
    for candidate in (BACKEND_DIR / ".env", Path.cwd() / ".env", ROOT / ".env"):
        if candidate.is_file():
            load_dotenv(candidate)
            return
    load_dotenv()


_load_dotenv()


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    value = _env(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    demo_mode: bool
    ado_org_url: str | None = None
    ado_project: str | None = None
    ado_pat: str | None = None
    ado_api_version: str = "7.1-preview"
    llm_api_key: str | None = None
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.1
    llm_max_tokens: int = 2048
    report_project_name: str | None = None
    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: tuple[str, ...] = ("http://localhost:5173",)
    work_item_mapping_path: Path = CONFIG_DIR / "work_item_mapping.yaml"
    audit_log_path: Path = BACKEND_ROOT / "logs" / "audit.jsonl"
    audit_log_enabled: bool = False

    @property
    def ado_configured(self) -> bool:
        return bool(self.ado_org_url and self.ado_project and self.ado_pat)

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def using_demo_data(self) -> bool:
        return self.demo_mode

    @property
    def project_label(self) -> str:
        """Client-facing project name for reports."""
        if self.report_project_name:
            return self.report_project_name
        if self.ado_project:
            return self.ado_project.replace("_", " ")
        return "Support"


def build_settings() -> Settings:
    ado_configured = all(
        _env(name)
        for name in ("ADO_ORG_URL", "ADO_PROJECT", "ADO_PAT")
    )
    demo_mode = _env_bool("DEMO_MODE", not ado_configured)

    origins = _env(
        "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ) or ""
    cors_origins = tuple(o.strip() for o in origins.split(",") if o.strip())

    return Settings(
        demo_mode=demo_mode,
        ado_org_url=_env("ADO_ORG_URL"),
        ado_project=_env("ADO_PROJECT"),
        ado_pat=_env("ADO_PAT"),
        ado_api_version=_env("ADO_API_VERSION", "7.1-preview") or "7.1-preview",
        llm_api_key=_env("LLM_API_KEY") or _env("OPENAI_API_KEY"),
        llm_base_url=_env("LLM_BASE_URL", "https://api.openai.com/v1")
        or "https://api.openai.com/v1",
        llm_model=_env("LLM_MODEL", "gpt-4o-mini") or "gpt-4o-mini",
        llm_temperature=_env_float("LLM_TEMPERATURE", 0.1),
        llm_max_tokens=_env_int("LLM_MAX_TOKENS", 2048),
        report_project_name=_env("REPORT_PROJECT_NAME"),
        host=_env("HOST", "127.0.0.1") or "127.0.0.1",
        port=_env_int("PORT", 8000),
        cors_origins=cors_origins,
        work_item_mapping_path=Path(
            _env("WORK_ITEM_MAPPING", str(CONFIG_DIR / "work_item_mapping.yaml"))
            or str(CONFIG_DIR / "work_item_mapping.yaml")
        ),
        audit_log_path=Path(
            _env("AUDIT_LOG", str(BACKEND_ROOT / "logs" / "audit.jsonl"))
            or str(BACKEND_ROOT / "logs" / "audit.jsonl")
        ),
        audit_log_enabled=_env_bool("AUDIT_LOG_ENABLED", False),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:  # type: ignore[name-defined]
    return build_settings()
