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
  server.py          # SHIPPED: MCPServer app + 4 tools + main()
  __main__.py        # SHIPPED: python -m gate_n_guard
tests/
  test_rules.py      # matcher: precedence, predicates, errors, determinism
  test_journal.py    # append/read, filters, malformed-line tolerance
  test_loader.py     # sources, ref hashing, attach_ref
  test_advise.py     # deterministic apply, escalation, shapes (fake client)
  test_client.py     # retries, fallback chain, routing (fake transports)
  test_server.py     # registration + wiring smoke (injected fakes)
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

## Modules shipped (tested, 102 tests)

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

## Remaining work

None for v1 scope: all seven design questions are resolved (Q7: static
reasons) and every module — matcher, journal, loader, advise, engines,
server, entry point — ships with tests (117). Live advisory calls against
real keys are the standing manual smoke; benchmark/model defaults can be
revisited like look-n-see did before release.

## Verification

- `python -m compileall` / import smoke after changes.
- `.venv/bin/python -m unittest discover -s tests -q` — the offline suite
  (matcher, journal, loader, advise, client; no network).
- Live advisory calls are manual smoke (real MCP call through the harness),
  not part of the suite.

## Release plan

1. ~~Q1–Q7 resolved in README (docs-first, same change).~~
2. ~~Matcher, journal, loader, advise, engines, server, entry point
   implemented; 117 offline tests.~~
3. Live stdio smoke passed: 4 tools register, `check` returns deny/confirm/
   allow-default with `rulebook_ref`, `record`→`audit` round-trip,
   `advise` deterministic apply, advise-without-keys errors clearly.
4. Standing: manual advisory smoke with real keys through the harness;
   OMP registration example in README exercised end-to-end; model default
   validated (look-n-see did a benchmark sweep before release —
   worthwhile for `GATE_N_GUARD_MODEL`).
