"""Environment-driven configuration with sane defaults.

Ported from look-n-see's config.py with GATE_N_GUARD_* variable names.
Secrets (OPENROUTER_API_KEY, HF_TOKEN) come from the environment only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_MODEL = "openrouter:google/gemma-4-26b-a4b-it"
DEFAULT_OPENROUTER_URL = "https://openrouter.ai/api/v1"
OPENROUTER_PREFIX = "openrouter:"
HF_PREFIX = "hf:"
# No default fallback chain: GATE_N_GUARD_FALLBACKS configures one. A chain is
# opt-in so a misconfigured fallback can never silently change the model used.
DEFAULT_FALLBACKS = ()


@dataclass(frozen=True)
class Config:
    model: str
    endpoint: str | None
    max_tokens: int
    timeout: float
    retries: int
    retry_backoff: float
    provider: str = "openrouter"
    fallbacks: tuple[str, ...] = ()
    openrouter_api_key: str | None = None
    openrouter_base_url: str = DEFAULT_OPENROUTER_URL
    openrouter_site_url: str | None = None
    openrouter_app_name: str | None = None
    rulebook_path: str | None = None
    journal_path: str | None = None

    @property
    def target(self) -> str:
        """The default model id or dedicated endpoint URL passed to the client."""
        return self.endpoint or self.model

    def provider_for(self, model_id: str) -> str:
        """Transport provider for a model id.

        An explicit `openrouter:`/`hf:` prefix always wins; otherwise the
        `provider` setting decides the interpretation of bare ids
        (GATE_N_GUARD_ENGINE).
        """
        if model_id.startswith(OPENROUTER_PREFIX):
            return "openrouter"
        if model_id.startswith(HF_PREFIX):
            return "hf"
        return self.provider

    @staticmethod
    def strip_prefix(model_id: str) -> str:
        if model_id.startswith(OPENROUTER_PREFIX):
            return model_id[len(OPENROUTER_PREFIX):]
        if model_id.startswith(HF_PREFIX):
            return model_id[len(HF_PREFIX):]
        return model_id


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(value, minimum)


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(value, minimum)


def _env_text(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_optional(name: str) -> str | None:
    return _env_text(name) or None


def _parse_fallbacks(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return DEFAULT_FALLBACKS
    return tuple(raw.replace(",", " ").split())


def load_config() -> Config:
    engine = os.environ.get("GATE_N_GUARD_ENGINE", "openrouter").strip().lower()
    if engine not in {"auto", "hf", "openrouter"}:
        engine = "openrouter"
    timeout = _env_float("GATE_N_GUARD_TIMEOUT", 60.0)
    if timeout <= 0:
        timeout = 60.0
    return Config(
        model=_env_text("GATE_N_GUARD_MODEL", DEFAULT_MODEL) or DEFAULT_MODEL,
        endpoint=_env_optional("HF_ENDPOINT"),
        max_tokens=_env_int("GATE_N_GUARD_MAX_TOKENS", 2048, minimum=1),
        timeout=timeout,
        retries=_env_int("GATE_N_GUARD_RETRIES", 3, minimum=0),
        retry_backoff=_env_float("GATE_N_GUARD_RETRY_BACKOFF", 2.0, minimum=0.0),
        provider=engine,
        fallbacks=_parse_fallbacks(os.environ.get("GATE_N_GUARD_FALLBACKS")),
        openrouter_api_key=_env_optional("OPENROUTER_API_KEY"),
        openrouter_base_url=(
            _env_text("OPENROUTER_BASE_URL", DEFAULT_OPENROUTER_URL)
            or DEFAULT_OPENROUTER_URL
        ),
        openrouter_site_url=_env_optional("OPENROUTER_SITE_URL"),
        openrouter_app_name=_env_optional("OPENROUTER_APP_NAME"),
        rulebook_path=_env_optional("GATE_N_GUARD_RULEBOOK"),
        journal_path=_env_optional("GATE_N_GUARD_JOURNAL"),
    )
