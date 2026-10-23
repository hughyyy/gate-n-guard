"""End-to-end entry-point test: `python -m gate_n_guard` (and the console
script it mirrors) must speak MCP over stdio.

Spawns the package as a real subprocess, performs the initialize handshake,
lists tools, gates one action, then closes stdin and checks a clean exit.
Requests are written one at a time and each reply read before the next send:
closing stdin while calls are in flight can cancel them.

Offline: no keys, no journal, temp rulebook. stdlib unittest only.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RULEBOOK = {
    "default": "allow",
    "precedence": "first-match",
    "rules": [{
        "id": "no-rm-rf",
        "when": [{"tool": "bash", "arguments": {"command": {"contains": "rm -rf"}}}],
        "verdict": "deny",
        "reason": "no recursive deletes",
    }],
}

INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "entrypoint-test", "version": "0"},
    },
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def clean_env(**overrides):
    env = {
        key: value for key, value in os.environ.items()
        if key not in ("OPENROUTER_API_KEY", "HF_TOKEN", "GATE_N_GUARD_JOURNAL",
                       "GATE_N_GUARD_RULEBOOK")
    }
    env["PYTHONPATH"] = os.pathsep.join([os.path.join(ROOT, "src")])
    env.update(overrides)
    return env


class EntryPointTests(unittest.TestCase):
    def run_session(self, requests, env, cwd):
        """Send each request, read its reply, then close stdin."""
        proc = subprocess.Popen(
            [sys.executable, "-m", "gate_n_guard"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=cwd, env=env,
        )
        replies = {}
        try:
            for request in requests:
                proc.stdin.write(json.dumps(request) + "\n")
                proc.stdin.flush()
                if "id" in request:
                    replies[request["id"]] = json.loads(proc.stdout.readline())
            proc.stdin.close()
            returncode = proc.wait(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        try:
            stderr = proc.stderr.read()
        finally:
            proc.stdout.close()
            proc.stderr.close()
        self.assertEqual(returncode, 0, f"stderr: {stderr}")
        return replies

    def test_stdio_session_registers_tools_and_gates_an_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            rulebook = os.path.join(tmp, "rules.json")
            with open(rulebook, "w", encoding="utf-8") as handle:
                json.dump(RULEBOOK, handle)
            with open(rulebook, "rb") as handle:
                expected_sha = hashlib.sha256(handle.read()).hexdigest()
            replies = self.run_session([
                INITIALIZE,
                INITIALIZED,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                    "name": "check",
                    "arguments": {"action": {"tool": "bash",
                                             "arguments": {"command": "rm -rf /"}}},
                }},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
                    "name": "check", "arguments": {},
                }},
            ], env=clean_env(GATE_N_GUARD_RULEBOOK=rulebook), cwd=tmp)

        handshake = replies[1]["result"]
        self.assertEqual(handshake["serverInfo"]["name"], "gate-n-guard")
        self.assertTrue(handshake["serverInfo"]["version"])
        self.assertIn("Call check before acting", handshake["instructions"])

        tools = replies[2]["result"]["tools"]
        self.assertEqual(sorted(tool["name"] for tool in tools),
                         ["advise", "audit", "check", "record"])

        verdict = json.loads(replies[3]["result"]["content"][0]["text"])
        self.assertEqual(verdict["verdict"], "deny")
        self.assertTrue(verdict["rulebook_ref"]["source"].startswith("file:"))
        self.assertEqual(verdict["rulebook_ref"]["sha256"], expected_sha)

        # schema validation failures come back as isError tool results
        self.assertTrue(replies[4]["result"]["isError"])
        self.assertIn("action", replies[4]["result"]["content"][0]["text"])

    def test_missing_rulebook_starts_and_fails_closed_with_a_structured_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            replies = self.run_session([
                INITIALIZE,
                INITIALIZED,
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                    "name": "check", "arguments": {"action": {"tool": "read"}},
                }},
            ], env=clean_env(), cwd=tmp)

        self.assertEqual(replies[1]["result"]["serverInfo"]["name"], "gate-n-guard")
        self.assertTrue(replies[2]["result"]["isError"])
        self.assertIn("GATE_N_GUARD_RULEBOOK", replies[2]["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
