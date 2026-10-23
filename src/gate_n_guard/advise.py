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
from .rules import MissingPathError, Rulebook, RulebookError, check

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
    if situation is not None and not isinstance(situation, dict):
        raise AdviseError("advise: situation must be an object")
    if question in ("apply", "gap") and (
        not isinstance(action, dict)
        or not isinstance(action.get("tool"), str)
        or not action["tool"]
    ):
        raise AdviseError(
            f"advise: question={question!r} requires an action object with a "
            "non-empty string tool"
        )
    if rule_id is not None:
        if question != "interpret":
            raise AdviseError("rule_id is only valid for question=interpret")
        if rule_id not in {rule.id for rule in rulebook.rules}:
            raise AdviseError(f"rule_id {rule_id!r} is not in this rulebook")

    if question == "apply" and mode == "auto":
        try:
            deterministic = _apply_deterministic(rulebook, action, situation)
        except (RulebookError, MissingPathError) as exc:
            raise AdviseError(f"advise: {exc}") from exc
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
    try:
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
        context_json = json.dumps(
            {"action": action, "situation": situation or {}}, ensure_ascii=False
        )
    except (TypeError, ValueError) as exc:
        raise AdviseError(f"advise: context is not JSON-serializable: {exc}") from exc
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
        f"RULEBOOK:\n{rulebook_json}\n\nCONTEXT: {context_json}\n\n"
        + instructions
        + "\n\nRespond with the JSON object only; no prose outside it."
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]


def _extract_json(text: str) -> dict:
    """Pull one JSON object out of model output, tolerating fences/prose.

    Scans for the first parseable object instead of slicing from the first
    "{" to the last "}", because prose around the answer may contain braces.
    A second parseable object is ambiguous and rejected.
    """
    if not isinstance(text, str) or not text.strip():
        raise AdviseError("advisory model returned empty output")
    candidates = []
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(text)
    for candidate in candidates:
        payload = _first_json_object(candidate)
        if payload is not None:
            return payload
    raise AdviseError(
        "advisory model output was not a JSON object: "
        + repr(text[:200])
    )


def _first_json_object(candidate: str):
    decoder = json.JSONDecoder()
    index = 0
    found = None
    while True:
        start = candidate.find("{", index)
        if start == -1:
            return found
        try:
            payload, end = decoder.raw_decode(candidate, start)
        except ValueError:
            index = start + 1
            continue
        if isinstance(payload, dict):
            if found is not None:
                raise AdviseError(
                    "advisory model output contains multiple JSON objects"
                )
            found = payload
        index = end


def _shape(question: str, payload: dict, rule_id: Optional[str]) -> dict:
    """Validate the shape contract per question kind and echo the mode.

    Only the keys named by the prompt survive: a model answer can never
    smuggle in a verdict or a mode of its own.
    """
    if question == "interpret":
        if rule_id is not None:
            result = _shape_interpretation(payload, "interpret")
            result["rule_id"] = rule_id
        else:
            entries = payload.get("interpretations")
            if not isinstance(entries, list):
                raise AdviseError(
                    "interpret: model output lacks an 'interpretations' list "
                    "of objects"
                )
            result = {"interpretations": [
                _shape_interpretation(entry, "interpret", require_rule_id=True)
                for entry in entries
            ]}
    elif question == "apply":
        applicable = payload.get("applicable")
        if not isinstance(payload.get("summary"), str) or not isinstance(
            applicable, list
        ):
            raise AdviseError(
                "apply: model output must be an object with 'summary' (str) "
                "and 'applicable' (list of objects)"
            )
        result = {
            "summary": payload["summary"],
            "applicable": [_shape_applicable(entry) for entry in applicable],
        }
        if "notable_missing" in payload:
            result["notable_missing"] = _string_list(
                payload, "notable_missing", "apply"
            )
    else:
        gaps = payload.get("gaps")
        if payload.get("coverage") not in ("adequate", "partial", "none") or not (
            isinstance(gaps, list)
        ):
            raise AdviseError(
                "gap: model output must have 'coverage' "
                "(adequate|partial|none) and 'gaps' (list of objects)"
            )
        result = {
            "coverage": payload["coverage"],
            "gaps": [_shape_gap(entry) for entry in gaps],
        }
    result["mode"] = "model"
    return result


def _string_list(container: dict, key: str, label: str) -> list:
    value = container[key]
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise AdviseError(f"{label}: '{key}' must be a list of strings")
    return list(value)


def _shape_interpretation(
    payload: dict, label: str, require_rule_id: bool = False
) -> dict:
    if not isinstance(payload, dict):
        raise AdviseError(f"{label}: each interpretation must be an object")
    if not isinstance(payload.get("meaning"), str):
        raise AdviseError(f"{label}: model output lacks a string 'meaning'")
    result = {"meaning": payload["meaning"]}
    for key in ("permits", "requires", "forbids", "edge_cases"):
        if key in payload:
            result[key] = _string_list(payload, key, label)
    _check_confidence(payload)
    if "confidence" in payload:
        result["confidence"] = payload["confidence"]
    if require_rule_id:
        rule = payload.get("rule_id")
        if not isinstance(rule, str) or not rule:
            raise AdviseError(
                f"{label}: each interpretation needs a non-empty string "
                "'rule_id'"
            )
        result["rule_id"] = rule
    return result


def _shape_applicable(entry: dict) -> dict:
    if not isinstance(entry, dict):
        raise AdviseError("apply: each applicable entry must be an object")
    for key in ("rule_id", "relevance", "reading"):
        value = entry.get(key)
        if not isinstance(value, str) or not value:
            raise AdviseError(
                f"apply: each applicable entry needs a non-empty string "
                f"'{key}'"
            )
    result = {key: entry[key] for key in ("rule_id", "relevance", "reading")}
    if "caveats" in entry:
        result["caveats"] = _string_list(entry, "caveats", "apply")
    return result


def _shape_gap(entry: dict) -> dict:
    if not isinstance(entry, dict):
        raise AdviseError("gap: each gap must be an object")
    result = {}
    for key in ("concern", "suggested_rule", "rationale"):
        value = entry.get(key)
        if not isinstance(value, str):
            raise AdviseError(f"gap: each gap needs a string '{key}'")
        result[key] = value
    return result


def _check_confidence(payload: dict) -> None:
    if "confidence" not in payload:
        return
    value = payload["confidence"]
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not 0 <= value <= 1
    ):
        raise AdviseError("interpret: 'confidence' must be a number in 0..1")
