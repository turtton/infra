"""Steering — dynamic plan modification during ULW-loop execution.

Inspired by oh-my-openagent's steering mechanism:

  - add_subgoal: Add a new goal mid-execution
  - split_subgoal: Split an in-progress goal into sub-goals (supersedes parent)
  - revise_criterion: Modify success criteria
  - Idempotency key: Prevent double-application of the same steering
"""

import hashlib
import logging
from typing import Optional

from . import state as st

logger = logging.getLogger(__name__)


def _compute_idempotency_key(prompt_signature: str, action: str, target_id: str) -> str:
    """Compute an idempotency key from the steering request.

    This prevents the same steering from being applied twice.
    """
    raw = f"{prompt_signature}:{action}:{target_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _is_already_applied(session_id: str, idempotency_key: str) -> bool:
    """Check if a steering with this key was already applied.

    Scans the ledger for ``steering:accept`` events.
    """
    entries = st.ledger_read(session_id, limit=50)
    for entry in entries:
        if entry.get("type") == "steering:accept":
            if entry.get("idempotency_key") == idempotency_key:
                return True
    return False


def add_subgoal(
    state_obj: st.UlwState,
    title: str,
    description: str = "",
    criteria_texts: Optional[list[str]] = None,
    idempotency_key: str = "",
) -> dict:
    """Add a new subgoal to the plan.

    Returns a dict with ``{"success": bool, "goal_id": str, "message": str}``.
    """
    if idempotency_key and _is_already_applied(state_obj.session_id, idempotency_key):
        st.ledger_append(state_obj.session_id, "steering:dedup", {
            "idempotency_key": idempotency_key,
            "action": "add_subgoal",
            "message": "Skipped — already applied",
        })
        return {"success": True, "goal_id": "", "message": "Already applied (dedup)"}

    # Generate goal id
    existing_ids = {g.id for g in state_obj.goals}
    n = 1
    while f"g_{n:03d}" in existing_ids:
        n += 1
    goal_id = f"g_{n:03d}"

    criteria = []
    if criteria_texts:
        for ct in criteria_texts:
            criteria.append(st.Criterion(description=ct))

    goal = st.Goal(
        id=goal_id,
        title=title,
        description=description,
        status=st.GOAL_STATUS_PENDING,
        criteria=criteria,
    )
    state_obj.goals.append(goal)
    st.save_goals(state_obj)
    state_obj.updated_at = __import__("time").time()

    st.ledger_append(state_obj.session_id, "steering:accept", {
        "idempotency_key": idempotency_key,
        "action": "add_subgoal",
        "goal_id": goal_id,
        "title": title,
    })

    logger.info("Steering: added subgoal %s — %s", goal_id, title)
    return {"success": True, "goal_id": goal_id, "message": f"Added goal {goal_id}: {title}"}


def split_subgoal(
    state_obj: st.UlwState,
    parent_id: str,
    sub_titles: list[str],
    idempotency_key: str = "",
) -> dict:
    """Split an in-progress goal into multiple sub-goals.

    The parent goal is marked as ``superseded`` and linked to its children.
    """
    if idempotency_key and _is_already_applied(state_obj.session_id, idempotency_key):
        return {"success": True, "goal_id": "", "message": "Already applied (dedup)"}

    # Find parent
    parent = None
    for g in state_obj.goals:
        if g.id == parent_id:
            parent = g
            break
    if not parent:
        return {"success": False, "goal_id": "", "message": f"Goal {parent_id} not found"}

    # Mark parent as superseded
    parent.status = st.GOAL_STATUS_SUPERSEDED

    # Create children
    existing_ids = {g.id for g in state_obj.goals}
    child_ids = []
    for title in sub_titles:
        n = 1
        while f"g_{n:03d}" in existing_ids:
            n += 1
        gid = f"g_{n:03d}"
        existing_ids.add(gid)
        child_ids.append(gid)

        child = st.Goal(
            id=gid,
            title=title,
            status=st.GOAL_STATUS_PENDING,
            parent_id=parent_id,
        )
        state_obj.goals.append(child)

    parent.superseded_by = child_ids
    st.save_goals(state_obj)
    state_obj.updated_at = __import__("time").time()

    st.ledger_append(state_obj.session_id, "steering:accept", {
        "idempotency_key": idempotency_key,
        "action": "split_subgoal",
        "parent_id": parent_id,
        "child_ids": child_ids,
    })

    logger.info("Steering: split %s into %s", parent_id, child_ids)
    return {
        "success": True,
        "goal_id": parent_id,
        "child_ids": child_ids,
        "message": f"Split {parent_id} into {len(sub_titles)} sub-goals: {', '.join(child_ids)}",
    }


def revise_criterion(
    state_obj: st.UlwState,
    goal_id: str,
    criterion_index: int,
    new_description: str,
    idempotency_key: str = "",
) -> dict:
    """Revise a success criterion for a goal."""
    if idempotency_key and _is_already_applied(state_obj.session_id, idempotency_key):
        return {"success": True, "message": "Already applied (dedup)"}

    for g in state_obj.goals:
        if g.id == goal_id:
            if 0 <= criterion_index < len(g.criteria):
                old = g.criteria[criterion_index].description
                g.criteria[criterion_index].description = new_description
                st.save_goals(state_obj)
                state_obj.updated_at = __import__("time").time()

                st.ledger_append(state_obj.session_id, "steering:accept", {
                    "idempotency_key": idempotency_key,
                    "action": "revise_criterion",
                    "goal_id": goal_id,
                    "criterion_index": criterion_index,
                    "old": old,
                    "new": new_description,
                })
                return {"success": True, "message": f"Revised criterion {criterion_index} of {goal_id}"}
            return {"success": False, "message": f"Criterion index {criterion_index} out of range"}

    return {"success": False, "message": f"Goal {goal_id} not found"}
