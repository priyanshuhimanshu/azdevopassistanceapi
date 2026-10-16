from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_settings


class AuditLogger:
    """Append-only audit log for every read/write operation performed by the assistant."""

    def __init__(self, path: Path | None = None):
        settings = get_settings()
        self.path = path or settings.audit_log_path
        self.enabled = settings.audit_log_enabled
        self._lock = threading.Lock()
        self._entries: list[dict[str, Any]] = []

    def record(
        self,
        action: str,
        *,
        target: str | None = None,
        actor: str = "support-ai-assistant",
        decision: str | None = None,
        details: dict[str, Any] | None = None,
        approved_by: str | None = None,
    ) -> dict[str, Any]:
        entry = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "action": action,
            "target": target,
            "actor": actor,
            "decision": decision,
            "approved_by": approved_by,
            "details": details or {},
        }
        if not self.enabled:
            return entry
        with self._lock:
            self._entries.append(entry)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError:
                pass  # audit file must never break the request path
        return entry

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._entries[-limit:])


audit_logger = AuditLogger()
