"""Model clients: HuggingFace Inference + OpenRouter, retries and fallbacks.

Ported from look-n-see's client.py — same retry/fallback policy, chat-only
(no image parts). The chain is `model or config.model` followed by the
configured fallbacks; every candidate gets the full retry policy; the final
error names every candidate tried.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

from huggingface_hub import InferenceClient

from .config import Config
from .openrouter import OpenRouterTransport, RawResult

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class InferenceError(RuntimeError):
    """Raised when the model call ultimately fails."""


@dataclass(frozen=True)
class Completion:
    """Result of one advise call: text plus the metadata worth showing."""

    text: str
    model: str
    provider: str
    transport: str
    retries: int
    latency_s: float
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None


def _is_retryable(exc: Exception) -> bool:
    status = getattr(exc, "response", None)
    if status is not None:
        code = getattr(status, "status_code", None)
        if code is not None:
            return code in RETRYABLE_STATUS
    # Connection/timeout errors from the underlying HTTP layer.
    return isinstance(exc, (ConnectionError, TimeoutError)) or exc.__class__.__name__ in {
        "ConnectionError",
        "Timeout",
        "ReadTimeout",
        "ConnectTimeout",
    }


def _describe_error(exc: Exception) -> str:
    """Human-readable error text; prefers the API's own error message."""
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            body = response.json()
        except Exception:
            body = {}
        message = body.get("error")
        if isinstance(message, dict):
            message = message.get("message")
        if message:
            return f"{response.status_code}: {message}"
        return str(exc)
    return str(exc)


class _HFTransport:
    """Adapter over huggingface_hub InferenceClient exposing RawResult."""

    def __init__(self, config: Config, client: InferenceClient | None = None):
        self.config = config
        self._client = client or InferenceClient(
            model=config.endpoint,
            token=os.environ.get("HF_TOKEN"),
            timeout=config.timeout,
        )

    def chat_completion(
        self, messages: list[dict[str, Any]], model: str, max_tokens: int
    ) -> RawResult:
        call_model = self.config.target if self.config.endpoint else model
        response = self._client.chat_completion(
            messages=messages,
            model=call_model,
            max_tokens=max_tokens,
        )
        return RawResult(
            text=response.choices[0].message.content or "",
            finish_reason=response.choices[0].finish_reason,
            usage=response.usage.__dict__ if response.usage else None,
        )


class AdvisoryClient:
    """Chat-completion client with the suite's retry and fallback policy."""

    def __init__(self, config: Config, client: InferenceClient | None = None):
        self.config = config
        self._hf = _HFTransport(config, client)
        self._openrouter: OpenRouterTransport | None = None

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        fallbacks: tuple[str, ...] | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Run chat completion and return the assistant text."""
        return self.complete_with_meta(
            messages, model=model, fallbacks=fallbacks, max_tokens=max_tokens
        ).text

    def complete_with_meta(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        fallbacks: tuple[str, ...] | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        """Like `complete`, but returns text plus model/retry/usage metadata.

        The effective model chain is `model or config.model` followed by the
        configured fallbacks; each entry is tried with the full retry policy.
        Pass `fallbacks=()` to disable the configured chain.
        """
        chain = [model or self.config.model]
        chain.extend(fallbacks if fallbacks is not None else self.config.fallbacks)
        seen: set[str] = set()
        errors: list[str] = []
        details: list[str] = []
        for candidate in chain:
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                return self._complete_one(candidate, messages, max_tokens)
            except Exception as exc:  # noqa: BLE001 - chain policy decides
                detail = _describe_error(exc)
                errors.append(f"{candidate}: {detail}")
                details.append(detail)
        if len(seen) == 1:
            raise InferenceError(f"advisory model call failed ({chain[0]}): {details[0]}")
        raise InferenceError(
            "advisory model call failed; tried "
            + ", ".join(f"[{m}]" for m in seen)
            + ": "
            + " | ".join(errors)
        )

    def _complete_one(
        self, model_id: str, messages: list[dict[str, Any]], max_tokens: int | None
    ) -> Completion:
        provider = self.config.provider_for(model_id)
        transport = self._transport_for(provider)
        bare_model = self.config.strip_prefix(model_id)
        last_error: Exception | None = None
        t0 = time.monotonic()
        for attempt in range(self.config.retries + 1):
            try:
                result = transport.chat_completion(
                    messages, bare_model, max_tokens or self.config.max_tokens
                )
                return Completion(
                    text=result.text,
                    model=model_id,
                    provider=provider,
                    transport=type(transport).__name__,
                    retries=attempt,
                    latency_s=round(time.monotonic() - t0, 3),
                    finish_reason=result.finish_reason,
                    usage=result.usage,
                )
            except Exception as exc:  # noqa: BLE001 - retry policy decides
                last_error = exc
                if not _is_retryable(exc) or attempt >= self.config.retries:
                    break
                time.sleep(self.config.retry_backoff * (2**attempt))
        # Bare detail: the chain wrapper builds the final message.
        raise InferenceError(_describe_error(last_error))

    def _transport_for(self, provider: str) -> _HFTransport | OpenRouterTransport:
        if provider == "openrouter":
            if self._openrouter is None:
                if not self.config.openrouter_api_key:
                    raise InferenceError(
                        "OpenRouter model requested but OPENROUTER_API_KEY is not set"
                    )
                self._openrouter = OpenRouterTransport(self.config)
            return self._openrouter
        return self._hf
