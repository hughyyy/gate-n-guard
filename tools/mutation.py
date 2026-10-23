"""Mutation spot-check for the gate-n-guard suite (stdlib only).

Applies one plausible bug at a time to a throwaway copy of the package and
runs the offline suite against it. A mutant that the suite still passes is a
*survivor*: behaviour nothing defends. Exits 1 if any mutant survives.

This is a spot-check, not full mutation testing: the mutants below target the
decisions a rule gate gets wrong quietly — precedence, type-strict equality,
journal ordering and filters, retry budgets, shape checks, error flags.

Usage:
    .venv/bin/python tools/mutation.py [--verbose]
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Equivalent mutants are deliberately absent, because no test can observe them:
#   * _extract_json's `or` → `and` brace test: both spellings reject every
#     malformed candidate (a slice without "{" can never parse to an object);
#   * fnmatchcase → fnmatch: `os.path.normcase` is identity on POSIX;
#   * dropping the "Z" normalisation: fromisoformat accepts "Z" since 3.11
#     (the branch still matters for the 3.10 floor in requires-python).
#
# (file, original snippet, mutated snippet, description)
MUTANTS = [
    ("rules.py", '        if outcome["matched"]:', '        if not outcome["matched"]:',
     "first-match ignores the match flag"),
    ("rules.py", 'STRICTEST_ORDER = {"allow": 0, "confirm": 1, "deny": 2}',
     'STRICTEST_ORDER = {"allow": 0, "confirm": 2, "deny": 1}',
     "strictest ranks confirm above deny"),
    ("rules.py", "    if type(left) is not type(right):\n        return False",
     "    if type(left) is not type(right):\n        return True",
     "equality becomes type-loose"),
    ("rules.py", "    if situation is None:\n        situation = {}",
     "    if situation is None:\n        situation = None",
     "missing situation is no longer defaulted"),
    ("rules.py", '    if op == "in":\n        return value in want',
     '    if op == "in":\n        return want in value',
     "in checks the wrong direction"),
    ("journal.py", '    limit = min(limit, MAX_AUDIT_LIMIT)', "    limit = limit",
     "audit limit is no longer capped"),
    ("journal.py", '        if tool is not None and record["action"].get("tool") != tool:',
     '        if tool is not None and record["action"].get("tool") == tool:',
     "tool filter is inverted"),
    ("journal.py", "        if before_dt is not None and ts >= before_dt:",
     "        if before_dt is not None and ts > before_dt:",
     "before becomes inclusive"),
    ("journal.py", '    matched_records.sort(key=lambda r: (r["__ts"], r["ts"]), reverse=True)',
     '    matched_records.sort(key=lambda r: (r["__ts"], r["ts"]))',
     "audit returns oldest-first"),
    ("journal.py", '    if "id" not in record or "ts" not in record or "verdict" not in record:',
     "    if False:", "records missing required keys are accepted"),
    ("journal.py", "    if parsed.tzinfo is None:\n        return None", "    if False:\n        return None",
     "naive timestamps are accepted"),
    ("config.py", '    if engine not in {"auto", "hf", "openrouter"}:\n        engine = "openrouter"',
     '    if engine not in {"auto", "hf", "openrouter"}:\n        engine = "hf"',
     "invalid engine falls back to hf"),
    ("client.py", "RETRYABLE_STATUS = {429, 500, 502, 503, 504}",
     "RETRYABLE_STATUS = {500, 502, 503, 504}", "429 is no longer retried"),
    ("client.py", "                if not _is_retryable(exc) or attempt >= self.config.retries:",
     "                if not _is_retryable(exc) or attempt > self.config.retries:",
     "retry budget is off by one"),
    ("client.py", "        chain.extend(fallbacks if fallbacks is not None else self.config.fallbacks)",
     "        chain.extend(())", "configured fallbacks are ignored"),
    ("advise.py", '        if deterministic["applicable"]:', "        if True:",
     "auto mode always takes the deterministic path"),
    ("advise.py", '        if question != "interpret":', '        if question == "interpret":',
     "rule_id guard is inverted"),
    ("advise.py", '            if not isinstance(payload.get("meaning"), str):',
     "            if False:", "interpret single-rule shape check is dropped"),
    ("openrouter.py", "        if response.status_code >= 400:", "        if response.status_code > 400:",
     "400 is treated as success"),
    ("openrouter.py",
     "        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):",
     "        if False:", "malformed responses are accepted"),
    ("loader.py", "    if not file.is_file():", "    if False:",
     "missing rulebook files are read anyway"),
    ("loader.py", '    if isinstance(rules, str):', "    if False:",
     "string rulebook sources are rejected"),
    ("server.py", "        is_error=True,", "        is_error=False,",
     "tool errors are not flagged"),
    ("server.py", "        return _wrap(handler.check, rules, action, situation)",
     "        return _wrap(handler.check, None, action, situation)",
     "explicit rules param is dropped"),
    # --- condition-level mutants: both directions of a decision must be tested
    ("advise.py", 'if payload.get("coverage") not in ("adequate", "partial", "none") or \\',
     'if payload.get("coverage") not in ("adequate", "partial", "none", "full") or \\',
     "gap coverage vocabulary widened"),
    ("rules.py", "        return len(left) == len(right) and all(", "        return len(left) <= len(right) and all(",
     "list equality ignores extra elements"),
    ("rules.py", "        return left.keys() == right.keys() and all(", "        return all(",
     "dict equality ignores the key set"),
    ("rules.py", '        elif key.startswith("situation.") and len(key) > len("situation."):',
     '        elif key.startswith("situation."):', "empty situation path becomes a predicate"),
    ("rules.py", '            if not isinstance(node, dict) or part not in node:\n                raise MissingPathError(f"situation path missing: {path}")',
     '            if part not in node:\n                raise MissingPathError(f"situation path missing: {path}")',
     "non-object situation walk is unchecked"),
    ("journal.py", '    if "id" not in record or "ts" not in record or "verdict" not in record:',
     '    if "id" not in record or "ts" not in record:', "records without a verdict are accepted"),
    ("client.py", "            if candidate in seen:\n                continue", "            if False:\n                continue",
     "fallback chain stops deduping"),
    ("client.py", "                if not self.config.openrouter_api_key:", "                if False:",
     "missing OpenRouter key is not reported"),
    ("config.py", '        if model_id.startswith(OPENROUTER_PREFIX):\n            return "openrouter"',
     '        if model_id.startswith(OPENROUTER_PREFIX):\n            return "hf"',
     "openrouter: prefix routes to hf"),
    ("loader.py", "    result = dict(response)", "    result = response",
     "attach_ref mutates its input"),
    ("openrouter.py", "    if isinstance(content, list):", "    if False:",
     "content parts are no longer unwrapped"),
]


def run_suite(python: str, src_root: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=src_root)
    return subprocess.run(
        [python, "-m", "unittest", "discover", "-s", "tests", "-q"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="print each mutant's failure tail")
    args = parser.parse_args()

    python = sys.executable
    work = tempfile.mkdtemp(prefix="gng-mutants-")
    try:
        src_root = os.path.join(work, "src")
        shutil.copytree(os.path.join(ROOT, "src"), src_root)
        pkg = os.path.join(src_root, "gate_n_guard")

        baseline = run_suite(python, src_root)
        if baseline.returncode != 0:
            print("baseline suite must pass before mutating")
            print(baseline.stderr[-2000:])
            return 2
        print(f"baseline: {baseline.stderr.strip().splitlines()[-1]}")

        killed = survivors = 0
        for name, old, new, label in MUTANTS:
            path = os.path.join(pkg, name)
            with open(path, encoding="utf-8") as handle:
                original = handle.read()
            if old not in original:
                print(f"SKIP     {name}: {label} (pattern no longer present)")
                continue
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(original.replace(old, new, 1))
            try:
                outcome = run_suite(python, src_root)
            finally:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(original)
            if outcome.returncode != 0:
                killed += 1
                tail = outcome.stderr.strip().splitlines()[-1]
                print(f"KILLED   {name}: {label} -> {tail}")
                if args.verbose:
                    print(outcome.stderr[-800:])
            else:
                survivors += 1
                print(f"SURVIVED {name}: {label}")
        print(f"\n{killed} killed, {survivors} survived, {len(MUTANTS)} mutants")
        return 0 if survivors == 0 else 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
