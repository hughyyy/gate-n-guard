# How a policy gate can work — approach comparison

Date: 2026-10-22. Method: primary sources fetched via the scrape-n-search MCP
(GitHub API + project READMEs + vendor docs). Purpose: decide gate-n-guard's
architecture by comparing the ways a gate for agent actions can be built.

## The five approaches

| # | Approach | Shape | Working precedents |
| --- | --- | --- | --- |
| A | **Inline decision service** | An MCP/policy-check tool the agent calls *before* acting; verdict is advisory, caller enforces | AgenticContract `policy_check`; gate-n-guard (this design) |
| B | **Reverse proxy / gateway** | All agent traffic re-routed through a service that enforces at relay time; per-agent or per-scope rules | apache/casbin-gateway; microsoft/mcp-gateway; alifanov/scopegate |
| C | **SDK auto-instrumentation** | Import-time patch wraps every tool call in-process; violation raises before execution | AEGIS agentguard SDK (9 Python frameworks + TS/JS + Go); asqav-sdk |
| D | **MCP wrapper proxy** | Client points at a wrapper around the upstream MCP server; every `tools/call` screened before it reaches upstream | AEGIS `mcp-proxy`; agent-chaperone (MCP proxy + hooks adapter); obsigna (signed receipts proxy) |
| E | **Agent-native permissions/hooks** | The agent product's own permission rules and lifecycle hooks gate built-in tools | Claude Code permission modes/rules + PreToolUse/PostToolUse hooks |

## The five metrics

1. **Enforcement strength** — can a violating action be stopped *before* it
   executes, unconditionally?
2. **Integration cost** — install/config effort: code changes, infra,
   per-agent setup.
3. **Coverage breadth** — which agents, tools, and transports are gated
   (MCP tools, built-in tools, shell, closed-source agents)?
4. **Audit fidelity** — per-action evidence: trace/record completeness,
   tamper-evidence, explainability of the verdict.
5. **Runtime overhead** — per-decision latency and the weight of infrastructure
   that must be up for a decision.

## Matrix

| Approach | Enforcement | Integration cost | Coverage | Audit fidelity | Overhead |
| --- | --- | --- | --- | --- | --- |
| A. Inline decision service | Low (advisory) | Lowest | Narrow (opt-in calls only) | High per call, caller-dependent completeness | Lowest (µs–ms, no infra) |
| B. Reverse proxy / gateway | High (for relayed traffic) | High (service to deploy, traffic re-route) | Broad, but bypassable (direct-connect agents exempt) | High for relayed traffic only | High (network hop + stateful service) |
| C. SDK instrumentation | Highest (in-process, pre-execution raise) | Medium (one import; per-framework support) | Wide inside supported SDKs; none for closed agents | Highest (hash-chained, tamper-evident) | Medium (per-call gateway round-trip) |
| D. MCP wrapper proxy | High (for wrapped servers) | Medium (re-point client, per server) | All MCP tools; built-in tools need a hooks adapter | High (call + result visible at boundary) | Medium (extra process hop per call) |
| E. Agent-native hooks | High (within that agent) | Low (settings files), vendor-specific | Single agent product; built-ins yes, MCP depends on client | Medium (debug logs, no tamper chain) | Medium (subprocess per hook event) |

## Evidence per metric

### 1. Enforcement strength

| Approach | Evidence |
| --- | --- |
| A | AgenticContract: workflow is convention — *"1. policy_check … 2. If denied → stop"*; no intercept path. AgenticContract README (MCP tools, `policy_check`). |
| B | casbin-gateway: *"A tool whose switch is off is taken out of the request before it leaves this machine"*, `permission_error` on refusal; but *"an agent bound directly to a provider … is not held to them"*. casbin-gateway README. microsoft/mcp-gateway is routing/lifecycle, not policy enforcement. |
| C | AEGIS: *"The tool never runs."* Block raises `AgentGuardBlockedError` back into the agent; `blocking_mode`, per-call classification pre-execution. AEGIS README. |
| D | AEGIS `mcp-proxy`: *"Every MCP `tools/call` is policy-checked and anomaly-scored before reaching the upstream server."* agent-chaperone: screens calls *"before they run and tool results before the agent reads them"*. |
| E | Claude Code: permission rules (allow/deny/ask per tool), modes as posture; PreToolUse hooks return a decision before the tool runs (JSON decision / exit codes); hook scripts run with full user permissions — a hook that blocks does block. Claude Code docs (permissions, hooks reference). |

### 2. Integration cost

| Approach | Evidence |
| --- | --- |
| A | One MCP server registration + the agent calling `check` before acting. No hooks, no proxy, no SDK. |
| B | casbin-gateway: full product — installer, agent discovery, hooks it installs into each agent, UI + SQLite + relay. microsoft/mcp-gateway: Kubernetes deployment. scopegate: proxy layer you run. |
| C | AEGIS: `import agentguard; agentguard.auto(...)` — but *supported* frameworks only; per-language SDKs. |
| D | AEGIS: `agentguard mcp-proxy --server npx -y … --agent-id … --blocking` — wrap the server command in the client config. agent-chaperone: MCP proxy + a hooks adapter for the client's built-in tools. |
| E | `.claude/settings.json` permission rules + hook scripts; per agent product, config formats differ (write once per vendor). Claude Code docs. |

### 3. Coverage breadth

| Approach | Evidence |
| --- | --- |
| A | Any agent that can call an MCP tool — but only actions the agent *chooses* to check; built-in tools (Read/Write/Bash) are unchecked unless the client routes them. |
| B | Every agent routed through the gateway (casbin-gateway manages Claude Code, Codex, Cursor, Gemini CLI, Windsurf, opencode, Hermes, OpenClaw; Codex/Claude Desktop listed as *not held* because they lack the hook). Direct-connect agents bypass. |
| C | Everything inside a supported SDK's `client.messages.create`/tool path; closed-source or binary agents need the proxy instead (AEGIS ships HTTP + MCP proxies for them). |
| D | All MCP servers wrapped; non-MCP built-in tools need a separate adapter (agent-chaperone's hooks adapter; AEGIS Claude Code setup hooks built-ins). |
| E | Only that one agent product. Claude Code: built-ins (Read/Write/Bash/Edit) fully gated; MCP servers via `.mcp.json` can be disabled by name (`disabledMcpjsonServers`). |

### 4. Audit fidelity

| Approach | Evidence |
| --- | --- |
| A | Trace per call (rules consulted, predicate outcomes) is explainable and diffable by construction; but completeness depends on the caller invoking `check` — a missed call is invisible. |
| B | casbin-gateway records every relayed request (LLM Records, model routing, policy that fired); only traffic through the proxy is visible. |
| C | Strongest: AEGIS hash-chained, optional Ed25519-signed traces, RFC 6962 transparency log, violation records grouped by policy with the exact matching condition. |
| D | Call + result both visible at the boundary (agent-chaperone screens results too); obsigna adds cryptographically signed receipts per action. |
| E | Hook lifecycle entries in the debug log (`Hooks: PreToolUse:Write … status 0 (31ms)`), stop hooks; no tamper-evident chain, log-based. |

### 5. Runtime overhead

| Approach | Evidence |
| --- | --- |
| A | AgenticContract benchmarks: 0.03 ms single policy, 2.9 ms for 10K policies; no service to keep up. |
| B | A service must be running (DB, routing, UI); every call pays a relay hop. |
| C | In-process wrap plus a gateway API round-trip per tool call (AEGIS gateway is a service; anomaly detection, PII, cost per call). |
| D | One extra process hop per call (stdio wrapper → gateway check → upstream), no agent-side latency. |
| E | Each hook execution spawns a process per tool event; async hooks run background processes (cost visible in `Hooks:` duration lines). |

## What this means for gate-n-guard

gate-n-guard is approach **A** — and the comparison explains why the suite
fits there, not in B/C/D:

- A is the only shape that stays **stateless, stdio-only, dependency-light**
  (collection constraints); B/C/D each require a service, SDKs across
  languages, or vendor hooks.
- A's weakness (advisory, caller-dependent) is exactly why the README's
  non-goals say enforcement is out of scope: B/C/D/E exist to *enforce*.
- The verdict semantics and precedence questions in README still borrow from
  every other row: three-way verdict from A (AgenticContract
  `require_approval`) and E (Claude Code allow/ask/deny); ordered
  first-match rules from B (Casbin); fail-safe "strictest wins" option from C
  (AEGIS); trace shape from C.

Practical consequence: the natural future integrations for gate-n-guard are a
Claude Code PreToolUse hook and/or an MCP wrapper proxy that call `check` — a
README-level note, not v1 scope.

Sources (all fetched 2026-10-22 via scrape-n-search MCP):

- [AgenticContract README](https://github.com/agentralabs/agentic-contract)
- [casbin-gateway README](https://github.com/apache/casbin-gateway)
- [AEGIS README](https://github.com/Justin0504/Aegis)
- [agent-chaperone](https://github.com/agent-chaperone/agent-chaperone)
- [obsigna](https://github.com/agent-receipts/obsigna)
- [scopegate](https://github.com/alifanov/scopegate)
- [microsoft/mcp-gateway](https://github.com/microsoft/mcp-gateway)
- Claude Code docs: [permissions](https://code.claude.com/docs/en/permissions),
  [hooks reference](https://code.claude.com/docs/en/hooks)
