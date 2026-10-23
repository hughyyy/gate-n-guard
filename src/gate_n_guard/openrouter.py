"""OpenRouter transport: OpenAI-compatible chat completions over httpx.

Ported from look-n-see's openrouter.py (identical transport contract).
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import Config


class RawResult:
    """Normalized chat-completion result shared by all transports."""

    __slots__ = ("text", "finish_reason", "usage")

    def __init__(
        self,
        text: str,
        finish_reason: str | None = None,
        usage: dict[str, Any] | None = None,
    ):
        self.text = text
        self.finish_reason = finish_reason
        self.usage = usage


class OpenRouterError(RuntimeError):
    """Raised for malformed OpenRouter responses (HTTP-level errors raise
    `httpx.HTTPStatusError` so the shared retry policy can evaluate them)."""


def _content_to_text(content: Any) -> str:
    """OpenRouter may return content as str or as a list of content parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    return str(content or "")


class OpenRouterTransport:
    """Single-shot chat completion against OpenRouter's OpenAI-compatible API.

    Retries are the caller's policy; this raises `httpx.HTTPStatusError` for
    non-2xx responses so the shared retry classifier can evaluate the status.
    """

    def __init__(self, config: Config):
        self.config = config
        if not config.openrouter_api_key:
            raise ValueError("OPENROUTER_API_KEY is required for the OpenRouter transport")
        headers = {
            "Authorization": f"Bearer {config.openrouter_api_key}",
            "Content-Type": "application/json",
        }
        if config.openrouter_site_url:
            headers["HTTP-Referer"] = config.openrouter_site_url
        if config.openrouter_app_name:
            headers["X-Title"] = config.openrouter_app_name
        self._http = httpx.Client(
            base_url=config.openrouter_base_url,
            headers=headers,
            timeout=config.timeout,
        )

    def chat_completion(
        self, messages: list[dict[str, Any]], model: str, max_tokens: int
    ) -> RawResult:
        response = self._http.post(
            "/chat/completions",
            json={"model": model, "messages": messages, "max_tokens": max_tokens},
        )
        if response.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"OpenRouter returned {response.status_code}",
                request=response.request,
                response=response,
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise OpenRouterError(
                f"malformed OpenRouter response: body is not JSON ({exc})"
            ) from exc
        if not isinstance(data, dict):
            raise OpenRouterError(
                f"malformed OpenRouter response: body is not an object "
                f"(body: {str(data)[:200]})"
            )
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise OpenRouterError(
                f"malformed OpenRouter response: no 'choices' (body: {str(data)[:200]})"
            )
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            message = {}
        return RawResult(
            text=_content_to_text(message.get("content")),
            finish_reason=choice.get("finish_reason"),
            usage=data.get("usage"),
        )
