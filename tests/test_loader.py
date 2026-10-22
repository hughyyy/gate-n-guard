"""Unit tests for rulebook source resolution: inline, file refs, env
default, ref hashing, and loud failures. Offline, stdlib unittest only."""

import hashlib
import json
import os
import tempfile
import unittest

from gate_n_guard.loader import RulebookSourceError, attach_ref, resolve_rulebook
from gate_n_guard.rules import RulebookError

RULES = {"default": "allow", "rules": [{"id": "a", "when": [{"tool": "git"}], "verdict": "deny"}]}
RULES_TWO = {"default": "allow", "rules": [
    {"id": "a", "when": [{"tool": "git"}], "verdict": "deny"},
    {"id": "b", "when": [{"tool": "bash"}], "verdict": "confirm"},
]}


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "rules.json")

    def tearDown(self):
        self.dir.cleanup()

    def write_rules(self, rules=RULES):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(rules, handle)

    def test_inline_dict(self):
        rulebook, ref = resolve_rulebook(RULES)
        self.assertEqual([r.id for r in rulebook.rules], ["a"])
        self.assertEqual(ref["source"], "inline")
        self.assertEqual(len(ref["sha256"]), 64)

    def test_inline_hash_is_canonical(self):
        _, ref_a = resolve_rulebook(RULES)
        _, ref_b = resolve_rulebook(json.loads(json.dumps(RULES)))  # same content
        self.assertEqual(ref_a["sha256"], ref_b["sha256"])
        _, ref_c = resolve_rulebook(RULES_TWO)
        self.assertNotEqual(ref_a["sha256"], ref_c["sha256"])

    def test_file_ref(self):
        self.write_rules()
        rulebook, ref = resolve_rulebook(f"file:{self.path}")
        self.assertEqual([r.id for r in rulebook.rules], ["a"])
        self.assertEqual(ref["source"], f"file:{self.path}")
        with open(self.path, "rb") as handle:
            self.assertEqual(ref["sha256"], hashlib.sha256(handle.read()).hexdigest())

    def test_inline_and_file_equal_rulebooks(self):
        self.write_rules()
        inline_book, _ = resolve_rulebook(RULES)
        file_book, _ = resolve_rulebook(f"file:{self.path}")
        self.assertEqual(inline_book, file_book)

    def test_default_path_fallback(self):
        self.write_rules()
        rulebook, ref = resolve_rulebook(None, default_path=self.path)
        self.assertEqual([r.id for r in rulebook.rules], ["a"])
        self.assertIn(self.path, ref["source"])

    def test_param_wins_over_default(self):
        # default path is bogus: param must win without touching it
        rulebook, ref = resolve_rulebook(RULES, default_path="/nonexistent/x.json")
        self.assertEqual([r.id for r in rulebook.rules], ["a"])
        self.assertEqual(ref["source"], "inline")

    def test_no_source_errors(self):
        with self.assertRaises(RulebookSourceError) as ctx:
            resolve_rulebook(None)
        self.assertIn("GATE_N_GUARD_RULEBOOK", str(ctx.exception))

    def test_missing_file_errors(self):
        with self.assertRaises(RulebookSourceError) as ctx:
            resolve_rulebook("file:/no/such/rules.json")
        self.assertIn("not found", str(ctx.exception))

    def test_default_missing_file_errors(self):
        with self.assertRaises(RulebookSourceError):
            resolve_rulebook(None, default_path="/no/such/rules.json")

    def test_bad_json_file_errors_naming_path(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertRaises(RulebookSourceError) as ctx:
            resolve_rulebook(f"file:{self.path}")
        message = str(ctx.exception)
        self.assertIn("not valid JSON", message)
        self.assertIn(self.path, message)

    def test_non_object_json_errors(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("[1, 2]")
        with self.assertRaises(RulebookSourceError):
            resolve_rulebook(f"file:{self.path}")

    def test_unknown_string_param_errors(self):
        with self.assertRaises(RulebookSourceError):
            resolve_rulebook("just-some-string")

    def test_bad_param_type_errors(self):
        with self.assertRaises(RulebookSourceError):
            resolve_rulebook(42)

    def test_malformed_rulebook_passes_through(self):
        with self.assertRaises(RulebookError):
            resolve_rulebook({"default": "maybe", "rules": []})

    def test_file_hash_tracks_content(self):
        self.write_rules(RULES)
        _, ref_a = resolve_rulebook(f"file:{self.path}")
        self.write_rules(RULES_TWO)
        _, ref_b = resolve_rulebook(f"file:{self.path}")
        self.assertNotEqual(ref_a["sha256"], ref_b["sha256"])

    def test_attach_ref_adds_without_mutating(self):
        envelope = {"verdict": "deny", "trace": []}
        ref = {"source": "inline", "sha256": "ab" * 32}
        result = attach_ref(envelope, ref)
        self.assertEqual(envelope.keys(), {"verdict", "trace"})
        self.assertEqual(result["rulebook_ref"], ref)
        self.assertEqual(result["verdict"], "deny")


if __name__ == "__main__":
    unittest.main()
