"""ULW-loop plugin for Hermes Agent.

Registers the ``/ulw-loop`` slash command and lifecycle hooks for
the full explore→plan→execute→verify→review→fix workflow.

Hooks (registered when available):
  - pre_llm_call: Inject current phase guidance into system prompt
  - post_llm_call: Detect tokens, advance phase, update state
  - pre_verify: Quality Gate check before allowing completion
"""

import logging
from typing import Any

from . import ulw_loop
from . import state as st
from . import phases as ph
from . import tokens as tk
from . import quality_gate as qg
from . import blocker as bl
from . import steering as steer

logger = logging.getLogger(__name__)

# Global session state cache (session_id → UlwState)
_active_states: dict[str, st.UlwState] = {}


def register(ctx):
    """Register the ``/ulw-loop`` slash command and hooks."""
    # Slash command
    ctx.register_command(
        name="ulw-loop",
        handler=ulw_loop.handle_ulw_command,
        description="ULW-loop: 目標をKanbanタスクに分解してマルチエージェントで実行する",
        args_hint="<goal description>",
    )

    # Steering sub-commands
    ctx.register_command(
        name="ulw-steer",
        handler=handle_steer_command,
        description="ULW-loop: 実行中の計画を変更する (add/split/revise)",
        args_hint="<action> <goal-id> <params>",
    )

    # Register lifecycle hooks
    _register_hooks(ctx)


def _register_hooks(ctx):
    """Register lifecycle hooks, gracefully skipping any that aren't supported."""
    hooks_registered = 0

    try:
        ctx.register_hook("pre_llm_call", on_pre_llm_call)
        hooks_registered += 1
    except (AttributeError, TypeError):
        logger.info("pre_llm_call hook not available in this Hermes version")

    try:
        ctx.register_hook("post_llm_call", on_post_llm_call)
        hooks_registered += 1
    except (AttributeError, TypeError):
        logger.info("post_llm_call hook not available in this Hermes version")

    try:
        ctx.register_hook("pre_verify", on_pre_verify)
        hooks_registered += 1
    except (AttributeError, TypeError):
        logger.info("pre_verify hook not available in this Hermes version")

    logger.info("ULW-loop: registered %d hooks", hooks_registered)


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _get_state(session_id: str) -> st.UlwState | None:
    """Get cached or load state for a session."""
    if session_id in _active_states:
        return _active_states[session_id]
    state = st.load_goals(session_id)
    if state:
        _active_states[session_id] = state
    return state


def _set_state(state_obj: st.UlwState) -> None:
    """Cache state and persist to disk."""
    _active_states[state_obj.session_id] = state_obj
    st.save_goals(state_obj)


def _clear_state(session_id: str) -> None:
    """Remove cached state."""
    _active_states.pop(session_id, None)
    st.clear_resume(session_id)


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

def on_pre_llm_call(agent, messages: list[dict], **kw) -> None:
    """Inject current ULW-loop phase guidance into the system prompt.

    Also handles auto-resume: if state file exists and is incomplete,
    inject a resume context block.
    """
    session_id = getattr(agent, "session_id", None)
    if not session_id:
        return

    state_obj = _get_state(session_id)
    if not state_obj:
        # Check for resume possibility
        resume = st.load_resume(session_id)
        if resume and resume.get("phase") != ph.PHASE_COMPLETE:
            # Auto-resume with 2-strike cap
            state_obj = st.load_goals(session_id)
            if state_obj:
                if state_obj.no_progress_count >= 2:
                    logger.warning(
                        "ULW-loop: 2-strike cap reached for %s, not resuming",
                        session_id,
                    )
                    return
                state_obj.resume_count += 1
                _set_state(state_obj)
                st.ledger_append(session_id, "resume", {
                    "phase": state_obj.phase,
                    "resume_count": state_obj.resume_count,
                })
                _inject_phase(messages, state_obj, resume=True)
                return
        return

    # Normal phase injection
    _inject_phase(messages, state_obj)
    st.save_resume(session_id, state_obj.phase, state_obj.iteration)


def _inject_phase(messages: list[dict], state_obj: st.UlwState, resume: bool = False) -> None:
    """Inject phase guidance into the system message."""
    phase_prompt = ph.build_phase_prompt(state_obj)
    if resume:
        phase_prompt += (
            f"\n\n🔄 **ULW-loop自動再開** (試行 {state_obj.resume_count}回目)\n"
            f"前回のセッションから再開しました。"
        )

    # Find the system message and append guidance
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if isinstance(content, str):
                if "===== ULW-LOOP PHASE:" not in content:
                    msg["content"] = content + phase_prompt
            break


def on_post_llm_call(agent, response: str, **kw) -> None:
    """Process LLM response: detect tokens, advance phase, update state.

    Handles:
      - Token detection and phase transition
      - Quality Gate invocation on verify phase
      - Blocker classification on failures
      - No-progress detection (2-strike cap)
    """
    session_id = getattr(agent, "session_id", None)
    if not session_id:
        return

    state_obj = _get_state(session_id)
    if not state_obj:
        return

    # Skip if completed
    if state_obj.phase == ph.PHASE_COMPLETE:
        return

    # Detect tokens
    tokens_found = tk.detect_tokens(response)
    old_phase = state_obj.phase

    # Phase transition
    new_phase = ph.next_phase(old_phase, response, state_obj)

    if new_phase != old_phase:
        state_obj.phase = new_phase
        state_obj.updated_at = __import__("time").time()

        if new_phase == ph.PHASE_COMPLETE:
            st.ledger_append(session_id, "ulw:complete", {
                "brief": state_obj.brief,
                "iteration": state_obj.iteration,
                "goals_completed": sum(
                    1 for g in state_obj.goals if g.status == st.GOAL_STATUS_COMPLETE
                ),
            })
            _clear_state(session_id)
        else:
            if new_phase == ph.PHASE_FIX:
                state_obj.iteration += 1  # Increment iteration on review→fix cycle
            _set_state(state_obj)

        logger.info(
            "ULW-loop: phase %s → %s (tokens: %s)",
            old_phase, new_phase, tokens_found,
        )
    else:
        # Check for no-progress (same phase, no tokens)
        if not tokens_found:
            state_obj.no_progress_count += 1
            st.ledger_append(session_id, "no_progress", {
                "phase": old_phase,
                "count": state_obj.no_progress_count,
            })
            if state_obj.no_progress_count >= 2:
                logger.warning(
                    "ULW-loop: 2-strike no-progress cap for %s", session_id
                )
        _set_state(state_obj)


def on_pre_verify(agent, **kw) -> dict | None:
    """Quality Gate check before allowing the agent to stop.

    Runs quality gates on the current state. If any gate fails,
    returns a ``continue`` directive to keep the agent going.
    """
    session_id = getattr(agent, "session_id", None)
    if not session_id:
        return None

    state_obj = _get_state(session_id)
    if not state_obj:
        return None

    # Only check during verify/review phases
    if state_obj.phase not in (ph.PHASE_VERIFY, ph.PHASE_REVIEW):
        return None

    changed_paths = list(kw.get("changed_paths", []) or [])
    active_goal = _get_active_goal(state_obj)

    report = qg.run_quality_gates(
        state=state_obj,
        changed_paths=changed_paths,
        criteria=active_goal.criteria if active_goal else None,
    )

    # Log to ledger
    st.ledger_append(session_id, "quality_gate", {
        "phase": state_obj.phase,
        "overall": report.overall,
        "gates": [{"name": g.gate_name, "status": g.status, "message": g.message}
                   for g in report.gates],
    })

    if report.overall == qg.GATE_FAIL:
        prompt = qg.gate_report_to_prompt(report)
        prompt += "\n\n修正して再度レビューを依頼してください。"
        return {"action": "continue", "message": prompt}

    # Pass all gates
    return None


def _get_active_goal(state_obj: st.UlwState):
    """Get the currently in-progress goal, or None."""
    for g in state_obj.goals:
        if g.status == st.GOAL_STATUS_IN_PROGRESS:
            return g
    # Fallback: first pending goal
    for g in state_obj.goals:
        if g.status == st.GOAL_STATUS_PENDING:
            return g
    return None


# ---------------------------------------------------------------------------
# Steering command handler
# ---------------------------------------------------------------------------

def handle_steer_command(raw_args: str) -> str | None:
    """Handle the ``/ulw-steer`` command."""
    if not raw_args.strip():
        return (
            "**使用方法:** `/ulw-steer <action> ...`\n\n"
            "**追加:** `/ulw-steer add <session-id> <title>`\n"
            "**分割:** `/ulw-steer split <session-id> <goal-id> <title1>|<title2>|...`\n"
            "**修正:** `/ulw-steer revise <session-id> <goal-id> <idx> <new-text>`"
        )

    # Compute idempotency key from full raw args
    import hashlib
    idempotency_key = hashlib.sha256(raw_args.strip().encode("utf-8")).hexdigest()[:16]

    parts = raw_args.strip().split(maxsplit=3)
    action = parts[0].lower()
    session_id = parts[1] if len(parts) >= 2 else ""

    # Path traversal guard
    if ".." in session_id or "/" in session_id:
        return f"❌ Invalid session_id: contains path separators"

    if action == "add":
        if len(parts) < 3:
            return "Usage: /ulw-steer add <session-id> <title>"
        _, session_id, title = parts[0], parts[1], parts[2]
        state_obj = _get_state(session_id)
        if not state_obj:
            return f"❌ Session {session_id} not found"
        result = steer.add_subgoal(state_obj, title=title, idempotency_key=idempotency_key)
        _set_state(state_obj)
        return f"✅ {result['message']}"

    elif action == "split":
        if len(parts) < 4:
            return "Usage: /ulw-steer split <session-id> <goal-id> <title1>|<title2>|..."
        _, session_id, goal_id = parts[0], parts[1], parts[2]
        titles = [t.strip() for t in parts[3].split("|") if t.strip()]
        if not titles:
            return "❌ At least one sub-title required (pipe-separated)"
        state_obj = _get_state(session_id)
        if not state_obj:
            return f"❌ Session {session_id} not found"
        result = steer.split_subgoal(state_obj, parent_id=goal_id, sub_titles=titles, idempotency_key=idempotency_key)
        _set_state(state_obj)
        if result["success"]:
            return f"✅ {result['message']}"
        return f"❌ {result['message']}"

    elif action == "revise":
        if len(parts) < 5:
            return "Usage: /ulw-steer revise <session-id> <goal-id> <idx> <new-text>"
        _, session_id, goal_id = parts[0], parts[1], parts[2]
        try:
            idx = int(parts[3])
        except ValueError:
            return f"❌ Invalid index: {parts[3]}"
        text = parts[4]
        state_obj = _get_state(session_id)
        if not state_obj:
            return f"❌ Session {session_id} not found"
        result = steer.revise_criterion(state_obj, goal_id, idx, text, idempotency_key=idempotency_key)
        _set_state(state_obj)
        if result["success"]:
            return f"✅ {result['message']}"
        return f"❌ {result['message']}"

    return f"❌ Unknown action: {action}. Use `add`, `split`, or `revise`."
