# gate-n-guard — Implementation Plan

## Goal

A stdio MCP server that gates an agent's proposed actions against a rulebook
(allow / confirm / deny with a trace), journals decisions for audit, and
answers interpret / apply / gap policy questions with a model. See
`BRIEF.md` for requirements; `README.md` is the user-facing spec and the
place all resolved decisions land first.

## Stack

- Python 3.10+ (3.14 on this workstation)
- `mcp` (MCPServer, mcp 2.x) + `huggingface_hub` (InferenceClient) +
  `httpx` (OpenRouter transport)
- stdlib for rule matching, journaling, hashing, JSON — no extra deps
- `pyproject.toml` (setuptools) with console-script entry point
  `gate-n-guard`
- Tests: stdlib `unittest` (zero dev deps)

## Layout

```
pyproject.toml
README.md            # user-facing: tools, rulebooks, verdicts, config
BRIEF.md             # requirements (this brief)
IMPLEMENTATION.md    # this file: design, remaining work, release plan
AGENTS.md            # conventions + workflow
CONTEXT.md           # vocabulary
research/
  approaches.md      # five gate architectures scored on five metrics
src/gate_n_guard/
  __init__.py        # version
  rules.py           # SHIPPED: rulebook validation + matcher (both
                     #   precedence modes), trace envelope
  journal.py         # SHIPPED: append-only JSONL journal, record/audit I/O
  loader.py          # SHIPPED: rulebook sources (inline/file/default) +
                     #   content-addressed rulebook_ref + attach_ref
  advise.py          # SHIPPED: question kinds, deterministic apply,
                     #   prompts, JSON extraction, shape checks
  openrouter.py      # SHIPPED: OpenRouter chat transport (httpx)
  client.py          # SHIPPED: AdvisoryClient retry/fallback chain
  config.py          # SHIPPED: GATE_N_GUARD_* env parsing
  server.py          # SHIPPED: MCPServer app (instructions, title, version) +
                     #   4 tools with typed schemas, annotations, isError
                     #   errors + main()
  __main__.py        # SHIPPED: python -m gate_n_guard
tests/
  test_rules.py      # matcher: precedence, predicates, validation matrix,
                     #   deep equality, operand types, resolution, determinism
  test_journal.py    # append/read, filters and bounds, ordering, malformed
                     #   lines, record/audit validation
  test_loader.py     # sources, ref hashing, attach_ref, source errors
  test_advise.py     # deterministic apply, escalation, prompts, extraction,
                     #   shape checks (fake client)
  test_client.py     # retries, budgets, fallback chain, routing, metadata,
                     #   both transports (fake transports)
  test_openrouter.py # request shape, headers, status boundaries, parsing
  test_config.py     # env parsing, defaults, invalid values, routing helpers
  test_server.py     # registration, handler wiring, advertised contract,
                     #   end-to-end tool calls, isError
  test_entrypoint.py # real `python -m gate_n_guard` subprocess over stdio
tools/
  coverage.py        # line-coverage report (sys.monitoring, stdlib only)
  mutation.py        # mutation spot-check: seeded bugs must fail the suite
```

## Config (env vars)

| Var | Default | Purpose |
|---|---|---|
| `GATE_N_GUARD_RULEBOOK` | — | default rulebook path; used when the `rules` param is omitted |
| `GATE_N_GUARD_JOURNAL` | — | append-only decision journal (JSONL); unset → `record`/`audit` error clearly |
| `GATE_N_GUARD_ENGINE` | `openrouter` | provider for bare model ids: `openrouter` \| `hf` \| `auto` |
| `GATE_N_GUARD_MODEL` | `openrouter:google/gemma-4-26b-a4b-it` | default advisory model; `openrouter:`/`hf:` prefixes route |
| `GATE_N_GUARD_FALLBACKS` | — | comma/space-separated chain tried after the default on hard failure; unset = none, `""` = disabled |
| `GATE_N_GUARD_MAX_TOKENS` | `2048` | max generated tokens |
| `GATE_N_GUARD_TIMEOUT` | `60` | per-request timeout (s) |
| `GATE_N_GUARD_RETRIES` | `3` | retries on 429/5xx/connection |
| `GATE_N_GUARD_RETRY_BACKOFF` | `2.0` | exponential backoff base (s) |
| `OPENROUTER_API_KEY` | — | OpenRouter auth |
| `HF_TOKEN` | — | HuggingFace auth |
| `HF_ENDPOINT` | — | dedicated HF inference endpoint (pins HF transport) |
| `OPENROUTER_BASE_URL` / `OPENROUTER_SITE_URL` / `OPENROUTER_APP_NAME` | defaults | OpenRouter endpoint + optional referer/title headers |

## Modules shipped (tested, 328 tests)

- **rules.py** — `load_rulebook` (validation, unique ids, single-segment
  argument names, non-empty nested argument objects, non-empty situation
  path segments), `check` envelope:
  `{verdict, precedence, matched[], default_applied, trace}`; both
  precedence modes and ties; type-strict equality and `in` membership;
  predicate ops incl. `matches` compiled at load; short-circuit semantics;
  `RulebookError` / `MissingPathError`.
- **journal.py** — `record_decision` (serialize-before-write),
  `read_journal` (newest-first, filters, malformed-line skip-count,
  undecodable-byte tolerance), `JournalError` for bad paths and IO errors.
- **loader.py** — `resolve_rulebook(rules, default_path)` → `(Rulebook,
  ref {source, sha256 over disk bytes})`; `attach_ref`; `~` expansion;
  UTF-8 and empty-`file:` refs are source errors.
- **advise.py** — `advise(...)` orchestration; action/situation validation
  for apply/gap; `_build_messages` prompts (serialization-checked);
  `_extract_json` (fence/prose tolerant); per-kind shape checks with key
  whitelisting, nested-entry objects, and confidence bounds.
- **openrouter.py / client.py / config.py** — look-n-see engine port,
  chat-only (`AdvisoryClient`), `GATE_N_GUARD_*` env binding with bounded
  numerics; httpx transport errors retried; malformed provider bodies
  rejected as declarative errors.

## Advertised contract (what a client sees before calling)

MCP offers four channels for a server to describe itself; all four now carry
content (`create_server`, `src/gate_n_guard/server.py`):

| Channel | Content | Why |
|---|---|---|
| server `instructions` | the gate contract — call `check` before acting; `deny` = do not act; `confirm` = a human decides; verdicts are advice and the server enforces nothing; every response names `rulebook_ref`; rulebook shape and any-entry (OR) semantics | the only prose a model sees without opening tool docs; a gate that never states its contract is discoverable but not usable |
| `inputSchema` | `check.action` required; `rules` typed `string \| object`; enums for `verdict` / `question` / `mode` / `engine`; `audit.limit` bounded 1–1000 | a bad argument fails at the protocol boundary (`isError`) instead of coming back as a handler message the model must interpret |
| `annotations` | `check` / `audit` read-only and closed-world; `record` non-destructive, local; `advise` open-world (it calls a model) | hosts can auto-approve the deterministic tools and prompt on journal writes and network calls |
| `isError` | module errors return the `{"error": ...}` JSON body with `isError: true` | the payload stays machine-readable while hosts that branch on the flag no longer read failures as results |

`serverInfo` carries `name`, `title`, `description`, `websiteUrl`, and
`version` (`gate_n_guard.__version__`).

Not advertised: `outputSchema` / `structuredContent`. Responses are JSON text
in one content block; declaring output schemas is a possible follow-up, and no
test claims otherwise.

## Remaining work

None for v1 scope: all seven design questions are resolved (Q7: static
reasons) and every module — matcher, journal, loader, advise, engines,
server, entry point — ships with tests (328). Live advisory calls with
real keys ran through the registered OMP server (2026-10-23: interpret
single/whole, apply model-mode, apply auto escalation, gap), and the
without-keys error path was live-verified against a fresh keyless
subprocess. Standing: benchmark `GATE_N_GUARD_MODEL` for advice
quality/latency like look-n-see did before release.

## Verification

- `python -m compileall` / import smoke after changes.
- `.venv/bin/python -m unittest discover -s tests -q` — the offline suite
  (328 tests; no network, no keys, no journal required).
- `python tools/coverage.py` — line coverage over function bodies using
  `sys.monitoring` (stdlib only). Current: **850/850 lines, 100%**. Decision
  points are reported alongside as an advisory count (272/272 executed):
  `sys.monitoring` does not report a destination for every CPython jump
  layout, so direction coverage is proven by mutation instead.
  measurement deliberately excludes module-level statements (they run at
  import), class bodies, and signature/decorator lines, which never emit
  LINE events; everything a function actually does is counted.
- `python tools/mutation.py` — mutation spot-check: 52 seeded bugs (precedence
  inversion, type-loose equality and `in` membership, grammar validation,
  filter bounds, retry budget off-by-one, dropped shape checks, unset
  `isError`, dropped `rules` param, encoding/IO failures, condition
  inversions in the guard clauses, …) must each fail the suite. Current:
  **52/52 killed, 0 survivors**. Further mutants are documented as
  equivalent and deliberately absent (see the module docstring): no test can
  observe them on this platform. A survivor means a behaviour nothing
  defends — add the test, not the mutant.
- The advertised contract is asserted offline in
  `tests/test_server.py::AdvertisementTests` (instructions, `serverInfo`
  version, required/enum/bound schema fields, annotations, `isError` on
  handler failures, schema rejection before the handler) — no harness needed.
- End-to-end tool calls (record→audit, filters, check/advise with the env
  rulebook, `main()` stdio dispatch) run in
  `tests/test_server.py::ToolCallTests`.
- The real entry point runs as a subprocess in
  `tests/test_entrypoint.py` (`python -m gate_n_guard`): handshake,
  `tools/list`, one gated verdict with a hash-checked `rulebook_ref`, an
  `isError` result for a schema violation, and a clean exit — plus a
  keyless, rulebook-less start that still registers and fails closed.
- Live advisory calls are manual smoke (real MCP call through the harness),
  not part of the suite.

### Test-selection rules used here

Each test defends an observable contract: a verdict, a trace field, an error
message, an ordering, a bound, a protocol field. Not tested: source text,
incidental formatting, third-party internals. Edge-case inventory per module:
both precedence modes and their tie behaviour; type-strict equality and
`in` membership over scalars, lists, and dicts; every operator's
operand-type errors; missing-path short-circuit order; rulebook validation
for every field plus duplicate ids, dotted/empty argument names, and empty
nested `arguments`/`situation.` segments; journal filters at their
inclusive/exclusive boundaries, limit bounds, malformed lines including
undecodable bytes and BOMs, non-file/unreadable paths, non-serializable
records, and equal-instant tie ordering; loader source precedence, CRLF
byte hashing, `~` expansion, and every failure mode; retry budget, backoff
schedule, real httpx transport exception classes, malformed HF/OpenRouter
bodies, dedupe, and fallback chain composition; OpenRouter status boundary
(399/400), malformed bodies, and content-part shapes; advise mode
escalation, action/situation requirements, key whitelisting, nested shape
contracts, and confidence bounds; the MCP advertisement and tool metadata.

Boundary choices that are easy to miss and are pinned by tests: the rulebook
operand being *longer* than the action value (not only shorter) must not match;
a `situation.<path>` segment that is a substring of a string fact must raise
`MissingPathError` instead of indexing it; `glob`/`contains` operands are
type-checked at match time (unlike `matches`, which fails at load); an
extraction candidate with `{` and no `}` is rejected; `situation.` with no path
is an unknown key, not a predicate on an empty path; a record whose `ts` lacks
a timezone is skipped, not compared naively; `audit` caps instead of rejecting
an oversized `limit`.

## Release plan

1. ~~Q1–Q7 resolved in README (docs-first, same change).~~
2. ~~Matcher, journal, loader, advise, engines, server, entry point
   implemented; 117 offline tests.~~
3. Live stdio smoke passed: 4 tools register, `check` returns deny/confirm/
   allow-default with `rulebook_ref`, `record`→`audit` round-trip,
   `advise` deterministic apply, advise-without-keys errors clearly.
4. Advisory smoke with real keys done (2026-10-23, via the registered OMP
   server: interpret single/whole, apply model-mode, apply auto
   escalation, gap; without-keys error verified against a fresh keyless
   subprocess). Standing: benchmark `GATE_N_GUARD_MODEL` (look-n-see did
   a sweep before release).
