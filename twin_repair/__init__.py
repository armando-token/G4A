"""BlackboxDC Twin Repair extension package (More Dimensions track).

Exposes core contracts, challenge fixture generator, physics adapter, and sealed verifier.
"""

from __future__ import annotations

from typing import Any

from twin_repair.contracts import (
    ActionOutcome,
    CANONICAL_EDGES_A,
    CANONICAL_EDGES_B,
    CANONICAL_FEEDER_IDS,
    CANONICAL_FEEDER_LIMITS_A,
    CANONICAL_RACK_IDS,
    GraphScore,
    ImageManifest,
    RepairPatch,
    SceneSpec,
    apply_patch,
    build_canonical_scene_spec,
    build_golden_repair_patch,
    canonical_json_bytes,
    extract_feeder_graph,
    sha256_bytes,
    validate_repair_patch,
    validate_scene_spec,
)
from twin_repair.physics_adapter import (
    compare_stale_and_repaired_actions,
    compute_feeder_currents,
    evaluate_actions,
    recommended_action,
)
from twin_repair.verifier import (
    REQUIRED_GATE_NAMES,
    score_graph,
    score_twin_repair_run,
    verify_visual_cable_change,
)

__all__ = [
    "ActionOutcome",
    "CANONICAL_EDGES_A",
    "CANONICAL_EDGES_B",
    "CANONICAL_FEEDER_IDS",
    "CANONICAL_FEEDER_LIMITS_A",
    "CANONICAL_RACK_IDS",
    "GraphScore",
    "ImageManifest",
    "REQUIRED_GATE_NAMES",
    "RepairPatch",
    "SceneSpec",
    "apply_patch",
    "build_canonical_scene_spec",
    "build_challenge_fixture",
    "build_golden_repair_patch",
    "canonical_json_bytes",
    "compare_stale_and_repaired_actions",
    "compute_feeder_currents",
    "evaluate_actions",
    "extract_feeder_graph",
    "recommended_action",
    "reference_case",
    "scan_for_truth_leakage",
    "score_graph",
    "score_twin_repair_run",
    "sha256_bytes",
    "validate_repair_patch",
    "validate_scene_spec",
    "verify_visual_cable_change",
]


def __getattr__(name: str) -> Any:
    if name in ("build_challenge_fixture", "reference_case", "scan_for_truth_leakage"):
        from twin_repair import challenge

        return getattr(challenge, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
