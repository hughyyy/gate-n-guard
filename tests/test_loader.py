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


class LoaderEdgeCaseTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "rules.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_rules_of_the_wrong_type_name_the_type(self):
        for value in (42, 3.5, ["rules"], True):
            with self.subTest(value=value), self.assertRaises(RulebookSourceError) as ctx:
                resolve_rulebook(value)
            self.assertIn(type(value).__name__, str(ctx.exception))

    def test_ref_to_a_directory_is_not_a_file(self):
        with self.assertRaises(RulebookSourceError) as ctx:
            resolve_rulebook(f"file:{self.dir.name}")
        self.assertIn("not found", str(ctx.exception))

    @unittest.skipIf(os.geteuid() == 0, "root ignores file permissions")
    def test_unreadable_file_reports_eacces(self):
        self.write_rules()
        os.chmod(self.path, 0o000)
        try:
            with self.assertRaises(RulebookSourceError) as ctx:
                resolve_rulebook(f"file:{self.path}")
        finally:
            os.chmod(self.path, 0o600)
        self.assertIn("unreadable", str(ctx.exception))

    def test_invalid_json_is_a_source_error(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertRaises(RulebookSourceError) as ctx:
            resolve_rulebook(f"file:{self.path}")
        self.assertIn("not valid JSON", str(ctx.exception))

    def test_empty_file_is_a_source_error(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("")
        with self.assertRaises(RulebookSourceError):
            resolve_rulebook(f"file:{self.path}")

    def test_default_path_missing_file_is_a_source_error(self):
        missing = os.path.join(self.dir.name, "absent.json")
        with self.assertRaises(RulebookSourceError) as ctx:
            resolve_rulebook(None, default_path=missing)
        self.assertIn("not found", str(ctx.exception))

    def test_default_path_is_used_when_rules_are_omitted(self):
        self.write_rules()
        rulebook, ref = resolve_rulebook(None, default_path=self.path)
        self.assertEqual([r.id for r in rulebook.rules], ["a"])
        self.assertEqual(ref["source"], f"file:{self.path}")

    def write_rules(self, rules=RULES):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(rules, handle)


class AttachRefTests(unittest.TestCase):
    def test_attach_ref_returns_a_copy(self):
        response = {"verdict": "allow"}
        ref = {"source": "inline", "sha256": "abc"}
        attached = attach_ref(response, ref)
        self.assertEqual(attached, {"verdict": "allow", "rulebook_ref": ref})
        self.assertNotIn("rulebook_ref", response)
        attached["rulebook_ref"]["sha256"] = "mutated"
        self.assertEqual(ref["sha256"], "abc")

    def test_attach_ref_overwrites_a_stale_ref(self):
        attached = attach_ref(
            {"verdict": "allow", "rulebook_ref": {"source": "old"}},
            {"source": "new", "sha256": "x"},
        )
        self.assertEqual(attached["rulebook_ref"]["source"], "new")



class LoaderRobustnessTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "rules.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_invalid_utf8_file_is_a_source_error(self):
        with open(self.path, "wb") as handle:
            handle.write(b'{"default": "allow", "rules": []}\xff')
        with self.assertRaises(RulebookSourceError) as ctx:
            resolve_rulebook(f"file:{self.path}")
        self.assertIn("not valid UTF-8", str(ctx.exception))
        self.assertIn(self.path, str(ctx.exception))

    def test_file_hash_covers_the_bytes_on_disk(self):
        content = (
            b'{\r\n  "default": "allow",\r\n'
            b'  "rules": [{"id": "a", "when": [{"tool": "git"}], "verdict": "deny"}]\r\n}\r\n'
        )
        with open(self.path, "wb") as handle:
            handle.write(content)
        _, ref = resolve_rulebook(f"file:{self.path}")
        self.assertEqual(ref["sha256"], hashlib.sha256(content).hexdigest())

    def test_tilde_ref_expands_home(self):
        previous = os.environ.get("HOME")
        try:
            with tempfile.TemporaryDirectory() as home:
                os.environ["HOME"] = home
                target = os.path.join(home, "rules.json")
                with open(target, "w", encoding="utf-8") as handle:
                    json.dump(RULES, handle)
                rulebook, ref = resolve_rulebook("file:~/rules.json")
                self.assertEqual(ref["source"], "file:~/rules.json")
                self.assertEqual(rulebook.rules[0].id, "a")
        finally:
            if previous is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = previous

    def test_empty_file_ref_is_rejected(self):
        with self.assertRaises(RulebookSourceError) as ctx:
            resolve_rulebook("file:")
        self.assertIn("non-empty path", str(ctx.exception))

    def test_inline_non_serializable_extra_is_a_source_error(self):
        with self.assertRaises(RulebookSourceError) as ctx:
            resolve_rulebook({**RULES, "extra": object()})
        self.assertIn("JSON-serializable", str(ctx.exception))


class LoaderSourcePrecedenceTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "rules.json")
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(RULES, handle)

    def tearDown(self):
        self.dir.cleanup()

    def test_empty_inline_object_errors_and_never_uses_the_default(self):
        with self.assertRaises(RulebookError) as ctx:
            resolve_rulebook({}, default_path=self.path)
        self.assertIn("rules list", str(ctx.exception))

    def test_inline_list_does_not_fall_back_to_the_default(self):
        with self.assertRaises(RulebookSourceError) as ctx:
            resolve_rulebook([], default_path=self.path)
        self.assertIn("list", str(ctx.exception))

    def test_bom_prefixed_file_is_a_source_error(self):
        bom = os.path.join(self.dir.name, "bom.json")
        content = json.dumps(RULES).encode("utf-8")
        with open(bom, "wb") as handle:
            handle.write(b"\xef\xbb\xbf" + content)
        with self.assertRaises(RulebookSourceError) as ctx:
            resolve_rulebook(f"file:{bom}")
        self.assertIn("not valid JSON", str(ctx.exception))

    def test_attach_ref_copies_ref_keys_without_mutation(self):
        ref = {"source": "inline", "sha256": "abc", "extra": "kept"}
        attached = attach_ref({"verdict": "allow"}, ref)
        self.assertEqual(attached["rulebook_ref"], ref)
        attached["rulebook_ref"]["extra"] = "changed"
        self.assertEqual(ref["extra"], "kept")

if __name__ == "__main__":
    unittest.main()
