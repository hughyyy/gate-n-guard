"""Unit tests for the advise tool: deterministic apply, escalation,
prompt building, JSON extraction, and shape checks. The model path uses a
fake client — no network, stdlib unittest only."""

import unittest

from gate_n_guard.advise import AdviseError, advise, _extract_json
from gate_n_guard.rules import load_rulebook

RULES = [
    {"id": "no-rm-rf", "when": [{"tool": "bash", "arguments": {"command": {"contains": "rm -rf"}}}],
     "verdict": "deny", "reason": "no recursive deletes"},
    {"id": "prod-deploy", "when": [{"tool": {"glob": "deploy*"}, "situation.agent": "ci-bot"}],
     "verdict": "confirm", "reason": "prod deploys need a human"},
]

BOOK = load_rulebook({"default": "allow", "precedence": "first-match", "rules": RULES})
ACTION_RM = {"tool": "bash", "arguments": {"command": "rm -rf /tmp"}}
ACTION_READ = {"tool": "read", "arguments": {"path": "x"}}
SITUATION = {"agent": "ci-bot"}


class FakeClient:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def complete(self, messages, model=None, fallbacks=None):
        self.calls.append((messages, model, fallbacks))
        return self.text


class DeterministicApplyTests(unittest.TestCase):
    def test_auto_with_match_is_deterministic_and_never_calls_model(self):
        client = FakeClient("{}")
        result = advise(BOOK, ACTION_RM, SITUATION, question="apply", client=client)
        self.assertEqual(result["mode"], "deterministic")
        self.assertEqual(result["verdict"], "deny")
        self.assertEqual(result["applicable"], [
            {"id": "no-rm-rf", "verdict": "deny", "reason": "no recursive deletes"},
        ])
        self.assertFalse(result["default_applied"])
        self.assertEqual(client.calls, [])

    def test_auto_no_match_escalates_to_model(self):
        client = FakeClient(
            '{"summary": "nothing matches", "applicable": []}'
        )
        result = advise(BOOK, ACTION_READ, SITUATION, question="apply", client=client)
        self.assertEqual(result["mode"], "model")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(result["applicable"], [])

    def test_auto_no_match_without_client_errors_clearly(self):
        with self.assertRaises(AdviseError) as ctx:
            advise(BOOK, ACTION_READ, SITUATION, question="apply")
        self.assertIn("no advisory client", str(ctx.exception))

    def test_model_mode_forces_engine_even_with_match(self):
        client = FakeClient('{"summary": "s", "applicable": []}')
        result = advise(BOOK, ACTION_RM, SITUATION, question="apply", mode="model", client=client)
        self.assertEqual(result["mode"], "model")
        self.assertEqual(len(client.calls), 1)


class InterpretTests(unittest.TestCase):
    def test_unknown_rule_id_rejected_before_model_call(self):
        client = FakeClient("{}")
        with self.assertRaises(AdviseError):
            advise(BOOK, ACTION_READ, SITUATION, question="interpret", rule_id="nope", client=client)
        self.assertEqual(client.calls, [])

    def test_rule_id_invalid_for_apply(self):
        with self.assertRaises(AdviseError):
            advise(BOOK, ACTION_READ, SITUATION, question="apply", rule_id="no-rm-rf")

    def test_interpret_single_rule_shaped(self):
        client = FakeClient(
            '{"meaning": "blocks recursive deletes", "permits": ["safe rm"], '
            '"requires": [], "forbids": ["rm -rf"], "edge_cases": [], "confidence": 0.9}'
        )
        result = advise(BOOK, ACTION_READ, SITUATION, question="interpret",
                        rule_id="no-rm-rf", client=client)
        self.assertEqual(result["mode"], "model")
        self.assertEqual(result["rule_id"], "no-rm-rf")
        self.assertEqual(result["meaning"], "blocks recursive deletes")

    def test_interpret_whole_rulebook(self):
        client = FakeClient('{"interpretations": [{"rule_id": "no-rm-rf", "meaning": "x"}]}')
        result = advise(BOOK, ACTION_READ, SITUATION, question="interpret", client=client)
        self.assertEqual(len(result["interpretations"]), 1)

    def test_interpret_missing_meaning_rejected(self):
        client = FakeClient('{"permits": []}')
        with self.assertRaises(AdviseError):
            advise(BOOK, ACTION_READ, SITUATION, question="interpret",
                   rule_id="no-rm-rf", client=client)


class GapTests(unittest.TestCase):
    def test_gap_shaped(self):
        client = FakeClient(
            '{"coverage": "partial", "gaps": [{"concern": "no staging guard", '
            '"suggested_rule": "confirm staging deploys too", "rationale": "r"}]}'
        )
        result = advise(BOOK, {"tool": "deploy"}, {"agent": "dev"}, question="gap", client=client)
        self.assertEqual(result["coverage"], "partial")
        self.assertEqual(len(result["gaps"]), 1)

    def test_gap_bad_coverage_rejected(self):
        client = FakeClient('{"coverage": "sort of", "gaps": []}')
        with self.assertRaises(AdviseError):
            advise(BOOK, {"tool": "deploy"}, {}, question="gap", client=client)


class ParametersAndExtractionTests(unittest.TestCase):
    def test_invalid_question(self):
        with self.assertRaises(AdviseError):
            advise(BOOK, ACTION_READ, {}, question="vet")

    def test_invalid_mode(self):
        with self.assertRaises(AdviseError):
            advise(BOOK, ACTION_READ, {}, question="apply", mode="always")

    def test_extract_json_from_fenced_text(self):
        payload = _extract_json('Sure! Here you go:\n```json\n{"a": 1}\n```\nDone.')
        self.assertEqual(payload, {"a": 1})

    def test_extract_json_from_prose(self):
        payload = _extract_json('The answer is {"a": [1, 2]} for this rule.')
        self.assertEqual(payload, {"a": [1, 2]})

    def test_extract_json_rejects_garbage(self):
        with self.assertRaises(AdviseError):
            _extract_json("no json here at all")
        with self.assertRaises(AdviseError):
            _extract_json("")

    def test_prompt_carries_rulebook_and_question(self):
        client = FakeClient('{"summary": "s", "applicable": []}')
        advise(BOOK, ACTION_RM, SITUATION, question="apply", mode="model", client=client)
        messages = client.calls[0][0]
        self.assertEqual(messages[0]["role"], "system")
        user = messages[1]["content"]
        self.assertIn("no-rm-rf", user)
        self.assertIn('"verdict": "confirm"', user)
        self.assertIn("prod-deploy", user)

    def test_prompt_predicates_structured_not_reprs(self):
        client = FakeClient('{"summary": "s", "applicable": []}')
        advise(BOOK, ACTION_RM, SITUATION, question="apply", mode="model", client=client)
        user = client.calls[0][0][1]["content"]
        self.assertNotIn("Predicate(", user)
        self.assertNotIn("compiled=", user)
        self.assertIn('"path": "arguments.command"', user)
        self.assertIn('"operator": "contains"', user)

    def test_interpret_whole_rulebook_prompt_schema_complete(self):
        client = FakeClient('{"interpretations": []}')
        advise(BOOK, ACTION_READ, SITUATION, question="interpret", client=client)
        user = client.calls[0][0][1]["content"]
        self.assertIn('"forbids": [str]', user)
        self.assertIn('"edge_cases": [str]', user)
        self.assertNotIn("forbids,", user)


if __name__ == "__main__":
    unittest.main()
