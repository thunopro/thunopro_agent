"""Self-scoring of team efficiency, computed only from durable SQLite evidence.

Score (0-100) = 100 * (0.30 completion + 0.20 first-pass + 0.20 unlimited-model compliance
                       + 0.15 attempt reliability + 0.15 speed)

- completion:   done tasks / all tasks in scope
- first-pass:   done tasks accepted with zero rework / done tasks
- compliance:   LLM attempts (workers + commander) that used a verified unlimited model / all attempts
- reliability:  worker attempts that succeeded / worker attempts
- speed:        1.0 if mean accept time <= 10 minutes, else 10 / mean minutes

Token volume and agent count are deliberately not part of the score.
"""
from __future__ import annotations

from typing import Any, Iterable

from . import core

TARGET_MINUTES = 10.0
WEIGHTS = {"completion": 0.30, "first_pass": 0.20, "compliance": 0.20, "reliability": 0.15, "speed": 0.15}


def _unlimited_ids() -> set[str]:
    config = core._load_config()
    ids = set()
    for model_id, attrs in (config.get("models") or {}).items():
        if not isinstance(attrs, dict):
            continue
        if attrs.get("verified") is True and attrs.get("quota_tier") == core.UNLIMITED_TIER \
                and attrs.get("enabled", True) is not False:
            ids.add(model_id)
    return ids


def _rating(score: float) -> str:
    if score >= 85:
        return "strong"
    if score >= 70:
        return "good"
    if score >= 50:
        return "needs work"
    return "poor"


def compute(session: str | None, task_ids: Iterable[str] | None = None) -> dict[str, Any]:
    ids = set(task_ids) if task_ids is not None else None
    if ids is not None and not ids:
        return {"score": 0.0, "rating": "no data", "tasks": 0}
    with core.connect() as db:
        if ids is not None:
            marks = ",".join("?" * len(ids))
            tasks = db.execute(f"SELECT * FROM tasks WHERE id IN ({marks})", tuple(ids)).fetchall()
        elif session is not None:
            tasks = db.execute("SELECT * FROM tasks WHERE tmux_session=?", (session,)).fetchall()
        else:
            tasks = db.execute("SELECT * FROM tasks").fetchall()
        task_ids_list = [row["id"] for row in tasks]
        if not task_ids_list:
            return {"score": 0.0, "rating": "no data", "tasks": 0}
        placeholders = ",".join("?" * len(task_ids_list))
        events = db.execute(f"SELECT task_id, event, created_at FROM events WHERE task_id IN ({placeholders})",
                            task_ids_list).fetchall()
        attempts = db.execute("SELECT task_id, model, status FROM attempts").fetchall()

    created = {row["id"]: row["created_at"] for row in tasks}
    rework_events: dict[str, int] = {}
    accepted_at: dict[str, float] = {}
    for event in events:
        if event["event"] == "task.rework_requested":
            rework_events[event["task_id"]] = rework_events.get(event["task_id"], 0) + 1
        if event["event"] == "task.accepted":
            accepted_at[event["task_id"]] = event["created_at"]

    status_counts: dict[str, int] = {}
    for row in tasks:
        status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
    done = [row for row in tasks if row["status"] == "done"]
    completion = len(done) / len(tasks)
    first_pass_count = sum(1 for row in done if rework_events.get(row["id"], 0) == 0)
    first_pass = first_pass_count / len(done) if done else 0.0

    scoped_attempts = []
    for attempt in attempts:
        task_ref = attempt["task_id"]
        if task_ref.startswith("leader:"):
            reference = task_ref.split(":", 2)[2] if task_ref.count(":") >= 2 else ""
            if reference in set(task_ids_list) or (session and reference == session):
                scoped_attempts.append(attempt)
        elif task_ref in set(task_ids_list):
            scoped_attempts.append(attempt)
    worker_attempts = [a for a in scoped_attempts if not a["task_id"].startswith("leader:")]
    unlimited = _unlimited_ids()
    compliant = sum(1 for a in scoped_attempts if a["model"] in unlimited)
    compliance = compliant / len(scoped_attempts) if scoped_attempts else 1.0
    reliability = (sum(1 for a in worker_attempts if a["status"] == "done") / len(worker_attempts)) if worker_attempts else 0.0

    minutes = [(accepted_at[tid] - created[tid]) / 60 for tid in accepted_at if tid in created]
    mean_minutes = sum(minutes) / len(minutes) if minutes else None
    speed = 0.0 if mean_minutes is None else min(1.0, TARGET_MINUTES / max(mean_minutes, 1e-9))
    if mean_minutes is not None and mean_minutes <= TARGET_MINUTES:
        speed = 1.0

    parts = {"completion": completion, "first_pass": first_pass, "compliance": compliance,
             "reliability": reliability, "speed": speed}
    score = round(100 * sum(WEIGHTS[name] * value for name, value in parts.items()), 1)
    return {
        "score": score,
        "rating": _rating(score),
        "tasks": len(tasks),
        "status": status_counts,
        "components": {name: round(value, 3) for name, value in parts.items()},
        "weights": WEIGHTS,
        "rework_cycles": sum(rework_events.values()),
        "worker_attempts": len(worker_attempts),
        "llm_attempts": len(scoped_attempts),
        "unlimited_attempts": compliant,
        "mean_accept_minutes": None if mean_minutes is None else round(mean_minutes, 2),
    }
