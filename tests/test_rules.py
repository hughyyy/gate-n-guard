"""Unit tests for the gate-n-guard matcher.

Offline, stdlib unittest only. Covers both precedence modes, predicates,
validation errors, missing-path errors, and determinism.
"""

import unittest

from gate_n_guard.rules import (
    MissingPathError,
    RulebookError,
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


if __name__ == "__main__":
    unittest.main()
