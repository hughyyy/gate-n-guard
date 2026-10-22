# gate-n-guard

A lightweight [MCP](https://modelcontextprotocol.io) server that gates an
agent's proposed actions against a rulebook: **check** returns a deterministic
**allow / confirm / deny** verdict with the rules that matched and why, and
**advise** asks a model for an interpretation of a rule or policy, or whether
any significant rules apply to this agent and/or situation. Sixth in the
collection: [look-n-see](../look-n-see) (vision/OCR), [scrape-n-search](../scrape-n-search)
(web), [store-n-recall](../store-n-recall) (memory), [listen-n-speak](../listen-n-speak)
(speech), [weigh-n-decide](../weigh-n-decide) (decisions), gate-n-guard (policy).

> **Status: in progress** — docs-first: this README is the draft spec, and the
> open design questions at the bottom gate the tool surface. Question 1
> (precedence) is resolved — both modes ship behind a rulebook-level
> `precedence` field — and the deterministic matcher is implemented and
> tested (`src/gate_n_guard/rules.py`). The remaining questions gate the MCP
> surface (`check`/`advise` wiring), not the matcher.

## The idea

Agents act fast and at machine speed; a policy check between "the model decided"
and "the tool ran" is the cheapest place to enforce what an agent may do.
Three gaps this closes for an agent:

- **Deterministic gating where the model is the weak link.** A model is a poor
  policy enforcer: it forgets rules under pressure, rationalizes, and is not
  reproducible. A rulebook of literal matches over the proposed action is pure
  local compute — the same action + rulebook always yields the same verdict,
  with a trace naming every rule consulted. No tokens spent re-reasoning policy
  that is pure data.
- **Three verdicts, not a boolean.** `allow` and `deny` cover the clear cases;
  `confirm` is the honest middle — the rulebook says this action needs a human
  before it runs. The gate reports the verdict and the calling agent (or its
  harness) decides how to surface it; the gate never blocks anything by itself.
- **Advisory reading of policy.** Natural-language rules ("never deploy to
  prod on a Friday") don't compile to exact matches. `advise` interprets rules
  and answers "does anything here apply to me?" — the model reads the rulebook
  with the situation in hand and returns structured findings the agent can act
  on. Only `advise` talks to a model; the gate itself is mechanical.

The server is **stateless** — rulebooks and actions are passed in with each call
and nothing is stored. `check` is deterministic and never touches the network;
`advise` is the only networked call.

## Tool surface (draft)

| Tool | Params | Returns |
| --- | --- | --- |
| `check` | `rules` (required: rulebook, inline or ref), `action` (required: tool + arguments), `situation`? (agent id, context facts) | verdict `allow` / `confirm` / `deny`, matched rule(s), per-rule reasons, trace of rules consulted, no-match default applied |
| `advise` | `rules` (required), `situation`? (agent + context), `question` (required: interpret / apply / gap), `engine`?, `model`? | structured advice: applicable rules with interpretations, answers to the question, caveats |

Input discipline (house rules): rulebooks are JSON or JSON path refs, actions
are `{tool, arguments}` objects, situations are JSON objects of facts the
rulebook can match against; every tool returns structured JSON the calling
model can act on, never prose-only.

## Rulebooks (draft)

Canonical v1 shape — an ordered list of rules, each a match plus a verdict:

```json
{
  "default": "allow",
  "precedence": "first-match",
  "rules": [
    {
      "id": "no-rm-rf",
      "when": [
        {"tool": "bash", "arguments": {"command": {"contains": "rm -rf"}}},
        {"tool": {"glob": "shell*"}}
      ],
      "verdict": "deny",
      "reason": "recursive deletes are never needed here"
    },
    {
      "id": "prod-deploy-needs-human",
      "when": [
        {"tool": {"glob": "deploy*"}, "situation.agent": "ci-bot"}
      ],
      "verdict": "confirm",
      "reason": "production deploys require a human"
    }
  ]
}
```

Draft semantics:

- A **rule** matches when any of its `when` entries holds; each entry is a
  conjunction of predicates on the action (`tool`, `arguments`), the agent
  (`situation.agent`), or situation facts (`situation.path.to.fact`).
- Predicates: equality/comparison for literals, `in` for lists, string `glob`
  (fnmatch) and `contains`, and `matches` — regular-expression search
  (stdlib `re`; the pattern is compiled and syntax-checked when the rulebook
  loads, so a bad regex is a load error). `matches` is the casbin/AEGIS
  command-guard precedent (`\bdocker\s+system\s+prune\b`). This is a
  deliberate subset of weigh-n-decide's literal condition grammar, evaluated
  the same deterministic way (no `eval()`, missing fact path is an error, not
  a silent false).
- **Grammar growth:** v1 stops at this predicate subset on purpose.
  Aligning the matcher with weigh-n-decide's full condition grammar (boolean
  composition — `not`/nested `and`/`or` — and arithmetic) is a possible later
  evolution; predicate normalization in the matcher is the single seam where
  that alignment would land, so v1 loses nothing by deferring.
- **Precedence** is a rulebook-level mode, `first-match` (default) or
  `strictest`:
  - `first-match` scans rules in order; the **first** matched rule's verdict
    decides and later rules are not consulted (ordered exceptions: put
    specific rules first — the casbin model).
  - `strictest` evaluates **every** rule; the strictest matched verdict wins
    (`deny` > `confirm` > `allow`), so rule order can never loosen a deny
    (the AEGIS fail-safe model).
  Both modes run in the same deterministic matcher; the trace names the mode
  and shows exactly which rules were consulted and which matched.
- No rule matches → the rulebook's `default` verdict applies (`allow`
  default; `rules.default` may set `confirm` or `deny`).
- Every consulted rule — matched or not — appears in the trace with its
  predicates and their values, so a verdict can be explained and diffed.

## Verdict semantics (draft)

- **allow** — proceed; the action is inside policy.
- **confirm** — the action needs a human before it runs. The gate does not
  block or prompt; the calling agent surfaces the request with the trace as the
  exhibit. How a confirmed action becomes executed is the caller's policy.
- **deny** — do not proceed; the matched rule and reason name the boundary.
- **No-match default — configurable, defaults to `allow` (resolved 2026-10-22).**
  When no rule matches, the rulebook's `default` verdict applies; `default` is
  rulebook-level config (`allow` / `confirm` / `deny`) and the matcher already
  accepts all three. `allow`-by-default means policy expresses prohibitions and
  restrictions, and the trace's `default_applied: true` flags the unexamined
  action so fail-closed callers can treat it as suspect — a rulebook typo that
  silently widens access is visible in every trace, not invisible.

`check` is a decision service, not an enforcement point: a client that ignores
a `deny` is out of scope. Enforcement (intercepting the tool call) is what the
gateway-style tools do (see research notes); this server makes the verdict, the
harness makes it stick.

## Advise (draft)

`advise` is the only model-backed tool, following look-n-see's engine pattern
(OpenRouter / HuggingFace Inference; engine param per call, env-configured
default, fallback chain, clear error without keys). Three question kinds:

1. **interpret** — explain one rule or the whole rulebook in plain language:
   what it permits, requires, or forbids, and edge cases.
2. **apply** — "does anything here apply to me?" Given the rulebook and the
   situation, list applicable rules, how they read for this agent/action, and
   any whose wording is ambiguous enough that `check`'s literal match should
   not be trusted.
3. **gap** — for a given action, whether the rulebook *should* have said
   something and does not (advisory only; never invents verdicts).

The model returns structured JSON (applicable rule ids, per-rule
interpretations, caveats, suggested follow-ups), never prose-only. The model's
word is never a verdict: `check` is the only thing that decides.

## Configuration (draft)

| Variable | Default | Purpose |
| --- | --- | --- |
| `GATE_N_GUARD_ENGINE` | `openrouter` | default advisory engine |
| `GATE_N_GUARD_MODEL` | engine-dependent | default model for `advise` |
| `GATE_N_GUARD_FALLBACKS` | — | fallback chain tried after the default fails |
| `GATE_N_GUARD_RULEBOOK` | — | optional default rulebook path used when `rules` is omitted |
| `OPENROUTER_API_KEY` / `HF_TOKEN` | — | secrets, env only (same as look-n-see) |

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/gate-n-guard   # stdio MCP server
```

### Register with Oh My Pi

`check` runs fine at default timeouts (sub-millisecond local compute);
`advise` needs the raised timeout look-n-see uses:

```json
{
  "mcpServers": {
    "gate-n-guard": {
      "type": "stdio",
      "command": "/absolute/path/to/gate-n-guard/.venv/bin/gate-n-guard",
      "timeout": 120000
    }
  }
}
```

## Non-goals

- No enforcement: the server never intercepts or executes anything; verdicts
  are advice to the caller. Enforcement is a gateway/hook problem (casbin
  gateway, AEGIS mcp-proxy, Claude Code PreToolUse hooks).
- No rule authorship or storage: rulebooks are passed per call (or read from
  an env-configured file); persistent policy management is not v1.
- No probabilistic scoring or learning rules from behavior in v1 — one
  deterministic matcher, executed well.
- No LLM in `check`: `advise` is the only model call.
- Not transport authorization: MCP's OAuth 2.1 authorization spec governs
  *who may connect to a server*; gate-n-guard decides *what an action may do*.
  They are complementary layers (see research notes).

## Research notes (2026-10)

Primary sources fetched via scrape-n-search MCP; this positions the server
against what exists so the surface borrows proven semantics and skips what is
already built. The full architecture comparison (five ways a gate can work,
scored on five metrics, with evidence) lives in
[research/approaches.md](research/approaches.md).

- **MCP authorization** is OAuth 2.1 transport authN (RFC 8707 resource
  indicators, audience-bound tokens, no passthrough) —
  [spec, 2025-06-18](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization).
  Not an action policy layer; gate-n-guard complements it.
- **[casbin-gateway](https://github.com/apache/casbin-gateway)** (Apache,
  Go): per-agent permissions compiled to Casbin policy
  (`p, claude-code, tool:shell/run, use, deny`), **first rule that matches
  decides** (exceptions before defaults), and "policy packs" that match *tool
  call arguments* — command, path, host — not just tool names. Validates the
  rulebook-as-ordered-policy and arguments-matching shape.
- **[AEGIS](https://github.com/Justin0504/Aegis)** (USC FORTIS Lab): gateway
  pipeline with 5 stages ending in `allow` / `pending` / `block`, strictest
  signal wins, fail-safe (missing config = block), and human-in-the-loop
  approval flow (pending → human approve/reject). Same three-way verdict
  family as `allow`/`confirm`/`deny`; AEGIS enforces, gate-n-guard decides.
- **[AgenticContract](https://github.com/agentralabs/agentic-contract)**:
  policy engine MCP server with actions `allow` / `deny` / `require_approval`
  / `audit_only`, `policy_check` before acting, and an
  `approval_request`/`approval_decide` lifecycle — the closest existing
  "policy gate as an MCP server" concept.
- **[Adrian](https://github.com/secureagentics/Adrian)** / **[asqav-sdk](https://github.com/jagmarques/asqav-sdk)**:
  runtime agent security and signed policy-evidence receipts — audit
  machinery gate-n-guard deliberately leaves out of v1.
- **[shellfirm](https://github.com/kaplanelad/shellfirm)** (Rust, ~950★):
  "interceptor" that asks before risky shell commands — a confirmation UI
  precedent for what `confirm` verdicts expect the caller to surface.

Gap this server fills: a dependency-light, stateless, stdio MCP *decision
service* in this collection's house style (deterministic kernel + engine-backed
advisory), with no gateway, SDK patching, or dashboard.

## Open design questions (gating the spec)

Evidence from `research/approaches.md` is cited where the landscape settles
the framing but not the call.

1. **Precedence — RESOLVED (2026-10-22).** Both modes ship: rulebook-level
   `precedence` field, `first-match` (casbin) by default, `strictest` (AEGIS)
   optional. Per-rule `priority` was rejected — either mode covers ordering
   needs without the extra knob. Implemented and tested in the matcher; see
   Rulebooks section.
2. **No-match default — RESOLVED (2026-10-22).** Rulebook-level `default`
   field, `allow` by default, `confirm`/`deny` selectable per rulebook
   (AEGIS-style fail-safe posture remains a configuration choice, not the
   shipping default). Matcher and tests already cover all three values.
3. **Match grammar scope — RESOLVED (2026-10-22).** v1 grammar = the shipped
   predicate subset plus the `matches` regex operator (stdlib `re`, compiled
   and syntax-checked at rulebook load; casbin/AEGIS command-guard
   precedent). Aligning with weigh-n-decide's full condition grammar
   (composition, arithmetic) is documented as a possible later evolution
   (see Rulebooks section), not v1.
4. **Confirm flow** — verdict-only (caller surfaces the human prompt) vs a
   `confirm`/`record` tool that journals decisions to a file for audit
   (store-n-recall-style sqlite or append-only JSONL)? Evidence: AEGIS keeps
   approvals as a queue + dashboard (service shape — out of scope);
   AgenticContract has an `approval_request`/`approval_decide` lifecycle in
   its store. Stateless v1?
5. **Advise surface** — one `advise` tool with a `question` enum, or separate
   tools (`interpret_rule`, `applicable_rules`, `rule_gap`)? Does
   "applicable rules" have a deterministic mode that skips the model entirely?
   Evidence: AgenticContract exposes `policy_check` (deterministic) separate
   from LLM-facing tools; nothing in the landscape makes applicability
   model-only.
6. **Rulebook source** — per-call `rules` param only, env-default file
   (`GATE_N_GUARD_RULEBOOK`), or both with param precedence? How do
   per-call rulebooks stay auditable when an env file is the fallback?
   Evidence: AgenticContract pins a file (`.acon`) as its store; the suite's
   weigh-n-decide passes models per call. 
7. **Reason templating** — static `reason` strings, or `{var}` expansion from
   the matched action/situation ("…because {arguments.command} touches
   {situation.env}")? Evidence: AEGIS surfaces "the exact policy that fired
   and the DSL condition that matched" — the template only has to name the
   matched rule and its predicates.