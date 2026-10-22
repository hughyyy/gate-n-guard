# AGENTS.md — gate-n-guard

## Project

MCP server gating an agent's proposed actions against a rulebook:
deterministic `check` (allow / confirm / deny with a trace) and model-backed
`advise` (interpretation of rules, applicability to an agent/situation). Sixth
in the collection (look-n-see, scrape-n-search, store-n-recall, listen-n-speak,
weigh-n-decide). **Status: docs-first, implementation not started** — the
design lives in `README.md` (draft tool surface, rulebook format, verdict
semantics, config) and `CONTEXT.md` (vocabulary). The open design questions at
the bottom of README gate the spec; resolve them with the user before
implementing.

## Conventions

- Use `CONTEXT.md` terms exactly: rule, rulebook, action, situation, match,
  verdict, trace, advice, gate, confirm, engine, matcher. They are not
  synonyms for anything else.
- Python 3.10+. Collection parity: setuptools src layout, package
  `gate_n_guard`, console script `gate-n-guard` (stdio transport), module
  `gate_n_guard.server:main`. No second toolchain (Node/Cargo/…) without a
  stated reason the stdlib-plus-mcp baseline cannot serve.
- Dependency baseline: `mcp>=2` + `httpx>=0.27` (parity with weigh-n-decide's
  `pyproject.toml`). Everything else stdlib; engines behind extras. Tests use
  stdlib `unittest`, zero dev deps.
- Determinism: `check` is pure local compute — no network, no randomness,
  stable output ordering; same action + rulebook + situation → same verdict and
  trace. `record`/`audit` are journal I/O (append JSONL / read back), the only
  file-touching tools; `advise` is the only networked tool, and its output is
  advice, never a verdict.
- Stateless mechanics: rulebooks, actions, and situations are passed per call;
  nothing is kept in memory. The only persistence is the optional append-only
  journal (`GATE_N_GUARD_JOURNAL`, JSONL) that `record` writes and `audit`
  reads — decision history, never rule storage; no rewrite or delete path.
- Every tool returns structured output the calling model can act on (JSON
  where it matters, not prose-only). Missing rule ids, action fields, or
  situation paths degrade to clear errors, never tracebacks.
- Rule matching may reuse weigh-n-decide's literal condition grammar patterns
  (recursive-descent, no `eval()`, fact paths, error on missing path) — the
  matcher is the one place rule language lives; nothing else parses predicates.
- Engine pluggability follows look-n-see's pattern (module trio
  `openrouter.py` / `client.py` / `config.py`): engine param per call,
  env-configured default, fallback chain, clear error without keys.
- Config via environment variables (`GATE_N_GUARD_*`) with sane defaults;
  secrets from env only, never hardcoded or committed.
- OMP's default MCP request timeout is 30 s — `check` must stay sub-second;
  `advise` needs a raised registration timeout (README shows 120 s).
- Tool surface stays minimal (`check`, `record`, `audit`, `advise`). A new
  tool needs a README row and a reason it cannot be a param or question kind;
  a new predicate, verdict, or env var needs a README update in the same change.

## Workflow

- The design in `README.md` is the draft spec. Open questions are resolved
  with the user first, one at a time, each framed with its problem context,
  options, and recommendation — never batched.
- When implementation starts, create `BRIEF.md` (requirements) and
  `IMPLEMENTATION.md` (design + release plan) like look-n-see, and keep them
  in sync with decisions made during implementation.
- Docs precede code: a change to the tool surface, rulebook format, or
  verdict semantics lands in README first, then implementation, in the same
  change.
- One commit per logical change; conventional commits, imperative subject.
  Docs-only changes say so.

## Verification

- `python -m compileall` / import smoke test after changes.
- `python -m unittest discover -s tests -q` — the offline suite. The matcher
  is fully unit-testable: precedence, default verdict, missing-path errors,
  glob/contains/in predicates, trace shape, and determinism (same input →
  same verdict) are test targets.
- Server must start and register tools with no keys present
  (`check` fully functional offline; `advise` errors clearly on use, no
  network at startup).
- Live advisory calls are manual smoke (a real MCP call through the harness),
  not part of the suite.