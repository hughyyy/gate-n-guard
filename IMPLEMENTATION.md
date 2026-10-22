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
  test_rules.py      # matcher: precedence, predicates, errors, determinism
  test_journal.py    # append/read, filters, malformed-line tolerance
  test_loader.py     # sources, ref hashing, attach_ref
  test_advise.py     # deterministic apply, escalation, shapes (fake client)
  test_client.py     # retries, fallback chain, routing (fake transports)
  test_server.py     # registration, handler wiring, and the advertised
                     #   contract (instructions, schemas, annotations, isError)
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

## Modules shipped (tested, 129 tests)

- **rules.py** — `load_rulebook` (validation), `check` envelope:
  `{verdict, precedence, matched[], default_applied, trace}`; both
  precedence modes; type-strict equality; predicate ops incl. `matches`
  compiled at load; short-circuit semantics; `RulebookError` /
  `MissingPathError`.
- **journal.py** — `record_decision`, `read_journal` (newest-first,
  filters, malformed-line skip-count), `JournalError`.
- **loader.py** — `resolve_rulebook(rules, default_path)` → `(Rulebook,
  ref {source, sha256})`; `attach_ref`.
- **advise.py** — `advise(...)` orchestration; `_build_messages` prompts;
  `_extract_json` (fence/prose tolerant); per-kind shape checks.
- **openrouter.py / client.py / config.py** — look-n-see engine port,
  chat-only (`AdvisoryClient`), `GATE_N_GUARD_*` env binding.

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
server, entry point — ships with tests (129). Live advisory calls with
real keys ran through the registered OMP server (2026-10-23: interpret
single/whole, apply model-mode, apply auto escalation, gap), and the
without-keys error path was live-verified against a fresh keyless
subprocess. Standing: benchmark `GATE_N_GUARD_MODEL` for advice
quality/latency like look-n-see did before release.

## Verification

- `python -m compileall` / import smoke after changes.
- `.venv/bin/python -m unittest discover -s tests -q` — the offline suite
  (matcher, journal, loader, advise, client; no network).
- The advertised contract is asserted offline in
  `tests/test_server.py::AdvertisementTests` (instructions, `serverInfo`
  version, required/enum/bound schema fields, annotations, `isError` on
  handler failures, schema rejection before the handler) — no harness needed.
- Live advisory calls are manual smoke (real MCP call through the harness),
  not part of the suite.

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
