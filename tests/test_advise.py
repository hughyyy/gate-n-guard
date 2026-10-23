"""Unit tests for the advise tool: deterministic apply, escalation,
prompt building, JSON extraction, and shape checks. The model path uses a
fake client — no network, stdlib unittest only."""

import unittest

from gate_n_guard.advise import AdviseError, advise, _extract_json
from gate_n_guard.rules import Rulebook, load_rulebook

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


class ExtractionEdgeTests(unittest.TestCase):
    def test_fenced_block_is_unwrapped(self):
        self.assertEqual(_extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(_extract_json('```\n{"a": 1}\n```'), {"a": 1})

    def test_invalid_fence_falls_back_to_the_whole_text(self):
        self.assertEqual(_extract_json('```json\nnot json\n```\n{"a": 2}'), {"a": 2})

    def test_prose_around_the_object_is_tolerated(self):
        self.assertEqual(_extract_json('Sure, here it is: {"a": 3} — done'), {"a": 3})

    def test_non_object_payloads_are_rejected(self):
        for text in ("[1, 2]", '"a string"', "42", "true", "null"):
            with self.subTest(text=text), self.assertRaises(AdviseError) as ctx:
                _extract_json(text)
            self.assertIn("not a JSON object", str(ctx.exception))

    def test_unbalanced_braces_are_rejected(self):
        # a '{' with no later '}' cannot be sliced into JSON
        for text in ('{"a": 1', 'prose {"a": 1', '```json\n{"a": 1\n```'):
            with self.subTest(text=text), self.assertRaises(AdviseError):
                _extract_json(text)


    def test_multiple_objects_are_not_merged_and_are_rejected(self):
        with self.assertRaises(AdviseError):
            _extract_json('{"a": 1} {"b": 2}')

    def test_empty_output_is_rejected(self):
        for text in ("", "   ", "\n", None, 42):
            with self.subTest(text=text), self.assertRaises(AdviseError) as ctx:
                _extract_json(text)
            self.assertIn("empty output", str(ctx.exception))


class ShapeValidationTests(unittest.TestCase):
    def test_interpret_single_rule_requires_meaning(self):
        client = FakeClient('{"permits": [], "confidence": 0.5}')
        with self.assertRaises(AdviseError) as ctx:
            advise(BOOK, ACTION_READ, SITUATION, question="interpret",
                   rule_id="no-rm-rf", client=client)
        self.assertIn("meaning", str(ctx.exception))

    def test_interpret_whole_rulebook_requires_interpretations_list(self):
        client = FakeClient('{"meaning": "one rule"}')
        with self.assertRaises(AdviseError) as ctx:
            advise(BOOK, ACTION_READ, SITUATION, question="interpret", client=client)
        self.assertIn("interpretations", str(ctx.exception))

    def test_apply_requires_summary_and_applicable(self):
        for text in ('{"applicable": []}', '{"summary": "s"}', '{"summary": 1, "applicable": []}'):
            with self.subTest(text=text):
                with self.assertRaises(AdviseError) as ctx:
                    advise(BOOK, ACTION_READ, SITUATION, question="apply", mode="model",
                           client=FakeClient(text))
                self.assertIn("summary", str(ctx.exception))

    def test_gap_requires_coverage_vocabulary_and_gaps_list(self):
        for text in ('{"gaps": []}', '{"coverage": "full", "gaps": []}',
                     '{"coverage": "partial", "gaps": "nope"}'):
            with self.subTest(text=text):
                with self.assertRaises(AdviseError) as ctx:
                    advise(BOOK, ACTION_READ, SITUATION, question="gap", client=FakeClient(text))
                self.assertIn("coverage", str(ctx.exception))

    def test_valid_gap_shape_is_echoed_with_model_mode(self):
        result = advise(BOOK, ACTION_READ, SITUATION, question="gap",
                        client=FakeClient('{"coverage": "none", "gaps": []}'))
        self.assertEqual(result, {"coverage": "none", "gaps": [], "mode": "model"})


class ClientRequirementTests(unittest.TestCase):
    def test_model_path_without_a_client_names_the_missing_configuration(self):
        with self.assertRaises(AdviseError) as ctx:
            advise(BOOK, ACTION_READ, SITUATION, question="gap")
        message = str(ctx.exception)
        self.assertIn("OPENROUTER_API_KEY", message)
        self.assertIn("HF_TOKEN", message)

    def test_apply_auto_without_client_falls_back_to_the_deterministic_path(self):
        # no match -> the auto path wants the model, and says so clearly
        with self.assertRaises(AdviseError):
            advise(BOOK, ACTION_READ, SITUATION, question="apply")



class ActionContractTests(unittest.TestCase):
    def test_apply_requires_an_action_object(self):
        client = FakeClient('{"summary": "s", "applicable": []}')
        for action in (None, {}, {"tool": ""}, {"tool": 1}, "bash"):
            with self.subTest(action=action), self.assertRaises(AdviseError) as ctx:
                advise(BOOK, action, SITUATION, question="apply",
                       mode="model", client=client)
            self.assertIn("requires an action", str(ctx.exception))
        self.assertEqual(client.calls, [])

    def test_gap_requires_an_action_object_before_any_model_call(self):
        client = FakeClient('{"coverage": "adequate", "gaps": []}')
        for action in (None, {}, "bash"):
            with self.subTest(action=action), self.assertRaises(AdviseError):
                advise(BOOK, action, SITUATION, question="gap", client=client)
        self.assertEqual(client.calls, [])

    def test_situation_must_be_an_object(self):
        for situation in ([], "prod", 7):
            with self.subTest(situation=situation), self.assertRaises(AdviseError):
                advise(BOOK, ACTION_READ, situation, question="interpret",
                       client=FakeClient("{}"))

    def test_non_serializable_context_is_an_advise_error(self):
        client = FakeClient('{"summary": "s", "applicable": []}')
        with self.assertRaises(AdviseError) as ctx:
            advise(
                BOOK,
                {"tool": "read", "arguments": {"bad": {1, 2}}},
                {},
                question="apply",
                mode="model",
                client=client,
            )
        self.assertIn("JSON-serializable", str(ctx.exception))
        self.assertEqual(client.calls, [])

    def test_deterministic_missing_fact_path_is_an_advise_error(self):
        with self.assertRaises(AdviseError) as ctx:
            advise(BOOK, {"tool": "deploy-prod"}, {}, question="apply")
        self.assertIn("situation.agent", str(ctx.exception))


class ShapeStrictnessTests(unittest.TestCase):
    def test_apply_answers_cannot_smuggle_a_verdict(self):
        client = FakeClient(
            '{"summary": "s", "applicable": [], "verdict": "allow", '
            '"mode": "deterministic"}'
        )
        result = advise(BOOK, ACTION_READ, SITUATION, question="apply",
                        mode="model", client=client)
        self.assertNotIn("verdict", result)
        self.assertEqual(result["mode"], "model")

    def test_gap_answers_cannot_smuggle_a_verdict(self):
        client = FakeClient(
            '{"coverage": "none", "gaps": [], "verdict": "allow"}'
        )
        result = advise(BOOK, ACTION_READ, SITUATION, question="gap", client=client)
        self.assertNotIn("verdict", result)
        self.assertEqual(result["mode"], "model")

    def test_nested_answer_entries_must_be_objects(self):
        cases = (
            ("interpret", '{"interpretations": ["hi"]}'),
            ("apply", '{"summary": "s", "applicable": ["x"]}'),
            ("gap", '{"coverage": "adequate", "gaps": ["x"]}'),
        )
        for question, text in cases:
            with self.subTest(question=question), self.assertRaises(AdviseError):
                advise(BOOK, ACTION_READ, SITUATION, question=question,
                       mode="model", client=FakeClient(text))

    def test_confidence_must_be_a_number_in_range(self):
        for text in (
            '{"meaning": "m", "confidence": 5}',
            '{"meaning": "m", "confidence": -0.1}',
            '{"meaning": "m", "confidence": true}',
            '{"meaning": "m", "confidence": "0.9"}',
        ):
            with self.subTest(text=text), self.assertRaises(AdviseError):
                advise(BOOK, ACTION_READ, SITUATION, question="interpret",
                       rule_id="no-rm-rf", client=FakeClient(text))

    def test_confidence_inside_whole_rulebook_entries_is_checked(self):
        client = FakeClient(
            '{"interpretations": [{"rule_id": "no-rm-rf", "meaning": "m", '
            '"confidence": 2}]}'
        )
        with self.assertRaises(AdviseError):
            advise(BOOK, ACTION_READ, SITUATION, question="interpret", client=client)

    def test_model_and_fallbacks_are_forwarded(self):
        client = FakeClient('{"summary": "s", "applicable": []}')
        advise(BOOK, ACTION_READ, SITUATION, question="apply", mode="model",
               client=client, model="openrouter:zz",
               fallbacks=("openrouter:yy",))
        self.assertEqual(
            client.calls[0][1:], ("openrouter:zz", ("openrouter:yy",))
        )

    def test_empty_rulebook_escalates_to_the_model(self):
        empty = Rulebook(default="allow", precedence="first-match", rules=())
        client = FakeClient('{"summary": "s", "applicable": []}')
        result = advise(empty, ACTION_READ, SITUATION, question="apply",
                        client=client)
        self.assertEqual(result["mode"], "model")
        self.assertEqual(len(client.calls), 1)


class ExtractionBraceTests(unittest.TestCase):
    def test_prose_braces_before_the_answer_are_tolerated(self):
        self.assertEqual(
            _extract_json('Setting {foo} to bar. The answer is {"a": 1}'),
            {"a": 1},
        )

    def test_trailing_brace_after_the_answer_is_tolerated(self):
        self.assertEqual(_extract_json('{"a": 1} trailing }'), {"a": 1})

    def test_brace_inside_a_json_string_is_not_a_boundary(self):
        self.assertEqual(_extract_json('{"a": "}"} then }'), {"a": "}"})

    def test_brace_in_prose_after_a_fenced_answer_is_tolerated(self):
        self.assertEqual(
            _extract_json('```json\n{"a": 2}\n```\nuse {this} later'), {"a": 2}
        )

    def test_two_parseable_objects_are_still_rejected(self):
        with self.assertRaises(AdviseError) as ctx:
            _extract_json('{"a": 1} and {"b": 2}')
        self.assertIn("multiple JSON objects", str(ctx.exception))


class NestedShapeTests(unittest.TestCase):
    def test_interpret_list_fields_must_be_string_lists(self):
        for key in ("permits", "requires", "forbids", "edge_cases"):
            with self.subTest(key=key), self.assertRaises(AdviseError):
                advise(
                    BOOK, ACTION_READ, SITUATION, question="interpret",
                    rule_id="no-rm-rf",
                    client=FakeClient('{"meaning": "m", "%s": [1]}' % key),
                )

    def test_whole_rulebook_entries_need_rule_id_and_meaning(self):
        for entry in ('{"meaning": "m"}', '{"rule_id": "no-rm-rf"}',
                      '{"rule_id": "", "meaning": "m"}'):
            with self.subTest(entry=entry), self.assertRaises(AdviseError):
                advise(
                    BOOK, ACTION_READ, SITUATION, question="interpret",
                    client=FakeClient('{"interpretations": [%s]}' % entry),
                )

    def test_whole_rulebook_entries_are_key_whitelisted(self):
        client = FakeClient(
            '{"interpretations": [{"rule_id": "no-rm-rf", "meaning": "m", '
            '"permits": ["safe"], "verdict": "allow", "mode": "deterministic"}]}'
        )
        result = advise(BOOK, ACTION_READ, SITUATION, question="interpret",
                        client=client)
        entry = result["interpretations"][0]
        self.assertEqual(entry["permits"], ["safe"])
        self.assertNotIn("verdict", entry)
        self.assertNotIn("mode", entry)

    def test_apply_entries_need_rule_id_relevance_and_reading(self):
        for entry in ('{}', '{"rule_id": "no-rm-rf"}',
                      '{"rule_id": "no-rm-rf", "reading": "x"}',
                      '{"rule_id": "no-rm-rf", "relevance": "r"}',
                      '{"rule_id": 5, "relevance": "r", "reading": "x"}'):
            with self.subTest(entry=entry), self.assertRaises(AdviseError):
                advise(
                    BOOK, ACTION_READ, SITUATION, question="apply", mode="model",
                    client=FakeClient('{"summary": "s", "applicable": [%s]}' % entry),
                )

    def test_apply_entry_caveats_must_be_strings(self):
        with self.assertRaises(AdviseError):
            advise(
                BOOK, ACTION_READ, SITUATION, question="apply", mode="model",
                client=FakeClient(
                    '{"summary": "s", "applicable": [{"rule_id": "a", '
                    '"relevance": "r", "reading": "x", "caveats": [1]}]}'
                ),
            )

    def test_notable_missing_must_be_a_string_list(self):
        with self.assertRaises(AdviseError):
            advise(
                BOOK, ACTION_READ, SITUATION, question="apply", mode="model",
                client=FakeClient(
                    '{"summary": "s", "applicable": [], "notable_missing": "x"}'
                ),
            )

    def test_gap_entries_need_all_three_string_fields(self):
        for entry in ('{}', '{"concern": "c"}',
                      '{"concern": "c", "suggested_rule": 5, "rationale": "r"}'):
            with self.subTest(entry=entry), self.assertRaises(AdviseError):
                advise(
                    BOOK, ACTION_READ, SITUATION, question="gap",
                    client=FakeClient(
                        '{"coverage": "partial", "gaps": [%s]}' % entry
                    ),
                )

    def test_valid_nested_entries_are_echoed_and_sanitized(self):
        result = advise(
            BOOK, ACTION_READ, SITUATION, question="apply", mode="model",
            client=FakeClient(
                '{"summary": "s", "applicable": [{"rule_id": "no-rm-rf", '
                '"relevance": "high", "reading": "blocks rm", "caveats": ["c"], '
                '"verdict": "allow"}], "notable_missing": ["gap"]}'
            ),
        )
        self.assertEqual(result["applicable"], [{
            "rule_id": "no-rm-rf", "relevance": "high",
            "reading": "blocks rm", "caveats": ["c"],
        }])
        self.assertEqual(result["notable_missing"], ["gap"])

if __name__ == "__main__":
    unittest.main()
