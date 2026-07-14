"""Token-based inter-agent communication protocol.

Maps to oh-my-openagent's Ralph Loop token system:

  - ``<promise>DONE</promise>``      → Final approval (reviewer → complete)
  - ``<promise>VERIFIED</promise>``  → Verification passed (verify → review)
  - ``<request_review>``            → Review requested (execute → verify)
  - ``<request_fix>``               → Fix requested (review → fix)

Tokens are simple XML-like tags embedded in LLM output.
Detection is done via regex (not XML parsing) for reliability.
"""

import re
from typing import Set

# Token definitions
TOKEN_DONE = "promise_done"
TOKEN_VERIFIED = "promise_verified"
TOKEN_REQUEST_REVIEW = "request_review"
TOKEN_REQUEST_FIX = "request_fix"

# Compiled patterns (case-insensitive)
_PATTERN_DONE = re.compile(r"<promise>\s*DONE\s*</promise>", re.IGNORECASE)
_PATTERN_VERIFIED = re.compile(r"<promise>\s*VERIFIED\s*</promise>", re.IGNORECASE)
_PATTERN_REVIEW = re.compile(r"<request_review\s*/?>", re.IGNORECASE)
_PATTERN_FIX = re.compile(r"<request_fix\s*/?>", re.IGNORECASE)


def detect_tokens(text: str) -> Set[str]:
    """Scan response text for ULW-loop tokens.

    Returns a set of token constants found.
    """
    found: Set[str] = set()
    if _PATTERN_DONE.search(text):
        found.add(TOKEN_DONE)
    if _PATTERN_VERIFIED.search(text):
        found.add(TOKEN_VERIFIED)
    if _PATTERN_REVIEW.search(text):
        found.add(TOKEN_REQUEST_REVIEW)
    if _PATTERN_FIX.search(text):
        found.add(TOKEN_REQUEST_FIX)
    return found


def strip_tokens(text: str) -> str:
    """Remove ULW-loop tokens from text for clean display."""
    result = _PATTERN_DONE.sub("", text)
    result = _PATTERN_VERIFIED.sub("", result)
    result = _PATTERN_REVIEW.sub("", result)
    result = _PATTERN_FIX.sub("", result)
    return result.strip()


def token_help_text() -> str:
    """Return help text about tokens for system prompt injection."""
    return (
        "【ULW-loop トークン】\n"
        "  完了/承認: <promise>DONE</promise>\n"
        "  検証完了:  <promise>VERIFIED</promise>\n"
        "  レビュー依頼: <request_review>\n"
        "  修正依頼:   <request_fix>\n"
        "適切なトークンを出力に含めることでフェーズ遷移が行われます。"
    )
