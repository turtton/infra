"""Quality Gate — multi-stage verification before allowing ULW-loop completion.

Inspired by oh-my-openagent's 5-gate checkpoint system:
  1. codeReview      — Code quality assessment
  2. manualQa        — Evidence collection across surfaces
  3. gateReview      — Secondary reviewer verification
  4. iteration       — Test re-run verification
  5. criteriaCoverage — Criteria pass/total tracking

Hermes version uses ``pre_verify`` hook to gate the "stop and finish" decision.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# Quality gate statuses
GATE_PASS = "pass"
GATE_FAIL = "fail"
GATE_WATCH = "watch"  # Non-blocking concern (ledger annotation only)
GATE_SKIP = "skip"


@dataclass
class GateResult:
    """Result of a single quality gate check."""
    gate_name: str
    status: str  # pass | fail | watch | skip
    message: str = ""
    details: dict = field(default_factory=dict)


@dataclass
class QualityGateReport:
    """Aggregated results of all quality gates."""
    gates: list[GateResult] = field(default_factory=list)
    overall: str = GATE_PASS  # pass if all gates pass; fail if any fail

    def add(self, gate: GateResult) -> None:
        self.gates.append(gate)
        if gate.status == GATE_FAIL:
            self.overall = GATE_FAIL

    @property
    def blockers(self) -> list[GateResult]:
        return [g for g in self.gates if g.status == GATE_FAIL]

    @property
    def watches(self) -> list[GateResult]:
        return [g for g in self.gates if g.status == GATE_WATCH]


# ---------------------------------------------------------------------------
# Built-in gate checkers
# ---------------------------------------------------------------------------

def check_criteria_coverage(
    criteria: list,
) -> GateResult:
    """Check how many criteria have been passed vs total.

    From oh-my-openagent: tracks passCount vs totalCriteria,
    originalIntent, and desiredOutcome.
    """
    total = len(criteria)
    passed = sum(1 for c in criteria if c.passed)
    if total == 0:
        return GateResult(
            gate_name="criteriaCoverage",
            status=GATE_SKIP,
            message="No criteria defined",
            details={"total": 0, "passed": 0},
        )
    if passed == total:
        return GateResult(
            gate_name="criteriaCoverage",
            status=GATE_PASS,
            message=f"All {total}/{total} criteria met",
            details={"total": total, "passed": passed},
        )
    # Partial coverage — WATCH (not a blocker, but note it)
    return GateResult(
        gate_name="criteriaCoverage",
        status=GATE_WATCH,
        message=f"{passed}/{total} criteria met",
        details={"total": total, "passed": passed},
    )


# Pattern for "adversarial classes" — edge cases, error conditions,
# security concerns. From oh-my-openagent: checks whether the
# implementation covers attack surfaces.
_ADVERSARIAL_KEYWORDS = [
    "edge case", "error handling", "exception", "timeout",
    "race condition", "null", "empty", "invalid input",
    "permission", "authorization", "rate limit", "concurrent",
    "rollback", "deadlock", "overflow", "injection",
]


def check_adversarial_coverage(changed_paths: list[str]) -> GateResult:
    """Check if adversarial cases are considered in the output.

    This is a lightweight heuristic — scans the list of changed files
    for test files that suggest edge case coverage.
    """
    test_files = [p for p in changed_paths if "test" in p.lower()]
    if test_files:
        return GateResult(
            gate_name="adversarialCoverage",
            status=GATE_PASS,
            message=f"Found {len(test_files)} test files",
            details={"test_files": test_files, "adversarial_check": "deferred"},
        )
    # No tests found — WATCH (not a blocker for simple tasks)
    return GateResult(
        gate_name="adversarialCoverage",
        status=GATE_WATCH,
        message="No test files found — adversarial coverage unverified",
        details={"test_files": [], "adversarial_check": "skipped"},
    )


def run_quality_gates(
    state,
    changed_paths: list[str] | None = None,
    criteria: list | None = None,
) -> QualityGateReport:
    """Run all applicable quality gates and return the report.

    This is called by the ``pre_verify`` hook to decide whether
    the agent can stop or needs to continue.
    """
    report = QualityGateReport()

    # Criteria coverage
    if criteria:
        report.add(check_criteria_coverage(criteria))

    # Adversarial coverage
    if changed_paths:
        report.add(check_adversarial_coverage(changed_paths))

    # Log results
    for gate in report.gates:
        status_icon = {
            GATE_PASS: "✅",
            GATE_FAIL: "❌",
            GATE_WATCH: "👀",
            GATE_SKIP: "⏭️",
        }.get(gate.status, "❓")
        logger.info(
            "%s QualityGate[%s]: %s — %s",
            status_icon, gate.gate_name, gate.status.upper(), gate.message,
        )

    return report


def gate_report_to_prompt(report: QualityGateReport) -> str:
    """Format quality gate report for model consumption."""
    lines = ["\n===== QUALITY GATE REPORT ====="]
    for gate in report.gates:
        icon = {
            GATE_PASS: "✅", GATE_FAIL: "❌",
            GATE_WATCH: "👀", GATE_SKIP: "⏭️",
        }.get(gate.status, "❓")
        lines.append(f"  {icon} {gate.gate_name}: {gate.message}")
    if report.overall == GATE_FAIL:
        lines.append("❌ QUALITY GATE FAILED — 修正が必要です")
    else:
        lines.append("✅ All gates passed")
    lines.append("================================")
    return "\n".join(lines)
