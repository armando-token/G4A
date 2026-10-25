"""Unit tests for paired worlds Case A and Case B, zero-leakage sealing, and single-shot probe."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path

import pytest

from blackbox.contracts import ProbeRequest
from blackbox.domain import DEGRADED_JOINT_RESISTANCE_OHM, NOMINAL_FEEDER_RESISTANCE_OHM
from blackbox.fixture import (
    build_shared_sensory_telemetry,
    generate_case_bundle,
    write_case_fixtures,
)
from blackbox.probe import SingleShotProbeSession


def test_paired_worlds_case_a_and_case_b_topology_swap(tmp_path: Path) -> None:
    """Verify Case A and Case B share identical sensory telemetry while swapping N2/N3 feeders."""
    pub_a, priv_a = generate_case_bundle("case_A", artifacts_root=tmp_path)
    pub_b, priv_b = generate_case_bundle("case_B", artifacts_root=tmp_path)

    # Shared telemetry must be identical across A and B
    assert pub_a.facility_name == pub_b.facility_name
    assert pub_a.nominal_voltage_v == pub_b.nominal_voltage_v
    assert pub_a.ambient_temp_c == pub_b.ambient_temp_c
    assert pub_a.node_load_currents_a == pub_b.node_load_currents_a
    assert pub_a.shared_plenum_alarm == pub_b.shared_plenum_alarm
    assert pub_a.scene_3d_image_path == pub_b.scene_3d_image_path

    # Degraded physical node is N2 in both worlds, but its connected feeder swaps F2 <-> F3
    assert priv_a.degraded_node_id == "N2"
    assert priv_b.degraded_node_id == "N2"
    assert priv_a.degraded_feeder_id == "F2"
    assert priv_b.degraded_feeder_id == "F3"

    assert priv_a.node_to_feeder == {"N1": "F1", "N2": "F2", "N3": "F3", "N4": "F4"}
    assert priv_b.node_to_feeder == {"N1": "F1", "N2": "F3", "N3": "F2", "N4": "F4"}
    assert priv_a.truth_sha256 != priv_b.truth_sha256


def test_zero_leakage_in_public_evidence(tmp_path: Path) -> None:
    """Verify private ground truth fields never appear in public evidence records or files."""
    written = write_case_fixtures(tmp_path)
    assert set(written.keys()) == {"case_A", "case_B"}

    forbidden_keys = {
        "node_to_feeder",
        "feeder_to_node",
        "node_resistances_ohm",
        "degraded_node_id",
        "degraded_feeder_id",
        "degraded_resistance_ohm",
        "true_max_safe_current_a",
        "node_states",
        "truth_sha256",
    }

    for cid in ("case_A", "case_B"):
        pub_file = tmp_path / "public" / f"public_{cid}.json"
        priv_file = tmp_path / "private" / f"sealed_truth_{cid}.json"
        assert pub_file.exists()
        assert priv_file.exists()

        pub_data = json.loads(pub_file.read_text(encoding="utf-8"))
        assert forbidden_keys.isdisjoint(pub_data.keys())

        # Ensure shared plenum alarm remains ambiguous between N2 and N3 in public telemetry
        assert pub_data["shared_plenum_alarm"]["candidate_nodes"] == ["N2", "N3"]


def test_single_shot_probe_behavior_and_budget() -> None:
    """Verify deterministic voltage-drop probe returns accurate measurements and enforces 1-probe limit."""
    _, priv_a = generate_case_bundle("case_A")
    _, priv_b = generate_case_bundle("case_B")

    session_a = SingleShotProbeSession(priv_a)
    assert session_a.executed is False
    assert session_a.last_response is None

    req_n2 = ProbeRequest(
        target_node="n2",
        delta_current_a=20.0,
        rationale="Probe N2 lug following 3D IR hotspot localization.",
    )
    resp_a = session_a.execute_probe(req_n2, timestamp_utc="2026-10-25T12:00:00Z")
    assert session_a.executed is True
    assert resp_a.target_node == "N2"
    assert resp_a.connected_feeder_in_sld == "F2"
    assert resp_a.inferred_resistance_ohm == pytest.approx(DEGRADED_JOINT_RESISTANCE_OHM, abs=1e-4)
    assert resp_a.delta_voltage_drop_v == pytest.approx(2.90, abs=1e-4)
    assert resp_a.terminal_temperature_c == pytest.approx(135.23, abs=0.05)

    # Second probe attempt must raise RuntimeError
    with pytest.raises(RuntimeError, match="Probe budget exhausted"):
        session_a.execute_probe(req_n2)

    # Probing N2 in Case B yields identical electrical/thermal physics on N2 but maps to feeder F3
    session_b = SingleShotProbeSession(priv_b)
    resp_b = session_b.execute_probe(req_n2, timestamp_utc="2026-10-25T12:00:00Z")
    assert resp_b.target_node == "N2"
    assert resp_b.connected_feeder_in_sld == "F3"
    assert resp_b.inferred_resistance_ohm == pytest.approx(DEGRADED_JOINT_RESISTANCE_OHM, abs=1e-4)
    assert resp_b.delta_voltage_drop_v == resp_a.delta_voltage_drop_v

    # Probing healthy node N3 returns nominal resistance 0.018 Ohm
    session_healthy = SingleShotProbeSession(priv_a)
    req_n3 = ProbeRequest(target_node="N3", delta_current_a=20.0, rationale="Control check on N3")
    resp_n3 = session_healthy.execute_probe(req_n3, timestamp_utc="2026-10-25T12:00:00Z")
    assert resp_n3.inferred_resistance_ohm == pytest.approx(NOMINAL_FEEDER_RESISTANCE_OHM["F3"], abs=1e-4)
    assert resp_n3.terminal_temperature_c < 45.0
