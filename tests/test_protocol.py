"""Unit tests for Phase 4 visual-v2 4-turn protocol state machine and Phase 00 compatibility."""

from __future__ import annotations

import copy
from pathlib import Path
import pytest
from pydantic import ValidationError

from blackbox.contracts import (
    AuditChallengeV2,
    IndependentGraphV2,
    LOCK_STAGE_ORDER,
    PlanV2,
    ProposalV2,
    build_reference_independent_graph_v2,
    build_reference_plan_v2,
    build_reference_proposal_v2,
    create_lock_record_v2,
    verify_lock_chain_v2,
)
from blackbox.fixture import generate_case_bundle, write_case_fixtures
from blackbox.managed import (
    ManagedAgentTransport,
    PairBudgetTracker,
    reset_environment_role_registry,
    validate_sandbox_handoff_files,
)
from blackbox.oracle import GuardedOracleTruthLoader, evaluate_ab_campaign_v2
from blackbox.probe import SingleShotProbeSession
from blackbox.protocol import (
    BlackboxProtocolStateMachine,
    DeterministicVisualV2MockClient,
    VisualV2ProtocolOrchestrator,
    run_visual_v2_case_protocol,
)


@pytest.fixture(autouse=True)
def _clean_env_registry() -> None:
    reset_environment_role_registry()


# ---------------------------------------------------------------------------
# Phase 4: Positive Dry-Run Tests (Case A, Case B, Paired Campaign, Modes)
# ---------------------------------------------------------------------------


def test_visual_v2_dry_run_case_a_and_case_b_pass(tmp_path: Path) -> None:
    """Verify passing 4-turn dry-runs for Case A (N4->N3) and Case B (N4->N2) with L1..L4 locks."""
    locks_dir = tmp_path / "locks"
    journal_dir = tmp_path / "journals"

    res_a = run_visual_v2_case_protocol(
        "case_A",
        diagnostic_mode="guided_diagnostic",
        locks_dir=locks_dir,
        journal_dir=journal_dir,
    )
    res_b = run_visual_v2_case_protocol(
        "case_B",
        diagnostic_mode="guided_diagnostic",
        locks_dir=locks_dir,
        journal_dir=journal_dir,
    )

    assert res_a.i2_plan.selected_action_id == "MIGRATE_N4_TO_N3_8A"
    assert res_b.i2_plan.selected_action_id == "MIGRATE_N4_TO_N2_8A"
    assert res_a.i2_plan.decision == "intervene"
    assert res_b.i2_plan.decision == "intervene"
    assert res_a.a2_challenge.verdict == "accept"
    assert res_b.a2_challenge.verdict == "accept"

    for res in (res_a, res_b):
        assert res.zero_leakage_verified is True
        assert list(res.locks.keys()) == list(LOCK_STAGE_ORDER)
        chain_check = verify_lock_chain_v2(res.lock_list)
        assert chain_check["valid"] is True
        assert res.evaluation_report.structural_valid is True
        assert res.evaluation_report.scientific_correct is True
        assert res.evaluation_report.operational_complete is True
        assert res.evaluation_report.live_provider_evidence is False
        assert res.evaluation_report.offline_strict_pass is True
        assert res.evaluation_report.strict_pass is False
        assert res.evaluation_report.physical_safety_certified is False
        assert res.turn_evidences["I1"].environment_id == res.turn_evidences["I2"].environment_id
        assert res.turn_evidences["A1"].environment_id == res.turn_evidences["A2"].environment_id
        assert res.turn_evidences["I1"].environment_id != res.turn_evidences["A1"].environment_id

        # Check persisted lock files exist on disk
        for stage in LOCK_STAGE_ORDER:
            lock_path = locks_dir / "guided_diagnostic" / res.case_id / f"{stage}.json"
            assert lock_path.is_file()

    ab_summary = evaluate_ab_campaign_v2(
        res_a.evaluation_report,
        res_b.evaluation_report,
        res_a.i2_plan,
        res_b.i2_plan,
    )
    assert ab_summary["offline_campaign_pass"] is True
    assert ab_summary["action_disambiguation_pass"] is True


def test_guarded_oracle_truth_loader_blocks_access_before_l4() -> None:
    """Verify calling open_private_truth() before L4_A2_CHALLENGE raises PermissionError."""
    loader = GuardedOracleTruthLoader("A")

    # Before any locks
    with pytest.raises(
        PermissionError,
        match=r"Evaluation truth accessed before all 4 locks \(L1\.\.L4\) are committed",
    ):
        loader.open_private_truth()

    # Probe before L1 lock is also blocked
    with pytest.raises(
        PermissionError,
        match="Cannot issue diagnostic probe before L1_I1_PROPOSAL is locked",
    ):
        loader.issue_scoped_probe("voltage_drop", "N4", "dummy_hash")

    # Instrument hooks at I1, I2, A1, and A2 (before L4 is committed)
    hook_stages_triggered: list[str] = []

    def _make_premature_hook(stage_label: str):
        def _hook(tl: GuardedOracleTruthLoader) -> None:
            hook_stages_triggered.append(stage_label)
            with pytest.raises(
                PermissionError,
                match=r"Evaluation truth accessed before all 4 locks \(L1\.\.L4\) are committed",
            ):
                tl.open_private_truth()

        return _hook

    fresh_loader = GuardedOracleTruthLoader("A")
    # Verify at each pre-L4 step that open_private_truth raises PermissionError
    prop = build_reference_proposal_v2("case_A")
    l1 = create_lock_record_v2("L1_I1_PROPOSAL", "case_A", prop, previous_lock_hash=None)
    fresh_loader.register_lock(l1)
    _make_premature_hook("after_L1")(fresh_loader)

    receipt = fresh_loader.issue_scoped_probe("voltage_drop", "N4", l1.canonical_sha256)
    plan = build_reference_plan_v2("case_A", probe_receipt=receipt.to_dict(), proposal=prop)
    l2 = create_lock_record_v2("L2_I2_PLAN", "case_A", plan, previous_lock_hash=l1.chain_hash)
    fresh_loader.register_lock(l2)
    _make_premature_hook("after_L2")(fresh_loader)

    graph = build_reference_independent_graph_v2("case_A")
    l3 = create_lock_record_v2("L3_A1_GRAPH", "case_A", graph, previous_lock_hash=l2.chain_hash)
    fresh_loader.register_lock(l3)
    _make_premature_hook("after_L3")(fresh_loader)

    assert hook_stages_triggered == ["after_L1", "after_L2", "after_L3"]
    assert fresh_loader.truth_access_count == 0
    assert fresh_loader.premature_access_attempts == 3


def test_guided_and_blind_diagnostic_modes_strictly_separated() -> None:
    """Verify guided_diagnostic and blind_diagnostic modes keep separate versions, budgets, and outcomes."""
    orchestrator = VisualV2ProtocolOrchestrator()

    res_guided = orchestrator.run_case("case_A", diagnostic_mode="guided_diagnostic")
    reset_environment_role_registry()

    def _mutate_i1_blind(d: dict) -> dict:
        d["selected_measurement"]["type"] = "controller_setting"
        return d

    blind_client = DeterministicVisualV2MockClient(artifact_mutators={"I1": _mutate_i1_blind})
    res_blind = orchestrator.run_case(
        "case_A",
        diagnostic_mode="blind_diagnostic",
        client=blind_client,
    )

    assert res_guided.protocol_version == "visual-v2/guided_diagnostic"
    assert res_blind.protocol_version == "visual-v2/blind_diagnostic"
    assert len(orchestrator.outcomes_by_mode["guided_diagnostic"]) == 1
    assert len(orchestrator.outcomes_by_mode["blind_diagnostic"]) == 1
    assert res_blind.probe_receipt.kind == "controller_setting"

    # Attempting to reuse a guided_diagnostic PairBudgetTracker in blind_diagnostic mode is blocked
    shared_tracker = PairBudgetTracker()
    reset_environment_role_registry()
    orchestrator.run_case("case_A", diagnostic_mode="guided_diagnostic", budget_tracker=shared_tracker)
    with pytest.raises(PermissionError, match="Cannot mix 'blind_diagnostic' run into PairBudgetTracker"):
        orchestrator.run_case("case_B", diagnostic_mode="blind_diagnostic", budget_tracker=shared_tracker)


# ---------------------------------------------------------------------------
# Phase 4: Protocol Negative & Tamper-Detection Tests
# ---------------------------------------------------------------------------


def test_negative_i1_unknown_critical_edges_requires_i2_abstention() -> None:
    """Negative test 8: When I1 has non-empty unknown_critical_edges, I2 must abstain (NO_ACTION)."""
    def _mutate_i1_unknown(d: dict) -> dict:
        d["unknown_critical_edges"] = ["F1->N2_arch_occluded"]
        return d

    # 1. Compliant I2 abstains with NO_ACTION
    client_abstain = DeterministicVisualV2MockClient(artifact_mutators={"I1": _mutate_i1_unknown})
    res = run_visual_v2_case_protocol("case_A", client=client_abstain)
    assert res.i1_proposal.unknown_critical_edges == ["F1->N2_arch_occluded"]
    assert res.i2_plan.blocking_graph_uncertainty is True
    assert res.i2_plan.decision == "abstain"
    assert res.i2_plan.selected_action_id == "NO_ACTION"
    assert res.evaluation_report.scientific_correct is False

    # 2. Non-compliant I2 trying to intervene despite unknown_critical_edges is rejected
    reset_environment_role_registry()

    def _mutate_i2_force_intervene(d: dict) -> dict:
        d["blocking_graph_uncertainty"] = False
        d["decision"] = "intervene"
        d["selected_action_id"] = "MIGRATE_N4_TO_N3_8A"
        return d

    client_bad = DeterministicVisualV2MockClient(
        artifact_mutators={
            "I1": _mutate_i1_unknown,
            "I2": _mutate_i2_force_intervene,
        }
    )
    with pytest.raises(ValueError, match="I2 must abstain"):
        run_visual_v2_case_protocol("case_A", client=client_bad)


def test_negative_second_probe_or_post_l2_probe_rejected() -> None:
    """Negative test 9: Second probe request or probe after L2 lock is rejected."""
    loader = GuardedOracleTruthLoader("A")
    prop = build_reference_proposal_v2("case_A")
    l1 = create_lock_record_v2("L1_I1_PROPOSAL", "case_A", prop, previous_lock_hash=None)
    loader.register_lock(l1)

    r1 = loader.issue_scoped_probe("voltage_drop", "N4", l1.canonical_sha256)
    assert r1.kind == "voltage_drop"

    with pytest.raises(RuntimeError, match="Probe budget exhausted"):
        loader.issue_scoped_probe("voltage_drop", "N4", l1.canonical_sha256)

    plan = build_reference_plan_v2("case_A", probe_receipt=r1.to_dict(), proposal=prop)
    l2 = create_lock_record_v2("L2_I2_PLAN", "case_A", plan, previous_lock_hash=l1.chain_hash)
    loader.register_lock(l2)

    with pytest.raises(PermissionError, match="Cannot issue diagnostic probe after L2_I2_PLAN"):
        loader.issue_scoped_probe("voltage_drop", "N4", l1.canonical_sha256)


def test_negative_tampered_lock_payload_and_out_of_order_locks() -> None:
    """Negative tests 15 & 16: Tampered lock payload and out-of-order lock registration fail."""
    res = run_visual_v2_case_protocol("case_A")
    locks = copy.deepcopy(res.lock_list)

    # Tamper L2 canonical_sha256 after lock creation
    tampered_l2 = locks[1].model_copy(update={"canonical_sha256": "0" * 64})
    bad_chain = [locks[0], tampered_l2, locks[2], locks[3]]
    ver = verify_lock_chain_v2(bad_chain)
    assert ver["valid"] is False
    assert "Tampered chain_hash" in str(ver["reason"])

    # Out-of-order lock registration on GuardedOracleTruthLoader (L3 before L2)
    loader = GuardedOracleTruthLoader("A")
    loader.register_lock(locks[0])
    with pytest.raises(PermissionError, match="Out-of-order lock registration"):
        loader.register_lock(locks[2])


def test_negative_a1_reuses_i1_environment_id_or_receives_investigator_fields() -> None:
    """Negative test 17: A1 reusing I1 environment_id or containing Investigator fields is rejected."""
    # 1. A1 attempting to reuse Investigator environment_id
    client_same_env = DeterministicVisualV2MockClient(
        custom_environment_id_fn=lambda role_prefix, idx: "env-shared-001"
    )
    with pytest.raises(PermissionError, match="Cross-role environment reuse forbidden|isolation violation"):
        run_visual_v2_case_protocol("case_A", client=client_same_env)

    # 2. A1 output containing forbidden Investigator field (`selected_measurement`)
    reset_environment_role_registry()

    def _mutate_a1_leak(d: dict) -> dict:
        d["selected_measurement"] = {"type": "voltage_drop", "target_asset": "N4", "rationale": "leaked"}
        return d

    client_leaked_a1 = DeterministicVisualV2MockClient(artifact_mutators={"A1": _mutate_a1_leak})
    with pytest.raises(ValidationError, match="forbidden Investigator fields"):
        run_visual_v2_case_protocol("case_A", client=client_leaked_a1)


def test_negative_a1_input_png_sha256_mismatch_rejected() -> None:
    """Negative test 18: A1 input_png_sha256 mismatch against A1 input manifest is rejected."""
    def _mutate_a1_png_hash(d: dict) -> dict:
        d["input_png_sha256"]["sld"] = "f" * 64
        return d

    client = DeterministicVisualV2MockClient(artifact_mutators={"A1": _mutate_a1_png_hash})
    with pytest.raises(ValueError, match="input_png_sha256\\['sld'\\] mismatch"):
        run_visual_v2_case_protocol("case_A", client=client)


def test_negative_i2_fabricated_trajectory_rejected_by_protocol() -> None:
    """Negative test 12: I2 fabricating trajectory inconsistent with its inferred network is rejected."""
    def _mutate_i2_traj(d: dict) -> dict:
        d["predictions_by_action"]["MIGRATE_N4_TO_N3_8A"]["temps_c"]["N4"][-1] += 5.0
        return d

    client = DeterministicVisualV2MockClient(artifact_mutators={"I2": _mutate_i2_traj})
    with pytest.raises(ValueError, match="predictions do not match its own inferred network"):
        run_visual_v2_case_protocol("case_A", client=client)


# ---------------------------------------------------------------------------
# Phase 00 Backward-Compatible Protocol Tests
# ---------------------------------------------------------------------------


def test_protocol_full_case_run_and_locks(tmp_path: Path) -> None:
    fixtures_dir = tmp_path / "fixtures"
    write_case_fixtures(fixtures_dir)
    public_ev, sealed_truth = generate_case_bundle("case_A", artifacts_root=fixtures_dir)
    Path(public_ev.sld_image_path).parent.mkdir(parents=True, exist_ok=True)
    Path(public_ev.sld_image_path).write_bytes(b"PNG_PLACEHOLDER")
    Path(public_ev.scene_3d_image_path).parent.mkdir(parents=True, exist_ok=True)
    Path(public_ev.scene_3d_image_path).write_bytes(b"PNG_PLACEHOLDER")

    transport = ManagedAgentTransport()
    sm = BlackboxProtocolStateMachine(transport=transport, sandbox_dir=tmp_path / "sandbox")
    probe_session = SingleShotProbeSession(sealed_truth)

    result = sm.run_case_protocol(public_ev, probe_session)
    assert result.case_id == "case_A"
    assert result.zero_leakage_verified is True
    assert set(result.locks.keys()) == {"I1", "I2", "A1", "A2"}
    for lock in result.locks.values():
        assert lock.private_truth_accessed_before_lock is False
        assert len(lock.payload_sha256) == 64

    handoff_records = validate_sandbox_handoff_files(
        [
            tmp_path / "sandbox" / "case_A" / "I1_output.json",
            tmp_path / "sandbox" / "case_A" / "I2_output.json",
            tmp_path / "sandbox" / "case_A" / "A1_output.json",
            tmp_path / "sandbox" / "case_A" / "A2_output.json",
        ]
    )
    assert len(handoff_records) == 4


def test_single_shot_probe_enforces_one_probe_limit() -> None:
    _, sealed_truth = generate_case_bundle("case_B")
    probe_session = SingleShotProbeSession(sealed_truth)
    from blackbox.contracts import ProbeRequest

    req = ProbeRequest(target_node="N2", delta_current_a=20.0, rationale="Test N2 terminal drop")
    resp = probe_session.execute_probe(req)
    assert resp.target_node == "N2"
    assert probe_session.executed is True

    with pytest.raises(RuntimeError, match="Probe budget exhausted"):
        probe_session.execute_probe(req)


def test_zero_leakage_detector_catches_leaked_token(tmp_path: Path) -> None:
    from dataclasses import replace

    public_ev, _ = generate_case_bundle("case_A", artifacts_root=tmp_path / "fixtures")
    bad_evidence = replace(public_ev, sld_image_path="/tmp/private/sealed_truth_sld.png")

    with pytest.raises(PermissionError, match="Zero-leakage violation"):
        BlackboxProtocolStateMachine.verify_zero_leakage_in_public_bundle(bad_evidence)
