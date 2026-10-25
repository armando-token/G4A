"""Unit tests for Twin Repair Phase A challenge fixture, SLD topology, privacy barrier, and physics oracle."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blackbox.render_sld import parse_sld_png_topology
from twin_repair.challenge import (
    build_challenge_fixture,
    reference_case,
    scan_for_truth_leakage,
)
from twin_repair.contracts import CANONICAL_EDGES_A, CANONICAL_EDGES_B
from twin_repair.physics_adapter import (
    compare_stale_and_repaired_actions,
    compute_feeder_currents,
    evaluate_actions,
    recommended_action,
)


def test_phase_a3_reference_case_oracle() -> None:
    ref = reference_case()
    assert ref["passed"] is True
    assert ref["total_current_a"] == pytest.approx(68.0)
    assert ref["initial_feeder_currents_A"] == {"F1": 40.0, "F2": 28.0}
    assert ref["initial_feeder_currents_B"] == {"F1": 40.0, "F2": 28.0}
    assert ref["no_action_n4_temp_180s_c"] == pytest.approx(98.829091, abs=1e-4)
    assert ref["offload_8a_n4_temp_180s_c"] == pytest.approx(52.534807, abs=1e-4)
    assert ref["no_action_n4_temp_180s_c"] > 90.0
    assert ref["offload_8a_n4_temp_180s_c"] < 90.0
    assert ref["stale_recommended_receiver"] == "N3"
    assert ref["current_recommended_receiver"] == "N2"


def test_compute_feeder_currents_conservation_and_rejections() -> None:
    assert compute_feeder_currents(CANONICAL_EDGES_A) == {"F1": 40.0, "F2": 28.0}
    assert compute_feeder_currents(CANONICAL_EDGES_B) == {"F1": 40.0, "F2": 28.0}

    with pytest.raises(ValueError, match="conservation violated"):
        compute_feeder_currents(CANONICAL_EDGES_B, {"N1": 36.0, "N2": 4.0, "N3": 4.0, "N4": 20.0})

    with pytest.raises(ValueError, match="unknown feeder"):
        compute_feeder_currents({"N1": "F1", "N2": "F3", "N3": "F1", "N4": "F2"})


def test_evaluate_actions_and_recommendation_flip() -> None:
    outcomes_a = {o.action_id: o for o in evaluate_actions(CANONICAL_EDGES_A)}
    outcomes_b = {o.action_id: o for o in evaluate_actions(CANONICAL_EDGES_B)}

    # In stale A: N3 is safe (F1=40, F2=28), N2 overloads F1 (48 > 44)
    assert outcomes_a["MIGRATE_N4_TO_N3_8A"].safe is True
    assert outcomes_a["MIGRATE_N4_TO_N3_8A"].f1_current_a == pytest.approx(40.0)
    assert outcomes_a["MIGRATE_N4_TO_N3_8A"].f2_current_a == pytest.approx(28.0)
    assert outcomes_a["MIGRATE_N4_TO_N2_8A"].safe is False
    assert outcomes_a["MIGRATE_N4_TO_N2_8A"].f1_current_a == pytest.approx(48.0)

    # In current B: N3 overloads F1 (48 > 44), N2 is safe (F1=40, F2=28)
    assert outcomes_b["MIGRATE_N4_TO_N3_8A"].safe is False
    assert outcomes_b["MIGRATE_N4_TO_N3_8A"].f1_current_a == pytest.approx(48.0)
    assert outcomes_b["MIGRATE_N4_TO_N3_8A"].feeder_safe is False
    assert outcomes_b["MIGRATE_N4_TO_N3_8A"].thermal_safe is True  # cools N4, but violates F1 limit!

    assert outcomes_b["MIGRATE_N4_TO_N2_8A"].safe is True
    assert outcomes_b["MIGRATE_N4_TO_N2_8A"].f1_current_a == pytest.approx(40.0)
    assert outcomes_b["MIGRATE_N4_TO_N2_8A"].f2_current_a == pytest.approx(28.0)

    rec_a = recommended_action(list(outcomes_a.values()))
    rec_b = recommended_action(list(outcomes_b.values()))
    assert rec_a is not None and rec_a.target_rack == "N3"
    assert rec_b is not None and rec_b.target_rack == "N2"

    comp = compare_stale_and_repaired_actions(CANONICAL_EDGES_A, CANONICAL_EDGES_B)
    assert comp["stale_recommended_receiver"] == "N3"
    assert comp["repaired_recommended_receiver"] == "N2"
    assert comp["recommendation_flipped"] is True


def test_build_challenge_fixture_reproducibility_and_privacy(tmp_path: Path) -> None:
    drift_dir1 = tmp_path / "drift1"
    drift_dir2 = tmp_path / "drift2"
    ctrl_dir = tmp_path / "control1"

    res1 = build_challenge_fixture(drift_dir1, seed=20261025, case_type="drift_A_to_B")
    res2 = build_challenge_fixture(drift_dir2, seed=20261025, case_type="drift_A_to_B")
    res_ctrl = build_challenge_fixture(ctrl_dir, seed=20261025, case_type="control_A_to_A")

    # Deterministic SHA-256 manifest identity
    assert res1["manifest"] == res2["manifest"]

    # SLD topology readability at 1280x800
    assert parse_sld_png_topology(Path(res1["old_sld_path"]))["mapping"] == CANONICAL_EDGES_A
    assert parse_sld_png_topology(Path(res1["current_sld_path"]))["mapping"] == CANONICAL_EDGES_B
    assert res1["manifest"]["public_assets_sha256"]["old_sld_A.png"] != (
        res1["manifest"]["public_assets_sha256"]["current_sld_B.png"]
    )

    # Control case has A->A with identical SLD topology
    assert parse_sld_png_topology(Path(res_ctrl["old_sld_path"]))["mapping"] == CANONICAL_EDGES_A
    assert parse_sld_png_topology(Path(res_ctrl["current_sld_path"]))["mapping"] == CANONICAL_EDGES_A
    assert res_ctrl["manifest"]["public_assets_sha256"]["old_sld_A.png"] == (
        res_ctrl["manifest"]["public_assets_sha256"]["current_sld_B.png"]
    )

    # Public state privacy check
    pub_state = json.loads(Path(res1["public_state_path"]).read_text(encoding="utf-8"))
    scan_res = scan_for_truth_leakage(drift_dir1, raise_on_leak=True)
    assert scan_res["leaked"] is False

    # Injecting private truth into public state must trigger scan_for_truth_leakage
    leaked_payload = dict(pub_state)
    leaked_payload["current_edges"] = dict(CANONICAL_EDGES_B)
    with pytest.raises(ValueError, match="Truth leakage detected"):
        scan_for_truth_leakage(leaked_payload, raise_on_leak=True)
