from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from typing import Any

from . import agents, context, core, orchestrator, scorecard


def out(value: Any) -> None:
    if isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2))


def _active_session() -> str | None:
    with core.connect() as db:
        row = db.execute("SELECT value FROM meta WHERE key='active_session'").fetchone()
        return row[0] if row else None


def _stderr_log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _plan_objective(objective: str, model_override: str | None = None) -> tuple[list[dict[str, Any]], str]:
    return orchestrator.plan_objective(objective, _active_session(), model_override, log=_stderr_log)


def _watch(session: str, interval: float) -> None:
    while True:
        with core.connect() as db:
            panes = db.execute("SELECT pane_id, role FROM panes WHERE session_name=? ORDER BY role", (session,)).fetchall()
            tasks = db.execute("SELECT id, assigned_agent, status, selected_model, title FROM tasks "
                               "WHERE tmux_session=? ORDER BY created_at DESC LIMIT 12", (session,)).fetchall()
        lines = [f"thunopro watch  session={session}  {time.strftime('%H:%M:%S')}  (Ctrl+C to quit)", ""]
        for pane in panes:
            info = subprocess.run(["tmux", "display-message", "-p", "-t", pane["pane_id"],
                                   "#{pane_current_command}"], capture_output=True, text=True, check=False).stdout.strip()
            lines.append(f"  {pane['role']:<10} {pane['pane_id']:<6} {info or '?'}")
        lines.append("")
        for task in tasks:
            lines.append(f"  {task['id'][:8]} {task['assigned_agent']:<9} {task['status']:<15} "
                         f"{(task['selected_model'] or '-')[:30]:<30} {task['title'][:50]}")
        print("\033[2J\033[H" + "\n".join(lines), flush=True)
        time.sleep(interval)


def _handle(args: argparse.Namespace) -> None:
    if args.command == "init":
        core.initialize()
        try:
            synced: Any = agents.sync_agent_files()
        except RuntimeError as exc:
            synced = f"skipped: {exc}"
        out({"initialized": str(core.DB_PATH), "active_tmux_session": _active_session(),
            "quota_config": str(core.STATE_DIR / "models.json"), "agents": synced})
        return

    if args.command == "worker-run":
        raise SystemExit(core.run_worker_task(args.task_id))

    if args.command == "plan":
        out(core.create_plan(args.request, args.kind, args.model))
        return

    if args.command == "delegate":
        session = args.session or _active_session()
        if not session:
            raise ValueError("Bind the target six-pane tmux session first: team bind --session NAME")
        tasks, commander_model = _plan_objective(args.request, args.model)
        created: list[dict[str, Any]] = []
        for task in tasks:
            dependencies = [created[index]["id"] for index in task.pop("depends_on")]
            created.append(core.add_task(**task, priority=args.priority, depends_on=dependencies, session=session))
        dispatched = core.dispatch_ready(session) if getattr(args, "run", False) else []
        out({"commander_model": commander_model,
             "tmux_session": session, "tasks": created, "dispatched": dispatched,
             "next": "Review the plan, then dispatch selected queued tasks with `task dispatch TASK_ID`."})
        return

    if args.command == "up":
        bindings = core.create_team(args.session) if args.create else core.bind_team(args.session)
        synced = agents.sync_agent_files()
        out({"session": args.session, "panes": bindings, "agents": synced,
             "next": f"thuno run \"<objective>\" --session {args.session}"})
        return

    if args.command == "run":
        session = args.session or _active_session()
        if not session:
            raise ValueError("No tmux session bound. Run: thuno up --session NAME")
        summary = orchestrator.run_objective(args.objective, session, args.model, args.max_minutes, log=_stderr_log)
        out(summary)
        return

    if args.command in {"smoke", "smoke-test", "smoke_test", "smoketest"}:
        session = getattr(args, "session", None) or _active_session()
        if not session:
            raise ValueError("No tmux session bound. Run: thuno up --session NAME")
        objective = getattr(args, "objective", None) or "Smoke test the six-pane orchestration"
        summary = orchestrator.smoke_test(session, objective, getattr(args, "model", None),
                                         getattr(args, "max_minutes", 10), log=_stderr_log)
        out(summary)
        return

    if args.command == "scorecard":
        out(scorecard.compute(args.session or _active_session()))
        return

    if args.command == "reconcile":
        out({"requeued": core.reconcile_stale(args.session or _active_session(), args.max_age)})
        return

    if args.command == "context":
        session = args.session or _active_session()
        out(context.snapshot(session, max_chars=args.max_chars))
        return

    if args.command == "watch":
        session = args.session or _active_session()
        if not session:
            raise ValueError("No tmux session bound. Run: thuno up --session NAME")
        _watch(session, args.interval)
        return

    if args.command == "team":
        if args.action == "bind":
            out(core.bind_team(args.session))
        elif args.action == "create":
            out(core.create_team(args.session))
        else:
            with core.connect() as db:
                rows = db.execute("SELECT * FROM panes ORDER BY session_name, role").fetchall()
            out([dict(row) for row in rows])
        return

    if args.command == "task":
        if args.action == "add":
            deps = [dep for dep in (args.depends_on or "").split(",") if dep]
            out(core.add_task(args.title, args.description, args.agent, args.kind,
                              args.priority, deps, args.model, args.session, args.acceptance))
        elif args.action == "list":
            query, values = "SELECT * FROM tasks", []
            if args.session:
                query += " WHERE tmux_session=?"
                values.append(args.session)
            query += " ORDER BY created_at"
            if getattr(args, "limit", None) is not None:
                query += " LIMIT ?"
                values.append(args.limit)
            with core.connect() as db:
                rows = db.execute(query, values).fetchall()
            out([dict(row) for row in rows])
        elif args.action == "claim":
            core.task_claim(args.task_id, args.agent)
            out({"claimed": args.task_id, "agent": args.agent})
        elif args.action == "complete":
            core.task_complete(args.task_id, args.agent, args.result, args.failed)
            out({"completed": args.task_id, "status": "failed" if args.failed else "awaiting_review"})
        elif args.action == "accept":
            core.task_accept(args.task_id, args.decision, args.notes, args.agent)
            out({"task": args.task_id, "decision": args.decision,
                 "status": "done" if args.decision == "accept" else "queued"})
        elif args.action == "dispatch":
            pane = core.dispatch_task(args.task_id)
            with core.connect() as db:
                row = db.execute("SELECT tmux_session FROM tasks WHERE id=?", (args.task_id,)).fetchone()
            out({"task": args.task_id, "pane": pane, "tmux_session": row[0], "status": "dispatched"})
        elif args.action == "evaluate":
            eval_id = core.record_evaluation(args.task_id, args.score, args.outcome, args.notes)
            out({"evaluation": eval_id, "task": args.task_id})
        return

    if args.command == "message":
        if args.action == "send":
            out({"message_id": core.send_message(args.sender, args.to, args.body, args.kind, args.reply_to)})
        else:
            with core.connect() as db:
                rows = db.execute("SELECT * FROM messages WHERE recipient=? AND status='new' ORDER BY created_at",
                                  (args.agent,)).fetchall()
                if args.mark_read and rows:
                    db.executemany("UPDATE messages SET status='read' WHERE id=?", [(r["id"],) for r in rows])
            out([dict(row) for row in rows])
        return

    if args.command == "score":
        models = core.available_models()
        routes = []
        for kind in core.TASK_REQUIREMENTS:
            if models:
                model, score_value, reason = core.choose_model(kind, models)
                routes.append(dict(kind=kind, model=model.id, score=round(score_value, 2), reason=reason))
        out({"models_available": len(models),
             "quota_metadata": "partially configured" if any(m.quota_tier for m in models) else "unknown for all models",
             "routes": routes})
        return

    if args.command == "status":
        with core.connect() as db:
            tasks = db.execute("SELECT status, COUNT(*) AS count FROM tasks GROUP BY status").fetchall()
            events = db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 12").fetchall()
            active = db.execute("SELECT value FROM meta WHERE key='active_session'").fetchone()
        out({"project": str(core.ROOT), "active_tmux_session": active[0] if active else None,
             "tasks": {row["status"]: row["count"] for row in tasks},
             "recent_events": [dict(row) for row in reversed(events)]})
        return

    if args.command == "quota-config":
        out({"path": str(core.STATE_DIR / "models.json"),
             "instruction": "After checking the provider/account plan, set verified=true and quota_tier (0 unknown, 1 limited, 2 standard, 3 high, 4 generous/unlimited); optionally add verified monthly_requests/remaining_requests and monthly_tokens/remaining_tokens."})
        return


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="thuno", description="Commander-led six-pane tmux agent coordinator")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Initialize the durable local task/message store")
    worker = commands.add_parser("worker-run", help="Internal bounded worker execution command")
    worker.add_argument("task_id")
    plan = commands.add_parser("plan", help="Preview deterministic role/model routing")
    plan.add_argument("request")
    plan.add_argument("--kind", choices=core.TASK_REQUIREMENTS)
    plan.add_argument("--model")
    delegate = commands.add_parser("delegate", help="Ask the commander model to decompose a request into scoped tasks")
    delegate.add_argument("request")
    delegate.add_argument("--session")
    delegate.add_argument("--model", help="Override the commander model")
    delegate.add_argument("--priority", type=int, choices=range(1, 6), default=3)
    delegate.add_argument("--run", action="store_true", help="Dispatch ready tasks after planning")
    team = commands.add_parser("team", help="Bind or create a six-pane tmux team")
    team_actions = team.add_subparsers(dest="action", required=True)
    bind = team_actions.add_parser("bind")
    bind.add_argument("--session", required=True)
    create = team_actions.add_parser("create")
    create.add_argument("--session", required=True)
    team_actions.add_parser("show")
    task = commands.add_parser("task", help="Manage durable, session-scoped tasks")
    task_actions = task.add_subparsers(dest="action", required=True)
    add = task_actions.add_parser("add")
    add.add_argument("--title", required=True)
    add.add_argument("--description", required=True)
    add.add_argument("--agent", choices=[r.name for r in core.ROLES])
    add.add_argument("--kind", choices=core.TASK_REQUIREMENTS)
    add.add_argument("--priority", type=int, choices=range(1, 6), default=3)
    add.add_argument("--depends-on")
    add.add_argument("--model")
    add.add_argument("--session")
    add.add_argument("--acceptance", default="")
    listing = task_actions.add_parser("list")
    listing.add_argument("--session")
    listing.add_argument("--limit", type=int)
    claim = task_actions.add_parser("claim")
    claim.add_argument("task_id")
    claim.add_argument("--agent", required=True, choices=[r.name for r in core.ROLES])
    complete = task_actions.add_parser("complete")
    complete.add_argument("task_id")
    complete.add_argument("--agent", required=True, choices=[r.name for r in core.ROLES])
    complete.add_argument("--result", required=True)
    complete.add_argument("--failed", action="store_true")
    accept = task_actions.add_parser("accept", help="Commander accepts the result or requests bounded rework")
    accept.add_argument("task_id")
    accept.add_argument("--agent", choices=("commander",), default="commander")
    accept.add_argument("--decision", choices=("accept", "rework"), required=True)
    accept.add_argument("--notes", default="")
    dispatch = task_actions.add_parser("dispatch")
    dispatch.add_argument("task_id")
    evaluate = task_actions.add_parser("evaluate")
    evaluate.add_argument("task_id")
    evaluate.add_argument("--score", type=float, required=True)
    evaluate.add_argument("--outcome", required=True)
    evaluate.add_argument("--notes", default="")
    message = commands.add_parser("message", help="Send/read addressed messages between roles")
    message_actions = message.add_subparsers(dest="action", required=True)
    send = message_actions.add_parser("send")
    send.add_argument("--from", dest="sender", required=True, choices=[r.name for r in core.ROLES])
    send.add_argument("--to", required=True, choices=[r.name for r in core.ROLES])
    send.add_argument("--body", required=True)
    send.add_argument("--kind", choices=("notify", "request", "reply", "delegate"), default="notify")
    send.add_argument("--reply-to")
    inbox = message_actions.add_parser("inbox")
    inbox.add_argument("--agent", required=True, choices=[r.name for r in core.ROLES])
    inbox.add_argument("--mark-read", action="store_true")
    up = commands.add_parser("up", help="Bind (or create with --create) the six-pane team and sync agent files")
    up.add_argument("--session", required=True)
    up.add_argument("--create", action="store_true", help="Create a new six-pane session instead of binding one")
    run = commands.add_parser("run", help="Plan, dispatch, review and summarise an objective across the team")
    run.add_argument("objective")
    run.add_argument("--session", help="tmux session to drive (defaults to the bound active session)")
    run.add_argument("--model", help="Verified unlimited model for the commander (must be in the unlimited pool)")
    run.add_argument("--max-minutes", type=int, default=45)
    scorecard_parser = commands.add_parser("scorecard", help="Self-score team efficiency from durable evidence")
    scorecard_parser.add_argument("--session")
    reconcile_parser = commands.add_parser("reconcile", help="Requeue stale running tasks")
    reconcile_parser.add_argument("--session")
    reconcile_parser.add_argument("--max-age", type=int, default=core.WORKER_TIMEOUT_SECONDS + 300)
    context_parser = commands.add_parser("context", help="Print bounded machine/team context snapshot")
    context_parser.add_argument("--session")
    context_parser.add_argument("--max-chars", type=int, default=6000)

    watch_parser = commands.add_parser("watch", help="Live view of pane states and recent tasks")
    watch_parser.add_argument("--session")
    watch_parser.add_argument("--interval", type=float, default=3.0)
    smoke = commands.add_parser("smoke", help="Run a minimal end-to-end orchestration smoke test")
    smoke.add_argument("--session")
    smoke.add_argument("--objective", default="Smoke test the six-pane orchestration")
    smoke.add_argument("--model")
    smoke.add_argument("--max-minutes", type=int, default=10)
    smoke_hyphen = commands.add_parser("smoke-test", help="Run a minimal end-to-end orchestration smoke test")
    smoke_hyphen.add_argument("--session")
    smoke_hyphen.add_argument("--objective", default="Smoke test the six-pane orchestration")
    smoke_hyphen.add_argument("--model")
    smoke_hyphen.add_argument("--max-minutes", type=int, default=10)
    for alias in ("smoke_test", "smoketest"):
        alias_parser = commands.add_parser(alias, help="Run a minimal end-to-end orchestration smoke test")
        alias_parser.add_argument("--session")
        alias_parser.add_argument("--objective", default="Smoke test the six-pane orchestration")
        alias_parser.add_argument("--model")
        alias_parser.add_argument("--max-minutes", type=int, default=10)
    commands.add_parser("score", help="Explain available task/model routes and quota metadata")
    commands.add_parser("status", help="Show scoped task and event state")
    commands.add_parser("quota-config", help="Show how to configure verified request/token allowance metadata")
    return root


def main() -> None:
    cli = parser()
    args = cli.parse_args()
    try:
        _handle(args)
    except KeyboardInterrupt:
        print("\ninterrupted; durable task state is preserved in .thunopro/state.sqlite3", file=sys.stderr)
        raise SystemExit(130)
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
