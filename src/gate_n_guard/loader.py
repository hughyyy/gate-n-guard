"""Rulebook sources: inline JSON, file refs, and the env default.

Resolution order (resolved 2026-10-22): the `rules` param wins — inline JSON
object or a `file:<path>` string ref; when it is omitted, `default_path`
(GATE_N_GUARD_RULEBOOK) is used. A failed param errors loudly and never
silently falls back.

Every resolution returns a `rulebook_ref` ({source, sha256}) naming the exact
version that decided. The server attaches the ref to check/advise responses
with `attach_ref`; the ref is content-addressed, so inline rulebooks hash
canonically and file rulebooks hash the bytes on disk.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional, Tuple

from .rules import Rulebook, load_rulebook

FILE_PREFIX = "file:"


class RulebookSourceError(ValueError):
    """A rulebook source is invalid, missing, or unreadable."""


def resolve_rulebook(
    rules: Optional[Any],
    default_path: Optional[str] = None,
) -> Tuple[Rulebook, dict]:
    """Load and validate a rulebook from any accepted source.

    Returns (rulebook, ref) where ref = {"source": str, "sha256": str}.
    Raises RulebookSourceError for missing files, bad JSON, unknown refs, or
    no source at all; RulebookError passes through for malformed rulebooks.
    """
    if isinstance(rules, dict):
        return load_rulebook(rules), _inline_ref(rules)
    if isinstance(rules, str):
        if not rules.startswith(FILE_PREFIX):
            raise RulebookSourceError(
                f"rules string must be a '{FILE_PREFIX}<path>' ref or inline JSON, "
                f"got {rules[:60]!r}"
            )
        if len(rules) == len(FILE_PREFIX):
            raise RulebookSourceError(
                f"'{FILE_PREFIX}' ref must include a non-empty path"
            )
        return _load_file(rules[len(FILE_PREFIX):], source=rules)
    if rules is not None:
        raise RulebookSourceError(
            f"rules must be a rulebook object or '{FILE_PREFIX}<path>' ref, "
            f"got {type(rules).__name__}"
        )
    if not default_path:
        raise RulebookSourceError(
            "no rulebook provided: pass rules (inline or file:<path>) or set "
            "GATE_N_GUARD_RULEBOOK"
        )
    return _load_file(default_path, source=f"file:{default_path}")


def attach_ref(response: dict, ref: dict) -> dict:
    """Return `response` with the rulebook_ref attached (does not mutate)."""
    result = dict(response)
    result["rulebook_ref"] = dict(ref)
    return result


def _load_file(path: str, source: str) -> Tuple[Rulebook, dict]:
    file = Path(path).expanduser()
    if not file.is_file():
        raise RulebookSourceError(f"rulebook file not found: {path}")
    try:
        raw_bytes = file.read_bytes()
    except OSError as exc:
        raise RulebookSourceError(f"rulebook file unreadable: {path}: {exc}") from exc
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RulebookSourceError(
            f"rulebook file is not valid UTF-8: {path}: {exc}"
        ) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RulebookSourceError(
            f"rulebook file is not valid JSON: {path}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise RulebookSourceError(f"rulebook file must contain a JSON object: {path}")
    return load_rulebook(data), {
        "source": source,
        "sha256": hashlib.sha256(raw_bytes).hexdigest(),
    }


def _inline_ref(rules: dict) -> dict:
    try:
        canonical = json.dumps(rules, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise RulebookSourceError(
            f"inline rulebook is not JSON-serializable: {exc}"
        ) from exc
    return {
        "source": "inline",
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }
