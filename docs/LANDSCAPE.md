# Landscape — tmux-based agent orchestrators

V1 design notes for `thunopro_agent`: comparison of three reference projects,
what we borrow, and what we deliberately do not borrow. Research source:
independent subagent review of public GitHub README/docs (2026-10).

## Sources

- AWS CLI Agent Orchestrator (CAO) — https://github.com/awslabs/cli-agent-orchestrator
  (Apache-2.0; Python, PyPI `cli-agent-orchestrator`)
- omux — https://github.com/Happenmass/omux
  (MIT; Node 20+, npm `@happenmass/omux`)
- tmux-agents — https://github.com/mattmight/tmux-agents
  (MIT; Python 3.11+, uv, tmux 3.2a+)

## Summary

| Project | Primary role | Stack | tmux role | Delegation | Messaging | Persistence | Routing |
|---|---|---|---|---|---|---|---|
| CAO | Fleet orchestrator (supervisor→workers) | Python (FastAPI + React) | Backend abstraction | Handoff / Assign / Send Message | SQLite inbox | SQLite (sessions/terminals/inbox/memory/workflows) | Launch-decision platform + profiles |
| omux | Meta-agent loop over coding CLIs | Node/TS (Express + WS) | Shell-out bridge + state detector | MainAgent assigns; execute-then-review | Shared files / pane scraping (no structured bus verified) | SQLite (agents/conversations/memory/learning) | Fit-based among enabled adapters |
| tmux-agents | tmux agent control plane (MCP/CLI) | Python (FastMCP) | Source of truth | None (control only) | tmux user-option channels | None (tmux user options only) | None |

## Patterns borrowed (V1)

From **tmux-agents** (highest value, lowest risk):
- Canonical refs = socket path + session/window/pane IDs (not names).
- Batched inventory via `list-sessions`/`list-windows -a`/`list-panes -a` with custom `-F`.
- `tmux -N` for all read probes (never accidentally spawn a server).
- Alternate-screen capture (`screen: auto|primary|alternate`) for TUIs like Claude.
- Separate `send_text` (literal) vs `send_keys` (key interpretation).
- Pane metadata in tmux user options (`@thunopro.meta`).
- Redaction of tokens in captured pane text before returning to agents.
- Prefix-scoped visibility for safety.
- Test isolation using a throwaway tmux server on a private socket.

From **CAO**:
- Three message classes: sync handoff, async assign, direct send-message — map to bounded agent-to-agent messages with explicit size limits and ack.
- SQLite-backed inbox with event-driven + reconciliation delivery.
- Provider/adapter split: terminal backend vs agent adapter responsibilities.
- Tool allowlists/roles and a safe mode that does not register destructive tools.

From **omux** (selectively):
- Push-based waiting instead of polling (zero LLM calls while panes grind; wake on settle).
- Cross-vendor execute-then-review concept (optional V1.1, not core).
- Crash recovery: re-adopt managed tmux sessions, restore conversation from SQLite, repair interrupted tool calls.
- Per-agent tool scoping aligned with capability-first routing.

Shared persistence pattern: CAO and omux both use SQLite as the source of truth for
orchestration state; tmux user options hold only pane-local metadata. `thunopro_agent`
keeps SQLite authoritative (already planned) and treats tmux as the execution
substrate.

## Deliberate V1 tradeoffs — what we do NOT borrow, and why

1. **CAO's full client-server + web UI + plugins + telemetry stack.** V1 is a 6-pane supervisor, not a fleet platform; FastAPI/React/OTel multiply failure surface and maintenance. Keep CLI + SQLite + tmux; web UI deferred.
2. **omux's single general MainAgent loop as the supervisor model.** V1 wants an explicit commander that decomposes and delegates with capability-first routing; omux's one loop conflates scheduling and judgment and relies on regex pane scraping. Keep structured SQLite tasks/messages and explicit routing.
3. **omux's pane-scraping state detection as primary.** Brittle across TUI versions. V1 relies on bounded messages + tmux pane metadata + process checks; cooperative hooks can be a later milestone.
4. **tmux-agents' statelessness.** V1 needs SQLite state for tasks/outcomes/messages/routing reasons/evaluation evidence; a stateless control plane would lose auditability and recovery.
5. **CAO's broad provider matrix in V1.** Supporting 12+ CLIs requires adapter maintenance; V1 targets the 6-pane supervisor with selected models and adds adapters incrementally.
6. **omux's default `0.0.0.0` binding / mDNS.** Security risk; V1 binds loopback by default with explicit opt-in to widen.
7. **tmux-agents' inter-pane channels as the only messaging.** They live in tmux user options with no persistence/audit. V1 uses SQLite-backed bounded messages with size limits and evidence.
8. **Git-worktree isolation.** Adds complexity for 6 panes; revisit when parallel edits collide.
9. **CAO's launch-decision platform wholesale.** Too heavy; V1 uses simpler capability-first routing with verified quota/request headroom recorded in SQLite, not a full decision graph.
10. **omux's memory/embedding stack (sqlite-vec + 5 providers).** Valuable but out of V1 scope (6-pane supervisor + bounded messages); defer to V2 to avoid scope creep.

## Unverified / gaps

- CAO exact SQLite schema and whether inbox messages have size limits — not verified from fetched docs (only `clients/database.py` ownership stated).
- omux whether there is any structured inter-agent message bus — README describes shared files/pane scraping; unverified.
- tmux-agents channel message size/retention limits — not specified in fetched README/PLAN.
- tmux-agents maintenance status / canonical repo (mattmight vs `woonyong-choi` PyPI homepage) — unverified.
- CAO "routing" whether capability-scored or provider-allowlist based — not verified beyond launch-decisions docs.
- Star counts/versions are point-in-time from search and may have changed.