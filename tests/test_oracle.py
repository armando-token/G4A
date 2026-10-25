"""Unit tests for Phase 4 4-layer V2 oracle, GuardedOracleTruthLoader, and all 22 negative tests."""

from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any, Dict, List, Tuple
import pytest
from pydantic import ValidationError

from blackbox.contracts import (
    ArtifactLockRecordV2,
    AuditChallengeV2,
    IndependentGraphV2,
    PlanV2,
    ProposalV2,
    build_reference_audit_challenge_v2,
    build_reference_independent_graph_v2,
    build_reference_plan_v2,
    build_reference_proposal_v2,
    create_lock_record_v2,
    validate_contract_hygiene,
    verify_lock_chain_v2,
)
from blackbox.fixture import generate_case_bundle, write_case_fixtures
from blackbox.managed import ManagedAgentTransport, reset_environment_role_registry
from blackbox.oracle import (
    DEFAULT_3D_N4_CONNECTOR_BBOX,
    GuardedOracleTruthLoader,
    compute_bbox_iou,
    evaluate_ab_campaign_v2,
    evaluate_case_v2,
    evaluate_connector_localization,
    grade_ab_campaign,
    grade_case_run,
    is_center_inside_bbox,
)
from blackbox.probe import ProbeReceipt, SingleShotProbeSession
from blackbox.protocol import (
    BlackboxProtocolStateMachine,
    DeterministicVisualV2MockClient,
    run_visual_v2_case_protocol,
)


@pytest.fixture(autouse=True)
def _clean_env_registry() -> None:
    reset_environment_role_registry()


def _build_locked_reference_bundle(
    case_id: str = "case_A",
    *,
    i1_proposal: ProposalV2 | None = None,
    i2_plan: PlanV2 | None = None,
    a1_graph: IndependentGraphV2 | None = None,
    a2_challenge: AuditChallengeV2 | None = None,
    probe_kind: str | None = None,
    probe_target: str | None = None,
    i_env_id: str = "env-inv-001",
    a_env_id: str = "env-aud-001",
) -> Tuple[
    ProposalV2,
    ProbeReceipt,
    PlanV2,
    IndependentGraphV2,
    AuditChallengeV2,
    List[ArtifactLockRecordV2],
    GuardedOracleTruthLoader,
]:
    """Build a complete hash-chained L1..L4 bundle for oracle testing."""
    norm_case = case_id.strip().upper()
    short_id = "A" if norm_case in ("A", "CASE_A") else "B"
    loader = GuardedOracleTruthLoader(short_id)

    prop = i1_proposal or build_reference_proposal_v2(case_id)
    l1 = create_lock_record_v2(
        "L1_I1_PROPOSAL",
        case_id,
        prop,
        environment_id=i_env_id,
        interaction_id="int-i1",
        previous_lock_hash=None,
    )
    loader.register_lock(l1)

    p_kind = probe_kind or prop.selected_measurement.type
    p_target = probe_target or prop.selected_measurement.target_asset
    receipt = loader.issue_scoped_probe(p_kind, p_target, l1.canonical_sha256)

    plan = i2_plan or build_reference_plan_v2(
        case_id,
        probe_receipt=receipt.to_dict(),
        proposal=prop,
        blocking_graph_uncertainty=bool(prop.unknown_critical_edges),
    )
    l2 = create_lock_record_v2(
        "L2_I2_PLAN",
        case_id,
        plan,
        environment_id=i_env_id,
        interaction_id="int-i2",
        previous_lock_hash=l1.chain_hash,
    )
    loader.register_lock(l2)

    graph = a1_graph or build_reference_independent_graph_v2(case_id)
    l3 = create_lock_record_v2(
        "L3_A1_GRAPH",
        case_id,
        graph,
        environment_id=a_env_id,
        interaction_id="int-a1",
        previous_lock_hash=l2.chain_hash,
    )
    loader.register_lock(l3)

    challenge = a2_challenge or build_reference_audit_challenge_v2(
        case_id,
        a1_graph=graph,
        i2_plan=plan,
    )
    l4 = create_lock_record_v2(
        "L4_A2_CHALLENGE",
        case_id,
        challenge,
        environment_id=a_env_id,
        interaction_id="int-a2",
        previous_lock_hash=l3.chain_hash,
    )
    loader.register_lock(l4)

    return prop, receipt, plan, graph, challenge, [l1, l2, l3, l4], loader


# ---------------------------------------------------------------------------
# Phase 4: Positive Oracle & Live Evidence Separation Tests
# ---------------------------------------------------------------------------


def test_oracle_v2_passing_case_a_and_case_b_and_live_evidence_flag() -> None:
    """Verify 4-layer grading separates offline_strict_pass from live_provider_evidence (strict_pass)."""
    prop_a, rec_a, plan_a, graph_a, chal_a, locks_a, loader_a = _build_locked_reference_bundle("case_A")
    report_a = evaluate_case_v2(
        i1_proposal=prop_a,
        probe_receipt=rec_a,
        i2_plan=plan_a,
        a1_graph=graph_a,
        a2_challenge=chal_a,
        locks=locks_a,
        truth_loader=loader_a,
        turn_evidences=None,
    )
    assert report_a.structural_valid is True
    assert report_a.scientific_correct is True
    assert report_a.operational_complete is True
    assert report_a.live_provider_evidence is False
    assert report_a.offline_strict_pass is True
    assert report_a.strict_pass is False
    assert report_a.physical_safety_certified is False
    assert loader_a.truth_access_count == 1
    assert loader_a.premature_access_attempts == 0

    # Now with live_provider_evidence populated across all 4 turns
    prop_b, rec_b, plan_b, graph_b, chal_b, locks_b, loader_b = _build_locked_reference_bundle("case_B")
    live_ev = {
        tid: {
            "response_id": f"resp-{tid}",
            "model": "gemini-4-argon",
            "request_sha256": "a" * 64,
            "response_sha256": "b" * 64,
            "latency_ms": 420.0,
            "usage": {"total_tokens": 1800},
        }
        for tid in ("I1", "I2", "A1", "A2")
    }
    report_b = evaluate_case_v2(
        i1_proposal=prop_b,
        probe_receipt=rec_b,
        i2_plan=plan_b,
        a1_graph=graph_b,
        a2_challenge=chal_b,
        locks=locks_b,
        truth_loader=loader_b,
        turn_evidences=live_ev,
    )
    assert report_b.offline_strict_pass is True
    assert report_b.live_provider_evidence is True
    assert report_b.strict_pass is True

    ab = evaluate_ab_campaign_v2(report_a, report_b, plan_a, plan_b)
    assert ab["offline_campaign_pass"] is True
    assert ab["action_disambiguation_pass"] is True


# ---------------------------------------------------------------------------
# Phase 4: All 22 Negative Tests
# ---------------------------------------------------------------------------


def test_negative_1_extra_unknown_field_rejected() -> None:
    """1. Extra unknown field in any contract (extra='forbid') -> ValidationError."""
    prop_d = build_reference_proposal_v2("case_A").model_dump()
    prop_d["unexpected_field"] = 123
    with pytest.raises(ValidationError):
        ProposalV2.model_validate(prop_d)

    plan_d = build_reference_plan_v2("case_A").model_dump()
    plan_d["extra_plan_key"] = "forbidden"
    with pytest.raises(ValidationError):
        PlanV2.model_validate(plan_d)

    graph_d = build_reference_independent_graph_v2("case_A").model_dump()
    graph_d["extra_graph_key"] = True
    with pytest.raises(ValidationError):
        IndependentGraphV2.model_validate(graph_d)

    chal_d = build_reference_audit_challenge_v2("case_A").model_dump()
    chal_d["extra_challenge_key"] = "bad"
    with pytest.raises(ValidationError):
        AuditChallengeV2.model_validate(chal_d)


def test_negative_2_nan_or_inf_in_trajectory_or_bbox_rejected() -> None:
    """2. NaN or Inf in trajectory or bbox -> ValidationError."""
    prop_d = build_reference_proposal_v2("case_A").model_dump()
    prop_d["hotspot"]["bbox_xyxy_px"] = [1079, 325, float("nan"), 393]
    with pytest.raises(ValidationError):
        ProposalV2.model_validate(prop_d)

    plan_d = build_reference_plan_v2("case_A").model_dump()
    plan_d["predictions_by_action"]["NO_ACTION"]["temps_c"]["N4"][5] = float("inf")
    with pytest.raises(ValidationError):
        PlanV2.model_validate(plan_d)


@pytest.mark.parametrize(
    "bad_bbox",
    [
        [-5, 0, 100, 100],
        [100, 100, 50, 50],
        [0, 0, 1400, 800],
    ],
)
def test_negative_3_out_of_bounds_bbox_rejected(bad_bbox: List[int]) -> None:
    """3. Out-of-bounds bbox ([-5, 0, 100, 100], [100, 100, 50, 50], [0, 0, 1400, 800]) -> ValidationError."""
    prop_d = build_reference_proposal_v2("case_A").model_dump()
    prop_d["hotspot"]["bbox_xyxy_px"] = bad_bbox
    with pytest.raises(ValidationError):
        ProposalV2.model_validate(prop_d)


def test_negative_4_rack_wide_bbox_rejected_by_connector_localization() -> None:
    """4. Rack-wide bbox ([900, 100, 1270, 750]) -> rejected by connector localization check."""
    loc = evaluate_connector_localization([900, 100, 1270, 750])
    assert loc["is_rack_wide"] is True
    assert loc["localized_pass"] is False

    bad_prop = build_reference_proposal_v2("case_A", bbox_xyxy_px=[900, 100, 1270, 750])
    prop, rec, plan, graph, chal, locks, loader = _build_locked_reference_bundle(
        "case_A", i1_proposal=bad_prop
    )
    report = evaluate_case_v2(
        i1_proposal=prop,
        probe_receipt=rec,
        i2_plan=plan,
        a1_graph=graph,
        a2_challenge=chal,
        locks=locks,
        truth_loader=loader,
    )
    assert report.structural_valid is True
    assert report.scientific_correct is False
    assert report.scientific_checks["i1_bbox_localized_pass"] is False


@pytest.mark.parametrize("wrong_rack", ["N1", "N2", "N3"])
def test_negative_5_wrong_hotspot_asset_scientific_fail(wrong_rack: str) -> None:
    """5. Wrong hotspot asset (N1, N2, or N3) -> scientific fail."""
    prop_d = build_reference_proposal_v2("case_A").model_dump()
    prop_d["hotspot"]["asset_id"] = wrong_rack
    prop_d["selected_measurement"]["target_asset"] = wrong_rack
    bad_prop = ProposalV2.model_validate(prop_d)

    prop, rec, plan, graph, chal, locks, loader = _build_locked_reference_bundle(
        "case_A", i1_proposal=bad_prop
    )
    report = evaluate_case_v2(
        i1_proposal=prop,
        probe_receipt=rec,
        i2_plan=plan,
        a1_graph=graph,
        a2_challenge=chal,
        locks=locks,
        truth_loader=loader,
    )
    assert report.scientific_correct is False
    assert report.scientific_checks["i1_hot_rack_correct"] is False


def test_negative_6_wrong_sld_edge_scientific_fail() -> None:
    """6. Wrong SLD edge (Case A mapping submitted for Case B) -> scientific fail."""
    wrong_prop = build_reference_proposal_v2(
        "case_B",
        sld_power_map={"N1": "F1", "N2": "F1", "N3": "F2", "N4": "F2"},  # Case A topology
    )
    prop, rec, plan, graph, chal, locks, loader = _build_locked_reference_bundle(
        "case_B", i1_proposal=wrong_prop
    )
    report = evaluate_case_v2(
        i1_proposal=prop,
        probe_receipt=rec,
        i2_plan=plan,
        a1_graph=graph,
        a2_challenge=chal,
        locks=locks,
        truth_loader=loader,
    )
    assert report.scientific_correct is False
    assert report.scientific_checks["i1_power_edges_exact"] is False


def test_negative_7_wrong_sensor_to_rack_edge_scientific_fail() -> None:
    """7. Wrong sensor-to-rack edge (SNS_K2 -> N2) -> scientific fail."""
    prop_d = build_reference_proposal_v2("case_A").model_dump()
    # Swap SNS_K2 and SNS_K3 bindings so bijection is preserved structurally but scientifically wrong
    for se in prop_d["sensor_edges"]:
        if se["sensor_id"] == "SNS_K2":
            se["rack_id"] = "N2"
        elif se["sensor_id"] == "SNS_K3":
            se["rack_id"] = "N4"
    bad_prop = ProposalV2.model_validate(prop_d)

    prop, rec, plan, graph, chal, locks, loader = _build_locked_reference_bundle(
        "case_A", i1_proposal=bad_prop
    )
    report = evaluate_case_v2(
        i1_proposal=prop,
        probe_receipt=rec,
        i2_plan=plan,
        a1_graph=graph,
        a2_challenge=chal,
        locks=locks,
        truth_loader=loader,
    )
    assert report.scientific_correct is False
    assert report.scientific_checks["i1_sensor_edges_exact"] is False


def test_negative_8_missing_edge_or_unknown_critical_edges_and_i2_abstention() -> None:
    """8. Missing edge rejected structurally; non-empty unknown_critical_edges requires I2 abstention."""
    # Missing edge in I1 -> ValidationError
    prop_d = build_reference_proposal_v2("case_A").model_dump()
    prop_d["edges"] = prop_d["edges"][:3]
    with pytest.raises(ValidationError):
        ProposalV2.model_validate(prop_d)

    # Non-empty unknown_critical_edges -> I2 abstains (NO_ACTION), scientific_correct=False
    unc_prop = build_reference_proposal_v2("case_A", unknown_critical_edges=["F2->N3_occluded"])
    prop, rec, plan, graph, chal, locks, loader = _build_locked_reference_bundle(
        "case_A", i1_proposal=unc_prop
    )
    assert plan.decision == "abstain"
    assert plan.selected_action_id == "NO_ACTION"
    report = evaluate_case_v2(
        i1_proposal=prop,
        probe_receipt=rec,
        i2_plan=plan,
        a1_graph=graph,
        a2_challenge=chal,
        locks=locks,
        truth_loader=loader,
    )
    assert report.scientific_correct is False
    assert report.scientific_checks["i1_unknown_critical_edges_empty"] is False

    # If I2 attempts decision="intervene" when blocking_graph_uncertainty=True -> ValidationError
    bad_plan_d = plan.model_dump()
    bad_plan_d["decision"] = "intervene"
    bad_plan_d["selected_action_id"] = "MIGRATE_N4_TO_N3_8A"
    with pytest.raises(ValidationError, match="blocking_graph_uncertainty"):
        PlanV2.model_validate(bad_plan_d)


def test_negative_9_second_probe_or_probe_before_l1_lock() -> None:
    """9. Second probe request or probe before L1 lock -> PermissionError / RuntimeError."""
    loader = GuardedOracleTruthLoader("A")
    with pytest.raises(PermissionError, match="before L1_I1_PROPOSAL is locked"):
        loader.issue_scoped_probe("voltage_drop", "N4", "hash")

    prop = build_reference_proposal_v2("case_A")
    l1 = create_lock_record_v2("L1_I1_PROPOSAL", "case_A", prop, previous_lock_hash=None)
    loader.register_lock(l1)
    loader.issue_scoped_probe("voltage_drop", "N4", l1.canonical_sha256)

    with pytest.raises(RuntimeError, match="Probe budget exhausted"):
        loader.issue_scoped_probe("voltage_drop", "N4", l1.canonical_sha256)


def test_negative_10_i1_requests_temperature_snapshot_in_guided_mode() -> None:
    """10. I1 requests temperature_snapshot in guided_diagnostic mode -> scientific fail."""
    bad_prop = build_reference_proposal_v2("case_A", measurement_type="temperature_snapshot")
    prop, rec, plan, graph, chal, locks, loader = _build_locked_reference_bundle(
        "case_A", i1_proposal=bad_prop
    )
    report = evaluate_case_v2(
        i1_proposal=prop,
        probe_receipt=rec,
        i2_plan=plan,
        a1_graph=graph,
        a2_challenge=chal,
        locks=locks,
        truth_loader=loader,
    )
    assert report.structural_valid is True
    assert report.scientific_correct is False
    assert report.scientific_checks["i1_selected_measurement_correct"] is False
    assert report.scientific_checks["probe_targeted_n4_voltage_drop"] is False


def test_negative_11_i2_fits_wrong_cooling_hypothesis_parameters() -> None:
    """11. I2 fits wrong (R0, h, C) (cooling hypothesis 0.020, 0.2, 4.0) -> scientific fail."""
    plan_d = build_reference_plan_v2("case_A").model_dump()
    plan_d["post_measurement_parameters"] = {
        "target_rack": "N4",
        "r0_ohm": 0.020,
        "h_w_per_k": 0.2,
        "c_j_per_k": 4.0,
    }
    plan_d["inferred_network"]["racks"]["N4"]["r0_ohm"] = 0.020
    plan_d["inferred_network"]["racks"]["N4"]["h_w_per_k"] = 0.2
    plan_d["inferred_network"]["racks"]["N4"]["c_j_per_k"] = 4.0
    bad_plan = PlanV2.model_validate(plan_d)

    prop, rec, plan, graph, chal, locks, loader = _build_locked_reference_bundle(
        "case_A", i2_plan=bad_plan
    )
    report = evaluate_case_v2(
        i1_proposal=prop,
        probe_receipt=rec,
        i2_plan=plan,
        a1_graph=graph,
        a2_challenge=chal,
        locks=locks,
        truth_loader=loader,
    )
    assert report.scientific_correct is False
    assert report.scientific_checks["i2_r0_accurate"] is False
    assert report.scientific_checks["i2_h_accurate"] is False
    assert report.scientific_checks["i2_c_accurate"] is False


def test_negative_12_i2_fabricates_trajectory_scientific_fail() -> None:
    """12. I2 fabricates trajectory (does not match simulate_world on inferred_network) -> scientific fail."""
    plan_d = build_reference_plan_v2("case_A").model_dump()
    plan_d["predictions_by_action"]["MIGRATE_N4_TO_N3_8A"]["temps_c"]["N4"] = [
        x - 4.0 for x in plan_d["predictions_by_action"]["MIGRATE_N4_TO_N3_8A"]["temps_c"]["N4"]
    ]
    bad_plan = PlanV2.model_validate(plan_d)

    prop, rec, plan, graph, chal, locks, loader = _build_locked_reference_bundle(
        "case_A", i2_plan=bad_plan
    )
    report = evaluate_case_v2(
        i1_proposal=prop,
        probe_receipt=rec,
        i2_plan=plan,
        a1_graph=graph,
        a2_challenge=chal,
        locks=locks,
        truth_loader=loader,
    )
    assert report.scientific_correct is False
    assert report.scientific_checks["i2_self_consistent_pass"] is False
    assert report.scientific_checks["i2_trajectories_accurate"] is False


def test_negative_13_i2_violates_68a_current_conservation() -> None:
    """13. I2 violates 68.0 A total current conservation -> ValidationError."""
    plan_d = build_reference_plan_v2("case_A").model_dump()
    plan_d["predictions_by_action"]["MIGRATE_N4_TO_N3_8A"]["rack_currents_a"]["N3"] = [14.0] * 19
    with pytest.raises(ValidationError, match="68.0 A conservation"):
        PlanV2.model_validate(plan_d)


def test_negative_14_i2_selects_unsafe_action_scientific_fail() -> None:
    """14. I2 selects unsafe action (MIGRATE_N4_TO_N2_8A in Case A or NO_ACTION) -> scientific fail."""
    plan_d = build_reference_plan_v2("case_A").model_dump()
    plan_d["selected_action_id"] = "MIGRATE_N4_TO_N2_8A"
    bad_plan = PlanV2.model_validate(plan_d)

    prop, rec, plan, graph, chal, locks, loader = _build_locked_reference_bundle(
        "case_A", i2_plan=bad_plan
    )
    report = evaluate_case_v2(
        i1_proposal=prop,
        probe_receipt=rec,
        i2_plan=plan,
        a1_graph=graph,
        a2_challenge=chal,
        locks=locks,
        truth_loader=loader,
    )
    assert report.scientific_correct is False
    assert report.scientific_checks["i2_selected_action_correct"] is False


def test_negative_15_tampered_lock_payload_fails_verification() -> None:
    """15. Tampered L1/L2/L3/L4 payload after lock -> hash-chain verification fails."""
    prop, rec, plan, graph, chal, locks, loader = _build_locked_reference_bundle("case_A")
    # Mutate plan after L2 was locked
    mutated_plan_d = plan.model_dump()
    mutated_plan_d["reason_es"] = "Texto modificado después del bloqueo criptográfico L2."
    mutated_plan = PlanV2.model_validate(mutated_plan_d)

    report = evaluate_case_v2(
        i1_proposal=prop,
        probe_receipt=rec,
        i2_plan=mutated_plan,
        a1_graph=graph,
        a2_challenge=chal,
        locks=locks,
        truth_loader=loader,
    )
    assert report.structural_valid is False
    assert report.structural_checks["artifact_hash_matches"] is False
    assert report.structural_checks["lock_chain_valid"] is False


def test_negative_16_out_of_order_or_incomplete_lock_chain_fails() -> None:
    """16. Out-of-order or incomplete lock chain (L3 missing) -> fails."""
    prop, rec, plan, graph, chal, locks, _ = _build_locked_reference_bundle("case_A")
    incomplete_locks = [locks[0], locks[1], locks[3]]
    check = verify_lock_chain_v2(incomplete_locks)
    assert check["valid"] is False

    fresh_loader = GuardedOracleTruthLoader("A")
    with pytest.raises(PermissionError):
        evaluate_case_v2(
            i1_proposal=prop,
            probe_receipt=rec,
            i2_plan=plan,
            a1_graph=graph,
            a2_challenge=chal,
            locks=incomplete_locks,
            truth_loader=fresh_loader,
        )


def test_negative_17_a1_receives_i1_field_or_reuses_i1_environment_id() -> None:
    """17. A1 receives I1/I2 field or reuses I1's environment_id -> fails."""
    # A1 with Investigator field -> ValidationError
    graph_d = build_reference_independent_graph_v2("case_A").model_dump()
    graph_d["hypotheses"] = []
    with pytest.raises(ValidationError, match="forbidden Investigator fields"):
        IndependentGraphV2.model_validate(graph_d)

    # A1 reusing I1 environment_id -> operational_complete=False
    prop, rec, plan, graph, chal, locks, loader = _build_locked_reference_bundle(
        "case_A", i_env_id="env-shared-999", a_env_id="env-shared-999"
    )
    report = evaluate_case_v2(
        i1_proposal=prop,
        probe_receipt=rec,
        i2_plan=plan,
        a1_graph=graph,
        a2_challenge=chal,
        locks=locks,
        truth_loader=loader,
    )
    assert report.operational_complete is False
    assert report.operational_checks["investigator_auditor_env_isolated"] is False


def test_negative_18_a1_input_png_sha256_mismatch_fails() -> None:
    """18. A1 input_png_sha256 does not match A1 input manifest or is malformed -> fails."""
    graph_d = build_reference_independent_graph_v2("case_A").model_dump()
    graph_d["input_png_sha256"]["thermal"] = "not-a-sha256"
    with pytest.raises(ValidationError, match="Invalid SHA-256"):
        IndependentGraphV2.model_validate(graph_d)


def test_negative_19_a2_rubber_stamps_corrupted_i2_plan_scientific_fail() -> None:
    """19. A2 rubber-stamps (verdict='accept') a subtly corrupted I2 plan -> scientific fail."""
    # Build an I2 plan that selects the unsafe F1-overloading action (48A > 44A in Case A)
    plan_d = build_reference_plan_v2("case_A").model_dump()
    plan_d["selected_action_id"] = "MIGRATE_N4_TO_N2_8A"
    corrupted_plan = PlanV2.model_validate(plan_d)

    graph = build_reference_independent_graph_v2("case_A")
    chal_d = build_reference_audit_challenge_v2(
        "case_A", a1_graph=graph, i2_plan=corrupted_plan
    ).model_dump()
    # Force A2 to rubber-stamp with verdict="accept"
    chal_d["verdict"] = "accept"
    chal_d["reason_codes"] = ["RUBBER_STAMPED"]
    rubber_stamped_chal = AuditChallengeV2.model_validate(chal_d)

    prop, rec, plan, graph_obj, chal_obj, locks, loader = _build_locked_reference_bundle(
        "case_A",
        i2_plan=corrupted_plan,
        a1_graph=graph,
        a2_challenge=rubber_stamped_chal,
    )
    report = evaluate_case_v2(
        i1_proposal=prop,
        probe_receipt=rec,
        i2_plan=plan,
        a1_graph=graph_obj,
        a2_challenge=chal_obj,
        locks=locks,
        truth_loader=loader,
    )
    assert report.scientific_correct is False
    assert report.scientific_checks["a2_verdict_coherent"] is False


def test_negative_20_a2_sensitivity_canary_stale_or_corrupted() -> None:
    """20. A2 sensitivity canary is stale/unchanged (perturbed == baseline) -> ValidationError / scientific fail."""
    chal_d = build_reference_audit_challenge_v2("case_A").model_dump()
    base_val = chal_d["input_sensitivity_canary"]["baseline_n4_t180_c"]
    chal_d["input_sensitivity_canary"]["perturbed_n4_t180_c"] = base_val
    chal_d["input_sensitivity_canary"]["max_abs_diff_c"] = 0.0
    with pytest.raises(ValidationError, match="Sensitivity canary trajectory is identical to baseline"):
        AuditChallengeV2.model_validate(chal_d)

    # Also test a canary trajectory that is non-zero but physically wrong -> scientific fail
    chal_d2 = build_reference_audit_challenge_v2("case_A").model_dump()
    chal_d2["input_sensitivity_canary"]["trajectory_temps_c"]["N4"] = [
        x + 3.5 for x in chal_d2["input_sensitivity_canary"]["trajectory_temps_c"]["N4"]
    ]
    bad_canary_chal = AuditChallengeV2.model_validate(chal_d2)
    prop, rec, plan, graph, chal, locks, loader = _build_locked_reference_bundle(
        "case_A", a2_challenge=bad_canary_chal
    )
    report = evaluate_case_v2(
        i1_proposal=prop,
        probe_receipt=rec,
        i2_plan=plan,
        a1_graph=graph,
        a2_challenge=chal,
        locks=locks,
        truth_loader=loader,
    )
    assert report.scientific_correct is False
    assert report.scientific_checks["a2_canary_valid"] is False


def test_negative_21_physical_safety_certified_true_rejected() -> None:
    """21. physical_safety_certified=True -> rejected."""
    plan_d = build_reference_plan_v2("case_A").model_dump()
    plan_d["physical_safety_certified"] = True
    with pytest.raises(ValidationError, match="physical_safety_certified must be False"):
        PlanV2.model_validate(plan_d)


def test_negative_22_private_truth_key_leaked_into_contract_rejected() -> None:
    """22. Private truth key leaked into any contract -> rejected."""
    payload = build_reference_proposal_v2("case_A").model_dump()
    payload["safe_receiver"] = "N3"
    with pytest.raises(ValueError, match="Forbidden private key"):
        validate_contract_hygiene(payload)


# ---------------------------------------------------------------------------
# Phase 00 Backward-Compatible Oracle Tests
# ---------------------------------------------------------------------------


def test_oracle_grades_case_a_and_case_b_and_ab_campaign(tmp_path: Path) -> None:
    fixtures_dir = tmp_path / "fixtures"
    write_case_fixtures(fixtures_dir)
    pub_a, truth_a = generate_case_bundle("case_A", artifacts_root=fixtures_dir)
    pub_b, truth_b = generate_case_bundle("case_B", artifacts_root=fixtures_dir)

    for pub in (pub_a, pub_b):
        Path(pub.sld_image_path).parent.mkdir(parents=True, exist_ok=True)
        Path(pub.sld_image_path).write_bytes(b"PNG_PLACEHOLDER")
        Path(pub.scene_3d_image_path).parent.mkdir(parents=True, exist_ok=True)
        Path(pub.scene_3d_image_path).write_bytes(b"PNG_PLACEHOLDER")

    transport = ManagedAgentTransport()
    sm = BlackboxProtocolStateMachine(transport=transport, sandbox_dir=tmp_path / "sandbox")

    run_a = sm.run_case_protocol(pub_a, SingleShotProbeSession(truth_a))
    run_b = sm.run_case_protocol(pub_b, SingleShotProbeSession(truth_b))

    grade_a = grade_case_run(run_a, truth_a)
    grade_b = grade_case_run(run_b, truth_b)

    assert grade_a.overall_case_pass is True
    assert grade_b.overall_case_pass is True
    assert grade_a.i2_feeder_correct is True
    assert grade_b.i2_feeder_correct is True
    assert run_a.i2.diagnosed_fault_feeder == "F2"
    assert run_b.i2.diagnosed_fault_feeder == "F3"

    campaign = grade_ab_campaign(grade_a, grade_b, run_a, run_b)
    assert campaign["campaign_pass"] is True
    assert campaign["feeder_disambiguation_pass"] is True
    assert campaign["auditor_disambiguation_pass"] is True
    assert campaign["total_interactions_locked"] == 8


def test_oracle_rejects_wrong_feeder_diagnosis(tmp_path: Path) -> None:
    fixtures_dir = tmp_path / "fixtures"
    write_case_fixtures(fixtures_dir)
    pub_a, truth_a = generate_case_bundle("case_A", artifacts_root=fixtures_dir)
    Path(pub_a.sld_image_path).parent.mkdir(parents=True, exist_ok=True)
    Path(pub_a.sld_image_path).write_bytes(b"PNG_PLACEHOLDER")
    Path(pub_a.scene_3d_image_path).parent.mkdir(parents=True, exist_ok=True)
    Path(pub_a.scene_3d_image_path).write_bytes(b"PNG_PLACEHOLDER")

    transport = ManagedAgentTransport()
    sm = BlackboxProtocolStateMachine(transport=transport, sandbox_dir=tmp_path / "sandbox")
    run_a = sm.run_case_protocol(pub_a, SingleShotProbeSession(truth_a))

    tampered = copy.deepcopy(run_a)
    tampered.i2.diagnosed_fault_feeder = "F3"
    grade = grade_case_run(tampered, truth_a)
    assert grade.i2_feeder_correct is False
    assert grade.overall_case_pass is False
