"""Unit tests for the gate-n-guard matcher.

Offline, stdlib unittest only. Covers both precedence modes, predicates,
validation errors, missing-path errors, and determinism.
"""

import unittest

from gate_n_guard.rules import (
    MissingPathError,
    Predicate,
    Rule,
    Rulebook,
    RulebookError,
    _resolve,
    _satisfies,
    check,
    load_rulebook,
)


def book(rules, default="allow", precedence="first-match"):
    return load_rulebook({"default": default, "precedence": precedence, "rules": rules})


def rule(rule_id, when, verdict, reason=""):
    return {"id": rule_id, "when": when, "verdict": verdict, "reason": reason}


ACTION = {"tool": "bash", "arguments": {"command": "git push --force"}}
SITUATION = {"agent": "ci-bot", "env": "prod", "total": 150}

# ---------------------------------------------------------------------------
# Precedence: first-match
# ---------------------------------------------------------------------------

class FirstMatchTests(unittest.TestCase):
    def test_first_matched_rule_decides(self):
        # first match wins even though a later rule denies
        rbook = book([
            rule("first", [{"tool": "bash"}], "allow", reason="generic"),
            rule("second", [{"tool": "bash", "arguments": {"command": {"contains": "push"}}}], "deny"),
        ])
        result = check(rbook, ACTION, SITUATION)
        self.assertEqual(result["verdict"], "allow")
        self.assertEqual([m["id"] for m in result["matched"]], ["first"])
        self.assertTrue(result["matched"][0]["winner"])
        self.assertFalse(result["default_applied"])
        # later rule not consulted at all
        self.assertEqual([t["id"] for t in result["trace"]], ["first"])
        # first matched rule also wins a strictest-style conflict
        self.assertEqual(result["precedence"], "first-match")

    def test_order_matters_in_first_match(self):
        rbook = book([
            rule("deny-rm", [{"tool": "bash"}], "deny"),
            rule("allow-bash", [{"tool": "bash"}], "allow"),
        ])
        self.assertEqual(check(rbook, ACTION, SITUATION)["verdict"], "deny")
        self.assertEqual([m["id"] for m in check(rbook, ACTION, SITUATION)["matched"]], ["deny-rm"])

    def test_unmatched_rule_is_skipped(self):
        rbook = book([
            rule("nope", [{"tool": "git"}], "deny"),
            rule("catches", [{"tool": "bash", "arguments": {"command": {"contains": "push"}}}], "confirm"),
        ])
        result = check(rbook, ACTION, SITUATION)
        self.assertEqual(result["verdict"], "confirm")
        self.assertEqual([t["id"] for t in result["trace"]], ["nope", "catches"])

    def test_any_entry_in_rule_matches(self):
        rbook = book([
            rule("shell-or-rm", [
                {"tool": {"glob": "shell*"}},
                {"tool": "bash", "arguments": {"command": {"contains": "rm -rf"}}},
            ], "deny"),
        ])
        self.assertEqual(check(rbook, {"tool": "shell"}, SITUATION)["verdict"], "deny")
        self.assertEqual(check(rbook, {"tool": "bash", "arguments": {"command": "rm -rf /"}}, SITUATION)["verdict"], "deny")
        self.assertEqual(check(rbook, {"tool": "bash", "arguments": {"command": "ls"}}, SITUATION)["verdict"], "allow")

    def test_entry_is_conjunction(self):
        rbook = book([
            rule("prod-deploy", [{"tool": "deploy", "situation.agent": "ci-bot"}], "confirm"),
        ])
        self.assertEqual(check(rbook, {"tool": "deploy"}, SITUATION)["verdict"], "confirm")
        # same tool, different agent -> no match
        self.assertEqual(
            check(rbook, {"tool": "deploy"}, {"agent": "human"})["verdict"],
            "allow",
        )

    def test_matched_entry_short_circuits_later_entries(self):
        # entry 1 matches; entry 2 references an argument the action lacks —
        # it must never be consulted, so no MissingPathError
        rbook = book([
            rule("shell-or-rm", [
                {"tool": {"glob": "shell*"}},
                {"tool": "bash", "arguments": {"command": {"contains": "rm -rf"}}},
            ], "deny"),
        ])
        result = check(rbook, {"tool": "shell"}, SITUATION)
        self.assertEqual(result["verdict"], "deny")
        self.assertEqual(result["trace"][0]["entries"], [
            {"matched": True, "predicates": [
                {"path": "tool", "operator": "glob", "value": "shell*", "result": True},
            ]},
        ])

    def test_failed_predicate_short_circuits_entry(self):
        # entry's first predicate fails; the second references an argument
        # the action lacks — the entry fails without a MissingPathError
        rbook = book([
            rule("no-rm-rf", [{"tool": "bash", "arguments": {"command": {"contains": "rm -rf"}}}], "deny"),
            rule("deploy-confirm", [{"tool": {"glob": "deploy*"}}], "confirm"),
        ])
        result = check(rbook, {"tool": "deploy", "arguments": {"target": "prod"}}, SITUATION)
        self.assertEqual(result["verdict"], "confirm")
        # trace shows the failure predicate and no arguments.command entry
        self.assertEqual([p["path"] for p in result["trace"][0]["entries"][0]["predicates"]], ["tool"])


# ---------------------------------------------------------------------------
# Precedence: strictest
# ---------------------------------------------------------------------------

class StrictestTests(unittest.TestCase):
    def test_deny_wins_regardless_of_order(self):
        rules = [
            rule("allow-all", [{"tool": "bash"}], "allow"),
            rule("confirm-push", [{"tool": "bash", "arguments": {"command": {"contains": "push"}}}], "confirm"),
            rule("deny-force", [{"tool": "bash", "arguments": {"command": {"contains": "--force"}}}], "deny"),
        ]
        import itertools
        for perm in itertools.permutations(rules):
            result = check(book(list(perm), precedence="strictest"), ACTION, SITUATION)
            self.assertEqual(result["verdict"], "deny", f"order {[r['id'] for r in perm]}")
            self.assertFalse(result["default_applied"])
            self.assertEqual(len(result["matched"]), 3)
            winners = [m["id"] for m in result["matched"] if m["winner"]]
            self.assertEqual(winners, ["deny-force"])

    def test_confirm_beats_allow(self):
        rbook = book([
            rule("allow-all", [{"tool": "bash"}], "allow"),
            rule("confirm-push", [{"tool": "bash", "arguments": {"command": {"contains": "push"}}}], "confirm"),
        ], precedence="strictest")
        result = check(rbook, ACTION, SITUATION)
        self.assertEqual(result["verdict"], "confirm")
        self.assertEqual(result["precedence"], "strictest")

    def test_all_rules_consulted_in_strictest(self):
        rbook = book([
            rule("a", [{"tool": "git"}], "deny"),
            rule("b", [{"tool": "bash"}], "allow"),
        ], precedence="strictest")
        result = check(rbook, ACTION, SITUATION)
        self.assertEqual([t["id"] for t in result["trace"]], ["a", "b"])
        self.assertEqual(result["verdict"], "allow")

    def test_no_match_falls_back_to_default(self):
        rbook = book([
            rule("git-only", [{"tool": "git"}], "deny"),
        ], default="confirm", precedence="strictest")
        result = check(rbook, ACTION, SITUATION)
        self.assertEqual(result["verdict"], "confirm")
        self.assertTrue(result["default_applied"])
        self.assertEqual(result["matched"], [])


# ---------------------------------------------------------------------------
# Default verdict
# ---------------------------------------------------------------------------

class DefaultTests(unittest.TestCase):
    def test_default_allow(self):
        rbook = book([rule("git-only", [{"tool": "git"}], "deny")])
        result = check(rbook, ACTION, SITUATION)
        self.assertEqual(result["verdict"], "allow")
        self.assertTrue(result["default_applied"])

    def test_default_deny(self):
        rbook = book([rule("git-only", [{"tool": "git"}], "deny")], default="deny")
        result = check(rbook, ACTION, SITUATION)
        self.assertEqual(result["verdict"], "deny")
        self.assertTrue(result["default_applied"])


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------

class PredicateTests(unittest.TestCase):
    def test_glob(self):
        rbook = book([rule("shell", [{"tool": {"glob": "shell*"}}], "deny")])
        self.assertEqual(check(rbook, {"tool": "shell/run_command"}, SITUATION)["verdict"], "deny")
        self.assertEqual(check(rbook, {"tool": "bash"}, SITUATION)["verdict"], "allow")

    def test_contains(self):
        rbook = book([rule("rm", [{"tool": "bash", "arguments": {"command": {"contains": "rm -rf"}}}], "deny")])
        self.assertEqual(check(rbook, {"tool": "bash", "arguments": {"command": "rm -rf /"}}, SITUATION)["verdict"], "deny")
        self.assertEqual(check(rbook, {"tool": "bash", "arguments": {"command": "rm file"}}, SITUATION)["verdict"], "allow")

    def test_in(self):
        rbook = book([rule("prod-only", [{"tool": "deploy", "situation.env": {"in": ["prod", "staging"]}}], "deny")])
        self.assertEqual(check(rbook, {"tool": "deploy"}, {"env": "prod"})["verdict"], "deny")
        self.assertEqual(check(rbook, {"tool": "deploy"}, {"env": "dev"})["verdict"], "allow")

    def test_numeric_comparisons(self):
        rbook = book([rule("big", [{"tool": "pay", "situation.total": {"gte": 100}}], "confirm")])
        self.assertEqual(check(rbook, {"tool": "pay"}, {"total": 100})["verdict"], "confirm")
        self.assertEqual(check(rbook, {"tool": "pay"}, {"total": 99})["verdict"], "allow")
        rbook_lt = book([rule("small", [{"situation.total": {"lt": 10}}], "deny")])
        self.assertEqual(check(rbook_lt, {"tool": "x"}, {"total": 9})["verdict"], "deny")
        self.assertEqual(check(rbook_lt, {"tool": "x"}, {"total": 10})["verdict"], "allow")

    def test_eq_is_type_strict(self):
        rbook = book([rule("flag", [{"situation.flag": True}], "deny")])
        self.assertEqual(check(rbook, {"tool": "x"}, {"flag": True})["verdict"], "deny")
        # 1 and "true" are not the boolean true
        self.assertEqual(check(rbook, {"tool": "x"}, {"flag": 1})["verdict"], "allow")
        self.assertEqual(check(rbook, {"tool": "x"}, {"flag": "true"})["verdict"], "allow")

    def test_matches_regex_search(self):
        rbook = book([rule(
            "prune", [{"tool": "bash", "arguments": {"command": {"matches": r"\bdocker\s+system\s+prune\b"}}}], "deny",
        )])
        self.assertEqual(
            check(rbook, {"tool": "bash", "arguments": {"command": "sudo docker system prune -af"}}, SITUATION)["verdict"],
            "deny",
        )
        self.assertEqual(
            check(rbook, {"tool": "bash", "arguments": {"command": "docker ps"}}, SITUATION)["verdict"],
            "allow",
        )

    def test_matches_anchored_pattern(self):
        rbook = book([rule("rm", [{"tool": "bash", "arguments": {"command": {"matches": r"^rm\b"}}}], "deny")])
        self.assertEqual(check(rbook, {"tool": "bash", "arguments": {"command": "rm -rf /tmp"}}, SITUATION)["verdict"], "deny")
        self.assertEqual(check(rbook, {"tool": "bash", "arguments": {"command": "xrm -rf"}}, SITUATION)["verdict"], "allow")

    def test_matches_invalid_regex_rejected_at_load(self):
        with self.assertRaises(RulebookError):
            book([rule("bad", [{"tool": {"matches": "("}}], "deny")])

    def test_matches_pattern_must_be_string(self):
        with self.assertRaises(RulebookError):
            book([rule("bad", [{"tool": {"matches": 5}}], "deny")])

    def test_matches_non_string_value(self):
        rbook = book([rule("n", [{"situation.total": {"matches": r"\d+"}}], "deny")])
        with self.assertRaises(RulebookError):
            check(rbook, {"tool": "x"}, {"total": 150})

    def test_arguments_nested_path(self):
        rbook = book([rule("path", [{"arguments": {"options": {"path": {"contains": "secrets"}}}}], "deny")])
        action = {"tool": "read", "arguments": {"options": {"path": "/var/secrets/x"}}}
        self.assertEqual(check(rbook, action, SITUATION)["verdict"], "deny")

    def test_situation_dotted_path(self):
        rbook = book([rule("tenant", [{"situation.identity.role": "admin"}], "confirm")])
        self.assertEqual(check(rbook, {"tool": "x"}, {"identity": {"role": "admin"}})["verdict"], "confirm")
        self.assertEqual(check(rbook, {"tool": "x"}, {"identity": {"role": "dev"}})["verdict"], "allow")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ValidationTests(unittest.TestCase):
    def test_unknown_precedence(self):
        with self.assertRaises(RulebookError):
            book([rule("a", [{"tool": "x"}], "allow")], precedence="priority")

    def test_unknown_verdict(self):
        with self.assertRaises(RulebookError):
            book([rule("a", [{"tool": "x"}], "maybe")])

    def test_unknown_default(self):
        with self.assertRaises(RulebookError):
            book([rule("a", [{"tool": "x"}], "allow")], default="maybe")

    def test_missing_id(self):
        with self.assertRaises(RulebookError):
            book([{"when": [{"tool": "x"}], "verdict": "allow"}])

    def test_empty_when(self):
        with self.assertRaises(RulebookError):
            book([rule("a", [], "allow")])

    def test_empty_rules(self):
        with self.assertRaises(RulebookError):
            book([])

    def test_unknown_predicate_key(self):
        with self.assertRaises(RulebookError):
            book([rule("a", [{"toolname": "x"}], "allow")])

    def test_multi_operator_object(self):
        with self.assertRaises(RulebookError):
            book([rule("a", [{"tool": {"glob": "x*", "contains": "y"}}], "allow")])

    def test_unknown_operator(self):
        with self.assertRaises(RulebookError):
            book([rule("a", [{"tool": {"startswith": "x"}}], "allow")])

    def test_in_requires_list(self):
        with self.assertRaises(RulebookError):
            book([rule("a", [{"tool": {"in": "bash"}}], "allow")])

    def test_bad_action(self):
        rbook = book([rule("a", [{"tool": "bash"}], "allow")])
        with self.assertRaises(RulebookError):
            check(rbook, {"tool": 42})
        with self.assertRaises(RulebookError):
            check(rbook, {"arguments": {}})

    def test_missing_situation_path(self):
        rbook = book([rule("a", [{"situation.env": "prod"}], "deny")])
        with self.assertRaises(MissingPathError):
            check(rbook, {"tool": "x"}, {"other": 1})

    def test_missing_argument_path(self):
        rbook = book([rule("a", [{"arguments": {"command": "x"}}], "deny")])
        with self.assertRaises(MissingPathError):
            check(rbook, {"tool": "bash"})

    def test_glob_on_non_string(self):
        rbook = book([rule("a", [{"situation.total": {"glob": "1*"}}], "deny")])
        with self.assertRaises(RulebookError):
            check(rbook, {"tool": "x"}, {"total": 150})


# ---------------------------------------------------------------------------
# Trace and determinism
# ---------------------------------------------------------------------------

class TraceTests(unittest.TestCase):
    def test_trace_shapes(self):
        rbook = book([
            rule("r1", [{"tool": "bash", "arguments": {"command": {"contains": "push"}}}], "deny", reason="no pushes"),
        ])
        trace = check(rbook, ACTION, SITUATION)["trace"]
        self.assertEqual(trace[0]["id"], "r1")
        self.assertTrue(trace[0]["matched"])
        predicates = trace[0]["entries"][0]["predicates"]
        self.assertEqual(
            [(p["path"], p["operator"], p["result"]) for p in predicates],
            [("tool", "eq", True), ("arguments.command", "contains", True)],
        )
        self.assertEqual(trace[0]["reason"], "no pushes")

    def test_determinism_both_modes(self):
        rules = [
            rule("allow-all", [{"tool": "bash"}], "allow"),
            rule("confirm-push", [{"tool": "bash", "arguments": {"command": {"contains": "push"}}}], "confirm"),
            rule("deny-force", [{"tool": "bash", "arguments": {"command": {"contains": "--force"}}}], "deny"),
        ]
        for precedence in ("first-match", "strictest"):
            rbook = book(rules, precedence=precedence)
            first = check(rbook, ACTION, SITUATION)
            second = check(rbook, ACTION, SITUATION)
            self.assertEqual(first, second, f"nondeterministic in {precedence}")


class RulebookValidationTests(unittest.TestCase):
    def assertInvalid(self, data, needle):
        with self.assertRaises(RulebookError) as ctx:
            load_rulebook(data)
        self.assertIn(needle, str(ctx.exception))

    def test_rulebook_must_be_an_object(self):
        for data in ([], "rules", 1, None):
            with self.subTest(data=data):
                self.assertInvalid(data, "JSON object")

    def test_rules_list_must_be_present_and_non_empty(self):
        for data in ({}, {"rules": []}, {"rules": {}}):
            with self.subTest(data=data):
                self.assertInvalid(data, "non-empty rules list")

    def test_rule_must_be_an_object(self):
        self.assertInvalid({"rules": ["bash"]}, "rules[0]: rule must be an object")

    def test_rule_id_must_be_a_non_empty_string(self):
        for rule_id in (None, "", 7):
            with self.subTest(rule_id=rule_id):
                self.assertInvalid(
                    {"rules": [{"id": rule_id, "when": [{"tool": "bash"}], "verdict": "deny"}]},
                    "non-empty string id",
                )

    def test_reason_must_be_a_string(self):
        self.assertInvalid(
            {"rules": [{"id": "r", "when": [{"tool": "bash"}], "verdict": "deny", "reason": 5}]},
            "reason must be a string",
        )

    def test_when_must_be_a_non_empty_list(self):
        for when in (None, [], {}, "bash"):
            with self.subTest(when=when):
                self.assertInvalid(
                    {"rules": [{"id": "r", "when": when, "verdict": "deny"}]},
                    "when must be a non-empty list",
                )

    def test_when_entry_must_be_a_non_empty_object(self):
        for entry in ({}, [], "x", 3):
            with self.subTest(entry=entry):
                self.assertInvalid(
                    {"rules": [{"id": "r", "when": [entry], "verdict": "deny"}]},
                    "each when entry must be a non-empty object",
                )

    def test_arguments_must_be_a_non_empty_object(self):
        for arguments in ({}, [], "x"):
            with self.subTest(arguments=arguments):
                self.assertInvalid(
                    {"rules": [{"id": "r", "when": [{"arguments": arguments}], "verdict": "deny"}]},
                    "arguments must be a non-empty object",
                )

    def test_two_operators_in_one_object_rejected(self):
        self.assertInvalid(
            {"rules": [{"id": "r", "when": [
                {"arguments": {"command": {"contains": "x", "glob": "y"}}}
            ], "verdict": "deny"}]},
            "at most one operator",
        )

    def test_operator_with_extra_keys_rejected(self):
        self.assertInvalid(
            {"rules": [{"id": "r", "when": [
                {"arguments": {"command": {"contains": "x", "extra": 1}}}
            ], "verdict": "deny"}]},
            "must be the only key",
        )

    def test_unknown_operator_rejected(self):
        self.assertInvalid(
            {"rules": [{"id": "r", "when": [{"tool": {"regex": "x"}}], "verdict": "deny"}]},
            "unknown operator",
        )

    def test_in_requires_a_list(self):
        self.assertInvalid(
            {"rules": [{"id": "r", "when": [{"tool": {"in": "bash"}}], "verdict": "deny"}]},
            "'in' requires a list",
        )

    def test_argument_key_colliding_with_an_operator_is_a_load_error(self):
        # "in" is an operator name, so it is read as one at a leaf position
        self.assertInvalid(
            {"rules": [{"id": "r", "when": [{"arguments": {"in": "x"}}], "verdict": "deny"}]},
            "'in' requires a list",
        )



    def test_empty_situation_path_is_not_a_predicate_key(self):
        # "situation." names no fact, so it is an unknown key rather than a
        # predicate on an empty path
        self.assertInvalid(
            {"rules": [{"id": "r", "when": [{"situation.": "ci-bot"}], "verdict": "deny"}]},
            "unknown predicate key",
        )

class ActionValidationTests(unittest.TestCase):
    def setUp(self):
        self.rbook = book([rule("deny-bash", [{"tool": "bash"}], "deny")])

    def test_action_must_be_an_object_with_a_non_empty_tool(self):
        for action in ({}, {"tool": ""}, {"tool": 1}, {"arguments": {}}, "bash", None, ["bash"]):
            with self.subTest(action=action), self.assertRaises(RulebookError) as ctx:
                check(self.rbook, action)
            self.assertIn("non-empty string tool", str(ctx.exception))

    def test_situation_must_be_an_object(self):
        for situation in ([], "prod", 7):
            with self.subTest(situation=situation), self.assertRaises(RulebookError) as ctx:
                check(self.rbook, {"tool": "bash"}, situation)
            self.assertIn("situation must be an object", str(ctx.exception))

    def test_situation_defaults_to_empty(self):
        self.assertEqual(check(self.rbook, {"tool": "bash"})["verdict"], "deny")


def matches_value(want, value):
    """Does an eq predicate with operand `want` match the action value `value`?"""
    rbook = book([
        rule("r", [{"tool": "x", "arguments": {"value": {"eq": want}}}], "deny"),
    ])
    return check(rbook, {"tool": "x", "arguments": {"value": value}})["verdict"] == "deny"


class EqualityDepthTests(unittest.TestCase):
    def test_scalars_are_type_strict(self):
        self.assertTrue(matches_value(1, 1))
        self.assertFalse(matches_value(True, 1))
        self.assertFalse(matches_value(1, True))
        self.assertFalse(matches_value(1, 1.0))
        self.assertFalse(matches_value("1", 1))
        self.assertTrue(matches_value(None, None))

    def test_lists_compare_elementwise(self):
        self.assertTrue(matches_value([1, 2], [1, 2]))
        self.assertFalse(matches_value([1, 2], [1, 2, 3]))
        # the rulebook operand longer than the action value must not match either
        self.assertFalse(matches_value([1, 2, 3], [1, 2]))
        self.assertFalse(matches_value([1], [1.0]))
        self.assertTrue(matches_value([1, [2]], [1, [2]]))
        self.assertFalse(matches_value([1, [2]], [1, [2.0]]))
        self.assertTrue(matches_value([], []))

    def test_dicts_compare_keys_and_values(self):
        self.assertTrue(matches_value({"a": 1, "b": [2]}, {"b": [2], "a": 1}))
        self.assertFalse(matches_value({"a": 1}, {"a": True}))
        self.assertFalse(matches_value({"a": 1}, {"a": 1, "b": 2}))

    def test_container_type_mismatch_is_false(self):
        self.assertFalse(matches_value({"a": 1}, [("a", 1)]))
        self.assertFalse(matches_value([1], 1))


class OperandTypeTests(unittest.TestCase):
    def verdict_for(self, when, action):
        return check(book([rule("r", [when], "deny")]), action)["verdict"]

    def test_numeric_comparison_operands_are_checked_at_load(self):
        for operand in (True, "1", None, [1]):
            with self.subTest(operand=operand), self.assertRaises(
                RulebookError
            ) as ctx:
                book([rule("r", [{"arguments": {"n": {"gt": operand}}}], "deny")])
            self.assertIn("numeric operand", str(ctx.exception))

    def test_numeric_comparisons_reject_non_number_action_values(self):
        for value in ("2", None, True):
            with self.subTest(value=value), self.assertRaises(
                RulebookError
            ) as ctx:
                self.verdict_for(
                    {"arguments": {"n": {"gt": 1}}},
                    {"tool": "x", "arguments": {"n": value}},
                )
            self.assertIn("requires numeric operands", str(ctx.exception))

    def test_string_operators_reject_non_strings(self):
        for operator in ("glob", "contains", "matches"):
            with self.subTest(operator=operator), self.assertRaises(RulebookError) as ctx:
                self.verdict_for(
                    {"arguments": {"n": {operator: "x"}}},
                    {"tool": "x", "arguments": {"n": 1}},
                )
            self.assertIn("requires string operands", str(ctx.exception))

    def test_bool_is_not_a_number_for_comparisons(self):
        # True is an int subclass; the matcher refuses it as a numeric operand
        self.assertRaises(
            RulebookError,
            self.verdict_for,
            {"arguments": {"n": {"gt": 0}}},
            {"tool": "x", "arguments": {"n": True}},
        )

    def test_string_operator_operands_are_checked_at_load(self):
        for operator in ("glob", "contains", "matches"):
            with self.subTest(operator=operator), self.assertRaises(
                RulebookError
            ) as ctx:
                book([rule("r", [{"arguments": {"n": {operator: 7}}}], "deny")])
            self.assertIn("requires a string operand", str(ctx.exception))


class ResolutionTests(unittest.TestCase):
    def test_nested_arguments_expand_to_dotted_paths(self):
        rbook = book([
            rule("r", [{"arguments": {"options": {"path": {"contains": "secrets"}}}}], "deny"),
        ])
        predicate = rbook.rules[0].entries[0][0]
        self.assertEqual(predicate.path, "arguments.options.path")
        action = {"tool": "x", "arguments": {"options": {"path": "/etc/secrets"}}}
        self.assertEqual(check(rbook, action)["verdict"], "deny")

    def test_missing_or_non_object_arguments_are_missing_paths(self):
        rbook = book([rule("r", [{"arguments": {"command": {"contains": "rm"}}}], "deny")])
        for action in ({"tool": "x"}, {"tool": "x", "arguments": None}, {"tool": "x", "arguments": {}}):
            with self.subTest(action=action), self.assertRaises(MissingPathError):
                check(rbook, action)

    def test_non_object_situation_mid_path_is_a_missing_path(self):
        # {"situation.agent.name": …} against {"agent": "ci-bot"}: the walk
        # hits a string and must error, not treat it as absent
        rbook = book([rule("r", [{"situation.agent.name": "ops"}], "deny")])
        with self.assertRaises(MissingPathError) as ctx:
            check(rbook, {"tool": "x"}, {"agent": "ci-bot"})
        self.assertIn("situation.agent.name", str(ctx.exception))
        self.assertEqual(
            check(rbook, {"tool": "x"}, {"agent": {"name": "ops"}})["verdict"], "deny"
        )

    def test_unreachable_grammar_asserts(self):
        # defensive invariants: the predicate grammar is closed
        with self.assertRaises(AssertionError):
            _resolve("bogus", {}, {})
        with self.assertRaises(AssertionError):
            _satisfies(Predicate("tool", "bogus", 1), "x")

    def test_situation_path_part_that_is_a_substring_of_a_string(self):
        # "ci" is `in` "ci-bot"; a non-object node must still fail as a
        # missing path rather than being indexed
        rbook = book([rule("r", [{"situation.agent.ci": "bot"}], "deny")])
        with self.assertRaises(MissingPathError):
            check(rbook, {"tool": "x"}, {"agent": "ci-bot"})

    def test_strictest_consults_rulebooks_even_when_a_rule_already_denies(self):
        rbook = book([
            rule("deny-bash", [{"tool": "bash"}], "deny"),
            rule("needs-agent", [{"situation.agent": "ci-bot"}], "confirm"),
        ], precedence="strictest")
        # every rule is consulted, so the absent situation path is an error here
        with self.assertRaises(MissingPathError):
            check(rbook, ACTION)
        self.assertEqual(check(rbook, ACTION, {"agent": "ci-bot"})["verdict"], "deny")


class PredicateSemanticsTests(unittest.TestCase):
    def verdict_for(self, when, action, default="allow"):
        return check(book([rule("r", [when], "deny")], default=default), action)["verdict"]

    def test_glob_is_case_sensitive(self):
        self.assertEqual(self.verdict_for({"tool": {"glob": "bash*"}}, {"tool": "bash"}), "deny")
        self.assertEqual(self.verdict_for({"tool": {"glob": "bash*"}}, {"tool": "Bash"}), "allow")

    def test_contains_is_a_substring_test(self):
        self.assertEqual(
            self.verdict_for({"tool": {"contains": ""}}, {"tool": "anything"}), "deny"
        )
        self.assertEqual(
            self.verdict_for({"tool": {"contains": "sh"}}, {"tool": "bash"}), "deny"
        )
        self.assertEqual(
            self.verdict_for({"tool": {"contains": "sh"}}, {"tool": "read"}), "allow"
        )

    def test_matches_searches_anywhere_in_the_string(self):
        self.assertEqual(
            self.verdict_for({"tool": {"matches": "push"}}, {"tool": "git push --force"}), "deny"
        )
        self.assertEqual(
            self.verdict_for({"tool": {"matches": "^push$"}}, {"tool": "git push"}), "allow"
        )

    def test_in_is_membership(self):
        when = {"tool": {"in": ["bash", "zsh"]}}
        self.assertEqual(self.verdict_for(when, {"tool": "zsh"}), "deny")
        self.assertEqual(self.verdict_for(when, {"tool": "read"}), "allow")


class DataclassShapeTests(unittest.TestCase):
    def test_rulebook_and_rule_are_frozen_data(self):
        rbook = load_rulebook({"rules": [rule("r", [{"tool": "bash"}], "deny", "why")]})
        self.assertIsInstance(rbook, Rulebook)
        loaded = rbook.rules[0]
        self.assertIsInstance(loaded, Rule)
        self.assertEqual((loaded.id, loaded.verdict, loaded.reason), ("r", "deny", "why"))
        self.assertEqual(loaded.entries, ((Predicate("tool", "eq", "bash"),),))
        with self.assertRaises(Exception):
            loaded.verdict = "allow"
        with self.assertRaises(Exception):
            rbook.default = "deny"



class StrictestTieTests(unittest.TestCase):
    def test_every_tied_strictest_rule_is_a_winner(self):
        rbook = book([
            rule("d1", [{"tool": "x"}], "deny"),
            rule("a", [{"tool": "x"}], "allow"),
            rule("d2", [{"tool": "x"}], "deny"),
        ], precedence="strictest")
        result = check(rbook, {"tool": "x"})
        self.assertEqual(result["verdict"], "deny")
        self.assertEqual(
            [match["id"] for match in result["matched"] if match["winner"]],
            ["d1", "d2"],
        )


class GrammarEdgeCaseTests(unittest.TestCase):
    def verdict_for(self, when, action, situation=None):
        return check(book([rule("r", [when], "deny")]), action, situation)["verdict"]

    def test_in_membership_uses_type_strict_equality(self):
        numbers = {"situation.flag": {"in": [1]}}
        self.assertEqual(self.verdict_for(numbers, {"tool": "x"}, {"flag": True}), "allow")
        self.assertEqual(self.verdict_for(numbers, {"tool": "x"}, {"flag": 1}), "deny")
        self.assertEqual(self.verdict_for(numbers, {"tool": "x"}, {"flag": 1.0}), "allow")
        floats = {"situation.flag": {"in": [1.0]}}
        self.assertEqual(self.verdict_for(floats, {"tool": "x"}, {"flag": 1}), "allow")
        objects = {"situation.flag": {"in": [{"a": 1}]}}
        self.assertEqual(
            self.verdict_for(objects, {"tool": "x"}, {"flag": {"a": True}}), "allow"
        )
        self.assertEqual(
            self.verdict_for(
                {"situation.flag": {"in": [["a"]]}}, {"tool": "x"}, {"flag": ["a"]}
            ),
            "deny",
        )

    def test_empty_in_list_is_a_load_error(self):
        with self.assertRaises(RulebookError) as ctx:
            book([rule("r", [{"tool": {"in": []}}], "deny")])
        self.assertIn("non-empty list", str(ctx.exception))

    def test_rule_ids_must_be_unique(self):
        with self.assertRaises(RulebookError) as ctx:
            book([
                rule("dup", [{"tool": "a"}], "allow"),
                rule("dup", [{"tool": "b"}], "deny"),
            ])
        self.assertIn("duplicate rule id", str(ctx.exception))

    def test_empty_nested_arguments_object_is_a_load_error(self):
        for arguments in ({"options": {}}, {"a": {"b": {}}}):
            with self.subTest(arguments=arguments), self.assertRaises(
                RulebookError
            ) as ctx:
                book([rule("r", [{"arguments": arguments}], "deny")])
            self.assertIn("must not be empty", str(ctx.exception))

    def test_argument_names_must_be_single_path_segments(self):
        for name in ("a.b", "", "a..b"):
            with self.subTest(name=name), self.assertRaises(RulebookError) as ctx:
                book([rule("r", [{"arguments": {name: "v"}}], "deny")])
            self.assertIn("argument name", str(ctx.exception))

    def test_situation_paths_must_not_have_empty_segments(self):
        for path in ("situation..agent", "situation.agent.", "situation.a..b"):
            with self.subTest(path=path), self.assertRaises(RulebookError) as ctx:
                book([rule("r", [{path: "x"}], "deny")])
            self.assertIn("empty segments", str(ctx.exception))

    def test_bare_situation_key_is_not_a_predicate(self):
        with self.assertRaises(RulebookError):
            book([rule("r", [{"situation": "x"}], "deny")])

    def test_reason_defaults_to_empty_string(self):
        self.assertEqual(book([rule("r", [{"tool": "x"}], "deny")]).rules[0].reason, "")

    def test_tool_operands_must_be_string_compatible(self):
        for operand in (5, True, {"eq": 5}, {"in": [1, 2]}, {"glob": 5}):
            with self.subTest(operand=operand), self.assertRaises(RulebookError):
                book([rule("r", [{"tool": operand}], "deny")])

    def test_situation_equality_may_use_any_json_type(self):
        rbook = book([rule("r", [{"situation.total": 5}], "deny")])
        self.assertEqual(check(rbook, {"tool": "x"}, {"total": 5})["verdict"], "deny")
        self.assertEqual(check(rbook, {"tool": "x"}, {"total": "5"})["verdict"], "allow")

    def test_glob_character_classes_and_question_marks(self):
        classes = book([rule("r", [{"tool": {"glob": "file[0-9].txt"}}], "deny")])
        self.assertEqual(check(classes, {"tool": "file5.txt"})["verdict"], "deny")
        self.assertEqual(check(classes, {"tool": "fileX.txt"})["verdict"], "allow")
        single = book([rule("r", [{"tool": {"glob": "file?.txt"}}], "deny")])
        self.assertEqual(check(single, {"tool": "fileX.txt"})["verdict"], "deny")
        self.assertEqual(check(single, {"tool": "file10.txt"})["verdict"], "allow")

    def test_numeric_comparisons_mix_int_and_float(self):
        rbook = book([rule("r", [{"situation.n": {"gte": 1.5}}], "deny")])
        self.assertEqual(check(rbook, {"tool": "x"}, {"n": 2})["verdict"], "deny")
        self.assertEqual(check(rbook, {"tool": "x"}, {"n": 1.5})["verdict"], "deny")
        self.assertEqual(check(rbook, {"tool": "x"}, {"n": 1.49})["verdict"], "allow")
        strict = book([rule("r", [{"situation.n": {"gt": 1}}], "deny")])
        self.assertEqual(check(strict, {"tool": "x"}, {"n": 1.5})["verdict"], "deny")
        self.assertEqual(check(strict, {"tool": "x"}, {"n": 1})["verdict"], "allow")

    def test_in_with_mixed_types_uses_exact_membership(self):
        when = {"situation.flag": {"in": ["1", 1, True]}}
        self.assertEqual(self.verdict_for(when, {"tool": "x"}, {"flag": 1}), "deny")
        self.assertEqual(self.verdict_for(when, {"tool": "x"}, {"flag": True}), "deny")
        self.assertEqual(self.verdict_for(when, {"tool": "x"}, {"flag": "1"}), "deny")
        self.assertEqual(self.verdict_for(when, {"tool": "x"}, {"flag": 1.0}), "allow")

    def test_arguments_intermediate_list_is_a_missing_path(self):
        rbook = book([
            rule("r", [{"arguments": {"opts": {"path": {"contains": "x"}}}}], "deny"),
        ])
        with self.assertRaises(MissingPathError) as ctx:
            check(rbook, {"tool": "x", "arguments": {"opts": [{"path": "/x"}]}})
        self.assertIn("arguments.opts.path", str(ctx.exception))

if __name__ == "__main__":
    unittest.main()
