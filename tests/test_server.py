"""Server wiring tests: handler logic, tool registration, and smoke
calls through the MCPServer with injected fakes. Offline, stdlib only."""

import asyncio
import json
import os
import tempfile
import unittest

from mcp.server.mcpserver import MCPServer

from gate_n_guard import config as config_mod
from gate_n_guard.server import GateHandler, create_server

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
        self.assertIn("journal not configured", str(result))


if __name__ == "__main__":
    unittest.main()
