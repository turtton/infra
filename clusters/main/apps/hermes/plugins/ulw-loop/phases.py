"""ULW-loop phase engine — 7-phase state machine.

Phases mirror oh-my-openagent's ULW-loop model with a plan-review gate:

  explore → plan → plan_review → execute → verify → review → fix ─→ complete
                       ↑  ↓                           ↑_____________|
                       └──┘ (plan revision loop)      (fix loop)

plan_review gates execution: the reviewer must approve the plan with
<promise>DONE</promise> before execute starts; <request_fix> loops back
to plan. Each phase transition is gated by token detection and/or
quality gates.
"""

import logging
from typing import Optional

from . import state as st
from . import tokens as tk
from . import quality_gate as qg
from . import blocker as bl

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Phase definitions
# ---------------------------------------------------------------------------

PHASE_EXPLORE = "explore"
PHASE_PLAN = "plan"
PHASE_PLAN_REVIEW = "plan_review"
PHASE_EXECUTE = "execute"
PHASE_VERIFY = "verify"
PHASE_REVIEW = "review"
PHASE_FIX = "fix"
PHASE_COMPLETE = "complete"

PHASE_CYCLE = [PHASE_EXPLORE, PHASE_PLAN, PHASE_PLAN_REVIEW,
               PHASE_EXECUTE, PHASE_VERIFY, PHASE_REVIEW, PHASE_FIX]

# Human-readable phase labels for system prompt injection
PHASE_LABELS = {
    PHASE_EXPLORE: "探索 (Explore)",
    PHASE_PLAN: "計画 (Plan)",
    PHASE_PLAN_REVIEW: "計画レビュー (Plan Review)",
    PHASE_EXECUTE: "実行 (Execute)",
    PHASE_VERIFY: "検証 (Verify)",
    PHASE_REVIEW: "レビュー (Review)",
    PHASE_FIX: "修正 (Fix)",
    PHASE_COMPLETE: "完了 (Complete)",
}

# Phase descriptions for LLM guidance
PHASE_DESCRIPTIONS = {
    PHASE_EXPLORE: (
        "目標を理解し、要件を明確にします。"
        "競合するアプローチを調査し、リスクを特定します。"
        "出力: 調査結果とアプローチの提案"
    ),
    PHASE_PLAN: (
        "ゴールを分解し、acceptance criteria を定義します。"
        "各ゴールに happy path / edge case / regression の3つの成功基準を設定します。"
        "完了したら `<request_review>` を出力して計画レビューを依頼してください。"
        "出力: ゴール一覧 (goals.json)"
    ),
    PHASE_PLAN_REVIEW: (
        "Plannerが立てた計画（ゴール分解・acceptance criteria）をレビューします。"
        "計画が妥当で実行可能なら `<promise>DONE</promise>` を出力して承認し、"
        "不十分なら `<request_fix>` を出力して具体的な修正を指示してください。"
        "出力: 承認 (<promise>DONE</promise>) or 修正指示 (<request_fix>)"
    ),
    PHASE_EXECUTE: (
        "現在のゴールを実装します。"
        "完了したら `<request_review>` を出力してレビューをリクエストしてください。"
    ),
    PHASE_VERIFY: (
        "実装が acceptance criteria を満たしているか検証します。"
        "テストを実行し、エビデンスを収集します。"
        "出力: 検証結果とエビデンス"
    ),
    PHASE_REVIEW: (
        "コードレビューを実施します。"
        "問題がなければ `<promise>DONE</promise>` を出力して承認します。"
        "修正が必要な場合は `<request_fix>` を出力して具体的な修正を指示してください。"
    ),
    PHASE_FIX: (
        "レビューで指摘された問題を修正します。"
        "修正後は再度 verify → review のサイクルに入ります。"
    ),
}


def phase_index(phase: str) -> int:
    """Return the numeric index of a phase in the cycle."""
    try:
        return PHASE_CYCLE.index(phase)
    except ValueError:
        return -1


def next_phase(current: str, response_text: str, state_obj: st.UlwState) -> str:
    """Determine the next phase based on current phase and LLM response tokens.

    Args:
        current: Current phase string.
        response_text: The LLM's full response text.
        state_obj: Current UlwState (may be mutated for blocker tracking).

    Returns:
        The next phase string.
    """
    tokens_found = tk.detect_tokens(response_text)

    # --- PLAN → PLAN_REVIEW: planner hands off the plan for review ---
    if current == PHASE_PLAN and tk.TOKEN_REQUEST_REVIEW in tokens_found:
        st.ledger_append(state_obj.session_id, "phase:transition", {
            "from": current,
            "to": PHASE_PLAN_REVIEW,
            "reason": "Planner requested plan review",
        })
        return PHASE_PLAN_REVIEW

    # --- PLAN_REVIEW gate: reviewer approves plan or requests plan fixes ---
    if current == PHASE_PLAN_REVIEW:
        if tk.TOKEN_DONE in tokens_found:
            st.ledger_append(state_obj.session_id, "phase:transition", {
                "from": current,
                "to": PHASE_EXECUTE,
                "reason": "Reviewer approved the plan",
            })
            return PHASE_EXECUTE
        if tk.TOKEN_REQUEST_FIX in tokens_found:
            state_obj.iteration += 1  # Count plan revision iterations
            st.ledger_append(state_obj.session_id, "phase:transition", {
                "from": current,
                "to": PHASE_PLAN,
                "reason": "Reviewer requested plan fixes",
            })
            return PHASE_PLAN
        # Gate: no approval token → stay in plan_review until reviewer decides
        return current

    # --- TERMINAL: promise DONE in review phase = complete ---
    if current == PHASE_REVIEW and tk.TOKEN_DONE in tokens_found:
        st.ledger_append(state_obj.session_id, "phase:complete", {
            "phase": current,
            "message": "Reviewer approved with <promise>DONE</promise>",
        })
        return PHASE_COMPLETE

    if current == PHASE_VERIFY and tk.TOKEN_VERIFIED in tokens_found:
        st.ledger_append(state_obj.session_id, "phase:transition", {
            "from": current,
            "to": PHASE_REVIEW,
            "reason": "Verification passed",
        })
        return PHASE_REVIEW

    # --- REVIEW → FIX loop ---
    if current == PHASE_REVIEW and tk.TOKEN_REQUEST_FIX in tokens_found:
        st.ledger_append(state_obj.session_id, "phase:transition", {
            "from": current,
            "to": PHASE_FIX,
            "reason": "Reviewer requested fixes",
        })
        return PHASE_FIX

    if current == PHASE_FIX:
        # After fix, go back to verify
        st.ledger_append(state_obj.session_id, "phase:transition", {
            "from": current,
            "to": PHASE_VERIFY,
            "reason": "Fixes applied, re-verifying",
        })
        return PHASE_VERIFY

    # --- EXECUTE → REQUEST REVIEW ---
    if current == PHASE_EXECUTE and tk.TOKEN_REQUEST_REVIEW in tokens_found:
        st.ledger_append(state_obj.session_id, "phase:transition", {
            "from": current,
            "to": PHASE_VERIFY,
            "reason": "Implementation complete, requesting review",
        })
        return PHASE_VERIFY

    # --- Default: linear progression through cycle ---
    idx = phase_index(current)
    if 0 <= idx < len(PHASE_CYCLE) - 1:
        next_p = PHASE_CYCLE[idx + 1]
        st.ledger_append(state_obj.session_id, "phase:transition", {
            "from": current,
            "to": next_p,
            "reason": "Normal phase progression",
        })
        return next_p

    # Fallback: stay in current phase
    return current


def build_phase_prompt(state_obj: st.UlwState) -> str:
    """Build the phase guidance block for system prompt injection.

    This is injected into the LLM context via ``pre_llm_call`` hook.
    If ``conversation_context`` is set on the state, a context block
    is included so the downstream profile sees the full discussion
    history that led to this ULW-loop goal.
    """
    phase = state_obj.phase
    label = PHASE_LABELS.get(phase, phase)
    desc = PHASE_DESCRIPTIONS.get(phase, "")
    goals_summary = _goals_summary(state_obj)

    context_block = ""
    if state_obj.conversation_context:
        context_block = (
            f"\n===== CONVERSATION CONTEXT =====\n"
            f"{state_obj.conversation_context}\n"
            f"================================\n"
        )

    return (
        f"\n\n===== ULW-LOOP PHASE: {label} =====\n"
        f"{desc}\n\n"
        f"{context_block}"
        f"目標: {state_obj.brief}\n"
        f"イテレーション: {state_obj.iteration}\n"
        f"ゴール進捗: {goals_summary}\n"
        f"---\n"
        f"利用可能なトークン:\n"
        f"  - 完了/承認: `<promise>DONE</promise>`\n"
        f"  - 検証完了: `<promise>VERIFIED</promise>`\n"
        f"  - レビュー依頼: `<request_review>`\n"
        f"  - 修正依頼: `<request_fix>`\n"
        f"================================"
    )


def _goals_summary(state_obj: st.UlwState) -> str:
    """Return a compact summary of all goals and their statuses."""
    parts = []
    for g in state_obj.goals:
        done = sum(1 for c in g.criteria if c.passed)
        total = len(g.criteria)
        parts.append(f"  [{g.status}] {g.id}: {g.title} ({done}/{total} criteria)")
    return "\n".join(parts) if parts else "  (未分解)"
