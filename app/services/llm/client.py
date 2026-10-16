from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings, get_settings


class LLMNotConfigured(RuntimeError):
    pass


class LLMClient:
    """Minimal OpenAI-compatible chat completions client (tool calling supported).

    The endpoint/model are configured purely through environment variables:
    LLM_API_KEY, LLM_BASE_URL, LLM_MODEL. No credentials are ever embedded in
    prompts or sent anywhere else.
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    @property
    def configured(self) -> bool:
        return bool(self.settings.llm_api_key)

    @property
    def model(self) -> str:
        return self.settings.llm_model

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not self.configured:
            raise LLMNotConfigured(
                "No LLM configured. Set LLM_API_KEY (and optionally LLM_BASE_URL / LLM_MODEL)."
            )
        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": self.settings.llm_temperature,
            "max_tokens": self.settings.llm_max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {
            "Authorization": f"Bearer {self.settings.llm_api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                f"{self.settings.llm_base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            return resp.json()
