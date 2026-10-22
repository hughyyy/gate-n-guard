"""Unit tests for the gate-n-guard decision journal.

Offline, stdlib unittest only. Exercises append/write, read-back filters,
ordering, malformed-line tolerance, and validation errors.
"""

import json
import os
import tempfile
import unittest

from gate_n_guard.journal import JournalError, read_journal, record_decision


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "decisions.jsonl")

    def tearDown(self):
        self.dir.cleanup()

    def record(self, **overrides):
        params = {
            "action": {"tool": "deploy", "arguments": {"target": "prod"}},
            "verdict": "confirm",
            "matched": ["prod-deploy-needs-human"],
            "rulebook": "prod-rules",
            "outcome": "approved",
            "actor": "alice",
            "note": "signed off in review",
        }
        params.update(overrides)
        return record_decision(self.path, **params)

    def test_record_appends_and_returns_record(self):
        stored = self.record()
        self.assertTrue(stored["id"])
        self.assertTrue(stored["ts"].endswith("+00:00"))
        self.assertEqual(stored["verdict"], "confirm")
        self.assertEqual(stored["action"]["tool"], "deploy")
        with open(self.path, encoding="utf-8") as handle:
            lines = handle.readlines()
        self.assertEqual(len(lines), 1)
        parsed = json.loads(lines[0])
        self.assertEqual(parsed, stored)

    def test_record_appends_multiple_lines(self):
        self.record()
        self.record(action={"tool": "bash", "arguments": {"command": "ls"}}, verdict="allow")
        with open(self.path, encoding="utf-8") as handle:
            self.assertEqual(len(handle.readlines()), 2)

    def test_record_preserves_optional_omissions(self):
        stored = self.record(outcome=None, actor=None, note=None, matched=None, rulebook=None)
        self.assertNotIn("outcome", stored)
        self.assertNotIn("matched", stored)
        self.assertNotIn("rulebook", stored)

    def test_record_creates_parent_directories(self):
        nested = os.path.join(self.dir.name, "a", "b", "j.jsonl")
        stored = record_decision(nested, action={"tool": "x"}, verdict="allow")
        self.assertTrue(os.path.isfile(nested))
        self.assertEqual(stored["verdict"], "allow")

    def test_unicode_survives_round_trip(self):
        stored = self.record(note="批准 — «quote»")
        records = read_journal(self.path)["records"]
        self.assertEqual(records[0]["note"], "批准 — «quote»")

    def test_unconfigured_journal_errors(self):
        with self.assertRaises(JournalError):
            record_decision(None, action={"tool": "x"}, verdict="allow")
        with self.assertRaises(JournalError):
            read_journal(None)

    def test_invalid_fields_rejected(self):
        with self.assertRaises(JournalError):
            self.record(verdict="maybe")
        with self.assertRaises(JournalError):
            self.record(action={})
        with self.assertRaises(JournalError):
            self.record(matched=["ok", 3])
        with self.assertRaises(JournalError):
            self.record(note=42)


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "decisions.jsonl")

    def tearDown(self):
        self.dir.cleanup()

    def seed(self):
        # fixed timestamps: newest-first read order is read, bash, deploy
        rows = [
            {"id": "r3", "ts": "2026-10-22T10:00:03+00:00",
             "action": {"tool": "read", "arguments": {"path": "x"}},
             "verdict": "allow", "outcome": "executed", "actor": "bot"},
            {"id": "r2", "ts": "2026-10-22T10:00:02+00:00",
             "action": {"tool": "bash", "arguments": {"command": "rm -rf /"}},
             "verdict": "deny", "outcome": "aborted", "actor": "bot"},
            {"id": "r1", "ts": "2026-10-22T10:00:01+00:00",
             "action": {"tool": "deploy", "arguments": {"target": "prod"}},
             "verdict": "confirm", "outcome": "approved", "actor": "alice"},
        ]
        with open(self.path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")

    def test_missing_file_is_empty_not_error(self):
        result = read_journal(os.path.join(self.dir.name, "nope.jsonl"))
        self.assertEqual(result, {"count": 0, "skipped": 0, "records": []})

    def test_newest_first_default(self):
        self.seed()
        result = read_journal(self.path)
        self.assertEqual(result["count"], 3)
        tools = [r["action"]["tool"] for r in result["records"]]
        self.assertEqual(tools, ["read", "bash", "deploy"])

    def test_verdict_filter(self):
        self.seed()
        result = read_journal(self.path, verdict="deny")
        self.assertEqual([r["action"]["tool"] for r in result["records"]], ["bash"])

    def test_outcome_filter(self):
        self.seed()
        result = read_journal(self.path, outcome="approved")
        self.assertEqual([r["action"]["tool"] for r in result["records"]], ["deploy"])

    def test_tool_filter(self):
        self.seed()
        result = read_journal(self.path, tool="bash")
        self.assertEqual([r["action"]["tool"] for r in result["records"]], ["bash"])

    def test_limit(self):
        self.seed()
        result = read_journal(self.path, limit=2)
        self.assertEqual(result["count"], 2)
        self.assertEqual([r["action"]["tool"] for r in result["records"]], ["read", "bash"])

    def test_since_before_filters(self):
        self.seed()
        # since is inclusive, before is exclusive; a half-second bound sits
        # between the 10:00:02 and 10:00:03 records
        bound = "2026-10-22T10:00:02.5+00:00"
        after = read_journal(self.path, since=bound)["records"]
        self.assertEqual([r["action"]["tool"] for r in after], ["read"])
        before = read_journal(self.path, before=bound)["records"]
        self.assertEqual([r["action"]["tool"] for r in before], ["bash", "deploy"])

    def test_malformed_lines_skipped_and_counted(self):
        self.seed()
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write("not json\n{\"id\": \"x\"}\n[1, 2, 3]\n")
        result = read_journal(self.path)
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["skipped"], 3)

    def test_record_without_parseable_ts_skipped(self):
        self.seed()
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"id": "y", "ts": "yesterday", "verdict": "allow"}) + "\n")
        result = read_journal(self.path)
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["skipped"], 1)

    def test_combined_filters(self):
        self.seed()
        result = read_journal(self.path, tool="deploy", verdict="confirm", outcome="approved")
        self.assertEqual(result["count"], 1)

    def test_invalid_filter_params(self):
        with self.assertRaises(JournalError):
            read_journal(self.path, limit=0)
        with self.assertRaises(JournalError):
            read_journal(self.path, verdict="maybe")
        with self.assertRaises(JournalError):
            read_journal(self.path, since="not-a-time")
        with self.assertRaises(JournalError):
            read_journal(self.path, tool="")


if __name__ == "__main__":
    unittest.main()
