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

- **328 tests pass** (`.venv/bin/python -m unittest discover -s tests -q`,
  zero dev deps): matcher (both precedence modes, predicates, the full
  validation matrix, deep type-strict equality, operand-type errors,
  resolution, determinism), journal (append/filters/bounds/ordering, five
  classes of malformed line, record/audit validation), loader (sources, ref
  hashing, every source error), advise (deterministic apply, escalation,
  prompts, extraction, all shape contracts), client (retries and budgets,
  fallback chain, routing, metadata, both transports), openrouter (request
  shape, headers, 399/400 boundary, malformed bodies, content parts), config
  (env parsing, invalid values, routing helpers), server (registration,
  wiring, the advertised contract, end-to-end tool calls, `main()`),
  entry point (`python -m gate_n_guard` spawned as a real subprocess:
  handshake, tools/list, one verdict, clean exit; and a keyless,
  rulebook-less run that still starts and fails closed with `isError`).
- **Coverage is measured, not claimed (2026-10-23)**: `tools/coverage.py`
  (stdlib `sys.monitoring`, no dev deps) reports **739/739 function-body
  lines, 100%**; `tools/mutation.py` seeds **35** plausible bugs (precedence
  inversion, type-loose equality, filter bounds, retry-budget off-by-one,
  dropped shape checks, unset `isError`, dropped `rules` param, condition
  inversions) and gets **35/35 killed**.
  Two lessons worth keeping:
  - The first round left two survivors (limit clamp, retry budget) and the
    second left six; each survivor named a missing test, and each is now
    covered. Treat a survivor as a bug report against the suite.
  - Direction (branch) coverage is **not** gated on `sys.monitoring`: it
    reports only one destination for some CPython jump layouts (reproduced on
    a synthetic guard taken both ways), so it is printed as an advisory
    decision-point count (223/223 executed) and condition directions are
    proven by mutation instead. Three mutants are documented as equivalent
    and excluded with reasons in `tools/mutation.py`.
  - Two dead conditions found this way were removed rather than tested:
    `_parse_fallbacks`'s `if p.strip()` (str.split already drops empties) and
    `_evaluate_rule`'s `entry_matched and result` (the loop breaks first, so
    the conjunction never short-circuits).
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
  runs post-fix code (its `gate-n-guard` child started after the 01:57:41
  edits): live prompts carry structured predicate rows, `strictest`,
  determinism, `gap`, `record`→`audit` all re-checked. Call-by-call log in
  *Post-restart verification log* below.
- **Registered with Oh My Pi** for direct testing (`~/.omp/agent/mcp.json`,
  mirroring look-n-see's entry) — **currently disabled**:
  - `command`: `/mnt/shared/Source/gate-n-guard/.venv/bin/gate-n-guard`
  - `env`: `GATE_N_GUARD_RULEBOOK` → `examples/rulebook.json`,
    `GATE_N_GUARD_JOURNAL` → `var/journal.jsonl`,
    `OPENROUTER_API_KEY` / `HF_TOKEN` via `!omp token openrouter` /
    `!omp token huggingface`
  - `timeout`: 120000 (advise needs the raised timeout)
  - `enabled: false` — set 2026-10-23 after the advertisement fix, using the
    OMP-native flag (schema `serverBase.enabled`). The entry, its env, and its
    tokens stay in place, so re-enabling is flipping that boolean back to
    `true` (or deleting the key) and restarting OMP; `disabledServers:
    ["gate-n-guard"]` in the same file is the higher-precedence alternative.
    While disabled the four tools are absent from every OMP session, so the
    standing smoke checklist below has to run against a direct stdio
    subprocess until it is turned back on.
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

### Standing smoke checklist (through OMP when enabled, else direct stdio)

The OMP entry is currently `enabled: false`, and OMP reads `mcp.json` at
startup — so the running session keeps its tools until it restarts, and new
sessions will not have them. Run this checklist either after re-enabling, or
against a direct subprocess (`subprocess.Popen([".venv/bin/gate-n-guard"])`
plus `initialize` → `notifications/initialized` → `tools/list` → `tools/call`).

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

## Post-restart verification log (2026-10-23)

Purpose: positively confirm the restarted server executes the post-fix source
(`0ef8cc7` advise prompt rendering, `62f1db7` empty-string `rules` guard).
The OMP session was restarted first, so the live rows below ran against a
process created after the edits: `omp` pid 484193 → its `gate-n-guard` child
started 02:03:04, source files edited 01:57:41.

### Offline gate

| Check | Result |
|---|---|
| `.venv/bin/python -m unittest discover -s tests -q` | `Ran 120 tests` — `OK` |
| `python -m compileall -q src/gate_n_guard` | clean |
| `python -c "import gate_n_guard.server"` | imports, resolves to the repo `src/` |

### Protocol level — fresh subprocess, keyless env, real stdio JSON-RPC

Driven directly (`subprocess.Popen([".venv/bin/gate-n-guard"])`, then
`initialize` → `notifications/initialized` → `tools/list` → `tools/call`),
with `GATE_N_GUARD_RULEBOOK=examples/rulebook.json` and a throwaway journal;
`OPENROUTER_API_KEY`/`HF_TOKEN` unset on purpose.

| Call | Observed |
|---|---|
| `tools/list` | exactly `advise`, `audit`, `check`, `record` |
| `check` `rules=""` | `{"error": "rules string must be a 'file:<path>' ref or inline JSON, got ''"}` — proves the `62f1db7` guard |
| `check` `rules=null` and `rules` omitted | `deny` via `no-rm-rf` (default rulebook from env) |
| `check` `{"tool": "deploy"}` + `situation.agent=ci-bot` | `confirm` via `prod-deploy-needs-human` |
| `check` `{"tool": "read"}` | `allow`, `default_applied: true` |
| `advise` apply + rm-rf action | `mode: deterministic`, `verdict: deny` |
| `advise` interpret, no keys | `advisory model call failed (openrouter:...): OpenRouter model requested but OPENROUTER_API_KEY is not set` — clear error, correct |
| `record` then `audit` | record stored with id/ts, `count: 1` |
| server lifecycle | exit 0, empty stderr |

### Live through the registered OMP server (real keys in env)

| Call | Observed |
|---|---|
| `advise` apply `mode: model` | `reading` = `[{"path": "tool", "operator": "eq", "value": "bash"}, {"path": "arguments.command", "operator": "contains", "value": "rm -rf"}]` — structured rows, no `Predicate(`/`compiled=` anywhere: the `0ef8cc7` fix, and the one result old code could not produce |
| `advise` interpret (whole rulebook) | complete shape — all 3 rules, every field (`meaning`, `permits`, `requires`, `forbids`, `edge_cases`, `confidence`), `mode: model` |
| `advise` gap | `{"coverage": "adequate", "gaps": []}`, `mode: model` |
| `check` inline `strictest` 3-rule book, bash/rm action | `deny` wins over `confirm`+`allow`; `matched` = confirm+deny with correct `winner` flags; trace carries all three rules |
| `check` identical payload ×2 (`file:` ref, docker prune) | byte-identical output, same `sha256` |
| `check` `rules=42` | `{"error": "rules must be a rulebook object or 'file:<path>' ref, got int"}` |
| `check` `rules=" "` | `{"error": "rules string must be a 'file:<path>' ref or inline JSON, got ' '"}` |
| `record` → `audit` | record `ed53a50a...` stored; `audit` returns `count: 1` |
| `rulebook_ref.sha256` | `0c84fc32...c172` — equals `sha256sum examples/rulebook.json` |

### Findings

- **OMP strips empty-string tool args before dispatch.** Live `check rules=""`
  returned the default rulebook's verdict and live `audit verdict=""` returned
  records instead of the `JournalError` the code raises; at the protocol level
  both `""` and `" "` reach the server and error as shown above, and
  `read_journal(verdict="")` raises
  `audit: verdict must be one of ('allow', 'confirm', 'deny'), got ''`. The
  guard is correct; it is simply unreachable through this harness.
- **The sample rulebook is an OR trap** — see Known issues below.

### Advertisement fix (2026-10-23, later)

`tools/list` used to advertise four names, docstring-only descriptions, no
enums, an untyped `rules`, an optional `check.action`, no annotations, no
`instructions`, and every failure as a successful result. Fixed in one change
(see `IMPLEMENTATION.md` → *Advertised contract*); evidence:

| Check | Observed |
|---|---|
| `initialize` | `serverInfo {name, title, description, version: "0.1.0", websiteUrl}`; `instructions` 1295 chars carrying the gate contract |
| `check` schema | `required: ["action"]`; `rules` = `anyOf[string, object, null]` |
| `record` / `audit` / `advise` schemas | `verdict` enum ×2, `question` enum, `mode` enum, `engine` enum; `audit.limit` `minimum 1 / maximum 1000` |
| annotations | `check`/`audit` read-only + closed; `record` non-destructive, closed; `advise` open-world |
| `check {}` | `isError: true`, `Error executing tool check: 1 validation error … action Field required` |
| `record verdict="maybe"` | `isError: true`, literal_error naming the three verdicts |
| `audit limit=5000` | `isError: true`, `less_than_or_equal` |
| `advise question="explain"` | `isError: true`, literal_error naming the three questions |
| `check rules="nope"` (handler error) | `isError: true`, body `{"error": "rules string must be a 'file:<path>' ref or inline JSON, got 'nope'"}` |
| suite | `Ran 328 tests` — `OK` |

## Known issues / notes

- **System python has no pip** — always use the repo `.venv` (editable
  install of the repo, includes mcp, httpx, huggingface_hub).
- `GATE_N_GUARD_MODEL` default (`openrouter:google/gemma-4-26b-a4b-it`) is
  inherited from look-n-see, **not benchmarked for advice quality/latency**.
  look-n-see did a benchmark sweep before release; worth repeating for the
  advisory path (prompt shapes differ).
- `serverInfo.version` now reports `gate_n_guard.__version__` (`0.1.0`); it used
  to be empty.
- Contract change worth knowing: `audit(limit=1001..)` is now rejected by the
  schema (`isError: true`) instead of being clamped; `read_journal` still
  clamps when called directly. Likewise `record(verdict="maybe")` and
  `advise(question="explain")` fail validation at the protocol boundary, so
  those module checks are now a second line of defence rather than the first.
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
- **The sample rulebook teaches the OR trap.** `examples/rulebook.json` (and
  the README copy at "Rulebooks") puts `{"tool": {"glob": "shell*"}}` as a
  second `when` entry of `no-rm-rf`. Entries are OR-joined (`rules.py`:
  "any-entry semantics"), so that rule denies *every* `shell*` tool with the
  reason "recursive deletes are never needed here" — the live `interpret` call
  read it correctly as "or" and exposed it. Not changed yet: fixing it is
  either two rules with their own reasons (keeps the OR demonstration, makes
  the sample honest) or dropping the second entry. The README section and
  `examples/rulebook.json` must change together; the sha in `rulebook_ref`
  changes with them.
- **The journal currently holds one record** (`var/journal.jsonl`, the
  `record` call in the verification log, actor `verification-session`). The
  earlier campaign's journal was removed after that session's testing — the
  journal is append-only, so deletion is the only clean reset.
