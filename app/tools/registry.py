from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from app.services.azure_devops.base import WorkItemProvider
from app.services.azure_devops.demo import DemoProvider


@dataclass
class ToolContext:
    provider: WorkItemProvider
    now: datetime = field(default_factory=datetime.now)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]
    mutates: bool = False
    returns: str = "application/json"

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        self._tools[tool.name] = tool
        return tool

    def tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any] | None = None,
        *,
        mutates: bool = False,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.register(
                Tool(
                    name=name,
                    description=description,
                    parameters=parameters or {"type": "object", "properties": {}},
                    handler=fn,
                    mutates=mutates,
                )
            )
            return fn

        return decorator

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def read_tools(self) -> list[Tool]:
        return [t for t in self._tools.values() if not t.mutates]

    def write_tools(self) -> list[Tool]:
        return [t for t in self._tools.values() if t.mutates]

    def openai_schemas(self, *, include_writes: bool = False) -> list[dict[str, Any]]:
        tools = self.read_tools() + (self.write_tools() if include_writes else [])
        return [t.to_openai_schema() for t in tools]

    def call(self, name: str, arguments: dict[str, Any], *, ctx: ToolContext) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"Unknown tool: {name}")
        return tool.handler(ctx, **arguments)


registry = ToolRegistry()
