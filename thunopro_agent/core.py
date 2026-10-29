from __future__ import annotations

import json
import re
import shutil
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import policy as policy_mod


ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / ".thunopro"
DB_PATH = STATE_DIR / "state.sqlite3"
UNLIMITED_TIER = 4
WORKER_TIMEOUT_SECONDS = 1200
LEADER_TIMEOUT_SECONDS = 300
WORKER_AGENT = "thuno-worker"
FAILURE_PREFIXES = ("Error:", "Upstream request failed")


@dataclass(frozen=True)
class Model:
    id: str
    capabilities: tuple[str, ...]
    reasoning: int
    coding: int
    context: int
    speed: int
    quota_tier: int
    quota_note: str = ""
    monthly_requests: int | None = None
    remaining_requests: int | None = None
    monthly_tokens: int | None = None
    remaining_tokens: int | None = None
    trains_on_data: bool = False


@dataclass(frozen=True)
class Role:
    name: str
    description: str
    capabilities: tuple[str, ...]


ROLES = (
    Role("commander", "Decompose and coordinate work", ("planning", "delegation", "review")),
    Role("scout", "Research sources and inspect repositories", ("research", "long_context")),
    Role("architect", "Design interfaces and assess tradeoffs", ("reasoning", "architecture")),
    Role("builder", "Implement a bounded code change", ("coding", "testing")),
    Role("reviewer", "Independently find defects and risks", ("reasoning", "review", "coding")),
    Role("tester", "Run tests and validate acceptance criteria", ("testing", "coding")),
)

# Ratings are relative routing hints (1–5), not vendor benchmark claims.
MODELS = (
    Model("opencode-go/beam-free", ("reasoning", "coding", "research", "review", "testing", "fast"), 3, 3, 3, 4, 0),
    Model("opencode-go/iceman-contributor-free", ("reasoning", "coding", "research", "review", "testing", "fast"), 3, 3, 3, 4, 0),
    Model("opencode-go/gpt-6-luna", ("reasoning", "coding", "architecture", "review", "planning", "long_context"), 5, 5, 5, 3, 0),
    Model("opencode-go/kimi-k3.1-pro", ("reasoning", "coding", "long_context", "research", "review"), 5, 5, 5, 3, 0),
    Model("opencode-go/deepseek-v4-pro", ("reasoning", "coding", "architecture", "review"), 5, 5, 4, 3, 0),
    Model("opencode-go/glm-5.3", ("coding", "reasoning", "testing", "architecture"), 4, 5, 4, 4, 0),
    Model("opencode-go/gemini-4-argon", ("reasoning", "research", "long_context", "architecture"), 5, 4, 5, 3, 0),
    Model("opencode-go/kimi-k2.7-code", ("coding", "testing", "review"), 4, 5, 4, 4, 0),
    Model("opencode-go/glm-5.3-flash", ("coding", "testing", "fast"), 3, 4, 3, 5, 0),
    Model("opencode-go/minimax-m3.1-flash", ("coding", "testing", "fast"), 3, 4, 3, 5, 0),
    Model("opencode-go/claude-haiku-5-5", ("research", "coding", "fast"), 3, 3, 3, 5, 0),
    Model("opencode-go/qwen3.8-max", ("reasoning", "coding", "research", "long_context"), 4, 4, 5, 3, 0),
)

TASK_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "reasoning": ("reasoning",),
    "coding": ("coding",),
    "research": ("research",),
    "review": ("review",),
    "testing": ("testing",),
    "long_context": ("long_context",),
    "fast": ("fast",),
    "general": (),
}

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
  kind TEXT NOT NULL, status TEXT NOT NULL, assigned_agent TEXT,
  selected_model TEXT, route_score REAL, route_reason TEXT, tmux_session TEXT,
  acceptance TEXT NOT NULL DEFAULT '',
  priority INTEGER NOT NULL DEFAULT 3, depends_on TEXT NOT NULL DEFAULT '[]',
  result TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, sender TEXT NOT NULL,
  recipient TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
  reply_to TEXT, status TEXT NOT NULL DEFAULT 'new', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, actor TEXT NOT NULL,
  event TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS evaluations (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, model TEXT, score REAL NOT NULL,
  outcome TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS panes (
  pane_id TEXT PRIMARY KEY, session_name TEXT NOT NULL, role TEXT NOT NULL,
  model TEXT, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attempts (
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, model TEXT NOT NULL,
  status TEXT NOT NULL, exit_code INTEGER, output TEXT NOT NULL DEFAULT '',
  started_at REAL NOT NULL, finished_at REAL
);
"""


class ClosingConnection(sqlite3.Connection):
    """SQLite's built-in connection context commits but does not close."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=5000")
    db.executescript(SCHEMA)
    columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
    if "tmux_session" not in columns:
        db.execute("ALTER TABLE tasks ADD COLUMN tmux_session TEXT")
    if "acceptance" not in columns:
        db.execute("ALTER TABLE tasks ADD COLUMN acceptance TEXT NOT NULL DEFAULT ''")
    return db


def emit(db: sqlite3.Connection, actor: str, event: str, task_id: str | None = None,
         details: dict[str, Any] | None = None) -> None:
    db.execute("INSERT INTO events(task_id, actor, event, details, created_at) VALUES(?,?,?,?,?)",
               (task_id, actor, event, json.dumps(details or {}, ensure_ascii=False), time.time()))


def _load_config() -> dict[str, Any]:
    model_config = STATE_DIR / "models.json"
    if not model_config.exists():
        return {}
    try:
        data = json.loads(model_config.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def available_models() -> list[Model]:
    """Refresh live OpenCode models, then apply provider/training policy and verified metadata."""
    installed: set[str] | None = None
    if shutil.which("opencode"):
        try:
            result = subprocess.run(["opencode", "models"], capture_output=True, text=True, timeout=20, check=False)
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or "opencode models returned an error")
            installed = set(re.findall(r"(?m)^([\w.-]+/[\w.-]+)\s*$", result.stdout))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Could not refresh OpenCode model catalogue: {exc}") from exc
    config = _load_config()
    configured: dict[str, dict[str, Any]] = config.get("models", {}) if isinstance(config.get("models", {}), dict) else {}
    policy = policy_mod.RoutingPolicy.from_config(config.get("routing"))
    known = {model.id: model for model in MODELS}
    model_ids = sorted(installed) if installed is not None else list(known)
    models = []
    for model_id in model_ids:
        if not policy.provider_allowed(model_id):
            continue
        base = known.get(model_id) or _infer_model(model_id)
        attrs = configured.get(model_id, {})
        if attrs.get("enabled", True) is False:
            continue
        trains = policy_mod.trains_on_request_data(model_id, attrs.get("trains_on_request_data"))
        if trains and not policy.allow_training_models:
            continue
        verified = attrs.get("verified", False) is True
        tier = attrs.get("quota_tier", base.quota_tier) if verified else 0
        if tier not in range(5):
            tier = 0
        capabilities = attrs.get("capabilities", base.capabilities)
        if not isinstance(capabilities, (list, tuple)):
            capabilities = base.capabilities
        models.append(Model(
            model_id, tuple(str(capability) for capability in capabilities),
            _rating(attrs.get("reasoning", base.reasoning)),
            _rating(attrs.get("coding", base.coding)),
            _rating(attrs.get("context", base.context)),
            _rating(attrs.get("speed", base.speed)), tier,
            str(attrs.get("quota_note", base.quota_note)),
            _optional_int(attrs.get("monthly_requests")) if verified else None,
            _optional_int(attrs.get("remaining_requests")) if verified else None,
            _optional_int(attrs.get("monthly_tokens")) if verified else None,
            _optional_int(attrs.get("remaining_tokens")) if verified else None,
            trains,
        ))
    if installed is not None and not models:
        raise RuntimeError("OpenCode returned an empty model catalogue; refusing to route to stale models")
    return models


def unlimited_models() -> list[Model]:
    """Models that may run LLM calls: verified unlimited tier only (workers, subagents, leader)."""
    models = [model for model in available_models() if model.quota_tier == UNLIMITED_TIER]
    if not models:
        raise RuntimeError(
            "No verified unlimited model is available. After checking the OpenCode plan, set "
            "verified=true and quota_tier=4 for a model in .thunopro/models.json (see `thuno quota-config`)."
        )
    return models


def supported_kinds(models: list[Model] | None = None) -> list[str]:
    candidates = models if models is not None else unlimited_models()
    kinds = []
    for kind in TASK_REQUIREMENTS:
        try:
            rank_models(kind, candidates)
        except RuntimeError:
            continue
        kinds.append(kind)
    return kinds


def _rating(value: Any) -> int:
    try:
        return max(1, min(5, int(value)))
    except (ValueError, TypeError):
        return 3


def _optional_int(value: Any) -> int | None:
    try:
        return max(0, int(value)) if value is not None else None
    except (ValueError, TypeError):
        return None


def _infer_model(model_id: str) -> Model:
    """Conservative starting profile; account-specific metadata remains configurable."""
    name = model_id.lower().split("/", 1)[-1]
    capabilities = {"general"}
    coding_family = ("code", "coder", "gpt", "glm", "kimi", "deepseek", "qwen", "grok",
                    "claude", "gemini", "mimo", "minimax", "step", "hy", "nemotron", "beam")
    if any(token in name for token in coding_family):
        capabilities.update(("coding", "testing", "review"))
    if any(token in name for token in ("deepseek", "kimi", "gpt", "qwen", "grok", "gemini", "claude")):
        capabilities.update(("reasoning", "research", "long_context", "architecture"))
    if any(token in name for token in ("flash", "haiku", "mini", "lightning", "beam")) or name.endswith("-free"):
        capabilities.add("fast")
    return Model(model_id, tuple(sorted(capabilities)), 3, 3, 3,
                 4 if "fast" in capabilities else 3, 0,
                 "capabilities inferred from model identifier; verify before relying on them")


def initialize() -> None:
    with connect() as db:
        emit(db, "commander", "project.initialized", details={"root": str(ROOT)})
    model_config = STATE_DIR / "models.json"
    try:
        config = json.loads(model_config.read_text()) if model_config.exists() else {}
    except (OSError, json.JSONDecodeError):
        config = {}
    config.setdefault("quota_tiers", {"0": "unknown", "1": "limited", "2": "standard",
                                       "3": "high monthly allowance", "4": "unlimited/generous"})
    config.setdefault("policy", "Set quota/capability metadata only from verified provider/account information. No quota is assumed by default.")
    configured = config.setdefault("models", {})
    for model in available_models():
        entry = {
            "enabled": True, "verified": False, "quota_tier": 0,
            "quota_note": "unverified; check provider/account plan",
            "capabilities": list(model.capabilities), "reasoning": model.reasoning,
            "coding": model.coding, "context": model.context, "speed": model.speed,
        }
        if (model.id in policy_mod.DOCUMENTED_UNLIMITED
                and policy_mod.RoutingPolicy.from_config(config.get("routing")).provider_allowed(model.id)):
            entry.update(verified=True, quota_tier=4,
                         quota_note="verified from documented OpenCode Go unlimited allowance (policy.py)")
        configured.setdefault(model.id, entry)
        # Promote previously unverified documented-unlimited entries without clobbering explicit user choices.
        existing = configured.get(model.id)
        if (isinstance(existing, dict) and not existing.get("verified")
                and model.id in policy_mod.DOCUMENTED_UNLIMITED):
            existing["verified"] = True
            existing["quota_tier"] = 4
            existing["quota_note"] = "verified from documented OpenCode Go unlimited allowance (policy.py)"
    model_config.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")


def infer_kind(text: str, explicit: str | None = None) -> str:
    if explicit:
        if explicit not in TASK_REQUIREMENTS:
            raise ValueError(f"Unknown task kind: {explicit}")
        return explicit
    value = text.lower()
    patterns = (
        ("testing", r"\b(test|tests|testing|pytest|verify|validate)\b"),
        ("review", r"\b(review|audit|inspect|critique|bug|security)\b"),
        ("coding", r"\b(implement|code|build|fix|refactor|write|develop)\b"),
        ("research", r"\b(research|internet|compare|sources|repo|repositories)\b"),
        ("long_context", r"\b(large|long context|many files|whole codebase)\b"),
        ("reasoning", r"\b(architecture|design|plan|strategy|tradeoff)\b"),
    )
    for kind, pattern in patterns:
        if re.search(pattern, value):
            return kind
    return "general"


def _quality_floor(kind: str, context: str | None = None) -> int:
    return 3


def _model_performance(model_id: str, kind: str) -> tuple[float, int]:
    """Return (performance adjustment, sample count) from recorded evaluations."""
    try:
        with connect() as db:
            row = db.execute(
                "SELECT AVG(e.score) AS avg_score, COUNT(*) AS n "
                "FROM evaluations e JOIN tasks t ON t.id = e.task_id "
                "WHERE e.model = ? AND t.kind = ?", (model_id, kind)).fetchone()
    except (sqlite3.Error, OSError):
        return 0.0, 0
    if not row or not row["n"] or row["n"] < 2:
        return 0.0, 0
    avg = row["avg_score"]
    if avg is None:
        return 0.0, int(row["n"])
    performance = max(-2.0, min(2.0, float(avg) - 3.0))
    return performance, int(row["n"])


def rank_models(kind: str, models: list[Model] | None = None, context: str | None = None) -> list[tuple[Model, float, str]]:
    requirements = TASK_REQUIREMENTS[kind]
    candidates = models if models is not None else available_models()
    eligible = [m for m in candidates if all(req in m.capabilities for req in requirements)]
    if not eligible:
        raise RuntimeError(f"No available model satisfies task kind {kind!r}; configure a capable model first")

    def quality(model: Model) -> float:
        return {"reasoning": model.reasoning, "coding": model.coding,
                "research": model.context, "review": (model.reasoning + model.coding) / 2,
                "testing": model.coding, "long_context": model.context,
                "fast": model.speed, "general": (model.reasoning + model.coding) / 2}[kind]

    floor = _quality_floor(kind, context)
    eligible = [model for model in eligible if quality(model) >= floor]
    if not eligible:
        raise RuntimeError(f"No model meets the minimum quality floor for task kind {kind!r}")

    def allowance(model: Model) -> float:
        if model.quota_tier == 4:
            return 1.0
        headrooms = []
        for monthly, remaining in ((model.monthly_requests, model.remaining_requests),
                                   (model.monthly_tokens, model.remaining_tokens)):
            if monthly is not None and remaining is not None and monthly > 0:
                headrooms.append(min(1.0, remaining / monthly))
        headroom = min(headrooms) if headrooms else 0.0
        return 0.7 * model.quota_tier / 4 + 0.3 * headroom

    def score(model: Model) -> float:
        # Verified allowance dominates; free models win close ties, not quality mismatches.
        free_bonus = 5.0 if model.id.rsplit("/", 1)[-1].endswith("-free") else 0.0
        performance, _ = _model_performance(model.id, kind)
        return 100 * allowance(model) + 10 * quality(model) / 5 + 2 * model.speed / 5 + free_bonus + performance

    ranked = sorted(eligible, key=lambda m: (-score(m), -quality(m), m.id))
    result = []
    for model in ranked:
        quota_known = (model.quota_tier > 0 or model.monthly_requests is not None
                       or model.monthly_tokens is not None)
        quota_state = "verified" if quota_known else "unknown"
        performance, samples = _model_performance(model.id, kind)
        reason = (f"kind={kind}; required={','.join(requirements) or 'none'}; capability and quality gate kept "
                  f"{len(eligible)}/{len(candidates)} models; quota_tier={model.quota_tier} "
                  f"(0=unknown, 4=verified generous/unlimited; allowance={quota_state}); "
                  f"quality floor={floor}/5; outcome_feedback={performance:+.2f} from {samples} samples; "
                  "free models win close ties; token cost is not a routing penalty")
        result.append((model, score(model), reason))
    return result


def choose_model(kind: str, models: list[Model] | None = None, context: str | None = None) -> tuple[Model, float, str]:
    return rank_models(kind, models, context)[0]


def create_plan(request: str, kind: str | None = None, model_override: str | None = None) -> dict[str, Any]:
    inferred = infer_kind(request, kind)
    candidates = unlimited_models()
    winner, score_value, reason = choose_model(inferred, candidates, context=request)
    if model_override:
        installed = {m.id: m for m in candidates}
        if model_override not in installed:
            raise ValueError(f"Model {model_override!r} is not in the available model catalog")
        winner = installed[model_override]
        try:
            _, score_value, eligibility = choose_model(inferred, [winner], context=request)
        except RuntimeError as exc:
            raise ValueError(f"Model {model_override!r} fails the capability/quality gate for {inferred}") from exc
        reason = f"explicit model override; {eligibility}"
    role_map = {"research": "scout", "coding": "builder", "testing": "tester",
                "review": "reviewer", "reasoning": "architect", "long_context": "scout",
                "fast": "builder", "general": "commander"}
    role = role_map[inferred]
    return {"request": request, "kind": inferred, "agent": role, "model": winner.id,
            "score": round(score_value, 2), "reason": reason,
            "next": ["commander decomposes request", f"{role} performs bounded task",
                     "reviewer checks output", "tester verifies acceptance", "commander accepts"]}


def add_task(title: str, description: str, agent: str | None, kind: str | None,
             priority: int = 3, depends_on: list[str] | None = None,
             model_override: str | None = None, session: str | None = None,
             acceptance: str = "") -> dict[str, Any]:
    inferred = infer_kind(f"{title} {description}", kind)
    candidates = unlimited_models()
    task_context = f"{title} {description}"
    model, score_value, reason = choose_model(inferred, candidates, context=task_context)
    if model_override:
        known = {m.id for m in candidates}
        if model_override not in known:
            raise ValueError(f"Model {model_override!r} is not in the available model catalog")
        model = next(m for m in candidates if m.id == model_override)
        try:
            _, score_value, eligibility = choose_model(inferred, [model], context=task_context)
        except RuntimeError as exc:
            raise ValueError(f"Model {model_override!r} fails the capability/quality gate for {inferred}") from exc
        reason = f"explicit model override; {eligibility}"
    selected_agent = agent or {"research": "scout", "coding": "builder", "testing": "tester",
                               "review": "reviewer", "reasoning": "architect",
                               "long_context": "scout", "fast": "builder", "general": "commander"}[inferred]
    if selected_agent not in {r.name for r in ROLES}:
        raise ValueError(f"Unknown agent role: {selected_agent}")
    task_id = uuid.uuid4().hex[:10]
    now = time.time()
    with connect() as db:
        if session is None:
            active = db.execute("SELECT value FROM meta WHERE key='active_session'").fetchone()
            session = active[0] if active else None
        if not session or not db.execute("SELECT 1 FROM panes WHERE session_name=? LIMIT 1", (session,)).fetchone():
            raise ValueError("Bind exactly one target tmux session first, or pass --session for an already-bound team")
        if not db.execute("SELECT 1 FROM panes WHERE session_name=? AND role=?", (session, selected_agent)).fetchone():
            raise ValueError(f"Agent {selected_agent} is not bound in tmux session {session}")
        for dependency in depends_on or []:
            dependency_row = db.execute("SELECT tmux_session FROM tasks WHERE id=?", (dependency,)).fetchone()
            if not dependency_row:
                raise ValueError(f"Dependency task does not exist: {dependency}")
            if dependency_row["tmux_session"] != session:
                raise ValueError("A task dependency must belong to the same tmux session")
        db.execute("INSERT INTO tasks(id,title,description,kind,status,assigned_agent,selected_model,route_score,route_reason,tmux_session,acceptance,priority,depends_on,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (task_id, title, description, inferred, "queued", selected_agent, model.id,
                    score_value, reason, session, acceptance, priority, json.dumps(depends_on or []), now, now))
        emit(db, "commander", "task.created", task_id, {"agent": selected_agent, "model": model.id, "kind": inferred, "tmux_session": session})
    return {"id": task_id, "title": title, "agent": selected_agent, "model": model.id,
            "kind": inferred, "tmux_session": session, "acceptance": acceptance,
            "score": round(score_value, 2), "reason": reason}


def bind_team(session: str) -> list[dict[str, str]]:
    if not shutil.which("tmux"):
        raise RuntimeError("tmux is not installed")
    result = subprocess.run(["tmux", "list-panes", "-t", session, "-F",
                             "#{pane_id}\t#{session_name}\t#{pane_index}"],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"Could not inspect tmux session {session}")
    panes = [line.split("\t") for line in result.stdout.splitlines() if line.strip()]
    if len(panes) != 6:
        raise ValueError(f"Expected exactly 6 panes in {session}; found {len(panes)}")
    roles = [role.name for role in ROLES]
    bindings = []
    with connect() as db:
        for (pane_id, session_name, _), role in zip(panes, roles):
            db.execute("INSERT INTO panes(pane_id,session_name,role,updated_at) VALUES(?,?,?,?) ON CONFLICT(pane_id) DO UPDATE SET session_name=excluded.session_name,role=excluded.role,updated_at=excluded.updated_at",
                       (pane_id, session_name, role, time.time()))
            subprocess.run(["tmux", "set-option", "-p", "-t", pane_id, "@thunopro_role", role], check=False)
            bindings.append({"pane": pane_id, "session": session_name, "role": role})
        db.execute("INSERT INTO meta(key,value) VALUES('active_session',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (session,))
        emit(db, "commander", "team.bound", details={"session": session, "count": len(bindings)})
    return bindings


def create_team(session: str) -> list[dict[str, str]]:
    if not shutil.which("tmux"):
        raise RuntimeError("tmux is not installed")
    exists = subprocess.run(["tmux", "has-session", "-t", session], capture_output=True, check=False)
    if exists.returncode == 0:
        raise ValueError(f"tmux session {session!r} already exists; choose another name")
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "-c", str(ROOT)], check=True)
    for _ in range(5):
        subprocess.run(["tmux", "split-window", "-t", session, "-c", str(ROOT)], check=True)
        subprocess.run(["tmux", "select-layout", "-t", session, "tiled"], check=True)
    subprocess.run(["tmux", "select-pane", "-t", f"{session}:0.0"], check=True)
    return bind_team(session)


def send_message(sender: str, recipient: str, body: str, kind: str = "notify",
                 reply_to: str | None = None) -> str:
    if sender not in {r.name for r in ROLES} or recipient not in {r.name for r in ROLES}:
        raise ValueError("sender and recipient must be one of the six registered roles")
    if kind not in {"notify", "request", "reply", "delegate"}:
        raise ValueError("message kind must be notify, request, reply, or delegate")
    message_id = uuid.uuid4().hex[:12]
    with connect() as db:
        thread_id = message_id
        if reply_to:
            parent = db.execute("SELECT thread_id FROM messages WHERE id=?", (reply_to,)).fetchone()
            if not parent:
                raise ValueError(f"Cannot reply to unknown message: {reply_to}")
            thread_id = parent["thread_id"]
        db.execute("INSERT INTO messages(id,thread_id,sender,recipient,kind,body,reply_to,created_at) VALUES(?,?,?,?,?,?,?,?)",
                   (message_id, thread_id, sender, recipient, "reply" if reply_to else kind, body, reply_to, time.time()))
        emit(db, sender, "message.sent", details={"id": message_id, "to": recipient, "kind": kind})
    return message_id


def task_claim(task_id: str, agent: str) -> None:
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise ValueError(f"Task not found: {task_id}")
        if row["assigned_agent"] != agent:
            raise ValueError(f"Task is assigned to {row['assigned_agent']}, not {agent}")
        blockers = []
        for dep in json.loads(row["depends_on"]):
            dep_row = db.execute("SELECT status FROM tasks WHERE id=?", (dep,)).fetchone()
            dep_status = dep_row["status"] if dep_row else None
            if dep_status == "done":
                continue
            if dep_status == "awaiting_review" and agent in ("reviewer", "tester"):
                continue
            blockers.append(dep)
        if blockers:
            raise ValueError(f"Unfinished dependencies: {', '.join(blockers)}")
        if row["status"] != "queued":
            raise ValueError(f"Cannot claim task in status {row['status']}")
        if not row["tmux_session"] or not db.execute("SELECT 1 FROM panes WHERE session_name=? AND role=?", (row["tmux_session"], agent)).fetchone():
            raise ValueError("Task's assigned role is no longer bound in its original tmux session")
        updated = db.execute("UPDATE tasks SET status='running',updated_at=? WHERE id=? AND status='queued'",
                             (time.time(), task_id))
        if updated.rowcount != 1:
            raise ValueError("Task was claimed concurrently; refresh task status")
        emit(db, agent, "task.claimed", task_id)


def task_complete(task_id: str, agent: str, result: str, failed: bool = False) -> None:
    status = "failed" if failed else "awaiting_review"
    with connect() as db:
        row = db.execute("SELECT status,assigned_agent FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise ValueError(f"Task not found: {task_id}")
        if row["assigned_agent"] != agent:
            raise ValueError(f"Task is assigned to {row['assigned_agent']}, not {agent}")
        if row["status"] != "running":
            raise ValueError(f"Cannot finish task in status {row['status']}; claim it first")
        db.execute("UPDATE tasks SET status=?,result=?,updated_at=? WHERE id=?",
                   (status, result, time.time(), task_id))
        emit(db, agent, f"task.{status}", task_id, {"result_chars": len(result)})


def task_accept(task_id: str, decision: str, notes: str = "", agent: str = "commander") -> None:
    if agent != "commander":
        raise ValueError("Only the commander can accept/rework a task")
    if decision not in {"accept", "rework"}:
        raise ValueError("decision must be accept or rework")
    with connect() as db:
        row = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise ValueError(f"Task not found: {task_id}")
        if row["status"] != "awaiting_review":
            raise ValueError(f"Cannot review task in status {row['status']}")
        if decision == "rework":
            retries = db.execute("SELECT COUNT(*) FROM events WHERE task_id=? AND event='task.rework_requested'",
                                 (task_id,)).fetchone()[0]
            if retries >= 2:
                raise ValueError("Maximum of two rework cycles reached")
            db.execute("UPDATE tasks SET status='queued',result=?,updated_at=? WHERE id=?",
                       ("Rework requested: " + notes, time.time(), task_id))
            emit(db, agent, "task.rework_requested", task_id, {"notes": notes, "retry": retries + 1})
        else:
            db.execute("UPDATE tasks SET status='done',updated_at=? WHERE id=?",
                       (time.time(), task_id))
            emit(db, agent, "task.accepted", task_id, {"notes": notes})


def record_evaluation(task_id: str, score: float, outcome: str, notes: str = "") -> str:
    if not 0 <= score <= 5:
        raise ValueError("evaluation score must be between 0 and 5")
    eval_id = uuid.uuid4().hex[:12]
    with connect() as db:
        row = db.execute("SELECT selected_model FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise ValueError(f"Task not found: {task_id}")
        db.execute("INSERT INTO evaluations(id,task_id,model,score,outcome,notes,created_at) VALUES(?,?,?,?,?,?,?)",
                   (eval_id, task_id, row["selected_model"], score, outcome, notes, time.time()))
        emit(db, "commander", "evaluation.recorded", task_id, {"score": score, "outcome": outcome})
    return eval_id


def dispatch_task(task_id: str) -> str:
    with connect() as db:
        task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise ValueError(f"Task not found: {task_id}")
        pane = db.execute("SELECT pane_id FROM panes WHERE session_name=? AND role=?",
                          (task["tmux_session"], task["assigned_agent"])).fetchone()
    if not pane:
        raise ValueError("No pane for this role in the task's original tmux session")
    info = subprocess.run(["tmux", "display-message", "-p", "-t", pane[0],
                           "#{pane_current_command}\t#{pane_current_path}"], capture_output=True, text=True, check=False)
    if info.returncode:
        raise ValueError("Assigned tmux pane no longer exists")
    command, pane_path = info.stdout.strip().split("\t", 1)
    if command not in {"bash", "zsh", "fish", "sh"}:
        raise ValueError(f"Assigned pane is busy ({command}); refusing to interrupt it")
    if Path(pane_path).resolve() != ROOT.resolve():
        raise ValueError(f"Pane workspace {pane_path} differs from task workspace {ROOT}; refusing cross-workspace dispatch")
    task_claim(task_id, task["assigned_agent"])
    cmd = "python3 -m thunopro_agent worker-run " + shlex.quote(task_id)
    try:
        subprocess.run(["tmux", "send-keys", "-t", pane[0], "-l", cmd], check=True)
        subprocess.run(["tmux", "send-keys", "-t", pane[0], "Enter"], check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        with connect() as db:
            db.execute("UPDATE tasks SET status='queued',updated_at=? WHERE id=? AND status='running'",
                       (time.time(), task_id))
            emit(db, "commander", "task.dispatch_failed", task_id,
                 {"pane": pane[0], "tmux_session": task["tmux_session"], "error": str(exc)})
        raise
    with connect() as db:
        emit(db, "commander", "task.dispatched", task_id,
             {"agent": task["assigned_agent"], "model": task["selected_model"], "tmux_session": task["tmux_session"], "pane": pane[0]})
    return pane[0]


def reconcile_stale(session: str | None = None, max_age_seconds: int = WORKER_TIMEOUT_SECONDS + 300) -> list[str]:
    """Requeue running tasks whose latest attempt has exceeded the age threshold.

    V1 has no reliable process liveness probe, so this is an explicit, opt-in
    recovery action rather than an automatic one. Returns requeued task ids.
    """
    now = time.time()
    requeued: list[str] = []
    with connect() as db:
        if session:
            rows = db.execute("SELECT * FROM tasks WHERE status='running' AND tmux_session=?", (session,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM tasks WHERE status='running'").fetchall()
        for row in rows:
            attempt = db.execute(
                "SELECT started_at FROM attempts WHERE task_id=? ORDER BY started_at DESC LIMIT 1",
                (row["id"],)).fetchone()
            started = float(attempt["started_at"]) if attempt else float(row["updated_at"])
            if now - started <= max_age_seconds:
                continue
            db.execute("UPDATE tasks SET status='queued', result=?, updated_at=? WHERE id=?",
                       (f"Requeued by reconcile: stale running (age>{int(now-started)}s)", now, row["id"]))
            emit(db, "commander", "task.requeued_stale", row["id"],
                 {"age_seconds": int(now - started), "session": row["tmux_session"]})
            requeued.append(row["id"])
    return requeued


def dispatch_ready(session: str) -> list[str]:
    """Dispatch queued tasks in one session whose dependencies are satisfied.

    Review tasks may start once their dependency is awaiting review; other tasks
    wait until the dependency is done. At most one task per role is dispatched per
    call, in descending priority order. Returns the task ids that were dispatched.
    """
    dispatched: list[str] = []
    with connect() as db:
        rows = db.execute("SELECT * FROM tasks WHERE tmux_session=? ORDER BY priority DESC, created_at",
                          (session,)).fetchall()
        busy_roles = {row["assigned_agent"] for row in rows if row["status"] == "running"}
        by_id = {row["id"]: dict(row) for row in rows}
        queued = [dict(row) for row in rows if row["status"] == "queued"]
    queued.sort(key=lambda row: (-int(row["priority"]), float(row["created_at"])))
    for row in queued:
        if row["assigned_agent"] in busy_roles:
            continue
        deps = list(json.loads(row["depends_on"] or "[]"))
        ready = True
        for dep in deps:
            status = by_id.get(dep, {}).get("status")
            if status == "done":
                continue
            if status == "awaiting_review" and row["assigned_agent"] in ("reviewer", "tester"):
                continue
            ready = False
            break
        if not ready:
            continue
        try:
            pane = dispatch_task(row["id"])
        except (ValueError, OSError, subprocess.SubprocessError):
            continue
        busy_roles.add(row["assigned_agent"])
        dispatched.append(row["id"])
    return dispatched


def retry_task(task_id: str) -> None:
    """Requeue a failed task so it can be dispatched again."""
    with connect() as db:
        row = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise ValueError(f"Task not found: {task_id}")
        if row["status"] != "failed":
            raise ValueError(f"Cannot retry task in status {row['status']}")
        db.execute("UPDATE tasks SET status='queued', result=NULL, updated_at=? WHERE id=?",
                   (time.time(), task_id))
        emit(db, "commander", "task.requeued", task_id, {"previous_status": "failed"})


def block_task(task_id: str, reason: str) -> None:
    """Mark a queued task failed because a prerequisite can never be satisfied."""
    with connect() as db:
        row = db.execute("SELECT status,assigned_agent FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row or row["status"] != "queued":
            return
        db.execute("UPDATE tasks SET status='failed',result=?,updated_at=? WHERE id=?",
                   ("Blocked: " + reason, time.time(), task_id))
        emit(db, "commander", "task.blocked", task_id, {"reason": reason})


def extract_json_object(text: str, required_key: str) -> dict[str, Any] | None:
    """Find the first JSON object in model output that contains `required_key`.

    Model output mixes tool logs, markdown fences and prose, so scanning for a
    decodable object is more reliable than slicing from the first brace.
    """
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and required_key in value:
            return value
    return None


def _run_streaming(command: list[str], log_path: Path, timeout: int) -> tuple[int, str]:
    """Run a command and echo its output live into the current tmux pane while logging it."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace", bufsize=1, cwd=str(ROOT))
    killed = threading.Event()

    def _kill() -> None:
        killed.set()
        process.kill()

    timer = threading.Timer(timeout, _kill)
    timer.start()
    chunks: list[str] = []
    try:
        with log_path.open("w", encoding="utf-8") as log:
            assert process.stdout is not None
            for line in process.stdout:
                print(line, end="", flush=True)
                log.write(line)
                chunks.append(line)
        code = process.wait()
    finally:
        timer.cancel()
    output = "".join(chunks)
    if killed.is_set():
        output = f"Timed out after {timeout} seconds.\n" + output
        code = 124
    return code, output


def run_leader(prompt: str, purpose: str, model_id: str, reference: str = "",
               timeout: int = LEADER_TIMEOUT_SECONDS) -> tuple[int, str]:
    """Single bounded commander/reviewer call on a verified unlimited model. Recorded in attempts."""
    attempt_id, started = uuid.uuid4().hex[:12], time.time()
    label = f"leader:{purpose}:{reference}"
    with connect() as db:
        db.execute("INSERT INTO attempts(id,task_id,model,status,started_at) VALUES(?,?,?,?,?)",
                   (attempt_id, label, model_id, "running", started))
    command = ["opencode", "run", "--auto", "--agent", "plan", "--model", model_id, "--dir", str(ROOT), prompt]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        code, output = result.returncode, result.stdout
        if result.stderr:
            output += "\n" + result.stderr
    except subprocess.TimeoutExpired:
        code, output = 124, f"Timed out after {timeout} seconds"
    except OSError as exc:
        code, output = 127, f"Could not start OpenCode: {exc}"
    success = code == 0 and not output.lstrip().startswith(FAILURE_PREFIXES)
    with connect() as db:
        db.execute("UPDATE attempts SET status=?,exit_code=?,output=?,finished_at=? WHERE id=?",
                   ("done" if success else "failed", code, output[-12000:], time.time(), attempt_id))
    return (0 if success else (code or 1)), output


def build_worker_prompt(task: Any, context_text: str) -> str:
    feedback = ""
    if task["status"] == "running" and (task["result"] or "").startswith("Rework requested"):
        feedback = f"\nREVIEWER FEEDBACK TO ADDRESS (previous attempt was sent back):\n{task['result']}\n"
    return (
        f"You are the '{task['assigned_agent']}' worker in a six-pane thunopro_agent team.\n"
        f"TASK ID: {task['id']}\nTITLE: {task['title']}\nTASK: {task['description']}\n"
        f"ACCEPTANCE: {task['acceptance']}\n{feedback}\n"
        "RULES:\n"
        f"- Work only inside {ROOT}. Do not touch other repositories or tmux sessions.\n"
        "- Do only what this task needs; do not widen scope.\n"
        "- For a helper, call the Task tool with subagent_type \"thuno-sub\" only. That subagent is pinned to an "
        "unlimited-token model. Do not use any other subagent.\n"
        "- Verify your work (run the relevant tests or commands) before reporting.\n"
        "- End with sections: CHANGES, COMMANDS, EVIDENCE, BLOCKERS.\n\n"
        f"MACHINE AND TEAM CONTEXT (snapshot):\n{context_text}\n"
    )


def run_worker_task(task_id: str, max_attempts: int = 3) -> int:
    """Run a claimed task on verified unlimited models; success waits for commander review."""
    with connect() as db:
        task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        raise ValueError(f"Task not found: {task_id}")
    if task["status"] != "running":
        raise ValueError(f"Worker requires a claimed task, found {task['status']}")
    try:
        models_pool = unlimited_models()
        ranked = rank_models(task["kind"], models_pool)
    except RuntimeError as exc:
        block_task(task_id, f"no verified unlimited model fits kind {task['kind']}: {exc}")
        print(f"[thuno] task {task_id} failed: {exc}", file=sys.stderr, flush=True)
        return 1
    models = [model for model, _, _ in ranked]
    ordered = [model for model in models if model.id == task["selected_model"]]
    ordered.extend(model for model in models if model.id != task["selected_model"])
    ordered = ordered[:max_attempts]

    from . import context as context_mod
    context_text = context_mod.snapshot(task["tmux_session"], max_chars=3000)
    prompt = build_worker_prompt(task, context_text)
    logs = STATE_DIR / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    agent = WORKER_AGENT if (ROOT / ".opencode" / "agent" / f"{WORKER_AGENT}.md").exists() else "build"
    errors = []
    for number, model in enumerate(ordered, 1):
        attempt_id, started = uuid.uuid4().hex[:12], time.time()
        print(f"\n[thuno] attempt {number}/{len(ordered)} model={model.id} agent={agent} task={task_id}", flush=True)
        with connect() as db:
            db.execute("INSERT INTO attempts(id,task_id,model,status,started_at) VALUES(?,?,?,?,?)",
                       (attempt_id, task_id, model.id, "running", started))
            db.execute("UPDATE tasks SET selected_model=?,updated_at=? WHERE id=?",
                       (model.id, started, task_id))
            emit(db, task["assigned_agent"], "task.model_attempt", task_id,
                 {"model": model.id, "attempt": number, "tmux_session": task["tmux_session"], "agent": agent})
        log_path = logs / f"{task_id}-{number}.log"
        command = ["opencode", "run", "--auto", "--agent", agent, "--model", model.id, "--dir", str(ROOT), prompt]
        try:
            code, output = _run_streaming(command, log_path, WORKER_TIMEOUT_SECONDS)
        except OSError as exc:
            code, output = 127, f"Could not start OpenCode: {exc}"
        success = code == 0 and not output.lstrip().startswith(FAILURE_PREFIXES)
        with connect() as db:
            db.execute("UPDATE attempts SET status=?,exit_code=?,output=?,finished_at=? WHERE id=?",
                       ("done" if success else "failed", code, output[-12000:], time.time(), attempt_id))
            emit(db, task["assigned_agent"], "task.model_attempt_finished", task_id,
                 {"model": model.id, "exit_code": code, "success": success, "log": str(log_path)})
        if success:
            with connect() as db:
                db.execute("UPDATE tasks SET status='awaiting_review',result=?,updated_at=? WHERE id=?",
                           (output, time.time(), task_id))
                emit(db, task["assigned_agent"], "task.awaiting_review", task_id,
                     {"model": model.id, "log": str(log_path)})
            print(f"\n[thuno] {task['assigned_agent']} finished; output ready for commander review. log={log_path}",
                  flush=True)
            return 0
        errors.append(f"{model.id}: exit={code}; {output[-1000:]}")
        error_lower = output.lower()
        if any(token in error_lower for token in ("requires global regions", "trains on request data",
                                                  "not available in your region", "model is not available")):
            _disable_model(model.id, output[-2000:])
    summary = "All configured model attempts failed. " + "\n".join(errors)
    with connect() as db:
        db.execute("UPDATE tasks SET status='failed',result=?,updated_at=? WHERE id=?",
                   (summary, time.time(), task_id))
        emit(db, task["assigned_agent"], "task.failed", task_id,
             {"attempts": len(ordered), "tmux_session": task["tmux_session"]})
    print(summary, file=sys.stderr, flush=True)
    return 1


def _disable_model(model_id: str, reason: str) -> None:
    """Persist provider/account policy failures so later tasks do not retry that model."""
    path = STATE_DIR / "models.json"
    try:
        config = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        config = {}
    models = config.setdefault("models", {})
    entry = models.setdefault(model_id, {})
    entry["enabled"] = False
    entry["disabled_reason"] = reason
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
