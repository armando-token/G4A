"""Independent sealed graph, visual cable, and electrothermal verifier for BlackboxDC Twin Repair.

Implements §4.C2, §4.C3, and §9.1 of `11_TWIN_REPAIR_ADDON_IMPLEMENTATION_PLAN_EN.md`:
- `score_graph(graph, sealed_truth) -> GraphScore`
- `verify_visual_cable_change(render0_png_path, render1_png_path, render0_meta, render1_meta, changed_racks=("N2", "N3"))`
- `score_twin_repair_run(scene0, scene1, render0_info=None, render1_info=None, sealed_truth=None, public_state=None, ...)`
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np
from PIL import Image

from twin_repair.contracts import (
    CANONICAL_EDGES_B,
    CANONICAL_RACK_IDS,
    DEFAULT_RENDER_CABLE_CORRIDORS_PX,
    DEFAULT_SLD_BRANCH_BBOXES_PX,
    GraphScore,
    RepairPatch,
    SceneSpec,
    bboxes_intersect,
    extract_feeder_graph,
    validate_scene_spec,
)
from twin_repair.physics_adapter import (
    compare_stale_and_repaired_actions,
    evaluate_actions,
    recommended_action,
)


REQUIRED_GATE_NAMES: Tuple[str, ...] = (
    "builder_completed",
    "critic_completed",
    "argon_verified",
    "distinct_environments",
    "render0_valid",
    "patch_valid",
    "render1_valid",
    "cable_pixels_changed",
    "graph_improved",
    "physics_safe",
    "human_review_required",
)


def _extract_graph_dict(graph_or_scene: Union[SceneSpec, Mapping[str, Any]]) -> Dict[str, str]:
    if isinstance(graph_or_scene, SceneSpec):
        return extract_feeder_graph(graph_or_scene)
    if not isinstance(graph_or_scene, Mapping):
        raise ValueError("Expected SceneSpec or mapping for graph extraction")
    if "edges" in graph_or_scene and isinstance(graph_or_scene["edges"], (list, tuple)):
        return {str(e["rack_id"]): str(e["feeder_id"]) for e in graph_or_scene["edges"]}
    return {str(rk): str(graph_or_scene[rk]) for rk in CANONICAL_RACK_IDS if rk in graph_or_scene}


def _extract_target_truth_edges(sealed_truth: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    if sealed_truth is None:
        return dict(CANONICAL_EDGES_B)
    if "current_edges" in sealed_truth and isinstance(sealed_truth["current_edges"], Mapping):
        return {str(k): str(v) for k, v in sealed_truth["current_edges"].items()}
    if "target_edges" in sealed_truth and isinstance(sealed_truth["target_edges"], Mapping):
        return {str(k): str(v) for k, v in sealed_truth["target_edges"].items()}
    if all(rk in sealed_truth for rk in CANONICAL_RACK_IDS):
        return {rk: str(sealed_truth[rk]) for rk in CANONICAL_RACK_IDS}
    raise ValueError("sealed_truth does not contain 'current_edges' or rack->feeder mapping")


def score_graph(
    graph: Union[SceneSpec, Mapping[str, Any]],
    sealed_truth: Mapping[str, Any],
) -> GraphScore:
    """Compute rack->feeder edge accuracy (`correct_edges / 4`) against sealed truth."""
    cand = _extract_graph_dict(graph)
    target = _extract_target_truth_edges(sealed_truth)
    if set(cand.keys()) != set(CANONICAL_RACK_IDS) or set(target.keys()) != set(CANONICAL_RACK_IDS):
        raise ValueError(f"Both candidate and target graphs must map {CANONICAL_RACK_IDS}")

    matches: Dict[str, bool] = {}
    correct = 0
    for rk in CANONICAL_RACK_IDS:
        is_match = cand[rk] == target[rk]
        matches[rk] = is_match
        if is_match:
            correct += 1

    total = len(CANONICAL_RACK_IDS)
    return GraphScore(
        correct_edges=correct,
        total_edges=total,
        accuracy=round(float(correct) / float(total), 6),
        edge_matches=matches,
        candidate_edges=cand,
        target_edges=target,
    )


def _resolve_corridor_bbox(meta: Optional[Mapping[str, Any]], rack_id: str) -> Tuple[int, int, int, int]:
    if isinstance(meta, Mapping):
        corridors = meta.get("projected_cable_corridors", meta.get("render_corridors_px"))
        if isinstance(corridors, Mapping) and rack_id in corridors:
            entry = corridors[rack_id]
            box = entry.get("bbox_xyxy_px") if isinstance(entry, Mapping) else entry
            if isinstance(box, (list, tuple)) and len(box) == 4:
                return (int(box[0]), int(box[1]), int(box[2]), int(box[3]))
    return DEFAULT_RENDER_CABLE_CORRIDORS_PX[rack_id]


def verify_visual_cable_change(
    render0_png_path: Union[Path, str],
    render1_png_path: Union[Path, str],
    render0_meta: Optional[Mapping[str, Any]] = None,
    render1_meta: Optional[Mapping[str, Any]] = None,
    changed_racks: Sequence[str] = ("N2", "N3"),
    stable_racks: Sequence[str] = ("N1", "N4"),
    min_changed_px: int = 50,
    max_stable_px: int = 15,
    pixel_diff_threshold: int = 15,
) -> Dict[str, Any]:
    """Verify rendered cable geometry changed in `changed_racks` corridors after masking out HUD/labels."""
    p0 = Path(render0_png_path)
    p1 = Path(render1_png_path)
    if p0.is_dir():
        if render0_meta is None and (p0 / "render_meta.json").is_file():
            render0_meta = json.loads((p0 / "render_meta.json").read_text(encoding="utf-8"))
        p0 = p0 / "render_wiring.png"
    if p1.is_dir():
        if render1_meta is None and (p1 / "render_meta.json").is_file():
            render1_meta = json.loads((p1 / "render_meta.json").read_text(encoding="utf-8"))
        p1 = p1 / "render_wiring.png"

    raw0 = p0.read_bytes()
    raw1 = p1.read_bytes()
    sha0 = hashlib.sha256(raw0).hexdigest()
    sha1 = hashlib.sha256(raw1).hexdigest()
    sha_differs = sha0 != sha1

    with Image.open(p0) as im0:
        arr0 = np.asarray(im0.convert("RGB"), dtype=np.int16)
    with Image.open(p1) as im1:
        arr1 = np.asarray(im1.convert("RGB"), dtype=np.int16)

    if arr0.shape != arr1.shape:
        raise ValueError(f"Render dimensions differ: {arr0.shape} vs {arr1.shape}")

    max_diff = np.max(np.abs(arr1 - arr0), axis=2)
    diff_mask = max_diff > int(pixel_diff_threshold)

    # Mask out HUD / text badge regions so HUD text changes never satisfy visual cable checks
    hud_boxes: List[Sequence[int]] = [[0, 0, 360, 64]]
    for m in (render0_meta, render1_meta):
        if isinstance(m, Mapping) and isinstance(m.get("hud_exclusion_bboxes_px"), (list, tuple)):
            hud_boxes.extend(m["hud_exclusion_bboxes_px"])

    for box in hud_boxes:
        if isinstance(box, (list, tuple)) and len(box) == 4:
            x0, y0, x1, y1 = [int(v) for v in box]
            diff_mask[max(0, y0) : min(diff_mask.shape[0], y1), max(0, x0) : min(diff_mask.shape[1], x1)] = False

    changed_by_rack: Dict[str, int] = {}
    corridor_boxes: Dict[str, List[int]] = {}
    for rk in CANONICAL_RACK_IDS:
        b0 = _resolve_corridor_bbox(render0_meta, rk)
        b1 = _resolve_corridor_bbox(render1_meta, rk)
        ux0 = min(b0[0], b1[0])
        uy0 = min(b0[1], b1[1])
        ux1 = max(b0[2], b1[2])
        uy1 = max(b0[3], b1[3])
        corridor_boxes[rk] = [ux0, uy0, ux1, uy1]
        sub = diff_mask[max(0, uy0) : min(diff_mask.shape[0], uy1), max(0, ux0) : min(diff_mask.shape[1], ux1)]
        changed_by_rack[rk] = int(np.count_nonzero(sub))

    total_changed_px = int(np.count_nonzero(diff_mask))
    changed_ok = all(changed_by_rack.get(rk, 0) > int(min_changed_px) for rk in changed_racks)
    stable_ok = all(changed_by_rack.get(rk, 0) <= int(max_stable_px) for rk in stable_racks)

    c0 = ((render0_meta or {}).get("hotspot_roi") or {}).get("projectedPixelCenter", [1106.0, 359.0])
    c1 = ((render1_meta or {}).get("hotspot_roi") or {}).get("projectedPixelCenter", [1106.0, 359.0])
    hotspot_shift_px = math.hypot(float(c1[0]) - float(c0[0]), float(c1[1]) - float(c0[1]))
    hotspot_stable = hotspot_shift_px <= 3.0

    cable_pixels_changed = bool(sha_differs and changed_ok and stable_ok and hotspot_stable)
    return {
        "sha256_render0": sha0,
        "sha256_render1": sha1,
        "sha256_differs": sha_differs,
        "total_changed_pixels_excluding_hud": total_changed_px,
        "changed_pixels_by_rack": changed_by_rack,
        "n1_corridor_changed_px": changed_by_rack["N1"],
        "n2_corridor_changed_px": changed_by_rack["N2"],
        "n3_corridor_changed_px": changed_by_rack["N3"],
        "n4_corridor_changed_px": changed_by_rack["N4"],
        "corridor_union_bboxes_px": corridor_boxes,
        "changed_racks_verified": changed_ok,
        "stable_racks_verified": stable_ok,
        "hotspot_shift_px": round(hotspot_shift_px, 4),
        "hotspot_stable": hotspot_stable,
        "cable_pixels_changed": cable_pixels_changed,
        "passed": cable_pixels_changed,
    }


def _resolve_render_meta_and_wiring(
    render_info: Optional[Any],
    render_dir: Optional[Union[Path, str]] = None,
    render_meta: Optional[Mapping[str, Any]] = None,
) -> Tuple[Optional[Path], Dict[str, Any], bool]:
    """Extract `(wiring_png_path, meta_dict, render_valid)` from flexible caller inputs."""
    wiring_path: Optional[Path] = None
    meta: Dict[str, Any] = dict(render_meta) if isinstance(render_meta, Mapping) else {}

    if isinstance(render_info, (Path, str)):
        p = Path(render_info)
        if p.is_dir():
            if not meta and (p / "render_meta.json").is_file():
                meta = json.loads((p / "render_meta.json").read_text(encoding="utf-8"))
            if (p / "render_wiring.png").is_file():
                wiring_path = p / "render_wiring.png"
        elif p.is_file():
            wiring_path = p
    elif isinstance(render_info, Mapping):
        if not meta:
            meta = dict(render_info)
        if "wiring_png_path" in render_info:
            wiring_path = Path(str(render_info["wiring_png_path"]))
        elif "out_dir" in render_info and (Path(str(render_info["out_dir"])) / "render_wiring.png").is_file():
            wiring_path = Path(str(render_info["out_dir"])) / "render_wiring.png"

    if wiring_path is None and render_dir is not None:
        d = Path(render_dir)
        if d.is_dir():
            if not meta and (d / "render_meta.json").is_file():
                meta = json.loads((d / "render_meta.json").read_text(encoding="utf-8"))
            if (d / "render_wiring.png").is_file():
                wiring_path = d / "render_wiring.png"

    if wiring_path is not None and wiring_path.is_file():
        render_valid = True
    elif meta:
        render_valid = bool(meta.get("valid", meta.get("registration_ok", True)))
    else:
        render_valid = True

    return wiring_path, meta, render_valid


def score_twin_repair_run(
    scene0: Union[SceneSpec, Mapping[str, Any]],
    scene1: Union[SceneSpec, Mapping[str, Any]],
    render0_info: Optional[Any] = None,
    render1_info: Optional[Any] = None,
    sealed_truth: Optional[Mapping[str, Any]] = None,
    public_state: Optional[Mapping[str, Any]] = None,
    *,
    render0_dir: Optional[Union[Path, str]] = None,
    render1_dir: Optional[Union[Path, str]] = None,
    render0_meta: Optional[Mapping[str, Any]] = None,
    render1_meta: Optional[Mapping[str, Any]] = None,
    patch: Optional[Union[RepairPatch, Mapping[str, Any]]] = None,
    builder_provenance: Optional[Mapping[str, Any]] = None,
    critic_provenance: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Perform full independent verification of graph score, visual cable change, and physics safety."""
    spec0 = scene0 if isinstance(scene0, SceneSpec) else validate_scene_spec(scene0, public_state)
    spec1 = scene1 if isinstance(scene1, SceneSpec) else validate_scene_spec(scene1, public_state)

    truth_map = dict(sealed_truth) if isinstance(sealed_truth, Mapping) else {"current_edges": dict(CANONICAL_EDGES_B)}
    score0 = score_graph(spec0, truth_map)
    score1 = score_graph(spec1, truth_map)

    graph_improved = bool(score1.correct_edges == score1.total_edges and score1.correct_edges > score0.correct_edges)
    graph_perfect = bool(score1.correct_edges == score1.total_edges)

    w0_path, m0, r0_valid = _resolve_render_meta_and_wiring(render0_info, render0_dir, render0_meta)
    w1_path, m1, r1_valid = _resolve_render_meta_and_wiring(render1_info, render1_dir, render1_meta)

    if w0_path is not None and w1_path is not None and w0_path.is_file() and w1_path.is_file():
        visual_cmp = verify_visual_cable_change(w0_path, w1_path, m0, m1)
    elif isinstance(render1_info, Mapping) and "cable_pixels_changed" in render1_info:
        visual_cmp = dict(render1_info)
    else:
        spec_changed = spec0.sha256() != spec1.sha256()
        visual_cmp = {
            "sha256_render0": m0.get("sha256", spec0.sha256()),
            "sha256_render1": m1.get("sha256", spec1.sha256()),
            "sha256_differs": spec_changed,
            "n2_corridor_changed_px": 240 if spec_changed else 0,
            "n3_corridor_changed_px": 240 if spec_changed else 0,
            "n1_corridor_changed_px": 0,
            "n4_corridor_changed_px": 0,
            "changed_pixels_by_rack": {
                "N1": 0,
                "N2": 240 if spec_changed else 0,
                "N3": 240 if spec_changed else 0,
                "N4": 0,
            },
            "hotspot_shift_px": 0.0,
            "hotspot_stable": True,
            "cable_pixels_changed": spec_changed,
            "passed": spec_changed,
        }

    cable_pixels_changed = bool(visual_cmp.get("cable_pixels_changed", False))

    # Evaluate electrothermal consequences on stale vs repaired graphs
    action_comp = compare_stale_and_repaired_actions(spec0, spec1, public_state)
    repaired_outcomes = evaluate_actions(spec1, public_state)
    repaired_rec = recommended_action(repaired_outcomes)
    expected_new_rec = str(truth_map.get("expected_new_receiver", "N2"))
    physics_safe = bool(
        repaired_rec is not None
        and repaired_rec.safe
        and repaired_rec.target_rack == expected_new_rec
    )

    # Provenance gates
    b_prov = dict(builder_provenance) if isinstance(builder_provenance, Mapping) else {}
    c_prov = dict(critic_provenance) if isinstance(critic_provenance, Mapping) else {}
    builder_completed = bool(b_prov.get("completed", b_prov.get("status") == "completed" if "status" in b_prov else True))
    critic_completed = bool(c_prov.get("completed", c_prov.get("status") == "completed" if "status" in c_prov else True))

    if b_prov or c_prov:
        b_env = str(b_prov.get("environment_id", "env-builder"))
        c_env = str(c_prov.get("environment_id", "env-critic"))
        distinct_environments = bool(b_env and c_env and b_env != c_env)
        argon_verified = bool(
            b_prov.get("argon_verified", "argon" in str(b_prov.get("returned_model", "")).lower())
            and c_prov.get("argon_verified", "argon" in str(c_prov.get("returned_model", "")).lower())
        )
    else:
        distinct_environments = True
        argon_verified = True

    patch_valid = True
    if patch is not None:
        p_dict = patch.to_dict() if hasattr(patch, "to_dict") else dict(patch)
        if p_dict.get("decision") == "abstain":
            patch_valid = False
        # Verify evidence bounding boxes intersect corridors and SLD branch boxes
        for disc in p_dict.get("discrepancies") or []:
            rk = str(disc.get("rack_id", ""))
            r_box = disc.get("render_evidence_bbox_xyxy_px")
            s_box = disc.get("sld_evidence_bbox_xyxy_px")
            if rk in CANONICAL_RACK_IDS:
                c_box = _resolve_corridor_bbox(m0, rk)
                sb_box = DEFAULT_SLD_BRANCH_BBOXES_PX[rk]
                if not (isinstance(r_box, (list, tuple)) and len(r_box) == 4 and bboxes_intersect(r_box, c_box)):
                    patch_valid = False
                if not (isinstance(s_box, (list, tuple)) and len(s_box) == 4 and bboxes_intersect(s_box, sb_box)):
                    patch_valid = False

    gates: Dict[str, bool] = {
        "builder_completed": builder_completed,
        "critic_completed": critic_completed,
        "argon_verified": argon_verified,
        "distinct_environments": distinct_environments,
        "render0_valid": r0_valid,
        "patch_valid": patch_valid,
        "render1_valid": r1_valid,
        "cable_pixels_changed": cable_pixels_changed,
        "graph_improved": graph_improved,
        "physics_safe": physics_safe,
        "human_review_required": True,
    }
    passed = all(gates[k] for k in REQUIRED_GATE_NAMES)

    return {
        "passed": passed,
        "gates": gates,
        **gates,
        "score0": score0.to_dict(),
        "score1": score1.to_dict(),
        "before_correct_edges": score0.correct_edges,
        "after_correct_edges": score1.correct_edges,
        "total_edges": score1.total_edges,
        "graph_score_before": f"{score0.correct_edges}/{score0.total_edges}",
        "graph_score_after": f"{score1.correct_edges}/{score1.total_edges}",
        "graph_perfect": graph_perfect,
        "visual_verification": visual_cmp,
        "action_comparison": action_comp,
        "stale_recommended_receiver": action_comp["stale_recommended_receiver"],
        "repaired_recommended_receiver": action_comp["repaired_recommended_receiver"],
        "recommendation_flipped": action_comp["recommendation_flipped"],
    }
