"""Durable state management for ULW-loop — goals.json + ledger.jsonl.

Mirrors oh-my-openagent's ``.omo/ulw-loop/`` pattern:
  - ``goals.json``:  Current goals, subgoals, acceptance criteria, evidence status
  - ``ledger.jsonl``: Append-only event log (audit trail, crash recovery)
  - ``resume.json``:  Lightweight pointer for auto-resume after restart

Files live under ``$HERMES_HOME/ulw-loop/<session_id>/`` for durability
across gateway restarts.
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

GOAL_STATUS_PENDING = "pending"
GOAL_STATUS_IN_PROGRESS = "in_progress"
GOAL_STATUS_COMPLETE = "complete"
GOAL_STATUS_FAILED = "failed"
GOAL_STATUS_BLOCKED = "blocked"
GOAL_STATUS_SUPERSEDED = "superseded"


@dataclass
class Criterion:
    """A single success criterion for a goal."""
    description: str
    evidence: str = ""           # How to verify (CLI output, HTTP, screenshot…)
    evidence_type: str = "text"  # text | cli_output | screenshot | http_response
    passed: bool = False
    evidence_path: str = ""      # Path to stored evidence file


@dataclass
class Goal:
    """A single goal in the ULW-loop plan."""
    id: str                      # Unique id (e.g. "g_001")
    title: str                   # Goal title
    description: str = ""
    status: str = GOAL_STATUS_PENDING
    criteria: list[Criterion] = field(default_factory=list)
    superseded_by: list[str] = field(default_factory=list)  # Child goal ids
    parent_id: str = ""
    blocker_count: int = 0
    last_blocker: str = ""
    result_summary: str = ""


@dataclass
class UlwState:
    """Complete ULW-loop session state."""
    session_id: str
    brief: str                   # Original user goal description
    phase: str = "explore"       # Current phase
    goals: list[Goal] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    completed_at: float = 0.0
    kanban_task_id: str = ""
    iteration: int = 1
    no_progress_count: int = 0   # For 2-strike cap
    resume_count: int = 0        # How many times resumed
    conversation_context: str = ""  # Injected from parent conversation


# ---------------------------------------------------------------------------
# Mutation Lock (simple threading-based)
# ---------------------------------------------------------------------------

_mutation_locks: dict[str, threading.Lock] = {}
_mutation_lock_lock = threading.Lock()


def _get_lock(session_id: str) -> threading.Lock:
    """Get or create a per-session mutation lock (thread-safe)."""
    with _mutex_lock:
        if session_id not in _mutation_locks:
            _mutation_locks[session_id] = threading.Lock()
        return _mutation_locks[session_id]


_mutex_lock = threading.Lock()


def _is_valid_session_id(session_id: str) -> bool:
    """Validate session_id — must not contain path traversal sequences."""
    return bool(session_id) and ".." not in session_id and "/" not in session_id


# ---------------------------------------------------------------------------
# State I/O
# ---------------------------------------------------------------------------

def _get_hermes_home() -> Path:
    """Resolve the Hermes home directory."""
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _state_dir(session_id: str) -> Path:
    """Return the state directory for a session. Does NOT create it."""
    return _get_hermes_home() / "ulw-loop" / session_id


def _ensure_state_dir(session_id: str) -> Path:
    """Return the state directory, creating it if needed."""
    d = _state_dir(session_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _goals_path(session_id: str) -> Path:
    return _state_dir(session_id) / "goals.json"


def _goals_tmp_path(session_id: str) -> Path:
    return _state_dir(session_id) / "goals.tmp"


def _ledger_path(session_id: str) -> Path:
    return _state_dir(session_id) / "ledger.jsonl"


def _resume_path(session_id: str) -> Path:
    return _state_dir(session_id) / "resume.json"


def save_goals(state: UlwState) -> None:
    """Atomically write goals.json."""
    if not _is_valid_session_id(state.session_id):
        logger.error("Invalid session_id: %s", state.session_id)
        return
    _ensure_state_dir(state.session_id)
    lock = _get_lock(state.session_id)
    with lock:
        path = _goals_path(state.session_id)
        data = {
            "session_id": state.session_id,
            "brief": state.brief,
            "phase": state.phase,
            "goals": [_goal_to_dict(g) for g in state.goals],
            "created_at": state.created_at,
            "updated_at": time.time(),
            "completed_at": state.completed_at,
            "kanban_task_id": state.kanban_task_id,
            "iteration": state.iteration,
            "no_progress_count": state.no_progress_count,
            "resume_count": state.resume_count,
            "conversation_context": state.conversation_context,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.rename(path)


def load_goals(session_id: str) -> Optional[UlwState]:
    """Load state from goals.json, or None."""
    path = _goals_path(session_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        state = UlwState(
            session_id=data["session_id"],
            brief=data.get("brief", ""),
            phase=data.get("phase", "explore"),
            goals=[_goal_from_dict(g) for g in data.get("goals", [])],
            created_at=data.get("created_at", 0.0),
            updated_at=data.get("updated_at", 0.0),
            completed_at=data.get("completed_at", 0.0),
            kanban_task_id=data.get("kanban_task_id", ""),
            iteration=data.get("iteration", 1),
            no_progress_count=data.get("no_progress_count", 0),
            resume_count=data.get("resume_count", 0),
            conversation_context=data.get("conversation_context", ""),
        )
        return state
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("Failed to load ulw-loop state for %s: %s", session_id, e)
        return None


def _goal_to_dict(g: Goal) -> dict:
    return {
        "id": g.id,
        "title": g.title,
        "description": g.description,
        "status": g.status,
        "criteria": [asdict(c) for c in g.criteria],
        "superseded_by": g.superseded_by,
        "parent_id": g.parent_id,
        "blocker_count": g.blocker_count,
        "last_blocker": g.last_blocker,
        "result_summary": g.result_summary,
    }


def _goal_from_dict(d: dict) -> Goal:
    return Goal(
        id=d["id"],
        title=d["title"],
        description=d.get("description", ""),
        status=d.get("status", GOAL_STATUS_PENDING),
        criteria=[Criterion(**c) for c in d.get("criteria", [])],
        superseded_by=d.get("superseded_by", []),
        parent_id=d.get("parent_id", ""),
        blocker_count=d.get("blocker_count", 0),
        last_blocker=d.get("last_blocker", ""),
        result_summary=d.get("result_summary", ""),
    )


# ---------------------------------------------------------------------------
# Ledger (append-only JSONL)
# ---------------------------------------------------------------------------

def ledger_append(session_id: str, event_type: str, payload: dict) -> None:
    """Append an event to the ledger."""
    if not _is_valid_session_id(session_id):
        logger.error("Invalid session_id for ledger: %s", session_id)
        return
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": event_type,
        **payload,
    }
    _ensure_state_dir(session_id)
    path = _ledger_path(session_id)
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.error("Failed to write ledger for %s: %s", session_id, e)


def ledger_read(session_id: str, limit: int = 0) -> list[dict]:
    """Read ledger entries, newest first. ``limit=0`` returns all."""
    path = _ledger_path(session_id)
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        logger.error("Failed to read ledger for %s: %s", session_id, e)
        return []
    entries = []
    for line in reversed(lines):
        line = line.strip()
        if line:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if limit and len(entries) >= limit:
            break
    return entries


def ledger_count(session_id: str) -> int:
    """Count total ledger entries."""
    path = _ledger_path(session_id)
    if not path.exists():
        return 0
    try:
        with open(path, "r", encoding="utf-8") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Resume state
# ---------------------------------------------------------------------------

def save_resume(session_id: str, phase: str, iteration: int) -> None:
    """Write lightweight resume pointer."""
    if not _is_valid_session_id(session_id):
        return
    data = {
        "session_id": session_id,
        "phase": phase,
        "iteration": iteration,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _ensure_state_dir(session_id)
    path = _resume_path(session_id)
    try:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.error("Failed to write resume for %s: %s", session_id, e)


def load_resume(session_id: str) -> Optional[dict]:
    """Read resume pointer, or None."""
    if not _is_valid_session_id(session_id):
        return None
    path = _resume_path(session_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def clear_resume(session_id: str) -> None:
    """Remove resume pointer (session complete)."""
    if not _is_valid_session_id(session_id):
        return
    path = _resume_path(session_id)
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass
