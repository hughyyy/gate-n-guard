"""MCPServer exposing the gate-n-guard tools: check, record, audit, advise."""

from __future__ import annotations

import dataclasses
import json
from typing import Annotated, Any, Literal, Optional, get_args

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from . import __version__
from . import advise as advise_mod
from . import config as config_mod
from . import journal as journal_mod
from . import rules as rules_mod
from .client import AdvisoryClient
from .loader import attach_ref, resolve_rulebook

_Verdict = Literal["allow", "confirm", "deny"]
_Question = Literal["interpret", "apply", "gap"]
_Mode = Literal["auto", "model"]
_Engine = Literal["auto", "hf", "openrouter"]

# Runtime mirror of the closed vocabularies above (one definition each).
_ENGINES = get_args(_Engine)

_INSTRUCTIONS = (
    "gate-n-guard gates proposed actions against a rulebook. Call check before "
    "acting: it computes locally, keeps no state, and returns allow, confirm, or "
    "deny with the rules that matched, each reason, a per-predicate trace, and "
    "rulebook_ref {source, sha256} naming the exact rulebook that decided. deny "
    "means do not act; confirm means a human decides; allow with "
    "default_applied true means nothing matched and the rulebook default "
    "applied. The verdict is advice to the caller - this server enforces "
    "nothing.\n\n"
    'Rulebooks are supplied per call (inline JSON object or "file:<path>") or '
    'default from GATE_N_GUARD_RULEBOOK: {"default": verdict, "precedence": '
    '"first-match" | "strictest", "rules": [{"id", "when", "verdict", '
    '"reason"}]}. A rule matches when any of its when entries holds; an entry '
    "is a conjunction of predicates over the action (tool, arguments.<key>) and "
    "situation facts (situation.<path>). strictest takes deny > confirm > "
    "allow. A missing fact path is an error, never a silent false.\n\n"
    "record appends one decision to the audit journal; audit reads it back "
    "newest-first with filters. advise answers interpret / apply / gap "
    "questions with a model and never returns a verdict - except the "
    "deterministic apply path, which is check run verbatim. Errors return "
    '{"error": ...} with isError set.'
)


class GateHandler:
    """Tool logic bound to one configuration.

    A plain object so tests can inject fake configs and client factories;
    the MCPServer layer stays a thin registration shell.
    """

    def __init__(self, cfg: config_mod.Config, client_factory=None):
        self.config = cfg
        self._client_factory = client_factory
        self._default: Optional[tuple] = None

    # -- rulebook resolution (default cached on first use) ----------------

    def _default_rulebook(self):
        if self._default is None:
            self._default = resolve_rulebook(None, default_path=self.config.rulebook_path)
        return self._default

    def _rulebook(self, rules):
        if rules is None:
            return self._default_rulebook()
        return resolve_rulebook(rules)

    # -- tools -------------------------------------------------------------

    def check(self, rules: Any, action: dict, situation: dict | None = None) -> dict:
        rulebook, ref = self._rulebook(rules)
        return attach_ref(rules_mod.check(rulebook, action, situation), ref)

    def record(
        self,
        action: dict,
        verdict: str,
        matched: Optional[list] = None,
        rulebook: Optional[str] = None,
        outcome: Optional[str] = None,
        actor: Optional[str] = None,
        note: Optional[str] = None,
    ) -> dict:
        return journal_mod.record_decision(
            self.config.journal_path,
            action=action,
            verdict=verdict,
            matched=matched,
            rulebook=rulebook,
            outcome=outcome,
            actor=actor,
            note=note,
        )

    def audit(
        self,
        limit: int = 50,
        since: Optional[str] = None,
        before: Optional[str] = None,
        verdict: Optional[str] = None,
        outcome: Optional[str] = None,
        tool: Optional[str] = None,
    ) -> dict:
        return journal_mod.read_journal(
            self.config.journal_path,
            limit=limit,
            since=since,
            before=before,
            verdict=verdict,
            outcome=outcome,
            tool=tool,
        )

    def advise(
        self,
        rules: Any,
        question: str = "apply",
        situation: dict | None = None,
        action: dict | None = None,
        rule_id: Optional[str] = None,
        mode: str = "auto",
        engine: Optional[str] = None,
        model: Optional[str] = None,
    ) -> dict:
        cfg = self.config
        if engine is not None:
            if engine not in _ENGINES:
                raise advise_mod.AdviseError(f"engine must be one of {_ENGINES!r}, got {engine!r}")
            if engine != "auto":
                cfg = dataclasses.replace(self.config, provider=engine)
        rulebook, ref = self._rulebook(rules)
        client = self._client_factory(cfg) if self._client_factory else AdvisoryClient(cfg)
        result = advise_mod.advise(
            rulebook,
            action,
            situation,
            question=question,
            mode=mode,
            rule_id=rule_id,
            client=client,
            model=model,
        )
        return attach_ref(result, ref)


def create_server(
    cfg: config_mod.Config | None = None,
    handler: GateHandler | None = None,
) -> MCPServer:
    cfg = cfg or config_mod.load_config()
    handler = handler or GateHandler(cfg)
    mcp = MCPServer(
        name="gate-n-guard",
        title="gate-n-guard",
        description="Gate agent actions against a rulebook: allow / confirm / deny "
                    "with a trace, an append-only audit journal, and model-backed "
                    "policy advice.",
        instructions=_INSTRUCTIONS,
        website_url="https://github.com/hughyyy/gate-n-guard",
        version=__version__,
    )

    @mcp.tool(
        title="Gate an action",
        annotations=ToolAnnotations(
            read_only_hint=True, idempotent_hint=True, open_world_hint=False
        ),
    )
    def check(
        action: dict,
        rules: str | dict | None = None,
        situation: dict | None = None,
    ) -> Any:
        """Gate one proposed action against a rulebook.

        Args:
            action: The proposed action: {"tool": str, "arguments": {...}}.
            rules: Rulebook as an inline JSON object, a `file:<path>` ref, or
                omitted to use the GATE_N_GUARD_RULEBOOK default.
            situation: Optional context facts the rulebook can match
                (agent identity, environment, ...).
        """
        return _wrap(handler.check, rules, action, situation)

    @mcp.tool(
        title="Record a decision",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=False,
        ),
    )
    def record(
        action: dict,
        verdict: _Verdict,
        matched: list[str] | None = None,
        rulebook: str | None = None,
        outcome: str | None = None,
        actor: str | None = None,
        note: str | None = None,
    ) -> Any:
        """Append one decision record to the audit journal.

        Args:
            action: The proposed action: {"tool": str, "arguments": {...}}.
            verdict: The gate verdict: allow, confirm, or deny.
            matched: Optional rule ids that matched.
            rulebook: Optional rulebook name/ref the decision used.
            outcome: How the decision resolved (e.g. approved, denied,
                executed, aborted, escalated).
            actor: Who resolved or recorded the decision.
            note: Free-form note.
        """
        return _wrap(handler.record, action, verdict, matched, rulebook, outcome, actor, note)

    @mcp.tool(
        title="Read the decision journal",
        annotations=ToolAnnotations(
            read_only_hint=True, idempotent_hint=True, open_world_hint=False
        ),
    )
    def audit(
        limit: Annotated[int, Field(ge=1, le=journal_mod.MAX_AUDIT_LIMIT)] = 50,
        since: str | None = None,
        before: str | None = None,
        verdict: _Verdict | None = None,
        outcome: str | None = None,
        tool: str | None = None,
    ) -> Any:
        """Read the decision journal back, newest-first.

        Args:
            limit: Max records to return (default 50, max 1000).
            since: ISO-8601 timestamp; records at or after this time.
            before: ISO-8601 timestamp; records strictly before this time.
            verdict: Filter to allow/confirm/deny records.
            outcome: Filter to records with this outcome.
            tool: Filter to records for this action tool.
        """
        return _wrap(handler.audit, limit, since, before, verdict, outcome, tool)

    @mcp.tool(
        title="Ask about policy",
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=False,
            open_world_hint=True,
        ),
    )
    def advise(
        rules: str | dict | None = None,
        question: _Question = "apply",
        situation: dict | None = None,
        action: dict | None = None,
        rule_id: str | None = None,
        mode: _Mode = "auto",
        engine: _Engine | None = None,
        model: str | None = None,
    ) -> Any:
        """Ask for an interpretation of a rule or policy, or which rules apply.

        Args:
            rules: Rulebook as an inline JSON object, a `file:<path>` ref, or
                omitted to use the GATE_N_GUARD_RULEBOOK default.
            question: interpret (explain a rule or the rulebook), apply
                ("does anything here apply to me?"), or gap (missing policy).
            situation: Context facts (agent identity, environment, ...).
            action: The proposed action; required for question=apply/gap.
            rule_id: For question=interpret, the rule to explain.
            mode: auto (matcher first; model only when nothing matched) or
                model (always use the model).
            engine: Provider for bare model ids: auto, hf, or openrouter.
            model: Model id override; `openrouter:<slug>` or `hf:<id>`.
        """
        return _wrap(handler.advise, rules, question, situation, action, rule_id, mode, engine, model)

    return mcp


def _wrap(fn, *args) -> Any:
    """Run one handler call, turning module errors into isError results.

    The payload stays a structured `{"error": ...}` JSON body so the calling
    model can act on it; `isError` is set so hosts and models that branch on the
    protocol flag see a failure rather than a result.
    """
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool result
        return _error_result(exc)


def _error_result(exc: Exception) -> CallToolResult:
    return CallToolResult(
        content=[
            TextContent(type="text", text=json.dumps({"error": str(exc)}, ensure_ascii=False))
        ],
        is_error=True,
    )


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
