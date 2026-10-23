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
    ("rules.py", '    if op == "in":\n        return any(_same_type_eq(value, item) for item in want)',
     '    if op == "in":\n        return value in want',
     "in membership becomes type-loose"),
    ("journal.py", '    limit = min(limit, MAX_AUDIT_LIMIT)', "    limit = limit",
     "audit limit is no longer capped"),
    ("journal.py", '        if tool is not None and record["action"].get("tool") != tool:',
     '        if tool is not None and record["action"].get("tool") == tool:',
     "tool filter is inverted"),
    ("journal.py", "        if before_dt is not None and ts >= before_dt:",
     "        if before_dt is not None and ts > before_dt:",
     "before becomes inclusive"),
    ("journal.py", "    timed_records.sort(key=lambda item: (item[0], item[1]), reverse=True)",
     "    timed_records.sort(key=lambda item: (item[0], item[1]))",
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
    ("client.py", "                if not _is_retryable(exc) or attempt >= retries:",
     "                if not _is_retryable(exc) or attempt > retries:",
     "retry budget is off by one"),
    ("client.py", "        chain.extend(fallbacks if fallbacks is not None else self.config.fallbacks)",
     "        chain.extend(())", "configured fallbacks are ignored"),
    ("advise.py", '        if deterministic["applicable"]:', "        if True:",
     "auto mode always takes the deterministic path"),
    ("advise.py", '        if question != "interpret":', '        if question == "interpret":',
     "rule_id guard is inverted"),
    ("advise.py", '    if not isinstance(payload.get("meaning"), str):',
     "    if False:", "interpret single-rule shape check is dropped"),
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
    ("advise.py", 'if payload.get("coverage") not in ("adequate", "partial", "none") or not (',
     'if payload.get("coverage") not in ("adequate", "partial", "none", "full") or not (',
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
    ("rules.py", '        if not operand:\n            raise RulebookError(\n                f"rule {rule_id!r}: {path}: arguments object must not be empty"',
     '        if False:\n            raise RulebookError(\n                f"rule {rule_id!r}: {path}: arguments object must not be empty"',
     "empty nested argument objects match vacuously"),
    ("rules.py", 'if not isinstance(sub_key, str) or not sub_key or "." in sub_key:',
     'if not isinstance(sub_key, str) or not sub_key:',
     "dotted argument names are reinterpreted as paths"),
    ("rules.py", "        if rule.id in seen_ids:", "        if False:",
     "duplicate rule ids are accepted"),
    ("advise.py", "        or not 0 <= value <= 1", "        or False",
     "confidence range is not checked"),
    ("journal.py", 'with open(path, encoding="utf-8", errors="replace") as handle:',
     'with open(path, encoding="utf-8") as handle:', "undecodable journal bytes are fatal"),
    ("journal.py", "    if journal.exists() and not journal.is_file():",
     "    if False:", "a directory reads as an empty journal"),
    ("journal.py", "        payload = json.dumps(record, ensure_ascii=False)",
     "        payload = json.dumps(record, ensure_ascii=False, default=str)",
     "non-serializable record fields are coerced"),
    ("journal.py", "    return os.path.expanduser(journal_path)",
     "    return journal_path", "tilde journal paths are not expanded"),
    ("loader.py", '        raw = raw_bytes.decode("utf-8")',
     '        raw = raw_bytes.decode("utf-8", "replace")', "undecodable rulebook bytes are tolerated"),
    ("loader.py", "        raw_bytes = file.read_bytes()",
     '        raw_bytes = file.read_text(encoding="utf-8").encode("utf-8")',
     "file hash covers newline-normalized text, not disk bytes"),
    ("loader.py", "    file = Path(path).expanduser()", "    file = Path(path)",
     "tilde rulebook paths are not expanded"),
    ("config.py", "    return max(value, minimum)\n\n\ndef _env_float", "    return value\n\n\ndef _env_float",
     "negative int env values are not bounded"),
    ("client.py", "    if isinstance(exc, (ConnectionError, TimeoutError, httpx.TransportError)):",
     "    if isinstance(exc, (ConnectionError, TimeoutError)):",
     "httpx transport errors are not retried"),
    ("openrouter.py", "        if not isinstance(data, dict):", "        if False:",
     "non-object success bodies are accepted"),
    ("advise.py", '    if question in ("apply", "gap") and (', '    if False and (',
     "apply/gap accept a missing action"),
    ("advise.py", '        result = {\n            "coverage": payload["coverage"],\n            "gaps": [_shape_gap(entry) for entry in gaps],\n        }',
     '        result = dict(payload)\n        result["coverage"] = payload["coverage"]',
     "gap answers can smuggle extra keys"),
    ("server.py", '            if engine != "auto":', '            if True:',
     "explicit auto engine overrides the provider"),
    ("advise.py", "            index = start + 1", "            index = len(candidate)",
     "prose before the answer stops the scan"),
    ("advise.py", '    for key in ("rule_id", "relevance", "reading"):',
     '    for key in ("rule_id", "reading"):', "apply entries may omit relevance"),
    ("advise.py", '    for key in ("concern", "suggested_rule", "rationale"):',
     '    for key in ("concern", "rationale"):', "gap entries may omit suggested_rule"),
    ("advise.py", "    if not isinstance(value, list) or not all(\n        isinstance(item, str) for item in value\n    ):",
     "    if not isinstance(value, list):", "non-string list items are accepted"),
    ("config.py", "    return os.environ.get(name, default).strip()",
     "    return os.environ.get(name, default)", "blank env values are not sanitized"),
    ("rules.py", '    if operator in ("glob", "contains", "matches") and not isinstance(value, str):',
     '    if operator == "matches" and not isinstance(value, str):',
     "glob/contains operands are unchecked at load"),
    ("rules.py", "        if not value:\n            raise RulebookError(\n                f\"rule {rule_id!r}: {path}: 'in' requires a non-empty list\"",
     "        if False:\n            raise RulebookError(\n                f\"rule {rule_id!r}: {path}: 'in' requires a non-empty list\"",
     "empty 'in' lists never match silently"),
    ("rules.py", '    if operator in ("lt", "lte", "gt", "gte") and not _is_number(value):',
     "    if False:", "numeric operands are unchecked at load"),
    ("rules.py", "        if path == \"tool\" and not all(isinstance(item, str) for item in value):",
     "        if False:", "tool 'in' lists accept non-strings"),
    ("rules.py", '    if path == "tool" and operator == "eq" and not isinstance(value, str):',
     "    if False:", "tool equality accepts non-strings"),
    ("rules.py", '    if path == "tool" and not isinstance(operand, str):',
     "    if False:", "bare tool operands accept non-strings"),
    ("client.py", "        if content is not None and not isinstance(content, str):",
     "        if False:", "non-string HF content is accepted"),
    ("server.py", "        if self._default is None:", "        if False:",
     "the default rulebook cache is bypassed"),
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
