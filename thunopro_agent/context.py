"""Bounded snapshot of machine, workspace, instructions and team state for prompts.

The snapshot is deliberately small: every prompt that carries it pays for it in
tokens, so each section is truncated and the total is capped by `max_chars`.
"""
from __future__ import annotations

import getpass
import os
import platform
import socket
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from . import core

GLOBAL_AGENTS_MD = Path.home() / ".config" / "opencode" / "AGENTS.md"
HOME_NOTES_MD = Path.home() / "computer.md"


def _head(path: Path, limit: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "\n...[truncated]"


def _run(args: list[str], timeout: float = 3.0) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _machine_section() -> str:
    return "\n".join([
        f"time: {time.strftime('%Y-%m-%d %H:%M %Z')}",
        f"user: {getpass.getuser()}  host: {socket.gethostname()}  home: {Path.home()}",
        f"os: {platform.system()} {platform.release()}  shell: {os.environ.get('SHELL', 'unknown')}",
    ])


def _instructions_section() -> str:
    parts = []
    global_text = _head(GLOBAL_AGENTS_MD, 1200)
    if global_text:
        parts.append(f"[global AGENTS.md]\n{global_text}")
    notes = _head(HOME_NOTES_MD, 600)
    if notes:
        parts.append(f"[~/computer.md]\n{notes}")
    project_text = _head(core.ROOT / "AGENTS.md", 1500)
    if project_text:
        parts.append(f"[project AGENTS.md]\n{project_text}")
    return "\n\n".join(parts) or "(no instruction files found)"


def _environment_section() -> str:
    lines = [f"python: {platform.python_version()}"]
    tools = ["git", "tmux", "opencode", "pytest", "ruff"]
    present = [tool for tool in tools if shutil.which(tool)]
    lines.append("tools: " + (", ".join(present) if present else "none detected"))
    git_log = _run(["git", "-C", str(core.ROOT), "log", "--oneline", "-5"])
    if git_log.strip():
        lines.append("recent commits:")
        lines.extend("  " + line for line in git_log.strip().splitlines()[:5])
    return "\n".join(lines)


def _workspace_section() -> str:
    lines = [f"root: {core.ROOT}"]
    git_status = _run(["git", "-C", str(core.ROOT), "status", "--short", "--branch"])
    if git_status.strip():
        status_lines = git_status.strip().splitlines()
        lines.append("git: " + status_lines[0])
        lines.extend(status_lines[1:25])
    else:
        lines.append("git: not a repository or git unavailable")
    entries = sorted(p.name + ("/" if p.is_dir() else "") for p in core.ROOT.iterdir()
                     if p.name not in {".git", "__pycache__", ".pytest_cache"})
    shown = entries[:40]
    lines.append("top-level: " + ", ".join(shown) + (f" ... (+{len(entries) - 40} more)" if len(entries) > 40 else ""))
    return "\n".join(lines)


def _tmux_section(session: str | None) -> str:
    sessions = _run(["tmux", "list-sessions", "-F", "#{session_name}"]).split()
    lines = [f"tmux sessions: {', '.join(sessions) if sessions else 'none (tmux not running)'}"]
    if not session:
        return "\n".join(lines)
    lines.append(f"target session: {session}")
    with core.connect() as db:
        rows = db.execute("SELECT pane_id, role FROM panes WHERE session_name=? ORDER BY role", (session,)).fetchall()
    for row in rows:
        info = _run(["tmux", "display-message", "-p", "-t", row["pane_id"],
                     "#{pane_current_command}|#{pane_current_path}"]).strip()
        command, _, path = info.partition("|")
        lines.append(f"  {row['role']:<10} pane={row['pane_id']} cmd={command or '?'} path={path or '?'}")
    return "\n".join(lines)


def _history_section(session: str | None) -> str:
    if not session:
        return "recent tasks: (no session bound)"
    with core.connect() as db:
        rows = db.execute("SELECT id, assigned_agent, status, selected_model, title FROM tasks "
                          "WHERE tmux_session=? ORDER BY created_at DESC LIMIT 8", (session,)).fetchall()
    if not rows:
        return "recent tasks: none"
    lines = ["recent tasks (newest first):"]
    for row in rows:
        lines.append(f"  {row['id'][:8]} {row['assigned_agent']:<9} {row['status']:<15} "
                     f"{(row['selected_model'] or '-')[:32]:<32} {row['title'][:70]}")
    return "\n".join(lines)


def _models_section() -> str:
    config: dict[str, Any] = core._load_config()
    unlimited = []
    for model_id, attrs in (config.get("models") or {}).items():
        if isinstance(attrs, dict) and attrs.get("verified") is True and attrs.get("quota_tier") == core.UNLIMITED_TIER \
                and attrs.get("enabled", True) is not False:
            unlimited.append(model_id)
    return "verified unlimited models: " + (", ".join(sorted(unlimited)) if unlimited else "none configured")


def _cap(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "\n...[truncated]"


def snapshot(session: str | None, max_chars: int = 6000) -> str:
    """Team state first, long instruction files last, so truncation only ever drops the tail."""
    bodies = {
        "MACHINE": _cap(_machine_section(), 400),
        "ENVIRONMENT": _cap(_environment_section(), 600),
        "TMUX TEAM": _cap(_tmux_section(session), 1500),
        "TASK HISTORY": _cap(_history_section(session), 1200),
        "WORKSPACE": _cap(_workspace_section(), 1200),
        "MODEL POLICY": _cap(_models_section(), 400),
    }
    remaining = max(300, max_chars - sum(len(body) for body in bodies.values()) - 200)
    bodies["INSTRUCTIONS"] = _cap(_instructions_section(), remaining)
    order = ["MACHINE", "ENVIRONMENT", "TMUX TEAM", "TASK HISTORY", "WORKSPACE", "MODEL POLICY", "INSTRUCTIONS"]
    text = "\n\n".join(f"## {title}\n{bodies[title]}" for title in order)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n...[snapshot truncated]"
    return text
