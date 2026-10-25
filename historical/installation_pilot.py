"""Predecessor installation-v1 pilot runner and historical outcome registry.

Accurately records that the historical installation-v1 run had an incomplete Case A
investigator turn and therefore did NOT pass its strict pair gate even though local
numerical checks succeeded.
"""

from __future__ import annotations

from typing import Any

from historical.installation_contracts import (
    CaseDiagnostics,
    InstallationPairReport,
    evaluate_strict_pair_gate,
)
from historical.installation_lab import InstallationLab

HISTORICAL_INSTALLATION_V1_OUTCOME: dict[str, Any] = {
    "benchmark_id": "installation-v1",
    "local_numerical_checks_succeeded": True,
    "case_a_investigator_turn_complete": False,
    "case_b_investigator_turn_complete": True,
    "strict_pair_gate_passed": False,
    "outcome_label": "INCOMPLETE_INVESTIGATOR_TURN_PAIR_FAIL",
}


def get_historical_installation_v1_record() -> InstallationPairReport:
    """Return the exact historical installation-v1 audit record.

    In the historical installation-v1 run:
    - Local numerical checks succeeded on both Case A and Case B.
    - Case B completed its investigator turn and identified the damper obstruction.
    - Case A had an incomplete investigator turn and did not emit a final verdict.
    - Consequently, `strict_pair_gate_passed` is `False`.
    """
    lab = InstallationLab()
    sim_a = lab.simulate_case("Case A")
    sim_b = lab.simulate_case("Case B")

    case_a = CaseDiagnostics(
        case_id="Case A",
        fault_mode=sim_a["fault_mode"],
        local_numerical_check_passed=bool(sim_a["local_numerical_check_passed"]),
        investigator_turn_complete=False,
        root_cause_identified=False,
        notes="Historical run terminated with an incomplete Case A investigator turn.",
    )
    case_b = CaseDiagnostics(
        case_id="Case B",
        fault_mode=sim_b["fault_mode"],
        local_numerical_check_passed=bool(sim_b["local_numerical_check_passed"]),
        investigator_turn_complete=True,
        root_cause_identified=True,
        notes="Case B investigator turn completed and identified obstructed_floor_damper.",
    )
    return evaluate_strict_pair_gate(case_a=case_a, case_b=case_b, benchmark_id="installation-v1")


def run_installation_pilot(simulate_complete_case_a: bool = False) -> dict[str, Any]:
    """Run the installation-v1 pilot.

    By default (`simulate_complete_case_a=False`), reproduces the exact historical audit
    record where Case A had an incomplete investigator turn (`strict_pair_gate_passed=False`).
    """
    lab = InstallationLab()
    sim_a = lab.simulate_case("Case A")
    sim_b = lab.simulate_case("Case B")

    if not simulate_complete_case_a:
        report = get_historical_installation_v1_record()
    else:
        case_a = CaseDiagnostics(
            case_id="Case A",
            fault_mode=sim_a["fault_mode"],
            local_numerical_check_passed=bool(sim_a["local_numerical_check_passed"]),
            investigator_turn_complete=True,
            root_cause_identified=True,
            notes="Synthetic complete override for Case A.",
        )
        case_b = CaseDiagnostics(
            case_id="Case B",
            fault_mode=sim_b["fault_mode"],
            local_numerical_check_passed=bool(sim_b["local_numerical_check_passed"]),
            investigator_turn_complete=True,
            root_cause_identified=True,
            notes="Case B investigator turn completed.",
        )
        report = evaluate_strict_pair_gate(case_a=case_a, case_b=case_b, benchmark_id="installation-v1")

    return {
        "pilot_id": "installation-v1",
        "lab_telemetry": {
            "case_a": sim_a,
            "case_b": sim_b,
        },
        "report": report.to_dict(),
    }
