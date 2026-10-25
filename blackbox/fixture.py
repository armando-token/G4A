"""Synthetic electrical & thermal data-center case generator for BlackboxDC.

Implements:
1. Phase 1 Paired Worlds (`build_private_world`, `export_public_packet`,
   `generate_phase1_manifest`) where Network A (`F1->N1,N2`, `F2->N3,N4`) and
   Network B (`F1->N1,N3`, `F2->N2,N4`) share byte-for-byte identical non-SLD
   public evidence while swapping the safe 8 A migration receiver (`N3` in A,
   `N2` in B) for the overheated rear connector on `N4` (`SNS_K2`).
2. Backward-compatible case bundle helpers (`PublicCaseEvidence`,
   `SealedWorldTruth`, `build_shared_sensory_telemetry`, `generate_case_bundle`,
   `write_case_fixtures`) used by downstream phases.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
from typing import Any, Dict, List, Mapping, Optional, Tuple

import numpy as np

from blackbox.domain import (
    AMBIENT_TEMP_C,
    DEFAULT_HORIZON_S,
    DEFAULT_MAX_TEMP_C,
    DEFAULT_MIGRATION_AMPS,
    DEFAULT_STEP_S,
    DEGRADED_JOINT_RESISTANCE_OHM,
    MAX_SAFE_FEEDER_CURRENT_A,
    MAX_SAFE_JUNCTION_TEMP_C,
    NOMINAL_FEEDER_RESISTANCE_OHM,
    NOMINAL_VOLTAGE_V,
    THERMAL_RESISTANCE_C_PER_W,
    Migration,
    PDU,
    RackParams,
    Trajectory,
    World,
    evaluate_node_state,
    fit_thermal_parameters_from_ramp,
    infer_r0_from_voltage_drop,
    simulate_world,
    step_temperature,
)


FIXTURE_SCHEMA_VERSION: str = "1.0.0"
DEFAULT_ALPHA_PER_C: float = 0.004
N4_RAMP_START_TEMP_C: float = 34.0
N4_T0_TEMP_C: float = 88.95030198217694
N4_SAFE_T180_TEMP_C: float = 52.5348072081335
N4_VOLTAGE_DROP_EXACT_V: float = 1.2094091596115595
N4_VOLTAGE_DROP_QUANTIZED_V: float = 1.209409

DIAGNOSTIC_MENU: Tuple[str, ...] = (
    "voltage_drop",
    "temperature_snapshot",
    "controller_setting",
)

SENSOR_TO_RACK_CANONICAL: Dict[str, str] = {
    "SNS_K1": "N1",
    "SNS_K2": "N4",
    "SNS_K3": "N2",
    "SNS_K4": "N3",
}

RACK_TO_SENSOR_CANONICAL: Dict[str, str] = {
    rack_id: sensor_id for sensor_id, rack_id in SENSOR_TO_RACK_CANONICAL.items()
}

CONNECTOR_BBOXES_CANONICAL: Dict[str, List[int]] = {
    "N1": [88, 220, 172, 316],
    "N2": [292, 220, 376, 316],
    "N3": [496, 220, 580, 316],
    "N4": [700, 220, 784, 316],
}

PRIVATE_TRUTH_MASK_KEYS: Tuple[str, ...] = (
    "R0_ohm",
    "h_W_per_K",
    "C_J_per_K",
    "sensor_to_rack",
    "rack_to_sensor",
    "safe_receiver",
    "wrong_receiver",
    "connector_bboxes",
    "feeder_map",
    "pdu_to_racks",
    "candidate_explanations",
    "reference_outcomes",
    "truth_mask",
)


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _compute_sha256(payload: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _normalize_network_case_id(case_id: str) -> Tuple[str, str]:
    """Normalize case identifier into ('A'|'B', 'case_A'|'case_B')."""
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError("case_id must be a non-empty string")
    raw = case_id.strip()
    if raw in ("A", "case_A"):
        return "A", "case_A"
    if raw in ("B", "case_B"):
        return "B", "case_B"
    raise ValueError(f"Unsupported case_id: {case_id!r}. Expected 'A', 'B', 'case_A', or 'case_B'.")


def _build_preceding_ramp() -> Dict[str, Any]:
    """Construct the exact 13-sample preceding thermal ramp over t = -120..0s."""
    t_s: List[int] = list(range(-120, 1, DEFAULT_STEP_S))
    # Three 40-second segments (4 steps each) at 12A, 18A, 24A, plus t=0 at 24A
    n4_currents_a: List[float] = [12.0] * 4 + [18.0] * 4 + [24.0] * 4 + [24.0]
    n4_temps_c: List[float] = [N4_RAMP_START_TEMP_C]
    t_curr = N4_RAMP_START_TEMP_C
    for k in range(12):
        t_curr = step_temperature(
            t_c=t_curr,
            i_a=n4_currents_a[k],
            ambient_c=AMBIENT_TEMP_C,
            r0_ohm=0.040,
            alpha=DEFAULT_ALPHA_PER_C,
            h_w_k=0.4,
            heat_capacity_j_k=8.0,
            dt_s=float(DEFAULT_STEP_S),
        )
        n4_temps_c.append(float(t_curr))

    sensor_series: Dict[str, Dict[str, Any]] = {
        "SNS_K1": {
            "sensor_id": "SNS_K1",
            "t_s": list(t_s),
            "temps_C": [36.0] * len(t_s),
            "currents_A": [36.0] * len(t_s),
            "temp_t0_C": 36.0,
            "current_t0_A": 36.0,
        },
        "SNS_K2": {
            "sensor_id": "SNS_K2",
            "t_s": list(t_s),
            "temps_C": list(n4_temps_c),
            "currents_A": list(n4_currents_a),
            "temp_t0_C": float(n4_temps_c[-1]),
            "current_t0_A": float(n4_currents_a[-1]),
        },
        "SNS_K3": {
            "sensor_id": "SNS_K3",
            "t_s": list(t_s),
            "temps_C": [25.0] * len(t_s),
            "currents_A": [4.0] * len(t_s),
            "temp_t0_C": 25.0,
            "current_t0_A": 4.0,
        },
        "SNS_K4": {
            "sensor_id": "SNS_K4",
            "t_s": list(t_s),
            "temps_C": [25.0] * len(t_s),
            "currents_A": [4.0] * len(t_s),
            "temp_t0_C": 25.0,
            "current_t0_A": 4.0,
        },
    }
    return {
        "t_s": t_s,
        "times_s": list(t_s),
        "sensors": sensor_series,
        "SNS_K1": copy.deepcopy(sensor_series["SNS_K1"]),
        "SNS_K2": copy.deepcopy(sensor_series["SNS_K2"]),
        "SNS_K3": copy.deepcopy(sensor_series["SNS_K3"]),
        "SNS_K4": copy.deepcopy(sensor_series["SNS_K4"]),
    }


def _summarize_trajectory(
    traj: Trajectory,
    migration: Optional[Migration] = None,
) -> Dict[str, Any]:
    temps_t180 = {rack_id: float(series[-1]) for rack_id, series in traj.temps_C.items()}
    currents_t180 = {rack_id: float(series[-1]) for rack_id, series in traj.currents_A.items()}
    pdu_currents_t180 = {pdu_id: float(series[-1]) for pdu_id, series in traj.pdu_currents_A.items()}
    target_rack = migration.target if migration is not None else None
    return {
        "migration": asdict(migration) if migration is not None else None,
        "target": target_rack,
        "safe": bool(traj.safety_flags["safe"]),
        "verdict": str(traj.safety_flags["verdict"]),
        "thermal_violation": bool(traj.safety_flags["thermal_violation"]),
        "pdu_overload": bool(traj.safety_flags["pdu_overload"]),
        "rack_overload": bool(traj.safety_flags["rack_overload"]),
        "violated_racks": list(traj.safety_flags["violated_racks"]),
        "violated_pdus": list(traj.safety_flags["violated_pdus"]),
        "temps_t180_C": temps_t180,
        "n4_temp_t180_C": float(temps_t180["N4"]),
        "receiver_temp_t180_C": float(temps_t180[target_rack]) if target_rack else None,
        "rack_currents_A": currents_t180,
        "pdu_currents_A": pdu_currents_t180,
        "F1_current_A": float(pdu_currents_t180["F1"]),
        "F2_current_A": float(pdu_currents_t180["F2"]),
        "trajectory": asdict(traj),
    }


def build_private_world(case_id: str, seed: int = 0) -> Dict[str, Any]:
    """Build the complete sealed private world for Network A or Network B."""
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a non-negative integer")

    short_id, canonical_case_id = _normalize_network_case_id(case_id)
    if short_id == "A":
        feeder_map = {"N1": "F1", "N2": "F1", "N3": "F2", "N4": "F2"}
        pdu_to_racks = {"F1": ["N1", "N2"], "F2": ["N3", "N4"]}
        safe_receiver = "N3"
        wrong_receiver = "N2"
    else:
        feeder_map = {"N1": "F1", "N2": "F2", "N3": "F1", "N4": "F2"}
        pdu_to_racks = {"F1": ["N1", "N3"], "F2": ["N2", "N4"]}
        safe_receiver = "N2"
        wrong_receiver = "N3"

    ramp = _build_preceding_ramp()
    n4_t0 = float(ramp["SNS_K2"]["temp_t0_C"])

    rack_specs: Tuple[RackParams, ...] = (
        RackParams(
            rack_id="N1",
            pdu_id=feeder_map["N1"],
            current_A=36.0,
            initial_temp_C=36.0,
            R0_ohm=0.005,
            alpha_per_C=DEFAULT_ALPHA_PER_C,
            h_W_per_K=0.5,
            C_J_per_K=8.0,
            max_current_A=40.0,
            max_temp_C=DEFAULT_MAX_TEMP_C,
        ),
        RackParams(
            rack_id="N2",
            pdu_id=feeder_map["N2"],
            current_A=4.0,
            initial_temp_C=25.0,
            R0_ohm=0.010,
            alpha_per_C=DEFAULT_ALPHA_PER_C,
            h_W_per_K=0.5,
            C_J_per_K=8.0,
            max_current_A=16.0,
            max_temp_C=DEFAULT_MAX_TEMP_C,
        ),
        RackParams(
            rack_id="N3",
            pdu_id=feeder_map["N3"],
            current_A=4.0,
            initial_temp_C=25.0,
            R0_ohm=0.010,
            alpha_per_C=DEFAULT_ALPHA_PER_C,
            h_W_per_K=0.5,
            C_J_per_K=8.0,
            max_current_A=16.0,
            max_temp_C=DEFAULT_MAX_TEMP_C,
        ),
        RackParams(
            rack_id="N4",
            pdu_id=feeder_map["N4"],
            current_A=24.0,
            initial_temp_C=n4_t0,
            R0_ohm=0.040,
            alpha_per_C=DEFAULT_ALPHA_PER_C,
            h_W_per_K=0.4,
            C_J_per_K=8.0,
            max_current_A=32.0,
            max_temp_C=DEFAULT_MAX_TEMP_C,
        ),
    )

    pdu_specs: Tuple[PDU, ...] = (
        PDU(pdu_id="F1", max_current_A=44.0),
        PDU(pdu_id="F2", max_current_A=50.0),
    )

    world = World(
        racks=rack_specs,
        pdus=pdu_specs,
        ambient_C=AMBIENT_TEMP_C,
        horizon_s=DEFAULT_HORIZON_S,
        step_s=DEFAULT_STEP_S,
        allowed_targets=("N2", "N3"),
    )

    roles = {
        "N1": "existing heavy load",
        "N2": "receiver",
        "N3": "receiver",
        "N4": "hot rear connector, 8A source",
    }

    racks_dict: Dict[str, Dict[str, Any]] = {}
    for r in rack_specs:
        r_dict = asdict(r)
        r_dict["role"] = roles[r.rack_id]
        r_dict["sensor_id"] = RACK_TO_SENSOR_CANONICAL[r.rack_id]
        racks_dict[r.rack_id] = r_dict

    pdus_dict: Dict[str, Dict[str, Any]] = {}
    for p in pdu_specs:
        connected = list(pdu_to_racks[p.pdu_id])
        init_i = float(sum(racks_dict[rid]["current_A"] for rid in connected))
        pdus_dict[p.pdu_id] = {
            "pdu_id": p.pdu_id,
            "max_current_A": float(p.max_current_A),
            "connected_racks": connected,
            "initial_current_A": init_i,
        }

    # Reference simulations over [0..180s]
    traj_no_action = simulate_world(world, migration=None)
    mig_safe = Migration(source="N4", target=safe_receiver, amps=DEFAULT_MIGRATION_AMPS)
    traj_safe = simulate_world(world, migration=mig_safe)
    mig_wrong = Migration(source="N4", target=wrong_receiver, amps=DEFAULT_MIGRATION_AMPS)
    traj_wrong = simulate_world(world, migration=mig_wrong)

    no_action_summary = _summarize_trajectory(traj_no_action, migration=None)
    safe_summary = _summarize_trajectory(traj_safe, migration=mig_safe)
    wrong_summary = _summarize_trajectory(traj_wrong, migration=mig_wrong)

    by_target = {
        safe_receiver: safe_summary,
        wrong_receiver: wrong_summary,
    }

    # Candidate explanations that match the temperature ramp identically
    v_elec = 24.0 * 0.040 * (1.0 + DEFAULT_ALPHA_PER_C * (n4_t0 - AMBIENT_TEMP_C))
    v_cool = 24.0 * 0.020 * (1.0 + DEFAULT_ALPHA_PER_C * (n4_t0 - AMBIENT_TEMP_C))
    candidate_explanations = {
        "electrical": {
            "hypothesis": "electrical",
            "R0_ohm": 0.040,
            "h_W_per_K": 0.4,
            "C_J_per_K": 8.0,
            "predicted_voltage_drop_V": float(v_elec),
            "quantized_voltage_drop_V": round(float(v_elec), 6),
            "is_ground_truth": True,
        },
        "cooling": {
            "hypothesis": "cooling",
            "R0_ohm": 0.020,
            "h_W_per_K": 0.2,
            "C_J_per_K": 4.0,
            "predicted_voltage_drop_V": float(v_cool),
            "quantized_voltage_drop_V": round(float(v_cool), 6),
            "is_ground_truth": False,
        },
    }

    controller_settings = {
        "N1": "NOMINAL_FIXED_36A",
        "N2": "RECEIVER_READY_AUTO",
        "N3": "RECEIVER_READY_AUTO",
        "N4": "SOURCE_DEGRADED_HOLD_24A",
    }

    return {
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "case_id": short_id,
        "canonical_case_id": canonical_case_id,
        "seed": int(seed),
        "ambient_C": AMBIENT_TEMP_C,
        "alpha_per_C": DEFAULT_ALPHA_PER_C,
        "max_temp_C": DEFAULT_MAX_TEMP_C,
        "horizon_s": DEFAULT_HORIZON_S,
        "step_s": DEFAULT_STEP_S,
        "migration_quantum_A": DEFAULT_MIGRATION_AMPS,
        "source_rack": "N4",
        "safe_receiver": safe_receiver,
        "wrong_receiver": wrong_receiver,
        "world": world,
        "racks": racks_dict,
        "pdus": pdus_dict,
        "feeder_map": dict(feeder_map),
        "pdu_to_racks": copy.deepcopy(pdu_to_racks),
        "sensor_to_rack": dict(SENSOR_TO_RACK_CANONICAL),
        "rack_to_sensor": dict(RACK_TO_SENSOR_CANONICAL),
        "connector_bboxes": copy.deepcopy(CONNECTOR_BBOXES_CANONICAL),
        "candidate_explanations": candidate_explanations,
        "preceding_ramp": ramp,
        "controller_settings": controller_settings,
        "reference_outcomes": {
            "no_action": no_action_summary,
            "safe_receiver": safe_summary,
            "wrong_receiver": wrong_summary,
            "safe_migration": safe_summary,
            "wrong_migration": wrong_summary,
            "by_target": by_target,
        },
        "truth_mask": {
            "forbidden_keys": list(PRIVATE_TRUTH_MASK_KEYS),
            "forbidden_substrings": [
                "R0_ohm",
                "0.04",
                "h_W_per_K",
                "C_J_per_K",
                "sensor_to_rack",
                "rack_to_sensor",
                "safe_receiver",
                "wrong_receiver",
                "connector_bboxes",
                "F1->",
                "F2->",
            ],
        },
    }


def export_public_packet(
    private_world: Mapping[str, Any],
    diagnostic_mode: str = "guided_diagnostic",
) -> Dict[str, Any]:
    """Export the redacted public evidence packet for an Investigator / Auditor session.

    Guaranteed to be byte-for-byte identical between Network A and Network B for all
    non-SLD content, with zero leakage of private resistances, thermal parameters,
    sensor-to-rack bindings, or feeder topology.
    """
    if not isinstance(private_world, Mapping) or "preceding_ramp" not in private_world:
        raise ValueError("private_world must be a valid mapping returned by build_private_world")
    if diagnostic_mode not in ("guided_diagnostic", "unassisted"):
        raise ValueError(
            f"Unsupported diagnostic_mode {diagnostic_mode!r}. Expected 'guided_diagnostic' or 'unassisted'."
        )

    ramp = copy.deepcopy(private_world["preceding_ramp"])
    if diagnostic_mode == "guided_diagnostic":
        task_prompt = (
            "GUIDED DIAGNOSTIC MODE: Inspect the rear-aisle RGB and thermal captures to "
            "bind anonymous sensor traces (SNS_K1..SNS_K4) to physical racks (N1..N4). "
            "Request a single 'voltage_drop' diagnostic probe on the overheated source rack "
            "to disambiguate connector resistance, fit thermal parameters from its ramp, "
            "consult the authoritative active SLD drawing ('sld.png') for feeder routing, "
            "and recommend migrating 8.0 A to the safe receiver rack ('N2' or 'N3')."
        )
    else:
        task_prompt = (
            "UNASSISTED DIAGNOSTIC MODE: Inspect the rear-aisle RGB and thermal captures to "
            "bind anonymous sensor traces (SNS_K1..SNS_K4) to physical racks (N1..N4). "
            "Select at most one diagnostic measurement from the diagnostic_menu "
            "('voltage_drop', 'temperature_snapshot', 'controller_setting') on a target rack, "
            "consult the authoritative active SLD drawing ('sld.png') for feeder routing, "
            "and recommend migrating 8.0 A to the safe receiver rack ('N2' or 'N3')."
        )

    visual_assets = {
        "sld_drawing": "sld.png",
        "rgb_capture": "rear_aisle_rgb.png",
        "thermal_capture": "rear_aisle_thermal.png",
    }

    component_limits = {
        "max_temp_C": DEFAULT_MAX_TEMP_C,
        "pdu_limits_A": {"F1": 44.0, "F2": 50.0},
        "rack_limits_A": {"N1": 40.0, "N2": 16.0, "N3": 16.0, "N4": 32.0},
        "migration_quantum_A": DEFAULT_MIGRATION_AMPS,
        "allowed_receivers": ["N2", "N3"],
    }

    maintenance_records = [
        {
            "record_id": "MNT-2026-0814",
            "status": "[OBSOLETE - SUPERSEDED BY ACTIVE SLD]",
            "summary": (
                "[OBSOLETE - SUPERSEDED BY ACTIVE SLD] Historical maintenance log noted "
                "rack N3 landed on PDU feeder F1 prior to busway re-termination. "
                "Do NOT rely on this obsolete text note for feeder topology; the active "
                "single-line diagram image ('sld.png') is the sole authoritative source "
                "for current feeder-to-rack assignments."
            ),
            "authoritative_source": "sld.png",
        },
        {
            "record_id": "OPS-2026-1025",
            "status": "ACTIVE",
            "summary": (
                "Rear connector overheating observed during step-load ramp. "
                "Exactly 8.0 A must be migrated from the overheated source rack to an "
                "eligible receiver rack ('N2' or 'N3') without exceeding PDU feeder or "
                "rack current/temperature limits over the 180 s horizon."
            ),
            "authoritative_source": "sld.png",
        },
    ]

    return {
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "diagnostic_mode": diagnostic_mode,
        "ambient_C": AMBIENT_TEMP_C,
        "alpha_per_C": DEFAULT_ALPHA_PER_C,
        "max_temp_C": DEFAULT_MAX_TEMP_C,
        "horizon_s": DEFAULT_HORIZON_S,
        "step_s": DEFAULT_STEP_S,
        "migration_quantum_A": DEFAULT_MIGRATION_AMPS,
        "pdu_limits_A": {"F1": 44.0, "F2": 50.0},
        "rack_limits_A": {"N1": 40.0, "N2": 16.0, "N3": 16.0, "N4": 32.0},
        "allowed_receivers": ["N2", "N3"],
        "component_limits": component_limits,
        "telemetry": ramp,
        "maintenance_records": maintenance_records,
        "diagnostic_menu": list(DIAGNOSTIC_MENU),
        "task_prompt": task_prompt,
        "visual_assets": visual_assets,
        "visual_references": dict(visual_assets),
    }


def _serializable_private_world(private_world: Mapping[str, Any]) -> Dict[str, Any]:
    """Convert a build_private_world dict (containing a World dataclass) to pure JSON types."""
    out: Dict[str, Any] = {}
    for k, v in private_world.items():
        if isinstance(v, World):
            out[k] = asdict(v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def generate_phase1_manifest(output_path: Optional[Path] = None) -> Dict[str, Any]:
    """Generate and optionally write the Phase 1 exit gate manifest."""
    priv_a = build_private_world("A", seed=0)
    priv_b = build_private_world("B", seed=0)

    pub_a_guided = export_public_packet(priv_a, diagnostic_mode="guided_diagnostic")
    pub_b_guided = export_public_packet(priv_b, diagnostic_mode="guided_diagnostic")
    pub_a_unassisted = export_public_packet(priv_a, diagnostic_mode="unassisted")
    pub_b_unassisted = export_public_packet(priv_b, diagnostic_mode="unassisted")

    domain_path = Path(__file__).resolve().parent / "domain.py"
    domain_sha256 = hashlib.sha256(domain_path.read_bytes()).hexdigest()

    priv_a_sha = _compute_sha256(_serializable_private_world(priv_a))
    priv_b_sha = _compute_sha256(_serializable_private_world(priv_b))
    pub_a_guided_sha = _compute_sha256(pub_a_guided)
    pub_b_guided_sha = _compute_sha256(pub_b_guided)
    pub_a_unassisted_sha = _compute_sha256(pub_a_unassisted)
    pub_b_unassisted_sha = _compute_sha256(pub_b_unassisted)

    ramp_k2 = pub_a_guided["telemetry"]["SNS_K2"]
    r0_inferred = infer_r0_from_voltage_drop(
        voltage_drop_v=N4_VOLTAGE_DROP_EXACT_V,
        current_a=24.0,
        temp_c=float(ramp_k2["temp_t0_C"]),
        ambient_c=AMBIENT_TEMP_C,
        alpha_per_c=DEFAULT_ALPHA_PER_C,
    )
    fitted = fit_thermal_parameters_from_ramp(
        times_s=ramp_k2["t_s"],
        temps_c=ramp_k2["temps_C"],
        currents_a=ramp_k2["currents_A"],
        r0_ohm=r0_inferred,
        ambient_c=AMBIENT_TEMP_C,
        alpha_per_c=DEFAULT_ALPHA_PER_C,
    )

    try:
        pydantic_version = importlib.metadata.version("pydantic")
    except importlib.metadata.PackageNotFoundError:
        pydantic_version = "unknown"

    manifest: Dict[str, Any] = {
        "phase": "01_DOMAIN_AND_FIXTURE",
        "fixture_version": FIXTURE_SCHEMA_VERSION,
        "python_version": platform.python_version(),
        "package_versions": {
            "numpy": np.__version__,
            "pydantic": pydantic_version,
        },
        "domain_py_sha256": domain_sha256,
        "private_world_sha256": {
            "A": priv_a_sha,
            "B": priv_b_sha,
            "distinct": priv_a_sha != priv_b_sha,
        },
        "public_packet_sha256": {
            "guided_diagnostic": {
                "A": pub_a_guided_sha,
                "B": pub_b_guided_sha,
                "byte_identical": pub_a_guided_sha == pub_b_guided_sha,
            },
            "unassisted": {
                "A": pub_a_unassisted_sha,
                "B": pub_b_unassisted_sha,
                "byte_identical": pub_a_unassisted_sha == pub_b_unassisted_sha,
            },
        },
        "reference_numbers": {
            "n4_t0_temp_C": float(ramp_k2["temp_t0_C"]),
            "n4_voltage_drop_exact_V": N4_VOLTAGE_DROP_EXACT_V,
            "n4_voltage_drop_quantized_V": N4_VOLTAGE_DROP_QUANTIZED_V,
            "inferred_r0_ohm": float(r0_inferred),
            "fitted_h_W_per_K": float(fitted["h_W_per_K"]),
            "fitted_C_J_per_K": float(fitted["C_J_per_K"]),
            "fitted_max_residual": float(fitted["max_residual"]),
            "case_A": {
                "safe_receiver": priv_a["safe_receiver"],
                "wrong_receiver": priv_a["wrong_receiver"],
                "no_action_n4_t180_C": priv_a["reference_outcomes"]["no_action"]["n4_temp_t180_C"],
                "safe_n4_t180_C": priv_a["reference_outcomes"]["safe_receiver"]["n4_temp_t180_C"],
                "safe_pdu_currents_A": priv_a["reference_outcomes"]["safe_receiver"]["pdu_currents_A"],
                "wrong_pdu_currents_A": priv_a["reference_outcomes"]["wrong_receiver"]["pdu_currents_A"],
            },
            "case_B": {
                "safe_receiver": priv_b["safe_receiver"],
                "wrong_receiver": priv_b["wrong_receiver"],
                "no_action_n4_t180_C": priv_b["reference_outcomes"]["no_action"]["n4_temp_t180_C"],
                "safe_n4_t180_C": priv_b["reference_outcomes"]["safe_receiver"]["n4_temp_t180_C"],
                "safe_pdu_currents_A": priv_b["reference_outcomes"]["safe_receiver"]["pdu_currents_A"],
                "wrong_pdu_currents_A": priv_b["reference_outcomes"]["wrong_receiver"]["pdu_currents_A"],
            },
        },
    }

    if output_path is not None:
        out_p = Path(output_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return manifest


# ---------------------------------------------------------------------------
# Backward-Compatible Phase 00 / Downstream Bundle Helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublicCaseEvidence:
    """Publicly observable evidence provided to Investigator and Auditor before lock."""

    case_id: str
    facility_name: str
    nominal_voltage_v: float
    ambient_temp_c: float
    thermal_resistance_c_per_w: float
    max_safe_junction_temp_c: float
    max_safe_feeder_current_a: float
    node_load_currents_a: Dict[str, float]
    shared_plenum_alarm: Dict[str, Any]
    sld_image_path: str
    scene_3d_image_path: str


@dataclass(frozen=True)
class SealedWorldTruth:
    """Private ground truth kept sealed in artifacts/private/ during agent execution."""

    case_id: str
    node_to_feeder: Dict[str, str]
    feeder_to_node: Dict[str, str]
    node_resistances_ohm: Dict[str, float]
    degraded_node_id: str
    degraded_feeder_id: str
    degraded_resistance_ohm: float
    true_max_safe_current_a: float
    node_states: Dict[str, Dict[str, Any]]
    truth_sha256: str = ""


def build_shared_sensory_telemetry() -> Dict[str, Any]:
    """Return the shared public sensory evidence identical across Case A and Case B."""
    return {
        "facility_name": "BlackboxDC Hall-4 Redundant PDU Cell",
        "nominal_voltage_v": NOMINAL_VOLTAGE_V,
        "ambient_temp_c": AMBIENT_TEMP_C,
        "thermal_resistance_c_per_w": THERMAL_RESISTANCE_C_PER_W,
        "max_safe_junction_temp_c": MAX_SAFE_JUNCTION_TEMP_C,
        "max_safe_feeder_current_a": MAX_SAFE_FEEDER_CURRENT_A,
        "node_load_currents_a": {
            "N1": 80.0,
            "N2": 95.0,
            "N3": 95.0,
            "N4": 75.0,
        },
        "shared_plenum_alarm": {
            "aisle_zone": "ZONE_B_N2_N3",
            "candidate_nodes": ["N2", "N3"],
            "observed_peak_terminal_temp_c": 135.2,
            "alarm_state": "CRITICAL_OVERHEAT",
            "note": (
                "Shared overhead IR sensor spans racks N2 and N3. "
                "Inspect 3D thermal render to localize rack hotspot, SLD for feeder "
                "routing, and run active voltage-drop probe to quantify joint resistance."
            ),
        },
    }


def generate_case_bundle(
    case_id: str,
    artifacts_root: Optional[Path] = None,
) -> Tuple[PublicCaseEvidence, SealedWorldTruth]:
    """Generate paired public evidence and sealed truth for case_A or case_B."""
    _, canonical_id = _normalize_network_case_id(case_id)

    shared = build_shared_sensory_telemetry()
    currents: Dict[str, float] = dict(shared["node_load_currents_a"])

    if canonical_id == "case_A":
        node_to_feeder = {"N1": "F1", "N2": "F2", "N3": "F3", "N4": "F4"}
    else:
        node_to_feeder = {"N1": "F1", "N2": "F3", "N3": "F2", "N4": "F4"}

    feeder_to_node = {f: n for n, f in node_to_feeder.items()}
    degraded_node_id = "N2"
    degraded_feeder_id = node_to_feeder[degraded_node_id]

    node_resistances_ohm: Dict[str, float] = {}
    node_states: Dict[str, Dict[str, Any]] = {}
    for node_id, feeder_id in sorted(node_to_feeder.items()):
        if node_id == degraded_node_id:
            r_ohm = DEGRADED_JOINT_RESISTANCE_OHM
        else:
            r_ohm = NOMINAL_FEEDER_RESISTANCE_OHM[feeder_id]
        node_resistances_ohm[node_id] = r_ohm
        st = evaluate_node_state(
            node_id=node_id,
            feeder_id=feeder_id,
            load_current_a=currents[node_id],
            resistance_ohm=r_ohm,
            ambient_temp_c=shared["ambient_temp_c"],
        )
        node_states[node_id] = asdict(st)

    true_max_safe_i = float(node_states[degraded_node_id]["max_safe_current_a"])

    root = Path(artifacts_root) if artifacts_root else Path("artifacts")
    sld_path = str(root / canonical_id / f"sld_{canonical_id}.png")
    scene_path = str(root / "public" / "scene_3d_shared.png")

    public_ev = PublicCaseEvidence(
        case_id=canonical_id,
        facility_name=shared["facility_name"],
        nominal_voltage_v=shared["nominal_voltage_v"],
        ambient_temp_c=shared["ambient_temp_c"],
        thermal_resistance_c_per_w=shared["thermal_resistance_c_per_w"],
        max_safe_junction_temp_c=shared["max_safe_junction_temp_c"],
        max_safe_feeder_current_a=shared["max_safe_feeder_current_a"],
        node_load_currents_a=currents,
        shared_plenum_alarm=shared["shared_plenum_alarm"],
        sld_image_path=sld_path,
        scene_3d_image_path=scene_path,
    )

    raw_truth = {
        "case_id": canonical_id,
        "node_to_feeder": node_to_feeder,
        "feeder_to_node": feeder_to_node,
        "node_resistances_ohm": node_resistances_ohm,
        "degraded_node_id": degraded_node_id,
        "degraded_feeder_id": degraded_feeder_id,
        "degraded_resistance_ohm": DEGRADED_JOINT_RESISTANCE_OHM,
        "true_max_safe_current_a": true_max_safe_i,
        "node_states": node_states,
    }
    truth_hash = _compute_sha256(raw_truth)
    sealed_truth = SealedWorldTruth(**raw_truth, truth_sha256=truth_hash)
    return public_ev, sealed_truth


def write_case_fixtures(
    artifacts_root: Path,
) -> Dict[str, Tuple[PublicCaseEvidence, SealedWorldTruth]]:
    """Materialize public and private JSON fixtures for Case A and Case B on disk."""
    artifacts_root = Path(artifacts_root)
    pub_dir = artifacts_root / "public"
    priv_dir = artifacts_root / "private"
    pub_dir.mkdir(parents=True, exist_ok=True)
    priv_dir.mkdir(parents=True, exist_ok=True)

    results: Dict[str, Tuple[PublicCaseEvidence, SealedWorldTruth]] = {}
    for cid in ("case_A", "case_B"):
        case_dir = artifacts_root / cid
        case_dir.mkdir(parents=True, exist_ok=True)
        pub, priv = generate_case_bundle(cid, artifacts_root=artifacts_root)
        results[cid] = (pub, priv)

        pub_file = pub_dir / f"public_{cid}.json"
        pub_file.write_text(json.dumps(asdict(pub), indent=2, sort_keys=True) + "\n", encoding="utf-8")

        priv_file = priv_dir / f"sealed_truth_{cid}.json"
        priv_file.write_text(json.dumps(asdict(priv), indent=2, sort_keys=True) + "\n", encoding="utf-8")

    shared_file = pub_dir / "shared_telemetry.json"
    shared_file.write_text(
        json.dumps(build_shared_sensory_telemetry(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return results
