"""Rule matching for gate-n-guard.

Deterministic evaluation of a rulebook against a proposed action and a
situation. Pure local compute: no network, no randomness — the same input
always produces the same verdict and trace (both precedence modes).

A rulebook is an ordered list of rules plus a no-match default and a
precedence mode (`first-match` or `strictest`). A rule matches when any of its
`when` entries holds; an entry is a conjunction of predicates on the action
(`tool`, `arguments.<path>`), the agent, or situation facts
(`situation.<path>`).

Predicate operands: a bare value means equality (same JSON type); a
single-key object selects an operator — `glob` (fnmatch over strings),
`contains` (string substring), `matches` (regular-expression search, pattern
compiled and syntax-checked at rulebook load), `in` (same-type membership in
a list), `eq`, and the numeric comparisons `lt`/`lte`/`gt`/`gte`.

This predicate subset is the whole v1 grammar by design; aligning the matcher
with weigh-n-decide's full condition grammar (boolean composition,
arithmetic) is a possible later evolution — `_operand_predicate` /
`_arguments_predicates` normalization is the single seam where it would land.

Missing paths are errors, never silent falses — but only when actually
consulted: predicates inside a `when` entry and entries inside a rule are
evaluated left to right with short-circuiting, so an action that already
failed an earlier predicate is never tripped up by a later one. Malformed
rulebooks raise `RulebookError` at load time, including duplicate rule ids,
empty nested `arguments` objects, dotted/empty argument names, and empty
`situation.` path segments.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any

VERDICTS = ("allow", "confirm", "deny")
PRECEDENCE_MODES = ("first-match", "strictest")
STRICTEST_ORDER = {"allow": 0, "confirm": 1, "deny": 2}

_OPERATORS = ("glob", "contains", "in", "lt", "lte", "gt", "gte", "eq", "matches")


class RulebookError(ValueError):
    """A rulebook is malformed: bad shape, verdict, precedence, or operator."""


class MissingPathError(ValueError):
    """A rule predicate references a path absent from the action or situation."""


@dataclass(frozen=True)
class Predicate:
    path: str
    operator: str
    value: Any
    compiled: Any = None  # precompiled regex for the `matches` operator


@dataclass(frozen=True)
class Rule:
    id: str
    entries: tuple[Any, ...]
    verdict: str
    reason: str


@dataclass(frozen=True)
class Rulebook:
    default: str
    precedence: str
    rules: tuple[Rule, ...]


# --------------------------------------------------------------------------
# Loading and validation
# --------------------------------------------------------------------------

def load_rulebook(data: dict) -> Rulebook:
    """Validate a parsed rulebook and return an immutable Rulebook.

    Raises RulebookError with a message naming the offending rule/field.
    """
    if not isinstance(data, dict):
        raise RulebookError("rulebook must be a JSON object")
    default = data.get("default", "allow")
    if default not in VERDICTS:
        raise RulebookError(
            f"default verdict must be one of {VERDICTS!r}, got {default!r}"
        )
    precedence = data.get("precedence", "first-match")
    if precedence not in PRECEDENCE_MODES:
        raise RulebookError(
            f"precedence must be one of {PRECEDENCE_MODES!r}, got {precedence!r}"
        )
    raw_rules = data.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise RulebookError("rulebook must contain a non-empty rules list")
    rules = tuple(_load_rule(raw, index) for index, raw in enumerate(raw_rules))
    seen_ids = set()
    for rule in rules:
        if rule.id in seen_ids:
            raise RulebookError(f"duplicate rule id {rule.id!r}")
        seen_ids.add(rule.id)
    return Rulebook(default=default, precedence=precedence, rules=rules)


def _load_rule(raw: Any, index: int) -> Rule:
    if not isinstance(raw, dict):
        raise RulebookError(f"rules[{index}]: rule must be an object")
    rule_id = raw.get("id")
    if not isinstance(rule_id, str) or not rule_id:
        raise RulebookError(f"rules[{index}]: rule must have a non-empty string id")
    verdict = raw.get("verdict")
    if verdict not in VERDICTS:
        raise RulebookError(
            f"rule {rule_id!r}: verdict must be one of {VERDICTS!r}, got {verdict!r}"
        )
    reason = raw.get("reason", "")
    if not isinstance(reason, str):
        raise RulebookError(f"rule {rule_id!r}: reason must be a string")
    when = raw.get("when")
    if not isinstance(when, list) or not when:
        raise RulebookError(
            f"rule {rule_id!r}: when must be a non-empty list of predicate objects"
        )
    entries = tuple(tuple(_entry_predicates(entry, rule_id)) for entry in when)
    return Rule(id=rule_id, entries=entries, verdict=verdict, reason=reason)


def _entry_predicates(entry: Any, rule_id: str) -> list:
    if not isinstance(entry, dict) or not entry:
        raise RulebookError(
            f"rule {rule_id!r}: each when entry must be a non-empty object"
        )
    predicates: list = []
    for key, operand in entry.items():
        if key == "tool":
            predicates.append(_operand_predicate("tool", operand, rule_id))
        elif key == "arguments":
            if not isinstance(operand, dict) or not operand:
                raise RulebookError(
                    f"rule {rule_id!r}: arguments must be a non-empty object"
                )
            predicates.extend(_arguments_predicates("arguments", operand, rule_id))
        elif key.startswith("situation.") and len(key) > len("situation."):
            segments = key.split(".")[1:]
            if any(not segment for segment in segments):
                raise RulebookError(
                    f"rule {rule_id!r}: situation path {key!r} must not contain "
                    "empty segments"
                )
            predicates.append(_operand_predicate(key, operand, rule_id))
        else:
            raise RulebookError(
                f"rule {rule_id!r}: unknown predicate key {key!r} "
                "(expected tool, arguments, or situation.<path>)"
            )
    return predicates


def _arguments_predicates(path: str, operand: Any, rule_id: str) -> list:
    """Expand an `arguments` value into leaf predicates.

    Keys under `arguments` are path segments into the arguments object; a
    leaf is either a bare value (equality) or a single-key object whose key
    is an operator. Objects without operator keys recurse, so
    {"options": {"path": {"contains": "secrets"}}} matches arguments.
    options.path. An argument name that collides with an operator name at a
    leaf position is a load error rather than a silent misinterpretation.
    """
    if isinstance(operand, dict):
        if not operand:
            raise RulebookError(
                f"rule {rule_id!r}: {path}: arguments object must not be empty"
            )
        operator_keys = [key for key in operand if key in _OPERATORS]
        if len(operator_keys) > 1:
            raise RulebookError(
                f"rule {rule_id!r}: {path}: at most one operator per predicate object"
            )
        if operator_keys:
            operator = operator_keys[0]
            if len(operand) != 1:
                raise RulebookError(
                    f"rule {rule_id!r}: {path}: operator {operator!r} must be "
                    "the only key"
                )
            return [_make_predicate(path, operator, operand[operator], rule_id)]
        predicates = []
        for sub_key, sub_operand in operand.items():
            if not isinstance(sub_key, str) or not sub_key or "." in sub_key:
                raise RulebookError(
                    f"rule {rule_id!r}: {path}: argument name {sub_key!r} must "
                    "be a non-empty string without '.'"
                )
            predicates.extend(
                _arguments_predicates(f"{path}.{sub_key}", sub_operand, rule_id)
            )
        return predicates
    return [Predicate(path, "eq", operand)]


def _operand_predicate(path: str, operand: Any, rule_id: str) -> Predicate:
    if isinstance(operand, dict):
        if len(operand) != 1:
            raise RulebookError(
                f"rule {rule_id!r}: {path}: predicate object must have exactly "
                "one operator"
            )
        operator, value = next(iter(operand.items()))
        if operator not in _OPERATORS:
            raise RulebookError(
                f"rule {rule_id!r}: {path}: unknown operator {operator!r}"
            )
        return _make_predicate(path, operator, value, rule_id)
    return Predicate(path, "eq", operand)


def _make_predicate(path: str, operator: str, value: Any, rule_id: str) -> Predicate:
    """Build a predicate, validating operator-specific operand shapes.

    `matches` patterns are compiled here so a syntax error in the rulebook is
    a load-time RulebookError, not a runtime surprise.
    """
    if operator == "matches":
        if not isinstance(value, str):
            raise RulebookError(
                f"rule {rule_id!r}: {path}: 'matches' requires a string pattern"
            )
        try:
            compiled = re.compile(value)
        except re.error as exc:
            raise RulebookError(
                f"rule {rule_id!r}: {path}: invalid regex pattern: {exc}"
            ) from exc
        return Predicate(path, operator, value, compiled)
    if operator == "in" and not isinstance(value, list):
        raise RulebookError(f"rule {rule_id!r}: {path}: 'in' requires a list")
    return Predicate(path, operator, value)


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

def check(rulebook: Rulebook, action: dict, situation: dict | None = None) -> dict:
    """Gate one action against a rulebook.

    Returns a JSON-safe envelope:

        {
            "verdict": "allow" | "confirm" | "deny",
            "precedence": "first-match" | "strictest",
            "matched": [{"id", "verdict", "reason", "winner"}...],
            "default_applied": bool,
            "trace": [per-rule entries with per-predicate results]
        }

    Raises RulebookError for a malformed action/situation, MissingPathError
    for a predicate path the caller did not provide.
    """
    if not isinstance(action, dict) or not isinstance(action.get("tool"), str) \
            or not action["tool"]:
        raise RulebookError("action must be an object with a non-empty string tool")
    if situation is None:
        situation = {}
    if not isinstance(situation, dict):
        raise RulebookError("situation must be an object")
    if rulebook.precedence == "strictest":
        return _check_strictest(rulebook, action, situation)
    return _check_first_match(rulebook, action, situation)


def _check_first_match(rulebook: Rulebook, action: dict, situation: dict) -> dict:
    trace = []
    for rule in rulebook.rules:
        outcome = _evaluate_rule(rule, action, situation)
        trace.append(outcome)
        if outcome["matched"]:
            return {
                "verdict": rule.verdict,
                "precedence": "first-match",
                "matched": [{
                    "id": rule.id,
                    "verdict": rule.verdict,
                    "reason": rule.reason,
                    "winner": True,
                }],
                "default_applied": False,
                "trace": trace,
            }
    return {
        "verdict": rulebook.default,
        "precedence": "first-match",
        "matched": [],
        "default_applied": True,
        "trace": trace,
    }


def _check_strictest(rulebook: Rulebook, action: dict, situation: dict) -> dict:
    trace = []
    hits = []
    for rule in rulebook.rules:
        outcome = _evaluate_rule(rule, action, situation)
        trace.append(outcome)
        if outcome["matched"]:
            hits.append((rule, outcome))
    if not hits:
        return {
            "verdict": rulebook.default,
            "precedence": "strictest",
            "matched": [],
            "default_applied": True,
            "trace": trace,
        }
    strictest = max(
        (rule.verdict for rule, _ in hits), key=lambda v: STRICTEST_ORDER[v]
    )
    matched = [
        {
            "id": rule.id,
            "verdict": rule.verdict,
            "reason": rule.reason,
            "winner": rule.verdict == strictest,
        }
        for rule, _ in hits
    ]
    return {
        "verdict": strictest,
        "precedence": "strictest",
        "matched": matched,
        "default_applied": False,
        "trace": trace,
    }


def _evaluate_rule(rule: Rule, action: dict, situation: dict) -> dict:
    entry_outcomes = []
    matched = False
    for entry in rule.entries:
        predicate_outcomes = []
        entry_matched = True
        for predicate in entry:
            if not entry_matched:
                # conjunction short-circuit: once a predicate fails, later
                # predicates in this entry are not consulted (their missing
                # paths cannot error)
                break
            value = _resolve(predicate.path, action, situation)
            result = _satisfies(predicate, value)
            predicate_outcomes.append({
                "path": predicate.path,
                "operator": predicate.operator,
                "value": predicate.value,
                "result": bool(result),
            })
            # entry_matched is still True here: a failed predicate breaks the
            # loop above, so the conjunction's second arm is never short-circuited
            entry_matched = result
        entry_outcomes.append({"matched": entry_matched, "predicates": predicate_outcomes})
        if entry_matched:
            # any-entry semantics: once an entry holds, the rule matches and
            # later entries are not consulted (their missing paths cannot
            # error, mirroring short-circuit evaluation)
            matched = True
            break
    return {
        "id": rule.id,
        "verdict": rule.verdict,
        "reason": rule.reason,
        "matched": matched,
        "entries": entry_outcomes,
    }


def _resolve(path: str, action: dict, situation: dict) -> Any:
    if path == "tool":
        return action["tool"]
    if path.startswith("arguments."):
        node: Any = action.get("arguments", {})
        for part in path.split(".")[1:]:
            if not isinstance(node, dict) or part not in node:
                raise MissingPathError(f"action argument path missing: {path}")
            node = node[part]
        return node
    if path.startswith("situation."):
        node = situation
        for part in path.split(".")[1:]:
            if not isinstance(node, dict) or part not in node:
                raise MissingPathError(f"situation path missing: {path}")
            node = node[part]
        return node
    raise AssertionError(f"unreachable predicate path {path!r}")


def _satisfies(predicate: Predicate, value: Any) -> bool:
    op, want = predicate.operator, predicate.value
    if op == "eq":
        return _same_type_eq(value, want)
    if op == "glob":
        _require_strings(predicate, value, want)
        return fnmatch.fnmatchcase(value, want)
    if op == "contains":
        _require_strings(predicate, value, want)
        return want in value
    if op == "matches":
        _require_strings(predicate, value, want)
        return predicate.compiled.search(value) is not None
    if op == "in":
        return any(_same_type_eq(value, item) for item in want)
    if op in ("lt", "lte", "gt", "gte"):
        if not _is_number(value) or not _is_number(want):
            raise RulebookError(
                f"{predicate.path}: {op} requires numeric operands, got "
                f"{type(value).__name__} vs {type(want).__name__}"
            )
        return {
            "lt": value < want,
            "lte": value <= want,
            "gt": value > want,
            "gte": value >= want,
        }[op]
    raise AssertionError(f"unreachable operator {op!r}")


def _same_type_eq(left: Any, right: Any) -> bool:
    """Equality that refuses cross-type matches (True != 1, 1 != 1.0).

    Containers compare elementwise with the same type rule applied
    recursively, so [1] != [1.0] but [] == [].
    """
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _same_type_eq(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _same_type_eq(left[k], right[k]) for k in left
        )
    return left == right


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_strings(predicate: Predicate, value: Any, want: Any) -> None:
    if not isinstance(value, str) or not isinstance(want, str):
        raise RulebookError(
            f"{predicate.path}: {predicate.operator} requires string operands, "
            f"got {type(value).__name__} vs {type(want).__name__}"
        )
