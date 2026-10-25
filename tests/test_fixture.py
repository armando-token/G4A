"""Unit tests for Phase 1 paired worlds, public packet privacy, diagnostic probe, and manifest."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from blackbox.contracts import ProbeRequest
from blackbox.domain import (
    AMBIENT_TEMP_C,
    DEGRADED_JOINT_RESISTANCE_OHM,
    NOMINAL_FEEDER_RESISTANCE_OHM,
    step_temperature,
)
from blackbox.fixture import (
    DEFAULT_ALPHA_PER_C,
    N4_RAMP_START_TEMP_C,
    build_private_world,
    export_public_packet,
    generate_case_bundle,
    generate_phase1_manifest,
    write_case_fixtures,
)
from blackbox.probe import DiagnosticProbeSession, SingleShotProbeSession


def test_build_private_world_a_and_b_topology_and_reference_outcomes() -> None:
    """Verify Phase 1 private worlds A and B graph edges, reversed safe/wrong receivers, and exact reference outcomes."""
    priv_a = build_private_world("A")
    priv_b = build_private_world("B")

    # 1. Graph edges: F1->{N1,N2}, F2->{N3,N4} in A; F1->{N1,N3}, F2->{N2,N4} in B
    assert priv_a["pdu_to_racks"] == {"F1": ["N1", "N2"], "F2": ["N3", "N4"]}
    assert priv_a["feeder_map"] == {"N1": "F1", "N2": "F1", "N3": "F2", "N4": "F2"}

    assert priv_b["pdu_to_racks"] == {"F1": ["N1", "N3"], "F2": ["N2", "N4"]}
    assert priv_b["feeder_map"] == {"N1": "F1", "N2": "F2", "N3": "F1", "N4": "F2"}

    # 2. Safe receiver and wrong receiver reverse between A and B
    assert priv_a["safe_receiver"] == "N3"
    assert priv_a["wrong_receiver"] == "N2"
    assert priv_b["safe_receiver"] == "N2"
    assert priv_b["wrong_receiver"] == "N3"

    # 3. Exact reference outcomes for both A and B
    for priv in (priv_a, priv_b):
        assert priv["racks"]["N4"]["initial_temp_C"] == pytest.approx(88.95030198217694, abs=1e-11)
        assert priv["preceding_ramp"]["SNS_K2"]["temp_t0_C"] == pytest.approx(88.95030198217694, abs=1e-11)

        no_act = priv["reference_outcomes"]["no_action"]
        assert no_act["n4_temp_t180_C"] == pytest.approx(98.82909050044, abs=1e-9)
        assert no_act["safe"] is False
        assert no_act["thermal_violation"] is True
        assert no_act["verdict"] == "unsafe, thermal"

        safe_out = priv["reference_outcomes"]["safe_receiver"]
        assert safe_out["target"] == priv["safe_receiver"]
        assert safe_out["n4_temp_t180_C"] == pytest.approx(52.53480720813, abs=1e-9)
        assert safe_out["F1_current_A"] == pytest.approx(40.0)
        assert safe_out["F2_current_A"] == pytest.approx(28.0)
        assert safe_out["safe"] is True
        assert safe_out["pdu_overload"] is False
        assert safe_out["verdict"] == "safe"

        wrong_out = priv["reference_outcomes"]["wrong_receiver"]
        assert wrong_out["target"] == priv["wrong_receiver"]
        assert wrong_out["n4_temp_t180_C"] == pytest.approx(52.53480720813, abs=1e-9)
        assert wrong_out["F1_current_A"] == pytest.approx(48.0)
        assert wrong_out["F2_current_A"] == pytest.approx(20.0)
        assert wrong_out["safe"] is False
        assert wrong_out["pdu_overload"] is True
        assert wrong_out["violated_pdus"] == ["F1"]
        assert wrong_out["verdict"] == "unsafe, F1 overload"


def test_candidate_explanations_produce_identical_thermal_trajectory() -> None:
    """Verify electrical (R0=0.04, h=0.4, C=8.0) and cooling (R0=0.02, h=0.2, C=4.0) produce identical thermal trajectories."""
    priv_a = build_private_world("A")
    elec = priv_a["candidate_explanations"]["electrical"]
    cool = priv_a["candidate_explanations"]["cooling"]

    assert (elec["R0_ohm"], elec["h_W_per_K"], elec["C_J_per_K"]) == (0.04, 0.4, 8.0)
    assert (cool["R0_ohm"], cool["h_W_per_K"], cool["C_J_per_K"]) == (0.02, 0.2, 4.0)

    currents = priv_a["preceding_ramp"]["SNS_K2"]["currents_A"][:-1] + [16.0] * 18
    t_elec = N4_RAMP_START_TEMP_C
    t_cool = N4_RAMP_START_TEMP_C

    for i_a in currents:
        t_elec = step_temperature(
            t_c=t_elec,
            i_a=i_a,
            ambient_c=AMBIENT_TEMP_C,
            r0_ohm=elec["R0_ohm"],
            alpha=DEFAULT_ALPHA_PER_C,
            h_w_k=elec["h_W_per_K"],
            heat_capacity_j_k=elec["C_J_per_K"],
            dt_s=10.0,
        )
        t_cool = step_temperature(
            t_c=t_cool,
            i_a=i_a,
            ambient_c=AMBIENT_TEMP_C,
            r0_ohm=cool["R0_ohm"],
            alpha=DEFAULT_ALPHA_PER_C,
            h_w_k=cool["h_W_per_K"],
            heat_capacity_j_k=cool["C_J_per_K"],
            dt_s=10.0,
        )
        assert t_elec == pytest.approx(t_cool, abs=1e-12)


def test_export_public_packet_byte_identity_and_zero_leakage() -> None:
    """Verify public packets for A and B are byte-for-byte identical and leak zero private truth."""
    priv_a = build_private_world("A")
    priv_b = build_private_world("B")

    for mode in ("guided_diagnostic", "unassisted"):
        pub_a = export_public_packet(priv_a, diagnostic_mode=mode)
        pub_b = export_public_packet(priv_b, diagnostic_mode=mode)

        bytes_a = json.dumps(pub_a, sort_keys=True)
        bytes_b = json.dumps(pub_b, sort_keys=True)
        assert bytes_a == bytes_b

        # Verify obsolete maintenance note is included
        assert "[OBSOLETE - SUPERSEDED BY ACTIVE SLD]" in bytes_a

        # Verify zero private fields, truth labels, bindings, bboxes, or R0/h/C parameters
        forbidden_tokens = (
            "R0_ohm",
            "0.04",
            "h_W_per_K",
            "0.4",
            "C_J_per_K",
            "safe_receiver",
            "wrong_receiver",
            "sensor_to_rack",
            "rack_to_sensor",
            "connector_bboxes",
            "SNS_K2->N4",
            "700",
            "316",
            "feeder_map",
            "pdu_to_racks",
        )
        for token in forbidden_tokens:
            assert token not in bytes_a, f"Forbidden token {token!r} leaked in public packet ({mode})"


def test_diagnostic_probe_session_single_shot_and_immutability() -> None:
    """Verify DiagnosticProbeSession enforces I1 hash freeze, issues 1 signed receipt, and rejects invalid/second calls."""
    priv_a = build_private_world("A")
    session = DiagnosticProbeSession(priv_a)

    # 1. Rejects issuing receipt before i1_proposal_hash is frozen/provided
    with pytest.raises(RuntimeError, match="i1_proposal_hash"):
        session.issue_receipt(kind="voltage_drop", target="N4")
    assert session.executed is False
    assert len(session.ledger) == 0
    assert len(session.rejected_attempts) == 1

    # 2. Freeze i1_proposal_hash and verify malformed target / invalid menu item are rejected before spending budget
    frozen_hash = session.freeze_i1_proposal({"candidate": "N3", "hypothesis": "electrical"})
    assert len(frozen_hash) == 64

    with pytest.raises(ValueError, match="Invalid diagnostic target"):
        session.issue_receipt(kind="voltage_drop", target="SNS_K2")
    with pytest.raises(ValueError, match="Invalid diagnostic kind"):
        session.issue_receipt(kind="infrared_thermography", target="N4")
    assert session.executed is False
    assert len(session.ledger) == 0
    assert len(session.rejected_attempts) == 3

    # 3. Issue the single allowed receipt for kind="voltage_drop", target="N4"
    receipt = session.issue_receipt(kind="voltage_drop", target="N4")
    assert session.executed is True
    assert receipt.kind == "voltage_drop"
    assert receipt.target == "N4"
    assert receipt.value == 1.209409
    assert receipt.exact_value == pytest.approx(1.2094091596115595, abs=1e-12)
    assert receipt.units == "V"
    assert receipt.i1_proposal_hash == frozen_hash
    assert len(receipt.receipt_sha256) == 64

    # Verify receipt SHA-256 signature
    expected_core = {
        "receipt_id": receipt.receipt_id,
        "case_id": receipt.case_id,
        "kind": receipt.kind,
        "target": receipt.target,
        "value": receipt.value,
        "exact_value": receipt.exact_value,
        "units": receipt.units,
        "i1_proposal_hash": receipt.i1_proposal_hash,
        "timestamp_s": receipt.timestamp_s,
    }
    expected_sha = hashlib.sha256(
        json.dumps(expected_core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert receipt.receipt_sha256 == expected_sha
    assert len(session.ledger) == 1
    assert session.ledger[0] == receipt

    # 4. Reject any second / post-hoc request while keeping ledger and receipt hash immutable
    with pytest.raises(RuntimeError, match="Probe budget exhausted"):
        session.issue_receipt(kind="temperature_snapshot", target="N4")
    with pytest.raises(RuntimeError, match="Probe budget exhausted"):
        session.probe(kind="controller_setting", target="N1")

    assert len(session.ledger) == 1
    assert session.ledger[0].receipt_sha256 == expected_sha
    assert session.receipt is not None
    assert session.receipt.receipt_sha256 == expected_sha
    assert len(session.rejected_attempts) == 5


def test_generate_phase1_manifest(tmp_path: Path) -> None:
    """Verify generate_phase1_manifest writes a valid manifest with hashes and reference values."""
    manifest_path = tmp_path / "phase1_manifest.json"
    manifest = generate_phase1_manifest(manifest_path)

    assert manifest_path.exists()
    on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert on_disk == manifest

    assert manifest["phase"] == "01_DOMAIN_AND_FIXTURE"
    assert len(manifest["domain_py_sha256"]) == 64
    assert manifest["private_world_sha256"]["distinct"] is True
    assert manifest["public_packet_sha256"]["guided_diagnostic"]["byte_identical"] is True
    assert manifest["public_packet_sha256"]["unassisted"]["byte_identical"] is True

    ref = manifest["reference_numbers"]
    assert ref["n4_t0_temp_C"] == pytest.approx(88.95030198217694, abs=1e-11)
    assert ref["n4_voltage_drop_quantized_V"] == 1.209409
    assert ref["inferred_r0_ohm"] == pytest.approx(0.04, abs=1e-9)
    assert ref["fitted_h_W_per_K"] == pytest.approx(0.4, abs=1e-6)
    assert ref["fitted_C_J_per_K"] == pytest.approx(8.0, abs=1e-6)
    assert ref["case_A"]["safe_receiver"] == "N3"
    assert ref["case_B"]["safe_receiver"] == "N2"


# ---------------------------------------------------------------------------
# Existing Backward-Compatible Case Bundle Tests
# ---------------------------------------------------------------------------


def test_paired_worlds_case_a_and_case_b_topology_swap(tmp_path: Path) -> None:
    """Verify Case A and Case B share identical sensory telemetry while swapping N2/N3 feeders."""
    pub_a, priv_a = generate_case_bundle("case_A", artifacts_root=tmp_path)
    pub_b, priv_b = generate_case_bundle("case_B", artifacts_root=tmp_path)

    assert pub_a.facility_name == pub_b.facility_name
    assert pub_a.nominal_voltage_v == pub_b.nominal_voltage_v
    assert pub_a.ambient_temp_c == pub_b.ambient_temp_c
    assert pub_a.node_load_currents_a == pub_b.node_load_currents_a
    assert pub_a.shared_plenum_alarm == pub_b.shared_plenum_alarm
    assert pub_a.scene_3d_image_path == pub_b.scene_3d_image_path

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

    with pytest.raises(RuntimeError, match="Probe budget exhausted"):
        session_a.execute_probe(req_n2)

    session_b = SingleShotProbeSession(priv_b)
    resp_b = session_b.execute_probe(req_n2, timestamp_utc="2026-10-25T12:00:00Z")
    assert resp_b.target_node == "N2"
    assert resp_b.connected_feeder_in_sld == "F3"
    assert resp_b.inferred_resistance_ohm == pytest.approx(DEGRADED_JOINT_RESISTANCE_OHM, abs=1e-4)
    assert resp_b.delta_voltage_drop_v == resp_a.delta_voltage_drop_v

    session_healthy = SingleShotProbeSession(priv_a)
    req_n3 = ProbeRequest(target_node="N3", delta_current_a=20.0, rationale="Control check on N3")
    resp_n3 = session_healthy.execute_probe(req_n3, timestamp_utc="2026-10-25T12:00:00Z")
    assert resp_n3.inferred_resistance_ohm == pytest.approx(NOMINAL_FEEDER_RESISTANCE_OHM["F3"], abs=1e-4)
    assert resp_n3.terminal_temperature_c < 45.0
