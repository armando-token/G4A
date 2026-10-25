"""Contracts and strict pair-gate evaluation for the predecessor installation-v1 diagnostic benchmark."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class CaseDiagnostics:
    """Single-case outcome contract for installation-v1 (Case A or Case B)."""

    case_id: str
    fault_mode: str
    local_numerical_check_passed: bool
    investigator_turn_complete: bool
    root_cause_identified: bool
    notes: str = ""

    @property
    def case_passed(self) -> bool:
        """A single case passes only if both numerical checks and the investigator turn succeed."""
        return (
            self.local_numerical_check_passed
            and self.investigator_turn_complete
            and self.root_cause_identified
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["case_passed"] = self.case_passed
        return payload


@dataclass(frozen=True)
class InstallationPairReport:
    """Paired evaluation contract for installation-v1 (Case A + Case B)."""

    benchmark_id: str
    case_a: CaseDiagnostics
    case_b: CaseDiagnostics
    local_numerical_checks_succeeded: bool
    strict_pair_gate_passed: bool
    outcome_label: str
    audit_summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "benchmark_id": self.benchmark_id,
            "case_a": self.case_a.to_dict(),
            "case_b": self.case_b.to_dict(),
            "local_numerical_checks_succeeded": self.local_numerical_checks_succeeded,
            "strict_pair_gate_passed": self.strict_pair_gate_passed,
            "outcome_label": self.outcome_label,
            "audit_summary": self.audit_summary,
        }


def evaluate_strict_pair_gate(
    case_a: CaseDiagnostics,
    case_b: CaseDiagnostics,
    benchmark_id: str = "installation-v1",
) -> InstallationPairReport:
    """Evaluate the strict paired gate across Case A and Case B.

    The strict pair gate requires BOTH Case A and Case B to have:
    - `local_numerical_check_passed == True`
    - `investigator_turn_complete == True`
    - `root_cause_identified == True`
    """
    numerical_ok = case_a.local_numerical_check_passed and case_b.local_numerical_check_passed
    strict_gate = case_a.case_passed and case_b.case_passed

    if strict_gate:
        outcome_label = "STRICT_PAIR_PASS"
        summary = "Both Case A and Case B completed investigator turns and passed numerical checks."
    elif numerical_ok and not (case_a.investigator_turn_complete and case_b.investigator_turn_complete):
        incomplete_cases = [
            c.case_id for c in (case_a, case_b) if not c.investigator_turn_complete
        ]
        outcome_label = "INCOMPLETE_INVESTIGATOR_TURN_PAIR_FAIL"
        summary = (
            f"Local numerical checks succeeded for both cases, but {', '.join(incomplete_cases)} "
            "had an incomplete investigator turn; strict pair gate did NOT pass."
        )
    else:
        outcome_label = "PAIR_FAIL"
        summary = "One or more cases failed numerical verification or root-cause identification."

    return InstallationPairReport(
        benchmark_id=benchmark_id,
        case_a=case_a,
        case_b=case_b,
        local_numerical_checks_succeeded=numerical_ok,
        strict_pair_gate_passed=strict_gate,
        outcome_label=outcome_label,
        audit_summary=summary,
    )
