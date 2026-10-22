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


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _parse_fallbacks(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return DEFAULT_FALLBACKS
    parts = [p.strip() for p in raw.replace(",", " ").split() if p.strip()]
    return tuple(parts)


def load_config() -> Config:
    engine = os.environ.get("GATE_N_GUARD_ENGINE", "openrouter").strip().lower()
    if engine not in {"auto", "hf", "openrouter"}:
        engine = "openrouter"
    return Config(
        model=os.environ.get("GATE_N_GUARD_MODEL", DEFAULT_MODEL),
        endpoint=os.environ.get("HF_ENDPOINT") or None,
        max_tokens=_env_int("GATE_N_GUARD_MAX_TOKENS", 2048),
        timeout=_env_float("GATE_N_GUARD_TIMEOUT", 60.0),
        retries=_env_int("GATE_N_GUARD_RETRIES", 3),
        retry_backoff=_env_float("GATE_N_GUARD_RETRY_BACKOFF", 2.0),
        provider=engine,
        fallbacks=_parse_fallbacks(os.environ.get("GATE_N_GUARD_FALLBACKS")),
        openrouter_api_key=os.environ.get("OPENROUTER_API_KEY") or None,
        openrouter_base_url=os.environ.get("OPENROUTER_BASE_URL", DEFAULT_OPENROUTER_URL),
        openrouter_site_url=os.environ.get("OPENROUTER_SITE_URL") or None,
        openrouter_app_name=os.environ.get("OPENROUTER_APP_NAME") or None,
        rulebook_path=os.environ.get("GATE_N_GUARD_RULEBOOK") or None,
        journal_path=os.environ.get("GATE_N_GUARD_JOURNAL") or None,
    )
