"""Server wiring tests: handler logic, tool registration, and smoke
calls through the MCPServer with injected fakes. Offline, stdlib only."""

import asyncio
import json
import os
import tempfile
import unittest
from typing import get_args
from unittest import mock

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from gate_n_guard import __version__
from gate_n_guard import config as config_mod
from gate_n_guard import journal as journal_mod
from gate_n_guard.server import GateHandler, _Verdict, create_server, main

RULES = {"default": "allow", "rules": [
    {"id": "no-rm", "when": [{"tool": "bash", "arguments": {"command": {"contains": "rm -rf"}}}],
     "verdict": "deny", "reason": "no recursive deletes"},
]}


class ConfigTests(unittest.TestCase):
    def test_env_paths_read(self):
        self.addCleanup(os.environ.pop, "GATE_N_GUARD_RULEBOOK", None)
        self.addCleanup(os.environ.pop, "GATE_N_GUARD_JOURNAL", None)
        os.environ["GATE_N_GUARD_RULEBOOK"] = "/tmp/r.json"
        os.environ["GATE_N_GUARD_JOURNAL"] = "/tmp/j.jsonl"
        cfg = config_mod.load_config()
        self.assertEqual(cfg.rulebook_path, "/tmp/r.json")
        self.assertEqual(cfg.journal_path, "/tmp/j.jsonl")

    def test_defaults_sane(self):
        cfg = config_mod.load_config()
        self.assertIsNone(cfg.rulebook_path)
        self.assertIsNone(cfg.journal_path)
        self.assertEqual(cfg.provider, "openrouter")
        self.assertEqual(cfg.max_tokens, 2048)


def make_config(tmp_dir, rulebook=None, journal=None):
    return config_mod.Config(
        model="openrouter:test/model",
        endpoint=None,
        max_tokens=64,
        timeout=5.0,
        retries=0,
        retry_backoff=1.0,
        provider="openrouter",
        openrouter_api_key="test-key",
        rulebook_path=rulebook,
        journal_path=journal,
    )


class DummyClient:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def complete(self, messages, model=None, fallbacks=None):
        self.calls.append(model)
        return self.text


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def write_rulebook(self, rules=None):
        path = os.path.join(self.dir.name, "rules.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(rules or RULES, handle)
        return path

    def test_check_inline(self):
        handler = GateHandler(make_config(self.dir.name))
        result = handler.check(RULES, {"tool": "bash", "arguments": {"command": "rm -rf /"}}, {})
        self.assertEqual(result["verdict"], "deny")
        self.assertEqual(result["rulebook_ref"]["source"], "inline")
        self.assertEqual(len(result["rulebook_ref"]["sha256"]), 64)

    def test_check_default_rulebook_cached(self):
        path = self.write_rulebook()
        handler = GateHandler(make_config(self.dir.name, rulebook=path))
        result = handler.check(None, {"tool": "bash", "arguments": {"command": "rm -rf /"}}, {})
        self.assertEqual(result["verdict"], "deny")
        self.assertIn(path, result["rulebook_ref"]["source"])
        # cached: same object on second call, and no re-read dependency
        self.assertIs(handler._default_rulebook()[0], handler._default_rulebook()[0])

    def test_check_param_overrides_default(self):
        path = self.write_rulebook()
        handler = GateHandler(make_config(self.dir.name, rulebook=path))
        other = {"default": "confirm", "rules": [{"id": "x", "when": [{"tool": "git"}], "verdict": "deny"}]}
        result = handler.check(other, {"tool": "git"}, {})
        self.assertEqual(result["verdict"], "deny")
        self.assertEqual(result["rulebook_ref"]["source"], "inline")

    def test_check_without_source_errors(self):
        handler = GateHandler(make_config(self.dir.name))
        from gate_n_guard.loader import RulebookSourceError
        with self.assertRaises(RulebookSourceError):
            handler.check(None, {"tool": "git"}, {})

    def test_check_empty_rules_string_errors_loudly(self):
        handler = GateHandler(make_config(self.dir.name, rulebook=self.write_rulebook()))
        from gate_n_guard.loader import RulebookSourceError
        with self.assertRaises(RulebookSourceError) as ctx:
            handler.check("", {"tool": "git"}, {})
        self.assertIn("file:<path>", str(ctx.exception))

    def test_record_and_audit_round_trip(self):
        journal = os.path.join(self.dir.name, "j.jsonl")
        handler = GateHandler(make_config(self.dir.name, journal=journal))
        stored = handler.record({"tool": "deploy"}, "confirm", matched=["prod"],
                                outcome="approved", actor="alice")
        self.assertIn("id", stored)
        audit = handler.audit(verdict="confirm")
        self.assertEqual(audit["count"], 1)
        self.assertEqual(audit["records"][0]["action"]["tool"], "deploy")

    def test_record_without_journal_errors(self):
        from gate_n_guard.journal import JournalError
        handler = GateHandler(make_config(self.dir.name))
        with self.assertRaises(JournalError):
            handler.record({"tool": "deploy"}, "confirm")

    def test_audit_without_journal_errors(self):
        from gate_n_guard.journal import JournalError
        handler = GateHandler(make_config(self.dir.name))
        with self.assertRaises(JournalError):
            handler.audit()

    def test_advise_apply_auto_deterministic_skips_client(self):
        client = DummyClient('{"summary": "s", "applicable": []}')
        handler = GateHandler(make_config(self.dir.name), client_factory=lambda c: client)
        result = handler.advise(RULES, question="apply",
                                action={"tool": "bash", "arguments": {"command": "rm -rf /"}})
        self.assertEqual(result["mode"], "deterministic")
        self.assertEqual(client.calls, [])
        self.assertIn("rulebook_ref", result)

    def test_advise_interpret_uses_client_and_engine_override(self):
        seen = {}
        def factory(cfg):
            seen["provider"] = cfg.provider
            return DummyClient(
                '{"meaning": "blocks rm", "permits": [], "requires": [], '
                '"forbids": ["rm -rf"], "edge_cases": [], "confidence": 0.9}'
            )
        handler = GateHandler(make_config(self.dir.name), client_factory=factory)
        result = handler.advise(RULES, question="interpret", rule_id="no-rm",
                                engine="hf")
        self.assertEqual(result["mode"], "model")
        self.assertEqual(result["rule_id"], "no-rm")
        self.assertEqual(seen["provider"], "hf")

    def test_advise_invalid_engine_rejected(self):
        from gate_n_guard.advise import AdviseError
        handler = GateHandler(make_config(self.dir.name))
        with self.assertRaises(AdviseError):
            handler.advise(RULES, question="interpret", rule_id="no-rm", engine="bogus")


class ServerTests(unittest.TestCase):
    def test_registers_all_four_tools(self):
        server = create_server()  # no keys/rulebook/journal: must still register
        tools = asyncio.run(server.list_tools())
        names = sorted(t.name for t in tools)
        self.assertEqual(names, ["advise", "audit", "check", "record"])

    def test_call_check_through_server(self):
        server = create_server()
        result = asyncio.run(server.call_tool("check", {
            "rules": RULES,
            "action": {"tool": "bash", "arguments": {"command": "rm -rf /"}},
            "situation": {},
        }))
        self.assertFalse(result.is_error)
        content = result.content[0].text
        self.assertIn("deny", content)

    def test_call_record_error_surfaces_structured(self):
        server = create_server()  # no journal configured
        result = asyncio.run(server.call_tool("record", {
            "action": {"tool": "deploy"},
            "verdict": "confirm",
        }))
        self.assertTrue(result.is_error)
        self.assertIn("journal not configured", json.loads(result.content[0].text)["error"])


class AdvertisementTests(unittest.TestCase):
    """The model-facing contract: what a fresh client learns before calling."""

    def setUp(self):
        self.server = create_server()
        self.tools = {tool.name: tool for tool in asyncio.run(self.server.list_tools())}

    def test_instructions_state_the_gate_contract(self):
        text = self.server.instructions or ""
        self.assertIn("Call check before acting", text)
        self.assertIn("deny means do not act", text)
        self.assertIn("enforces nothing", text)
        self.assertIn("isError", text)

    def test_server_info_names_a_version(self):
        self.assertEqual(self.server.version, __version__)
        self.assertTrue(self.server.version)

    def test_check_requires_action_and_types_both_rulebook_forms(self):
        schema = self.tools["check"].input_schema
        self.assertEqual(schema["required"], ["action"])
        arms = schema["properties"]["rules"]["anyOf"]
        self.assertIn({"type": "string"}, arms)
        self.assertTrue(any(arm.get("type") == "object" for arm in arms))

    def test_closed_vocabularies_are_enums(self):
        verdicts = ["allow", "confirm", "deny"]
        self.assertEqual(self.tools["record"].input_schema["properties"]["verdict"]["enum"], verdicts)
        audit_verdict = self.tools["audit"].input_schema["properties"]["verdict"]["anyOf"]
        self.assertEqual([arm["enum"] for arm in audit_verdict if "enum" in arm], [verdicts])
        advise = self.tools["advise"].input_schema["properties"]
        self.assertEqual(advise["question"]["enum"], ["interpret", "apply", "gap"])
        self.assertEqual(advise["mode"]["enum"], ["auto", "model"])
        self.assertEqual(
            [arm["enum"] for arm in advise["engine"]["anyOf"] if "enum" in arm],
            [["auto", "hf", "openrouter"]],
        )

    def test_audit_limit_bounds_match_the_journal(self):
        limit = self.tools["audit"].input_schema["properties"]["limit"]
        self.assertEqual((limit["minimum"], limit["maximum"]), (1, journal_mod.MAX_AUDIT_LIMIT))
        self.assertEqual(limit["default"], 50)

    def test_verdict_enum_matches_the_journal(self):
        self.assertEqual(set(get_args(_Verdict)), set(journal_mod.VERDICTS))

    def test_annotations_separate_reads_from_writes(self):
        self.assertTrue(self.tools["check"].annotations.read_only_hint)
        self.assertTrue(self.tools["audit"].annotations.read_only_hint)
        self.assertFalse(self.tools["record"].annotations.read_only_hint)
        self.assertFalse(self.tools["advise"].annotations.read_only_hint)
        self.assertFalse(self.tools["check"].annotations.open_world_hint)
        self.assertTrue(self.tools["advise"].annotations.open_world_hint)

    def test_schema_rejects_bad_arguments_before_the_handler(self):
        for name, args in (
            ("check", {}),
            ("audit", {"limit": journal_mod.MAX_AUDIT_LIMIT + 1}),
            ("advise", {"question": "explain"}),
            ("record", {"action": {"tool": "x"}, "verdict": "maybe"}),
        ):
            with self.subTest(tool=name), self.assertRaises(ToolError):
                asyncio.run(self.server.call_tool(name, args))

    def test_no_tool_advertises_an_output_schema(self):
        # Responses are JSON text the caller parses; nothing claims structuredContent.
        for name, tool in self.tools.items():
            with self.subTest(tool=name):
                self.assertIsNone(tool.output_schema)


class ToolCallTests(unittest.TestCase):
    """End-to-end tool calls through the MCPServer with a real temp rulebook
    and journal, so the wiring (not just the handler) is exercised."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.rulebook = os.path.join(self.dir.name, "rules.json")
        with open(self.rulebook, "w", encoding="utf-8") as handle:
            json.dump(RULES, handle)
        self.journal = os.path.join(self.dir.name, "journal.jsonl")
        self.server = create_server(make_config(
            self.dir.name, rulebook=self.rulebook, journal=self.journal,
        ))

    def call(self, name, args):
        return asyncio.run(self.server.call_tool(name, args))

    def payload(self, result):
        self.assertFalse(result.is_error, result.content[0].text)
        return json.loads(result.content[0].text)

    def test_record_then_audit_through_the_tools(self):
        stored = self.payload(self.call("record", {
            "action": {"tool": "deploy"}, "verdict": "confirm",
            "matched": ["prod"], "outcome": "approved", "actor": "alice",
        }))
        self.assertEqual(stored["verdict"], "confirm")
        audit = self.payload(self.call("audit", {"verdict": "confirm"}))
        self.assertEqual(audit["count"], 1)
        self.assertEqual(audit["records"][0]["id"], stored["id"])

    def test_audit_filters_are_applied(self):
        self.call("record", {"action": {"tool": "deploy"}, "verdict": "confirm"})
        self.call("record", {"action": {"tool": "read"}, "verdict": "allow"})
        self.assertEqual(self.payload(self.call("audit", {"tool": "read"}))["count"], 1)
        self.assertEqual(self.payload(self.call("audit", {"verdict": "deny"}))["count"], 0)
        self.assertEqual(self.payload(self.call("audit", {"limit": 1}))["count"], 1)

    def test_check_uses_the_env_rulebook_and_attaches_the_ref(self):
        result = self.payload(self.call("check", {
            "action": {"tool": "bash", "arguments": {"command": "rm -rf /"}},
        }))
        self.assertEqual(result["verdict"], "deny")
        self.assertEqual(result["rulebook_ref"]["source"], f"file:{self.rulebook}")

    def test_advise_deterministic_through_the_tool(self):
        result = self.payload(self.call("advise", {
            "question": "apply",
            "action": {"tool": "bash", "arguments": {"command": "rm -rf /"}},
        }))
        self.assertEqual(result["mode"], "deterministic")
        self.assertEqual(result["verdict"], "deny")
        self.assertEqual(result["rulebook_ref"]["source"], f"file:{self.rulebook}")

    def test_advise_model_path_through_the_tool(self):
        handler = GateHandler(
            make_config(self.dir.name, rulebook=self.rulebook),
            client_factory=lambda cfg: DummyClient('{"coverage": "adequate", "gaps": []}'),
        )
        server = create_server(handler=handler)
        result = asyncio.run(server.call_tool("advise", {
            "question": "gap", "action": {"tool": "read"},
        }))
        payload = json.loads(result.content[0].text)
        self.assertEqual(payload["mode"], "model")
        self.assertEqual(payload["rulebook_ref"]["source"], f"file:{self.rulebook}")

    def test_failed_tool_call_is_marked_and_still_structured(self):
        result = self.call("check", {"action": {"tool": "bash"}, "rules": "/etc/hosts"})
        self.assertTrue(result.is_error)
        self.assertIn("file:<path>", json.loads(result.content[0].text)["error"])

    def test_main_runs_the_stdio_transport(self):
        with mock.patch("gate_n_guard.server.create_server") as factory:
            main()
        factory.return_value.run.assert_called_once_with(transport="stdio")



class EngineOverrideTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def test_explicit_auto_engine_keeps_the_configured_provider(self):
        seen = {}

        def factory(cfg):
            seen["provider"] = cfg.provider
            return DummyClient('{"meaning": "m", "permits": [], "requires": [], '
                               '"forbids": [], "edge_cases": [], "confidence": 0.5}')

        handler = GateHandler(make_config(self.dir.name), client_factory=factory)
        handler.advise(RULES, question="interpret", rule_id="no-rm", engine="auto")
        self.assertEqual(seen["provider"], "openrouter")

    def test_explicit_engine_still_overrides_the_provider(self):
        seen = {}

        def factory(cfg):
            seen["provider"] = cfg.provider
            return DummyClient('{"meaning": "m", "permits": [], "requires": [], '
                               '"forbids": [], "edge_cases": [], "confidence": 0.5}')

        handler = GateHandler(make_config(self.dir.name), client_factory=factory)
        handler.advise(RULES, question="interpret", rule_id="no-rm", engine="hf")
        self.assertEqual(seen["provider"], "hf")


class ToolMetadataTests(unittest.TestCase):
    def setUp(self):
        server = create_server()
        self.tools = {
            tool.name: tool for tool in asyncio.run(server.list_tools())
        }

    def test_every_tool_carries_titles_and_hints(self):
        expected = {
            "check": ("Gate an action", True, True, None, False),
            "record": ("Record a decision", False, False, False, False),
            "audit": ("Read the decision journal", True, True, None, False),
            "advise": ("Ask about policy", False, False, False, True),
        }
        for name, (title, read_only, idempotent, destructive, open_world) in (
            expected.items()
        ):
            with self.subTest(tool=name):
                tool = self.tools[name]
                self.assertEqual(tool.title, title)
                self.assertIs(tool.annotations.read_only_hint, read_only)
                self.assertIs(tool.annotations.idempotent_hint, idempotent)
                self.assertIs(tool.annotations.destructive_hint, destructive)
                self.assertIs(tool.annotations.open_world_hint, open_world)

if __name__ == "__main__":
    unittest.main()
