"""Generate the OpenCode agent definitions used by the team.

- `thuno-worker` is the agent every pane runs under. Its permissions allow the
  Task tool to call only `thuno-sub`, so a worker cannot spawn an arbitrary
  subagent on a metered model.
- `thuno-sub` is a read-mostly helper subagent pinned to a verified unlimited
  model. The model is chosen by the router and written into the file.
"""
from __future__ import annotations

from pathlib import Path

from . import core

AGENT_DIR = core.ROOT / ".opencode" / "agent"

WORKER_TEMPLATE = """---
description: thunopro_agent pane worker. Executes one bounded task assigned by the commander.
mode: primary
permission:
  task:
    "*": deny
    "thuno-sub": allow
---

You are a worker pane in the thunopro_agent six-pane team. You receive one bounded
task with an acceptance check. Stay inside it: change only what the task requires,
verify with real commands, and report CHANGES, COMMANDS, EVIDENCE, BLOCKERS.
Use the Task tool only with subagent_type "thuno-sub" for a bounded helper job.
"""

SUB_TEMPLATE = """---
description: Unlimited-token helper for bounded research, review or test checks. Does not edit files.
mode: subagent
model: {model}
permission:
  edit: deny
---

You are a helper subagent for a worker in the thunopro_agent team. Answer only the
narrow question you were given. Report concrete findings with file paths and line
numbers, and state the commands you ran and their results. Do not modify files.
"""


def sub_model_id(models: list[core.Model] | None = None) -> str:
    pool = models if models is not None else core.unlimited_models()
    winner, _, _ = core.choose_model("coding", pool)
    return winner.id


def sync_agent_files(models: list[core.Model] | None = None, directory: Path | None = None) -> dict[str, str]:
    """Write worker and subagent definitions; return the subagent model that was pinned."""
    target = directory or AGENT_DIR
    target.mkdir(parents=True, exist_ok=True)
    model = sub_model_id(models)
    (target / f"{core.WORKER_AGENT}.md").write_text(WORKER_TEMPLATE, encoding="utf-8")
    (target / "thuno-sub.md").write_text(SUB_TEMPLATE.format(model=model), encoding="utf-8")
    return {"subagent_model": model, "directory": str(target)}
