"""Unit tests for the gate-n-guard decision journal.

Offline, stdlib unittest only. Exercises append/write, read-back filters,
ordering, malformed-line tolerance, and validation errors.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from gate_n_guard import journal as journal_mod
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


class RecordValidationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "decisions.jsonl")

    def tearDown(self):
        self.dir.cleanup()

    def test_action_must_have_a_non_empty_string_tool(self):
        for action in ({}, {"tool": ""}, {"tool": 1}, {"arguments": {}}, "bash", None, ["bash"]):
            with self.subTest(action=action), self.assertRaises(JournalError) as ctx:
                record_decision(self.path, action=action, verdict="allow")
            self.assertIn("action", str(ctx.exception))

    def test_verdict_must_be_known(self):
        for verdict in ("maybe", "", None, "ALLOW", 1):
            with self.subTest(verdict=verdict), self.assertRaises(JournalError):
                record_decision(self.path, action={"tool": "bash"}, verdict=verdict)

    def test_matched_must_be_a_list_of_non_empty_strings(self):
        for matched in ("no-rm", ["ok", ""], ["ok", 2], [None], {}, 1):
            with self.subTest(matched=matched), self.assertRaises(JournalError) as ctx:
                record_decision(
                    self.path, action={"tool": "bash"}, verdict="deny", matched=matched
                )
            self.assertIn("matched", str(ctx.exception))

    def test_optional_fields_must_be_strings_when_present(self):
        for name in ("rulebook", "outcome", "actor", "note"):
            with self.subTest(field=name), self.assertRaises(JournalError) as ctx:
                record_decision(
                    self.path, action={"tool": "bash"}, verdict="allow", **{name: 5}
                )
            self.assertIn(name, str(ctx.exception))

    def test_rejected_records_write_nothing(self):
        with self.assertRaises(JournalError):
            record_decision(self.path, action={"tool": ""}, verdict="allow")
        self.assertFalse(os.path.exists(self.path))

    def test_unconfigured_journal_errors_for_both_tools(self):
        with self.assertRaises(JournalError) as ctx:
            record_decision(None, action={"tool": "bash"}, verdict="allow")
        self.assertIn("GATE_N_GUARD_JOURNAL", str(ctx.exception))
        with self.assertRaises(JournalError) as ctx:
            read_journal(None)
        self.assertIn("GATE_N_GUARD_JOURNAL", str(ctx.exception))


class AuditValidationTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "decisions.jsonl")
        self.write_lines(
            {"id": "1", "ts": "2026-01-01T00:00:00+00:00", "verdict": "allow",
             "action": {"tool": "read"}},
            {"id": "2", "ts": "2026-01-02T00:00:00+00:00", "verdict": "deny",
             "action": {"tool": "bash"}},
            {"id": "3", "ts": "2026-01-03T00:00:00+00:00", "verdict": "confirm",
             "action": {"tool": "deploy"}},
        )

    def tearDown(self):
        self.dir.cleanup()

    def write_lines(self, *records):
        with open(self.path, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    def test_limit_must_be_a_positive_integer(self):
        for limit in (0, -1, True, "5", 1.5, None):
            with self.subTest(limit=limit), self.assertRaises(JournalError) as ctx:
                read_journal(self.path, limit=limit)
            self.assertIn("limit", str(ctx.exception))

    def test_oversized_limit_is_capped_not_rejected(self):
        # lower the cap so the assertion does not need 1000+ records
        with mock.patch.object(journal_mod, "MAX_AUDIT_LIMIT", 2):
            result = read_journal(self.path, limit=10**9)
        self.assertEqual(result["count"], 2)

    def test_verdict_filter_must_be_known(self):
        with self.assertRaises(JournalError):
            read_journal(self.path, verdict="maybe")

    def test_filter_strings_must_be_non_empty(self):
        for field in ("tool", "outcome"):
            for value in ("", 1, []):
                with self.subTest(field=field, value=value), self.assertRaises(JournalError):
                    read_journal(self.path, **{field: value})

    def test_time_filters_must_be_parseable_iso_strings(self):
        for value in (1, "", "not-a-time", "2026-13-40T00:00:00+00:00", "2026-01-01"):
            with self.subTest(value=value), self.assertRaises(JournalError):
                read_journal(self.path, since=value)

    def test_since_is_inclusive_and_before_is_exclusive(self):
        result = read_journal(self.path, since="2026-01-02T00:00:00+00:00",
                              before="2026-01-03T00:00:00+00:00")
        self.assertEqual([r["id"] for r in result["records"]], ["2"])

    def test_z_suffixed_timestamps_are_accepted(self):
        self.write_lines(
            {"id": "z", "ts": "2026-02-01T12:00:00Z", "verdict": "allow",
             "action": {"tool": "read"}},
        )
        self.assertEqual(read_journal(self.path)["records"][0]["id"], "z")


class MalformedLineTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "decisions.jsonl")

    def tearDown(self):
        self.dir.cleanup()

    def write_raw(self, text):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_blank_lines_are_skipped_and_counted(self):
        self.write_raw(
            '{"id": "1", "ts": "2026-01-01T00:00:00+00:00", "verdict": "allow"}\n'
            "\n"
            "   \n"
        )
        result = read_journal(self.path)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["skipped"], 2)

    def test_non_object_json_is_skipped(self):
        self.write_raw('[]\n"a string"\n12\nnull\n')
        self.assertEqual(read_journal(self.path)["skipped"], 4)

    def test_records_missing_required_keys_are_skipped(self):
        self.write_raw(
            '{"ts": "2026-01-01T00:00:00+00:00", "verdict": "allow"}\n'
            '{"id": "2", "verdict": "allow"}\n'
            '{"id": "3", "ts": "2026-01-01T00:00:00+00:00"}\n'
        )
        result = read_journal(self.path)
        self.assertEqual((result["count"], result["skipped"]), (0, 3))

    def test_unparseable_or_naive_timestamps_are_skipped(self):
        self.write_raw(
            '{"id": "1", "ts": 12, "verdict": "allow"}\n'
            '{"id": "2", "ts": "2026-01-01T00:00:00", "verdict": "allow"}\n'
            '{"id": "3", "ts": "nonsense", "verdict": "allow"}\n'
        )
        result = read_journal(self.path)
        self.assertEqual((result["count"], result["skipped"]), (0, 3))

    def test_tool_filter_skips_records_without_an_action_object(self):
        self.write_raw(
            '{"id": "1", "ts": "2026-01-01T00:00:00+00:00", "verdict": "allow", "action": "bash"}\n'
            '{"id": "2", "ts": "2026-01-02T00:00:00+00:00", "verdict": "allow", "action": {"tool": "bash"}}\n'
        )
        result = read_journal(self.path, tool="bash")
        self.assertEqual([r["id"] for r in result["records"]], ["2"])

    def test_missing_file_is_an_empty_journal(self):
        result = read_journal(os.path.join(self.dir.name, "never-written.jsonl"))
        self.assertEqual(result, {"count": 0, "skipped": 0, "records": []})

    def test_equal_timestamps_produce_a_deterministic_order(self):
        self.write_raw(
            '{"id": "a", "ts": "2026-01-01T00:00:00+00:00", "verdict": "allow"}\n'
            '{"id": "b", "ts": "2026-01-01T00:00:00+00:00", "verdict": "deny"}\n'
        )
        first = [r["id"] for r in read_journal(self.path)["records"]]
        second = [r["id"] for r in read_journal(self.path)["records"]]
        self.assertEqual(first, second)
        self.assertEqual(sorted(first), ["a", "b"])



class JournalRobustnessTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "decisions.jsonl")

    def tearDown(self):
        self.dir.cleanup()

    def write_bytes(self, payload):
        with open(self.path, "wb") as handle:
            handle.write(payload)

    def test_invalid_utf8_line_is_skipped_and_counted(self):
        self.write_bytes(
            b'{"id": "a", "ts": "2026-01-01T00:00:00+00:00", "verdict": "allow"}\n'
            b'\xff\xfe not json\n'
            b'{"id": "b", "ts": "2026-01-02T00:00:00+00:00", "verdict": "deny"}\n'
        )
        result = read_journal(self.path)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual([r["id"] for r in result["records"]], ["b", "a"])

    def test_directory_path_is_rejected_for_read_and_record(self):
        with self.assertRaises(JournalError) as ctx:
            read_journal(self.dir.name)
        self.assertIn("not a file", str(ctx.exception))
        with self.assertRaises(JournalError):
            record_decision(self.dir.name, action={"tool": "x"}, verdict="allow")

    @unittest.skipIf(os.geteuid() == 0, "root ignores file permissions")
    def test_unreadable_journal_is_a_journal_error(self):
        with open(self.path, "w", encoding="utf-8"):
            pass
        os.chmod(self.path, 0o000)
        try:
            with self.assertRaises(JournalError) as ctx:
                read_journal(self.path)
            self.assertIn("read failed", str(ctx.exception))
        finally:
            os.chmod(self.path, 0o600)

    def test_non_serializable_fields_are_rejected_before_writing(self):
        with self.assertRaises(JournalError) as ctx:
            record_decision(
                self.path,
                action={"tool": "x", "arguments": {"payload": object()}},
                verdict="allow",
            )
        self.assertIn("JSON-serializable", str(ctx.exception))
        self.assertFalse(os.path.exists(self.path))

    def test_private_ts_field_round_trips(self):
        self.write_bytes(
            b'{"id": "a", "ts": "2026-01-01T00:00:00+00:00", '
            b'"verdict": "allow", "__ts": "mine"}\n'
        )
        self.assertEqual(read_journal(self.path)["records"][0]["__ts"], "mine")

    def test_extra_fields_pass_through(self):
        row = {"id": "a", "ts": "2026-01-01T00:00:00+00:00",
               "verdict": "allow", "custom": {"k": 1}}
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
        self.assertEqual(read_journal(self.path)["records"][0], row)

    def test_bom_prefixed_line_is_skipped_not_fatal(self):
        self.write_bytes(
            b'\xef\xbb\xbf{"id": "a", "ts": "2026-01-01T00:00:00+00:00", "verdict": "allow"}\n'
            b'{"id": "b", "ts": "2026-01-02T00:00:00+00:00", "verdict": "deny"}\n'
        )
        result = read_journal(self.path)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["records"][0]["id"], "b")

    def test_equal_instants_with_different_offsets_order_deterministically(self):
        rows = [
            {"id": "a", "ts": "2026-01-01T10:00:00Z", "verdict": "allow"},
            {"id": "b", "ts": "2026-01-01T05:00:00-05:00", "verdict": "allow"},
            {"id": "c", "ts": "2026-01-01T10:00:00+00:00", "verdict": "deny"},
        ]
        with open(self.path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        first = [r["id"] for r in read_journal(self.path)["records"]]
        second = [r["id"] for r in read_journal(self.path)["records"]]
        self.assertEqual(first, second)
        self.assertEqual(first, ["a", "c", "b"])

    def test_non_string_paths_are_journal_errors(self):
        for path in (42, ["x"], {"path": "x"}):
            with self.subTest(path=path):
                with self.assertRaises(JournalError):
                    read_journal(path)
                with self.assertRaises(JournalError):
                    record_decision(path, action={"tool": "x"}, verdict="allow")

    def test_empty_matched_list_is_accepted(self):
        stored = record_decision(
            self.path, action={"tool": "x"}, verdict="deny", matched=[]
        )
        self.assertEqual(stored["matched"], [])

    def test_non_string_verdict_in_the_file_is_returned_verbatim(self):
        self.write_bytes(
            b'{"id": "a", "ts": "2026-01-01T00:00:00+00:00", "verdict": 5}\n'
        )
        self.assertEqual(read_journal(self.path)["records"][0]["verdict"], 5)
        self.assertEqual(read_journal(self.path, verdict="allow")["count"], 0)

    def test_tilde_path_expands_home(self):
        previous_home = os.environ.get("HOME")
        previous_cwd = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as home:
                os.environ["HOME"] = home
                os.chdir(self.dir.name)
                record_decision(
                    "~/decisions.jsonl", action={"tool": "x"}, verdict="allow"
                )
                self.assertTrue(
                    os.path.isfile(os.path.join(home, "decisions.jsonl"))
                )
                self.assertFalse(os.path.exists(os.path.join(self.dir.name, "~")))
        finally:
            os.chdir(previous_cwd)
            if previous_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = previous_home


if __name__ == "__main__":
    unittest.main()
