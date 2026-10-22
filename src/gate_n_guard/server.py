"""MCPServer exposing the gate-n-guard tools: check, record, audit, advise."""

from __future__ import annotations

import dataclasses
from typing import Any, Optional

from mcp.server.mcpserver import MCPServer

from . import advise as advise_mod
from . import config as config_mod
from . import journal as journal_mod
from . import rules as rules_mod
from .client import AdvisoryClient
from .loader import attach_ref, resolve_rulebook

_ENGINES = ("auto", "hf", "openrouter")


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
        if rules is None or rules == "":
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
    mcp = MCPServer(name="gate-n-guard")

    @mcp.tool()
    def check(rules: Any = None, action: dict = None, situation: dict | None = None) -> dict:
        """Gate one proposed action against a rulebook.

        Args:
            rules: Rulebook as an inline JSON object, a `file:<path>` ref, or
                omitted to use the GATE_N_GUARD_RULEBOOK default.
            action: The proposed action: {"tool": str, "arguments": {...}}.
            situation: Optional context facts the rulebook can match
                (agent identity, environment, ...).
        """
        return _wrap(handler.check, rules, action, situation)

    @mcp.tool()
    def record(
        action: dict,
        verdict: str,
        matched: list | None = None,
        rulebook: str | None = None,
        outcome: str | None = None,
        actor: str | None = None,
        note: str | None = None,
    ) -> dict:
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

    @mcp.tool()
    def audit(
        limit: int = 50,
        since: str | None = None,
        before: str | None = None,
        verdict: str | None = None,
        outcome: str | None = None,
        tool: str | None = None,
    ) -> dict:
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

    @mcp.tool()
    def advise(
        rules: Any = None,
        question: str = "apply",
        situation: dict | None = None,
        action: dict | None = None,
        rule_id: str | None = None,
        mode: str = "auto",
        engine: str | None = None,
        model: str | None = None,
    ) -> dict:
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


def _wrap(fn, *args) -> dict:
    """Surface module errors as structured tool results, never tracebacks."""
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - surfaced as a tool result
        return {"error": str(exc)}


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
