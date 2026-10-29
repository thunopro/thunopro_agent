# thunopro_agent

**V1 — a supervisor that coordinates six coding-agent roles through tmux.**

The goal is not to make six models chat endlessly. One commander decomposes a
request into bounded tasks, routes each task to a capable agent/model, persists
the work and messages, and checks results before handing them back. Humans can
inspect and override every decision.

## First run

Requirements: Python 3.10+, tmux, and OpenCode CLI authenticated with at least
one provider (`opencode auth list`). No Python packages are required.

```bash
cd ~/Tool_agent
python3 -m thunopro_agent init
python3 -m thunopro_agent plan "Research the architecture and implement a durable task board"
python3 -m thunopro_agent team bind --session grid-1793287148-2279814
python3 -m thunopro_agent status
```

`team bind` labels six panes in the selected tmux window without sending input
or interrupting their current processes. The first pane is commander; the
remaining panes are scout, architect, builder, reviewer, and tester. You can
inspect the mapping using `team show`.

Create an isolated demo grid (does not alter an existing tmux session):

```bash
python3 -m thunopro_agent team create --session thunopro-v1
```

## V1 coordination contract

1. **Commander owns the plan.** It creates small tasks with explicit outputs,
   dependencies, and acceptance checks. Workers do not independently expand
   the scope.
2. **Quota-first routing after a quality gate.** The router refreshes the live
   OpenCode catalog, filters for task capability and a 3/5 minimum quality,
   then prioritizes verified request/token allowance. High and unlimited quota
   beat small quality differences; free models win close ties. Unknown quota is
   not guessed, and token price is not a routing penalty. Each decision records
   the selected model and rationale; `--model` can override the route.
3. **Durable state, not terminal text, is the source of truth.** Tasks, agent
   assignments, messages, events, and evaluations live in a local SQLite WAL
   database at `.thunopro/state.sqlite3`.
4. **Bounded agent-to-agent communication.** Workers send addressed messages or
   replies through the mailbox. The receiver chooses what to do; a message is
   not an instruction with higher authority than the user or commander.
5. **Review before merge.** Parallelize research and independent analysis. Do
   not allow concurrent write access to the same checkout by default. Builder
   edits; reviewer and tester report independently; commander reconciles.
6. **Bound autonomy.** Dispatch runs only in the pane and tmux session that own
   the task. Failed model calls have at most three catalog-based fallbacks;
   successful work waits for commander review, with at most two rework cycles.
   Planning never dispatches work implicitly.

## Six roles

| Slot | Role | Responsibility |
|---|---|---|
| 0 | commander | Decompose, route, resolve dependencies, accept/reject results |
| 1 | scout | External research and repository reconnaissance |
| 2 | architect | Designs, interfaces, tradeoffs, risks |
| 3 | builder | Implements one bounded change |
| 4 | reviewer | Independent correctness and security review |
| 5 | tester | Reproducible test and acceptance evidence |

These are job roles, not model identities. Models are selected per task from
the configured catalog; unavailable models are excluded. This machine's
OpenCode catalog was observed to include `opencode-go/gpt-6-luna`,
`opencode-go/kimi-k3.1-pro`, `opencode-go/deepseek-v4-pro`,
`opencode-go/glm-5.3`, and `opencode-go/gemini-4-argon` among others. The
catalog is refreshed at runtime rather than assuming every machine has the
same providers.

## CLI

```bash
python3 -m thunopro_agent plan "..." [--kind coding] [--model provider/model]
python3 -m thunopro_agent delegate "Implement feature X"  # commander decomposes; review before dispatch
python3 -m thunopro_agent task add --title "..." --description "..." --agent builder --acceptance "..."
python3 -m thunopro_agent task list
python3 -m thunopro_agent task claim TASK_ID --agent builder
python3 -m thunopro_agent task complete TASK_ID --agent builder --result "..."
python3 -m thunopro_agent task dispatch TASK_ID
python3 -m thunopro_agent task accept TASK_ID --decision accept --notes "review passed"
python3 -m thunopro_agent message send --from builder --to reviewer --body "Please inspect ..."
python3 -m thunopro_agent message inbox --agent reviewer
python3 -m thunopro_agent score
python3 -m thunopro_agent quota-config
python3 -m thunopro_agent team show
```

`init` creates `.thunopro/models.json`. Only set `verified: true` and a
`quota_tier` after checking the provider/account's allowance: `0` unknown,
`1` limited, `2` standard, `3` high, `4` unlimited/generous. Optional monthly
and remaining request/token fields make remaining headroom part of routing.
Free models are preferred
after capability/quality gates; a free label does not prove unlimited quota
and does not outrank a stronger model with verified high allowance.

## Design scorecard (V1, 100 points)

| Dimension | Weight | V1 target | Rationale |
|---|---:|---:|---|
| Task decomposition and bounded ownership | 20 | 16 | Explicit roles, tasks, dependencies; commander decisions remain manual |
| Model/task fit and explainability | 20 | 16 | Live catalog + capability/quality gate + quota-first score; ratings still need outcome calibration |
| Communication and durable state | 20 | 17 | SQLite task/event/mailbox store; workers must poll/use the CLI to receive messages |
| Correctness, review, and test evidence | 15 | 12 | Dedicated reviewer/tester and acceptance field; no automatic quality oracle yet |
| Fault recovery and bounded autonomy | 15 | 12 | Durable attempts, up to three model fallbacks, commander acceptance; worker resume still needs hardening |
| Usability and visibility | 10 | 9 | CLI and tmux mapping; no live dashboard yet |
| **Total** | **100** | **82/100** | Strong V1 core; not yet unattended production orchestration |

**Why not run unlimited agents?** More concurrent agents increase duplicated
work, merge conflicts, and review load. Six slots are enough to cover command,
research, design, implementation, independent review, and tests. Spawn deeper
subagents only for independent, bounded tasks when expected value exceeds
coordination and verification cost.

## Evaluation rubric for a real task

Score each 0–5, with evidence: requirement coverage (25%), correctness/tests
(25%), independent review findings resolved (20%), task completion and
handoff quality (15%), quota headroom and latency against the chosen policy (10%), and
duplicate work avoided (5%). Record model, task kind, outcome, elapsed time,
retries, and review defects. Do not treat token volume or agent count as quality.

## Known V1 limits

- Model scores are transparent heuristics, not benchmark claims; tune from
  outcomes recorded in the local evaluation table.
- OpenCode can list models that a provider/account policy or region still
  cannot run. The worker records failures and tries up to two more suitable
  models; configure quotas and capabilities only from verified account data.
- Tmux controls and agent CLI behavior vary by provider. The first version
  records pane mapping and offers a visible isolated demo session; reliable
  lifecycle/state parsing requires provider-specific adapters.
- SQLite is intended for one local host, not a shared network filesystem.
- This repository does not assume access to GitHub credentials. Publish with
  `gh auth login`, then `gh repo create thunopro_agent --source . --public --push`.

## Research references

The V1 design borrows ideas, not code, from established projects and official
docs:

- [AWS CLI Agent Orchestrator](https://github.com/awslabs/cli-agent-orchestrator):
  supervisor delegation to real CLI processes, provider adapters, and tmux.
- [omux](https://github.com/Happenmass/omux):
  capability-based agent choice, agent adapters, outcome feedback, and
  independent cross-review.
- [tmux-agents](https://github.com/mattmight/tmux-agents):
  pane discovery/control, capture, wait, and inter-pane messaging.
- [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence):
  checkpointing, resumability, and human inspection.
- [Google ADK collaboration patterns](https://adk.dev/workflows/collaboration/):
  coordinator/specialists and bounded task/single-turn agent modes.
- [Agent handoff patterns](https://github.com/langchain-ai/langgraph-swarm-py):
  explicit transfer of control between specialists.

We choose a small local Python/SQLite core for V1 rather than adopting a large
agent framework: the existing OpenCode CLI sessions keep their native auth,
and orchestration remains inspectable and replaceable.
