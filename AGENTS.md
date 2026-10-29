# thunopro_agent project rules

- Work only in the tmux session explicitly selected/bound for this task. Never
  send commands to a different tmux session implicitly.
- The commander is the team lead: decompose first, assign bounded deliverables,
  route each task to the best model using verified quota/request headroom and
  task capability, then independently review and test before acceptance.
- Prefer verified unlimited-token or high-monthly-request models when they fit
  the task. Do not optimize primarily for token cost. Never fabricate quota
  information; update `.thunopro/models.json` only after checking the actual
  provider/account plan.
- Prefer a suitable free model when verified quota/request allowance and
  quality are comparable; a `-free` model name alone does not prove unlimited
  requests or tokens.
- Keep parallel tasks independent. Do not let two workers edit the same files
  in the same checkout; use sequential dependencies or isolated worktrees.
- Persist tasks, outcomes, messages, routing reasons, and evaluation evidence
  in the local SQLite store. Agent count and token volume are not quality.
