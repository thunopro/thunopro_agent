"""Commander loop: plan -> dispatch ready tasks to idle panes -> review -> accept/rework -> summary.

Every LLM call goes through `core.run_leader` or the worker path and uses only
verified unlimited models. Dispatch only reaches panes of the explicitly named
tmux session, and only when the pane is an idle shell in the project root.
"""
from __future__ import annotations

import json
import subprocess
import time
from typing import Any, Callable

from . import agents, context, core, scorecard

ROLES_WITHOUT_COMMANDER = ("scout", "architect", "builder", "reviewer", "tester")
MAX_TASKS = 8
Log = Callable[[str], None]


def _require_team(session: str) -> None:
    with core.connect() as db:
        roles = {row["role"] for row in db.execute(
            "SELECT role FROM panes WHERE session_name=?", (session,)).fetchall()}
    missing = [role.name for role in core.ROLES if role.name not in roles]
    if missing:
        raise ValueError(f"tmux session {session!r} is not a bound six-role team (missing {', '.join(missing)}). "
                         f"Run: thuno up --session {session}")


def _validate_tasks(raw_tasks: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_tasks, list) or not 1 <= len(raw_tasks) <= MAX_TASKS:
        raise ValueError(f"Commander plan must contain between 1 and {MAX_TASKS} tasks")
    roles = set(ROLES_WITHOUT_COMMANDER)
    result = []
    for index, task in enumerate(raw_tasks):
        if not isinstance(task, dict) or task.get("agent") not in roles:
            raise ValueError(f"Task {index} has an invalid role")
        title, description = task.get("title"), task.get("description")
        if not isinstance(title, str) or not title.strip() or not isinstance(description, str) or not description.strip():
            raise ValueError(f"Task {index} needs a title and description")
        kind = task.get("kind") if task.get("kind") in core.TASK_REQUIREMENTS else None
        deps = task.get("depends_on", [])
        if not isinstance(deps, list) or any(not isinstance(dep, int) or dep < 0 or dep >= index for dep in deps):
            raise ValueError(f"Task {index} has invalid dependencies; only earlier task indexes are allowed")
        result.append({"title": title.strip(), "description": description.strip(), "kind": kind,
                       "agent": task["agent"], "acceptance": str(task.get("acceptance", "")),
                       "depends_on": deps})
    return result


def _plan_prompt(objective: str, context_text: str, kinds: list[str]) -> str:
    return (
        "You are the COMMANDER of a six-pane tmux team (roles: scout, architect, builder, reviewer, tester). "
        "Decompose the user request into the smallest set of bounded tasks (2-6 usually).\n"
        "Rules:\n"
        "- Each task has one role, one concrete deliverable, and a checkable acceptance criterion.\n"
        "- Put a builder task before the reviewer/tester tasks that check its output, using depends_on.\n"
        "- Reviewer and tester tasks may run while a builder dependency is awaiting review.\n"
        "- Never run two tasks that edit the same files in parallel; chain them with depends_on.\n"
        "- Research and review tasks may run in parallel.\n"
        "- Do not create a task for the commander.\n"
        f"- kind must be one of: {', '.join(kinds)}.\n"
        'Return ONLY JSON: {"tasks":[{"title":"...","description":"...","acceptance":"...",'
        '"kind":"...","agent":"scout|architect|builder|reviewer|tester","depends_on":[0]}]}\n\n'
        f"MACHINE AND TEAM CONTEXT:\n{context_text}\n\nUSER REQUEST:\n{objective}\n"
    )


def plan_objective(objective: str, session: str | None, model_override: str | None = None,
                   context_text: str | None = None, log: Log = print) -> tuple[list[dict[str, Any]], str]:
    pool = core.unlimited_models()
    if model_override:
        candidates = [model for model in pool if model.id == model_override]
        if not candidates:
            raise ValueError(f"Model {model_override!r} is not a verified unlimited model")
    else:
        candidates = [model for model, _, _ in core.rank_models("reasoning", pool, context=objective)[:3]]
    kinds = core.supported_kinds(pool)
    ctx = context_text if context_text is not None else context.snapshot(session, max_chars=4000)
    prompt = _plan_prompt(objective, ctx, kinds)
    errors: list[str] = []
    parse_failure: str | None = None
    for model in candidates:
        log(f"[commander] planning with {model.id}")
        code, output = core.run_leader(prompt, "plan", model.id, reference=session or "-")
        if code != 0:
            errors.append(f"{model.id}: exit={code}; {output[-500:]}")
            continue
        data = core.extract_json_object(output, "tasks")
        if data is None:
            parse_failure = f"Commander did not return JSON task plan: {output[-500:]}"
            errors.append(f"{model.id}: no JSON plan")
            continue
        return _validate_tasks(data["tasks"]), model.id
    if parse_failure:
        raise ValueError(parse_failure)
    raise RuntimeError("Commander planning failed on all candidates: " + "\n".join(errors))


def _review_prompt(task: dict[str, Any], tail: str) -> str:
    return (
        "You are the COMMANDER reviewing one worker report. Decide whether the work meets the acceptance check.\n"
        'Return ONLY JSON: {"verdict":"accept" or "rework","notes":"short, specific, actionable"}.\n'
        'Use "accept" only if the report gives evidence (commands run with results, or concrete changes) that the '
        'acceptance check is met. Otherwise use "rework" and state exactly what is missing.\n\n'
        f"TASK {task['id']} (role {task['assigned_agent']}): {task['title']}\n"
        f"DESCRIPTION: {task['description']}\nACCEPTANCE: {task['acceptance']}\n\n"
        f"WORKER REPORT (tail):\n{tail}\n"
    )


def _reviewer_model(worker_model: str | None) -> str:
    pool = core.unlimited_models()
    try:
        ranked = [model.id for model, _, _ in core.rank_models("review", pool)]
    except RuntimeError:
        ranked = [model.id for model in pool]
    for model_id in ranked:
        if model_id != worker_model:
            return model_id
    return ranked[0]


def review_task(task: dict[str, Any], log: Log = print) -> str:
    """Commander decision for an awaiting_review task. Returns 'accept', 'rework', 'escalate' or 'undecided'."""
    model_id = _reviewer_model(task.get("selected_model"))
    tail = (task.get("result") or "")[-6000:]
    code, output = core.run_leader(_review_prompt(task, tail), "review", model_id, reference=task["id"])
    data = core.extract_json_object(output, "verdict") if code == 0 else None
    if data is None:
        log(f"[commander] could not parse review for {task['id'][:8]}; leaving for human decision")
        return "undecided"
    verdict = str(data.get("verdict", "")).lower()
    notes = str(data.get("notes", ""))[:1000]
    if verdict == "accept":
        core.task_accept(task["id"], "accept", notes)
        log(f"[commander] accepted {task['id'][:8]} ({task['assigned_agent']}) reviewed by {model_id}: {notes}")
        return "accept"
    if verdict == "rework":
        with core.connect() as db:
            retries = db.execute("SELECT COUNT(*) FROM events WHERE task_id=? AND event='task.rework_requested'",
                                 (task["id"],)).fetchone()[0]
        if retries >= 2:
            log(f"[commander] {task['id'][:8]} needs rework but the two-cycle limit is reached; escalating to human")
            return "escalate"
        core.task_accept(task["id"], "rework", notes)
        log(f"[commander] rework {task['id'][:8]} ({task['assigned_agent']}): {notes}")
        return "rework"
    log(f"[commander] unknown verdict {verdict!r} for {task['id'][:8]}; leaving for human decision")
    return "undecided"


def _tasks_for(session: str, ids: set[str]) -> list[dict[str, Any]]:
    with core.connect() as db:
        rows = db.execute("SELECT * FROM tasks WHERE tmux_session=? ORDER BY created_at", (session,)).fetchall()
    return [dict(row) for row in rows if row["id"] in ids]


def run_objective(objective: str, session: str, model_override: str | None = None, max_minutes: int = 45,
                  poll_seconds: float = 3.0, log: Log = print) -> dict[str, Any]:
    _require_team(session)
    pool = core.unlimited_models()
    synced = agents.sync_agent_files(pool)
    log(f"[thuno] session={session} subagent={synced['subagent_model']} unlimited_pool="
        f"{', '.join(model.id for model in pool)}")
    tasks, planner = plan_objective(objective, session, model_override, log=log)
    created: list[dict[str, Any]] = []
    for index, task in enumerate(tasks):
        deps = [created[dep]["id"] for dep in task["depends_on"]]
        row = core.add_task(task["title"], task["description"], task["agent"], task["kind"],
                            depends_on=deps, session=session, acceptance=task["acceptance"])
        created.append(row)
        log(f"[plan] {index} {row['agent']:<9} model={row['model']} deps={[d[:8] for d in deps]} {row['title']}")
    ids = {row["id"] for row in created}
    deadline = time.time() + max_minutes * 60
    dispatched_notice: set[str] = set()
    escalated: set[str] = set()
    timed_out = False
    while True:
        rows = _tasks_for(session, ids)
        by_id = {row["id"]: row for row in rows}
        for row in rows:
            if row["status"] != "queued":
                continue
            failed_deps = [dep for dep in _deps(row) if by_id.get(dep, {}).get("status") == "failed"]
            if failed_deps:
                core.block_task(row["id"], "dependency failed: " + ", ".join(d[:8] for d in failed_deps))
                log(f"[commander] blocked {row['id'][:8]} ({row['assigned_agent']}): dependency failed")
        rows = _tasks_for(session, ids)
        for row in rows:
            if row["status"] == "awaiting_review" and row["id"] not in escalated:
                outcome = review_task(row, log)
                if outcome in {"escalate", "undecided"}:
                    escalated.add(row["id"])
        rows = _tasks_for(session, ids)
        by_id = {row["id"]: row for row in rows}
        busy_roles = {row["assigned_agent"] for row in rows if row["status"] == "running"}
        if time.time() < deadline:
            for row in rows:
                if row["status"] != "queued" or row["assigned_agent"] in busy_roles:
                    continue
                deps_ready = True
                for dep in _deps(row):
                    dep_status = by_id.get(dep, {}).get("status")
                    if dep_status == "done":
                        continue
                    if dep_status == "awaiting_review" and row["assigned_agent"] in ("reviewer", "tester"):
                        continue
                    deps_ready = False
                    break
                if not deps_ready:
                    continue
                try:
                    pane = core.dispatch_task(row["id"])
                except (ValueError, subprocess.SubprocessError) as exc:
                    if row["id"] not in dispatched_notice:
                        log(f"[commander] waiting to dispatch {row['assigned_agent']}: {exc}")
                        dispatched_notice.add(row["id"])
                    continue
                busy_roles.add(row["assigned_agent"])
                log(f"[dispatch] {row['assigned_agent']} pane={pane} task={row['id'][:8]} "
                    f"model={row['selected_model']} {row['title']}")
        active = [row for row in rows if row["status"] in {"queued", "running"}
                  or (row["status"] == "awaiting_review" and row["id"] not in escalated)]
        if not active:
            break
        if time.time() >= deadline:
            timed_out = True
            log("[commander] time budget exhausted; stopping dispatch and reporting current state")
            break
        time.sleep(poll_seconds)
    final = _tasks_for(session, ids)
    summary = {
        "objective": objective,
        "session": session,
        "planner_model": planner,
        "tasks": [{"id": row["id"], "agent": row["assigned_agent"], "status": row["status"],
                   "model": row["selected_model"], "title": row["title"]} for row in final],
        "needs_human": sorted(escalated),
        "timed_out": timed_out,
        "scorecard": scorecard.compute(session, task_ids=ids),
    }
    return summary


def smoke_test(session: str | None = None, objective: str = "Smoke test the six-pane orchestration",
               model_override: str | None = None, max_minutes: int = 10,
               log: Log = print) -> dict[str, Any]:
    """Run a small end-to-end orchestration smoke test and return its summary."""
    target = session or _active_session_or_none()
    if not target:
        raise ValueError("Bind the target six-pane tmux session first: team bind --session NAME")
    return run_objective(objective, target, model_override, max_minutes, log=log)


def _active_session_or_none() -> str | None:
    try:
        from . import core as _core
        with _core.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key='active_session'").fetchone()
            return row[0] if row else None
    except Exception:
        return None


def _deps(row: dict[str, Any]) -> list[str]:
    return list(json.loads(row["depends_on"] or "[]"))
