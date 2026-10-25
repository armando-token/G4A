"""Synthetic electrical & thermal data-center case generator for BlackboxDC.

Generates paired worlds Case A and Case B where N2 and N3 feeder assignments
are swapped in the Single-Line Diagram (SLD) while shared public sensory
evidence (telemetry table, 3D rack render parameters) remains identical and
private ground truth remains cryptographically sealed until locks complete.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from blackbox.domain import (
    AMBIENT_TEMP_C,
    DEGRADED_JOINT_RESISTANCE_OHM,
    MAX_SAFE_FEEDER_CURRENT_A,
    MAX_SAFE_JUNCTION_TEMP_C,
    NOMINAL_FEEDER_RESISTANCE_OHM,
    NOMINAL_VOLTAGE_V,
    THERMAL_RESISTANCE_C_PER_W,
    evaluate_node_state,
)


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


def _compute_sha256(payload: Dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


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
    """Generate paired public evidence and sealed truth for case_A or case_B.

    In Case A:
        N1 -> F1, N2 -> F2, N3 -> F3, N4 -> F4
        Degraded high-resistance lug is at node N2 (therefore feeder F2).
    In Case B:
        N1 -> F1, N2 -> F3, N3 -> F2, N4 -> F4 (N2/N3 feeders swapped in SLD)
        Degraded high-resistance lug is at node N2 (therefore feeder F3).
    """
    norm_id = case_id.strip()
    if norm_id not in ("case_A", "case_B", "A", "B"):
        raise ValueError(f"Unsupported case_id: {case_id!r}. Expected 'case_A' or 'case_B'.")
    canonical_id = "case_A" if norm_id in ("case_A", "A") else "case_B"

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
