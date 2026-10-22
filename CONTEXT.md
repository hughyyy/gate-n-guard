# gate-n-guard

A lightweight MCP server that gates an agent's proposed actions against a
rulebook: deterministic allow / confirm / deny verdicts with a trace, and
model-backed advisory interpretation of rules and their applicability to an
agent and/or situation.

## Language

### Artifacts

**Rule**:
One policy statement in a rulebook: a match (predicates over the action and
situation) plus a verdict and a reason. Rules are ordered; the first rule that
matches decides.
_Avoid_: policy (the policy is the intent behind rules — say rule or rulebook),
regulation, directive, guard

**Rulebook**:
The ordered set of rules evaluated per `check` call, passed inline as JSON or
referenced by path. Carries the no-match `default` verdict.
_Avoid_: ruleset (a ruleset is any pile of rules; a rulebook is ordered and
eval-uated), policy, config

**Action**:
The proposed operation being gated: a tool name plus its arguments.
_Avoid_: tool call (the transport event, not the proposal), task, request

**Situation**:
The context facts a rule can match against: agent identity, environment,
anything the caller supplies as JSON. A missing fact path is an error, never a
silent false.
_Avoid_: context (ambiguous), state, facts (see weigh-n-decide: a fact is the
input to its condition evaluator — this server reads the same-shaped objects
but calls the whole bundle the situation)

**Match**:
The holding of a rule's `when` predicates against the action and situation.
_Avoid_: hit (trace jargon), trigger

**Verdict**:
The gate's answer: `allow`, `confirm`, or `deny`.
_Avoid_: decision (that is the human's call — same as weigh-n-decide), result,
judgement

**Trace**:
The per-call record of which rules were consulted, in order, with each
predicate's outcome and the verdict that fell out — reproducible and diffable.
_Avoid_: log, audit, proof

**Record**:
One journaled decision: id, UTC timestamp, the action, the verdict, matched
rule ids, optional rulebook/outcome/actor/note. Written verbatim by `record`;
never mutated or deleted.
_Avoid_: entry (taken by rules), log line (implies unstructured)

**Journal**:
The optional append-only JSONL file (`GATE_N_GUARD_JOURNAL`) that holds
records. Decision history only — rulebooks never live there. Unset journal
means `record`/`audit` error clearly and `check` is unaffected.
_Avoid_: log (unstructured), database, store (that is store-n-recall's term)

**Advice**:
The structured output of `advise`: applicable rules, their interpretations,
caveats, suggestions. Never a verdict.
_Avoid_: recommendation (implies the caller should act on it), suggestion,
analysis (too broad)

### Directions

**Gate**:
The act of checking one proposed action against a rulebook. `check` gates;
`advise` does not.
_Avoid_: screen, vet (both imply human review)

**Confirm**:
The verdict meaning "this action needs a human before it runs". The server
reports it; surfacing and recording the human's answer is the caller's job.
_Avoid_: pending (AEGIS's synonym — the gate does not hold state), approve

### Machinery

**Engine**:
A swappable model implementation behind `advise` — OpenRouter or HuggingFace
Inference. Selected per call, env-configured default, fallback chain. Same
pattern as look-n-see.
_Avoid_: backend, provider, model (an engine has a model; it is not one)

**Matcher**:
The deterministic module that evaluates rules over action + situation. No
model, no network; same input → same verdict and trace.
_Avoid_: engine (reserved for advisory engines), evaluator, kernel (taken by
weigh-n-decide)

**Precedence**:
The rulebook-level setting deciding how competing rule matches combine:
`first-match` (ordered scan; the first matched rule's verdict decides, later
rules are not consulted) or `strictest` (every rule is consulted; the
strictest matched verdict wins — deny > confirm > allow).
_Avoid_: priority (a per-rule knob gate-n-guard does not have), order (that is
only the first-match mode), mode (vague)