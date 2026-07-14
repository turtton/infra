"""Blocker classification and escalation.

Inspired by oh-my-openagent's ``quality-gate-blockers.ts``:

  - Pattern-based blocker classification from evidence text
  - EXTERNAL_AUTHORIZATION_REQUIRED detection
  - 3-strike escalation to human intervention
"""

import logging
import re
from dataclasses import dataclass
from typing import Optional

from . import state as st

logger = logging.getLogger(__name__)

BLOCKER_AUTH = "EXTERNAL_AUTHORIZATION_REQUIRED"
BLOCKER_NETWORK = "NETWORK_ERROR"
BLOCKER_COMPILE = "COMPILE_ERROR"
BLOCKER_TEST = "TEST_FAILURE"
BLOCKER_MISSING_DEP = "MISSING_DEPENDENCY"
BLOCKER_UNKNOWN = "UNKNOWN"

# Signature patterns for blocker classification
_AUTH_PATTERNS = [
    re.compile(r"(?i)\b(auth|authenticate|credential|token|api.?key)\b.*\b(missing|required|unset|invalid|expired|not.?found)\b"),
    re.compile(r"\b(401|403|HTTP\s*401|HTTP\s*403)\b"),
    re.compile(r"(?i)\b(permission\s+denied|access\s+denied|unauthorized|forbidden)\b"),
    re.compile(r"(?i)\b(GHCR|docker|ghcr\.io).*\b(401|pull.?access|auth)\b"),
]

_NETWORK_PATTERNS = [
    re.compile(r"(?i)\b(timeout|connection refused|connection reset|network unreachable|dns\s+lookup\s+failed)\b"),
    re.compile(r"\b(5\d\d|503|502|504)\b"),
]

_COMPILE_PATTERNS = [
    re.compile(r"(?i)\b(compile error|syntax error|undefined reference|undefined symbol|import error|module not found)\b"),
    re.compile(r"(?i)\b(SyntaxError|TypeError|ValueError|NameError|KeyError|IndexError)\b"),
    re.compile(r"(?i)\bexit code:?\s*[1-9]\d*\s*$"),
]

_TEST_PATTERNS = [
    re.compile(r"(?i)\b(test failed|test.*fail|FAILED|failed tests|test.*error)\b"),
    re.compile(r"(?i)\b(\d+)\s*failed\b"),
]

_DEP_PATTERNS = [
    re.compile(r"(?i)\b(module not found|no module named|cannot find package|missing dependency|pip install|npm install)\b"),
    re.compile(r"(?i)\b(not found|command not found|no such file)\b"),
]


@dataclass
class Blocker:
    """A classified blocker."""
    category: str
    evidence: str
    count: int = 1  # How many times this same blocker occurred


def classify_blocker(evidence_text: str) -> str:
    """Classify an evidence text into a blocker category.

    Returns a BLOCKER_* constant.
    """
    for pattern in _AUTH_PATTERNS:
        if pattern.search(evidence_text):
            return BLOCKER_AUTH
    for pattern in _NETWORK_PATTERNS:
        if pattern.search(evidence_text):
            return BLOCKER_NETWORK
    for pattern in _COMPILE_PATTERNS:
        if pattern.search(evidence_text):
            return BLOCKER_COMPILE
    for pattern in _TEST_PATTERNS:
        if pattern.search(evidence_text):
            return BLOCKER_TEST
    for pattern in _DEP_PATTERNS:
        if pattern.search(evidence_text):
            return BLOCKER_MISSING_DEP
    return BLOCKER_UNKNOWN


def check_escalation(
    goal,  # Goal object with blocker_count and last_blocker
    new_blocker_text: str,
    session_id: str,
    threshold: int = 3,
) -> Optional[str]:
    """Check if a blocker should be escalated to human.

    Args:
        goal: Goal object (mutated in place: blocker_count, last_blocker).
        new_blocker_text: The evidence text containing the blocker.
        session_id: For ledger logging.
        threshold: Number of same-blocker occurrences before escalation.

    Returns:
        A human-readable escalation message, or None if no escalation needed.
    """
    category = classify_blocker(new_blocker_text)
    goal.last_blocker = category
    goal.blocker_count += 1

    from . import state as st
    st.ledger_append(session_id, "blocker:recorded", {
        "goal_id": goal.id,
        "category": category,
        "count": goal.blocker_count,
        "evidence": new_blocker_text[:200],
    })

    if goal.blocker_count >= threshold:
        from . import state as st
        st.ledger_append(session_id, "blocker:escalated", {
            "goal_id": goal.id,
            "category": category,
            "count": goal.blocker_count,
            "message": "3回同じブロッカー発生 — 人間の介入が必要です",
        })
        return (
            f"🚨 **ブロッカー escalation**: ゴール `{goal.id}` で"
            f" `{category}` が{threshold}回発生しました。\n"
            f"最終エビデンス: `{new_blocker_text[:200]}`\n"
            f"人間の判断が必要です。"
        )

    return None
