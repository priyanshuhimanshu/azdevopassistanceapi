from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.models.incidents import Comment, Incident


class WorkItemProvider(ABC):
    """READ interface for support incidents (read-only)."""

    name: str = "abstract"

    @abstractmethod
    def list_work_item_types(self) -> list[str]:
        """Work item types configured in the connected project."""

    @abstractmethod
    def list_incidents(self) -> list[Incident]:
        """All support incidents/issues (open + resolved)."""

    @abstractmethod
    def get_incident(self, incident_id: str) -> Incident | None:
        """Fetch a single incident including comments/history."""

    @abstractmethod
    def get_comments(self, incident_id: str) -> list[Comment]:
        ...

    def find_by_id(self, incident_id: str) -> Incident | None:
        incident_id = incident_id.strip().upper()
        direct = self.get_incident(incident_id)
        if direct is not None:
            return direct
        for inc in self.list_incidents():
            if inc.id.upper() == incident_id:
                return inc
        return None



