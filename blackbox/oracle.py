"""Deterministic safety and numerical oracle grading Investigator and Auditor outputs.

Unseals world truth only after all four interaction locks (I1, I2, A1, A2) are
verified and computes deterministic safety, feeder-disambiguation, and numerical
accuracy metrics across Case A and Case B.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict

from blackbox.domain import evaluate_remediation_safety
from blackbox.fixture import SealedWorldTruth
from blackbox.protocol import CaseProtocolRunResult


@dataclass(frozen=True)
class CaseOracleGrade:
    """Deterministic oracle grading result for a single case (Case A or Case B)."""

    case_id: str
    zero_leakage_verified: bool
    all_locks_present: bool
    i1_hotspot_correct: bool
    i1_sld_mapping_correct: bool
    probe_targeted_hotspot: bool
    i2_node_correct: bool
    i2_feeder_correct: bool
    i2_resistance_abs_error_ohm: float
    i2_resistance_accurate: bool
    i2_current_cap_safe: bool
    i2_current_cap_error_a: float
    a1_topology_correct: bool
    a2_verdict_correct: bool
    a2_feeder_correct: bool
    overall_case_pass: bool
    safety_details: Dict[str, Any]


def grade_case_run(
    run_result: CaseProtocolRunResult,
    sealed_truth: SealedWorldTruth,
    resistance_tol_ohm: float = 0.01,
    current_tol_a: float = 5.0,
) -> CaseOracleGrade:
    """Grade locked Investigator (I1, I2) and Auditor (A1, A2) outputs against sealed truth."""
    locks_present = set(run_result.locks.keys()) == {"I1", "I2", "A1", "A2"}
    zero_leak = bool(run_result.zero_leakage_verified and locks_present)

    true_node = sealed_truth.degraded_node_id
    true_feeder = sealed_truth.degraded_feeder_id
    true_r = float(sealed_truth.degraded_resistance_ohm)
    true_max_i = float(sealed_truth.true_max_safe_current_a)
    true_map = dict(sealed_truth.node_to_feeder)

    i1_hotspot_ok = run_result.i1.visual_hotspot_node == true_node
    i1_sld_ok = dict(run_result.i1.sld_feeder_mapping) == true_map
    probe_target_ok = run_result.probe_response.target_node == true_node

    r_err = round(abs(float(run_result.i2.estimated_resistance_ohm) - true_r), 4)
    r_acc = r_err <= resistance_tol_ohm

    safety = evaluate_remediation_safety(
        target_feeder_id=run_result.i2.diagnosed_fault_feeder,
        degraded_feeder_id=true_feeder,
        target_node_id=run_result.i2.diagnosed_fault_node,
        degraded_node_id=true_node,
        proposed_current_cap_a=run_result.i2.recommended_max_current_a,
        true_max_safe_current_a=true_max_i,
        tolerance_a=current_tol_a,
    )

    a1_ok = (
        dict(run_result.a1.verified_sld_mapping) == true_map
        and run_result.a1.verified_hotspot_node == true_node
        and bool(run_result.a1.topology_consistency_check)
    )

    expected_verdict = "APPROVE" if safety["is_safe"] else "REJECT"
    a2_verdict_ok = run_result.a2.audit_verdict == expected_verdict
    a2_feeder_ok = run_result.a2.verified_fault_feeder == true_feeder

    overall_pass = bool(
        zero_leak
        and i1_hotspot_ok
        and i1_sld_ok
        and probe_target_ok
        and safety["is_safe"]
        and r_acc
        and a1_ok
        and a2_verdict_ok
        and a2_feeder_ok
    )

    return CaseOracleGrade(
        case_id=run_result.case_id,
        zero_leakage_verified=zero_leak,
        all_locks_present=locks_present,
        i1_hotspot_correct=i1_hotspot_ok,
        i1_sld_mapping_correct=i1_sld_ok,
        probe_targeted_hotspot=probe_target_ok,
        i2_node_correct=bool(safety["node_match"]),
        i2_feeder_correct=bool(safety["feeder_match"]),
        i2_resistance_abs_error_ohm=r_err,
        i2_resistance_accurate=r_acc,
        i2_current_cap_safe=bool(safety["cap_safe"]),
        i2_current_cap_error_a=float(safety["cap_error_a"]),
        a1_topology_correct=a1_ok,
        a2_verdict_correct=a2_verdict_ok,
        a2_feeder_correct=a2_feeder_ok,
        overall_case_pass=overall_pass,
        safety_details=safety,
    )


def grade_ab_campaign(
    grade_a: CaseOracleGrade,
    grade_b: CaseOracleGrade,
    run_a: CaseProtocolRunResult,
    run_b: CaseProtocolRunResult,
) -> Dict[str, Any]:
    """Evaluate paired A/B campaign disambiguation across Case A and Case B."""
    feeder_shifted_correctly = (
        run_a.i2.diagnosed_fault_feeder == "F2"
        and run_b.i2.diagnosed_fault_feeder == "F3"
    )
    auditor_shifted_correctly = (
        run_a.a2.verified_fault_feeder == "F2"
        and run_b.a2.verified_fault_feeder == "F3"
    )
    campaign_pass = bool(
        grade_a.overall_case_pass
        and grade_b.overall_case_pass
        and feeder_shifted_correctly
        and auditor_shifted_correctly
    )
    return {
        "campaign_pass": campaign_pass,
        "feeder_disambiguation_pass": feeder_shifted_correctly,
        "auditor_disambiguation_pass": auditor_shifted_correctly,
        "total_interactions_locked": len(run_a.locks) + len(run_b.locks),
        "case_A": asdict(grade_a),
        "case_B": asdict(grade_b),
    }
