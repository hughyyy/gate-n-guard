# gate-n-guard — Project Brief

## Purpose

An MCP (Model Context Protocol) server that gates an agent's proposed actions
against a rulebook: **check** returns a deterministic `allow` / `confirm` /
`deny` verdict with the rules that matched and why, and **advise** asks a
model for an interpretation of a rule or policy, or whether any significant
rules apply to this agent and/or situation. An append-only decision journal
(`record` / `audit`) preserves the human-resolution trail for confirmed and
denied actions. Sixth in the collection (look-n-see, scrape-n-search,
store-n-recall, listen-n-speak, weigh-n-decide).

## Refined requirements (from resolved concept Q&A)

### Gating (deterministic, no model, no network)

- **Rulebook**: ordered list of rules + no-match `default` + a `precedence`
  mode. A rule = id, `when` (list of predicate entries; any entry holds →
  rule matches; an entry is a conjunction of predicates), verdict, reason.
- **Precedence — resolved (both ship, rulebook-selected)**
  `first-match`: first matched rule's verdict decides; later rules not
  consulted. `strictest`: every rule evaluated; strictest matched verdict
  wins (`deny` > `confirm` > `allow`). Default `first-match`.
- **No-match default — resolved (configurable, default `allow`)**
  Rulebook-level `default` accepts `allow` / `confirm` / `deny`;
  `allow`-by-default means policy expresses prohibitions, and the trace's
  `default_applied: true` flags unexamined actions for fail-closed callers.
- **Predicates — resolved**: bare value = type-strict equality; single-op
  objects: `glob` (fnmatch), `contains`, `matches` (regex search, compiled
  and syntax-checked at rulebook load), `in`, `eq`, `lt`/`lte`/`gt`/`gte`.
  Paths: `tool`, nested `arguments.<path>`, dotted `situation.<path>`.
  Missing paths error only when consulted (left-to-right short-circuit);
  no `eval()`. Weigh-n-decide-grammar alignment documented as a possible
  later evolution, not v1.
- **Trace**: every consulted rule with per-predicate results, the mode,
  the default flag, and `rulebook_ref` `{source, sha256}` naming the exact
  rulebook version. Same input → identical output, always.
- **Rulebook source — resolved (param wins)** `rules` param: inline JSON or
  `file:<path>` ref; omitted → `GATE_N_GUARD_RULEBOOK` loaded once at
  startup. Failed params error loudly, never silently fall back.

### Journal (audit trail, resolved)

- Append-only JSONL at `GATE_N_GUARD_JOURNAL`; unset → `record`/`audit`
  error clearly, `check` unaffected. No rewrite/delete path.
- `record`: action, verdict, matched rule ids, optional
  rulebook/outcome/actor/note; server assigns uuid4 id + UTC ISO ts.
- `audit`: newest-first with filters (verdict, outcome, tool, since/before,
  limit ≤ 1000); malformed lines skipped and counted, never fatal.

### Advise (model-backed, resolved — one tool, deterministic apply)

- `question: interpret | apply | gap`; `rule_id` (interpret only) validated
  before any model call; `mode: auto | model` (default `auto`).
- `apply` + `auto` answers deterministically from the matcher when rules
  match (zero tokens); the model runs only when nothing matched or
  `mode: model`; `interpret`/`gap` always use the model.
- Engine pattern ports look-n-see (openrouter.py / client.py / config.py):
  OpenRouter + HuggingFace Inference, `openrouter:`/`hf:` prefixes,
  env-configured default model, fallback chain, retries with exponential
  backoff on 429/5xx/timeouts, clear error without keys.
- Model returns strict JSON per kind, parsed and shape-checked server-side;
  its word is never a verdict.

### Transport and operation

- **stdio** only (local MCP client spawns the server as a subprocess).
- Config via `GATE_N_GUARD_*` env vars + shared `OPENROUTER_API_KEY` /
  `HF_TOKEN`; secrets env-only.
- `check` must stay sub-second (matcher is pure local compute);
  `advise` needs the raised registration timeout (120 s) like look-n-see.

### Tools (v1 surface)

1. `check` — gates one action.
2. `record` — appends one decision record to the journal.
3. `audit` — reads the journal back newest-first with filters.
4. `advise` — interpret / apply / gap over a rulebook.

## Non-goals (v1)

- No enforcement: the server never intercepts or executes; a client that
  ignores a `deny` is out of scope (gateway/hook tools enforce, per
  `research/approaches.md`).
- No rule authorship or policy management; no probabilistic scoring;
  no approval queues (the MCP conversation is the approval channel, the
  journal is the trail).
- Not transport authorization (MCP OAuth 2.1 governs who connects; the gate
  decides what an action may do).

## Open items

None — all seven design questions resolved (Q7: static reasons, `{var}`
documented as a possible extension); the stdio server ships (`server.py`,
`__main__.py`) and registers `check` / `record` / `audit` / `advise`. See
`IMPLEMENTATION.md` for the standing manual smoke.