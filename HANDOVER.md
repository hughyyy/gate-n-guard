# HANDOVER — gate-n-guard

Handover for the next agent session. Project: **gate-n-guard** — a stdio MCP
server that gates an agent's proposed actions against a rulebook (allow /
confirm / deny with a trace), journals decisions for audit, and answers
policy questions (interpret / apply / gap) with a model. Sixth in the
collection (look-n-see, scrape-n-search, store-n-recall, listen-n-speak,
weigh-n-decide, gate-n-guard).

## What this project is

- **Problem solved**: an agent asks `check` before acting; the gate returns a
  deterministic verdict with the exact rules that matched and why. Unlike the
  gateway/SDK/hook tools in `research/approaches.md`, it is a *decision
  service*: enforcement stays with the caller.
- **Four tools**: `check` (verdict + trace), `record` (append one decision to
  the journal), `audit` (read the journal back, newest-first, filtered),
  `advise` (model-backed interpret / apply / gap with deterministic apply).
- **Seven design questions, all resolved with the user (2026-10-22)**: both
  precedence modes (`first-match` default, `strictest` optional); no-match
  `default` configurable, defaults `allow`; v1 predicate subset incl.
  `matches` regex; append-only decision journal; one advise tool with
  `mode auto|model`; rulebook source param-wins with env-default file;
  static reasons (`{var}` documented as possible extension).
- **Research**: `research/approaches.md` — five gate architectures (inline
  decision service, proxy/gateway, SDK instrumentation, MCP wrapper proxy,
  agent-native hooks) scored on five metrics with evidence; gate-n-guard is
  approach A.

## Current state (verified 2026-10-22)

- **117 tests pass** (`.venv/bin/python -m unittest discover -s tests -q`,
  zero dev deps): matcher (both precedence modes, predicates, errors,
  determinism), journal (append/filters/malformed-line tolerance), loader
  (sources, ref hashing), advise (deterministic apply, escalation, shapes),
  client (retries, fallback chain, routing), server (registration +
  wiring with injected fakes).
- **Live stdio smoke passed** over a real JSON-RPC handshake (initialize →
  tools/list → calls): `check` deny/confirm/allow-default with
  `rulebook_ref` (`file:` source), `record`→`audit` round-trip,
  `advise` apply-auto → deterministic, `advise` interpret without keys →
  clear `InferenceError`, server alive throughout. Artifacts at
  `/tmp/gn-smoke/`.
- **Registered with Oh My Pi** for direct testing (added to
  `~/.omp/agent/mcp.json`, mirroring look-n-see's entry):
  - `command`: `/mnt/shared/Source/gate-n-guard/.venv/bin/gate-n-guard`
  - `env`: `GATE_N_GUARD_RULEBOOK` → `examples/rulebook.json`,
    `GATE_N_GUARD_JOURNAL` → `var/journal.jsonl`,
    `OPENROUTER_API_KEY` / `HF_TOKEN` via `!omp token openrouter` /
    `!omp token huggingface`
  - `timeout`: 120000 (advise needs the raised timeout)
  - **Restart OMP to pick up the registration — no config change after.**
- Not pushed to any remote (local repo only, like the rest of the
  collection's new servers); nothing published to PyPI.
- Git history (HEAD `24440bf`): `97d4463` (docs-first scaffold) →
  `421ade0` (approach comparison research) → `13822d4` (evidence in open
  questions) → `217ec53` (matcher: both precedence modes) → `c9100d3`
  (default posture) → `89d7a35` (`matches` operator) → `4ac34c6` (journal +
  record/audit) → `75c769f` (advise + engine ports) → `d9fd660` (rulebook
  loader + refs) → `d7efee8` (BRIEF/IMPLEMENTATION) → `24440bf` (stdio
  server + entry point).

## How to run

```bash
cd /mnt/shared/Source/gate-n-guard
.venv/bin/python -m unittest discover -s tests -q   # full offline suite
.venv/bin/gate-n-guard                               # stdio MCP server
```

### Post-restart smoke checklist (direct, through OMP)

1. `tools/list` → exactly `advise`, `audit`, `check`, `record`.
2. `check` with no `rules` (default rulebook from env):
   - `action={"tool": "bash", "arguments": {"command": "rm -rf x"}}` → `deny`,
     `rulebook_ref.source` = `file:.../examples/rulebook.json`.
   - `action={"tool": "deploy", ...}, situation={"agent": "ci-bot"}` →
     `confirm`.
   - `action={"tool": "read", ...}` → `allow` with `default_applied: true`.
3. `record` (action deploy / verdict confirm / outcome approved / actor you)
   then `audit` → 1+ records, newest-first.
4. `advise` question=apply + the rm-rf action → `mode: deterministic`.
5. `advise` question=interpret with keys present → `mode: model`,
   `rule_id` echoed. Without keys it errors clearly (that is correct).
6. Journal file: `var/journal.jsonl` (append-only; audit is the only reader).

## Known issues / notes

- **System python has no pip** — always use the repo `.venv` (editable
  install of the repo, includes mcp, httpx, huggingface_hub).
- `GATE_N_GUARD_MODEL` default (`openrouter:google/gemma-4-26b-a4b-it`) is
  inherited from look-n-see, **not benchmarked for advice quality/latency**.
  look-n-see did a benchmark sweep before release; worth repeating for the
  advisory path (prompt shapes differ).
- MCPServer advertises an empty `version` string (cosmetic).
- `record`/`audit` need `GATE_N_GUARD_JOURNAL`; `check`/`advise` never touch
  the journal. `var/` is gitignored; `examples/rulebook.json` is the
  committed sample default.
- Static reasons only; `{var}` expansion is documented as a possible
  extension (render-at-match seam).
- Advise model calls are strict-JSON contracts; a model that stops returning
  valid JSON surfaces a clear `AdviseError`, not a crash.