"""
skill-gitops plugin — auto-sync modified skills to infra GitOps repo.

Responds to ``post_tool_call`` for ``skill_manage`` actions and batches
concurrent edits (e.g. curator consolidation) into a single PR per
30-second window.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────
HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data/.hermes"))
SKILLS_DIR = HERMES_HOME / "skills"
INFRA_REPO = Path("/opt/data/infra")
INFRA_SKILLS_DIR = INFRA_REPO / "clusters" / "main" / "apps" / "hermes" / "skills"
LOCK_FILE = Path("/tmp/skill-gitops.lock")
DEBOUNCE_SECONDS = 30  # seconds to wait for more changes before creating PR

# ── State ───────────────────────────────────────────────────────────────────
_dirty_skills: set[str] = set()
_dirty_lock = threading.Lock()
_debounce_timer: threading.Timer | None = None
_timer_lock = threading.Lock()


def register(ctx):
    """Plugin entry point: register the post_tool_call hook."""
    ctx.register_hook("post_tool_call", _on_tool_result)


def _on_tool_result(*, tool_name: str, args: dict, result: str, **kw):
    """Fires after every tool call — intercept skill_manage mutations."""
    if tool_name != "skill_manage":
        return

    action = args.get("action", "")
    if action not in ("patch", "edit", "write_file"):
        return  # create / delete are not synced automatically

    skill_name = args.get("name", "")
    if not skill_name:
        return

    # Only sync skills that exist in the infra repo
    if not (INFRA_SKILLS_DIR / skill_name).is_dir():
        logger.debug(
            "skill-gitops: %s not in infra repo — skipping", skill_name
        )
        return

    # Check the tool actually succeeded
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
        if isinstance(parsed, dict) and parsed.get("success") is False:
            logger.debug("skill-gitops: %s edit failed — skipping", skill_name)
            return
    except (json.JSONDecodeError, TypeError):
        pass

    # Record dirty skill and (re)schedule debounced sync
    with _dirty_lock:
        _dirty_skills.add(skill_name)
    _reschedule_debounce()

    logger.info("skill-gitops: queued %s (action=%s)", skill_name, action)


def _reschedule_debounce():
    """Cancel any pending sync timer and start a new one."""
    global _debounce_timer
    with _timer_lock:
        if _debounce_timer is not None:
            _debounce_timer.cancel()
        _debounce_timer = threading.Timer(DEBOUNCE_SECONDS, _flush)
        _debounce_timer.daemon = True
        _debounce_timer.start()


def _flush():
    """Grab all dirty skills and kick off the sync in a worker thread."""
    with _dirty_lock:
        skills = set(_dirty_skills)
        _dirty_skills.clear()

    if not skills:
        return

    logger.info("skill-gitops: flushing %d dirty skill(s)", len(skills))
    t = threading.Thread(
        target=_sync_with_lock,
        args=(skills,),
        daemon=True,
    )
    t.start()


def _sync_with_lock(skills: set[str]):
    """Acquire the filesystem lock, then sync."""
    # Avoid importing sync.py at module level — discover order may not have
    # resolved path dependencies yet.
    from . import sync as _sync

    try:
        _sync.sync_changed_skills(
            skills=skills,
            skills_dir=SKILLS_DIR,
            infra_skills_dir=INFRA_SKILLS_DIR,
            infra_repo=INFRA_REPO,
            lock_file=LOCK_FILE,
        )
    except Exception:
        logger.exception("skill-gitops: sync failed")
