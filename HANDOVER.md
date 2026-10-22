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

## Current state (verified 2026-10-23)

- **120 tests pass** (`.venv/bin/python -m unittest discover -s tests -q`,
  zero dev deps): matcher (both precedence modes, predicates, errors,
  determinism), journal (append/filters/malformed-line tolerance), loader
  (sources, ref hashing), advise (deterministic apply, escalation, shapes),
  client (retries, fallback chain, routing), server (registration + wiring
  with injected fakes). Three added 2026-10-23 defend the prompt fixes below.
- **Full live test campaign through the registered OMP server (~45 calls,
  2026-10-23)**: all four tools exercised. Highlights beyond the standing
  smoke: `strictest` mode deny-dominance and all-consulted trace; type-strict
  equality (`true != 1`); `in`/`gt`/`lte`/nested `arguments.<key>`/
  `matches`; left-to-right entry short-circuit proven via missing-path
  errors; determinism (identical payload twice → byte-identical output incl.
  same sha256); `file:` ref sha256 equals `sha256sum` of the file;
  newest-first audit proven with 2 records; malformed journal line skipped
  and counted; every invalid-input path returns a structured
  `{"error": ...}`, never a traceback.
- **Two prompt bugs found in that campaign and fixed (same day)**:
  1. `_build_messages` serialized rules as dataclass reprs
     (`Predicate(path=..., compiled=None)` leaked into prompts and the model
     echoed them into `reading`). Now renders `[{path, operator, value}]`.
  2. The whole-rulebook `interpret` schema sketch was garbled
     (`"forbids, edge_cases, confidence}`); now a complete shape string.
  Plus: `check(rules="")` now errors loudly (was silently using the default
  rulebook — against the loader's param-wins contract).
- **Fresh-subprocess stdio smoke re-run after the fixes (keyless env)**:
  tools register, deny/confirm/allow-default, `record`→`audit` round-trip,
  `advise` deterministic, advise-without-keys → clear
  `OPENROUTER_API_KEY is not set` error, server alive. This closed the one
  smoke item the earlier session could not verify live.
- **Live advisory calls with real keys** (through OMP, 2026-10-23):
  `interpret` single rule + whole rulebook, `apply` `mode: model`,
  `apply` auto escalation, `gap` — all returned valid strict-JSON shapes.
- **Post-restart positive verification (2026-10-23)**: the current OMP session
  (`omp` pid, child `gate-n-guard` pid started 02:03:04) runs post-fix code —
  live `advise` prompts now carry structured rows (`[{"path","operator",
  "value"}]` echoed back in `reading`, no `Predicate(`/`compiled=`),
  `strictest` deny-dominance with every rule consulted, identical `check`
  payload twice → byte-identical output incl. sha256, `gap` model call,
  `record`→`audit` round-trip.
  Harness note: OMP strips empty-string tool args before dispatch, so
  `rules: ""` and `audit verdict: ""` both behaved as omitted; the empty-string
  guard was verified at the protocol boundary instead (raw stdio client →
  `rules string must be a 'file:<path>' ref ...`, and a live `" "` reaches the
  same error).
- **Registered with Oh My Pi** for direct testing (`~/.omp/agent/mcp.json`,
  mirroring look-n-see's entry):
  - `command`: `/mnt/shared/Source/gate-n-guard/.venv/bin/gate-n-guard`
  - `env`: `GATE_N_GUARD_RULEBOOK` → `examples/rulebook.json`,
    `GATE_N_GUARD_JOURNAL` → `var/journal.jsonl`,
    `OPENROUTER_API_KEY` / `HF_TOKEN` via `!omp token openrouter` /
    `!omp token huggingface`
  - `timeout`: 120000 (advise needs the raised timeout)
- **Remote**: pushed to `https://github.com/hughyyy/gate-n-guard.git`
  (empty repo pre-created to match the sibling naming; the other four
  siblings live at `github.com/hughyyy/<name>.git`, weigh-n-decide is the
  one remaining collection repo without a remote).
- Not published to PyPI.

## How to run

```bash
cd /mnt/shared/Source/gate-n-guard
.venv/bin/python -m unittest discover -s tests -q   # full offline suite
.venv/bin/gate-n-guard                               # stdio MCP server
```

### Standing smoke checklist (direct, through OMP)

1. `tools/list` → exactly `advise`, `audit`, `check`, `record`.
2. `check` with no `rules` (default rulebook from env):
   - `action={"tool": "bash", "arguments": {"command": "rm -rf x"}}` →
     `deny`, `rulebook_ref.source` = `file:.../examples/rulebook.json`.
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
- Test-grammar footgun worth knowing when writing rulebooks: nested argument
  predicates are `{"arguments": {"<key>": <pred>}}` (expanded by
  `_arguments_predicates`), NOT dotted `"arguments.<key>"` — the dotted form
  is only for `situation.<path>`. Error messages name this.
- The test campaign's journal (`var/journal.jsonl` with 2 smoke records +
  1 injected malformed line) was removed after testing; the journal is
  append-only, so deletion was the only clean reset.
