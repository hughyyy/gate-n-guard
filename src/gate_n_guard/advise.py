"""The advise tool: interpret / apply / gap over a rulebook.

One surface, two producers:

- `mode: auto` (default) answers an `apply` question deterministically when
  the matcher finds matched rules — zero tokens, zero network, identical to a
  `check` run (single source of truth). The model is consulted only when
  nothing matched and judgment is genuinely needed.
- `mode: model` (and always for `interpret` / `gap`) runs the advisory model
  with a question-specific prompt and returns its strict-JSON answer,
  shape-checked server-side.

The model's word is never a verdict; every response names the mode that
produced it.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from .client import AdvisoryClient
from .rules import Rulebook, check

QUESTION_KINDS = ("interpret", "apply", "gap")
MODES = ("auto", "model")

_SYSTEM_PROMPT = (
    "You are a policy adviser for an AI agent. You read a rulebook (JSON), "
    "an action, and a situation, and answer with exactly one JSON object in "
    "the shape requested. You quote the semantics of rules as written; you "
    "never invent rules and never issue verdicts (allow/confirm/deny is the "
    "matcher's job). If the rulebook or question is ambiguous, say so in the "
    "caveat fields with a confidence score."
)


class AdviseError(ValueError):
    """Invalid advise parameters or unparseable/misshapen model output."""


def advise(
    rulebook: Rulebook,
    action: dict,
    situation: dict | None = None,
    *,
    question: str = "apply",
    mode: str = "auto",
    rule_id: Optional[str] = None,
    client: Optional[AdvisoryClient] = None,
    model: Optional[str] = None,
    fallbacks: Optional[tuple[str, ...]] = None,
) -> dict:
    """Answer one advise question.

    The deterministic path needs only the rulebook; the model path needs a
    client (an AdvisoryClient or anything with `complete(messages, model=,
    fallbacks=)` returning text). Errors are AdviseError with a message the
    calling model can act on.
    """
    if question not in QUESTION_KINDS:
        raise AdviseError(f"question must be one of {QUESTION_KINDS!r}, got {question!r}")
    if mode not in MODES:
        raise AdviseError(f"mode must be one of {MODES!r}, got {mode!r}")
    if rule_id is not None:
        if question != "interpret":
            raise AdviseError("rule_id is only valid for question=interpret")
        if rule_id not in {rule.id for rule in rulebook.rules}:
            raise AdviseError(f"rule_id {rule_id!r} is not in this rulebook")

    if question == "apply" and mode == "auto":
        deterministic = _apply_deterministic(rulebook, action, situation)
        if deterministic["applicable"]:
            return deterministic

    if client is None:
        raise AdviseError(
            "advise: a model call is required for this question/mode but no "
            "advisory client is configured (OPENROUTER_API_KEY or HF_TOKEN)"
        )
    messages = _build_messages(question, rulebook, action, situation, rule_id)
    text = client.complete(messages, model=model, fallbacks=fallbacks)
    payload = _extract_json(text)
    return _shape(question, payload, rule_id)


def _apply_deterministic(rulebook: Rulebook, action: dict, situation: dict) -> dict:
    result = check(rulebook, action, situation)
    return {
        "mode": "deterministic",
        "verdict": result["verdict"],
        "applicable": [
            {
                "id": match["id"],
                "verdict": match["verdict"],
                "reason": match["reason"],
            }
            for match in result["matched"]
        ],
        "default_applied": result["default_applied"],
    }


def _build_messages(
    question: str,
    rulebook: Rulebook,
    action: dict,
    situation: dict,
    rule_id: Optional[str],
) -> list[dict[str, Any]]:
    rulebook_json = json.dumps({
        "default": rulebook.default,
        "precedence": rulebook.precedence,
        "rules": [
            {"id": rule.id,
             "when": [[{"path": p.path, "operator": p.operator, "value": p.value}
                       for p in entry] for entry in rule.entries],
             "verdict": rule.verdict,
             "reason": rule.reason}
            for rule in rulebook.rules
        ],
    }, ensure_ascii=False)
    context = {
        "action": action,
        "situation": situation or {},
    }
    instructions = {
        "interpret": (
            "Question: interpret "
            + (f"rule {rule_id!r}" if rule_id else "the whole rulebook")
            + " in plain language.\n"
            "Return exactly:\n"
            + (
                '{"meaning": str, "permits": [str], "requires": [str], '
                '"forbids": [str], "edge_cases": [str], "confidence": 0..1}'
                if rule_id
                else '{"interpretations": [{"rule_id": str, "meaning": str, '
                     '"permits": [str], "requires": [str], "forbids": [str], '
                     '"edge_cases": [str], "confidence": 0..1}]}'
            )
        ),
        "apply": (
            "Question: which rules in this rulebook apply to this agent and "
            "this situation/action, and how do they read?\n"
            "Return exactly:\n"
            '{"summary": str, "applicable": [{"rule_id": str, "relevance": str, '
            '"reading": str, "caveats": [str]}], "notable_missing": [str]}'
        ),
        "gap": (
            "Question: does this rulebook have a policy gap for this "
            "situation/action — should it say something it does not?\n"
            "Return exactly:\n"
            '{"coverage": "adequate" | "partial" | "none", '
            '"gaps": [{"concern": str, "suggested_rule": str, "rationale": str}]}'
        ),
    }[question]
    user_prompt = (
        f"RULEBOOK:\n{rulebook_json}\n\nCONTEXT: {json.dumps(context, ensure_ascii=False)}\n\n"
        + instructions
        + "\n\nRespond with the JSON object only; no prose outside it."
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


def _extract_json(text: str) -> dict:
    """Pull one JSON object out of model output, tolerating fences/prose."""
    if not isinstance(text, str) or not text.strip():
        raise AdviseError("advisory model returned empty output")
    candidates = []
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(text)
    for candidate in candidates:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end <= start:
            continue
        try:
            payload = json.loads(candidate[start:end + 1])
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    raise AdviseError(
        "advisory model output was not a JSON object: "
        + repr(text[:200])
    )


def _shape(question: str, payload: dict, rule_id: Optional[str]) -> dict:
    """Validate the shape contract per question kind and echo the mode."""
    if question == "interpret":
        if rule_id is not None:
            if not isinstance(payload.get("meaning"), str):
                raise AdviseError("interpret: model output lacks a string 'meaning'")
            payload["rule_id"] = rule_id
        else:
            if not isinstance(payload.get("interpretations"), list):
                raise AdviseError(
                    "interpret: model output lacks an 'interpretations' list"
                )
    if question == "apply":
        if not isinstance(payload.get("applicable"), list) or \
                not isinstance(payload.get("summary"), str):
            raise AdviseError(
                "apply: model output must be an object with 'summary' (str) "
                "and 'applicable' (list)"
            )
    if question == "gap":
        if payload.get("coverage") not in ("adequate", "partial", "none") or \
                not isinstance(payload.get("gaps"), list):
            raise AdviseError(
                "gap: model output must have 'coverage' (adequate|partial|none) "
                "and 'gaps' (list)"
            )
    result = dict(payload)
    result["mode"] = "model"
    return result
