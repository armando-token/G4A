"""Unit tests for Twin Repair Phase B1 & Phase C contracts, schemas, verifier, and §4.C4 mutations."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict

import pytest

from twin_repair.contracts import (
    CANONICAL_EDGES_A,
    CANONICAL_EDGES_B,
    ImageManifest,
    apply_patch,
    build_canonical_scene_spec,
    build_golden_repair_patch,
    extract_feeder_graph,
    validate_repair_patch,
    validate_scene_spec,
)
from twin_repair.verifier import score_graph, score_twin_repair_run


SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "twin_repair" / "schemas"


def _assert_all_objects_forbid_additional_properties(node: Any, path: str = "root") -> None:
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            assert node.get("additionalProperties") is False, f"{path} missing additionalProperties: false"
        for k, v in node.items():
            _assert_all_objects_forbid_additional_properties(v, f"{path}.{k}")
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            _assert_all_objects_forbid_additional_properties(item, f"{path}[{idx}]")


def test_json_schemas_strict_draft_2020_12() -> None:
    scene_schema_path = SCHEMAS_DIR / "scene_spec_v1.schema.json"
    patch_schema_path = SCHEMAS_DIR / "repair_patch_v1.schema.json"
    assert scene_schema_path.is_file()
    assert patch_schema_path.is_file()

    scene_schema = json.loads(scene_schema_path.read_text(encoding="utf-8"))
    patch_schema = json.loads(patch_schema_path.read_text(encoding="utf-8"))

    assert "2020-12" in scene_schema["$schema"]
    assert "2020-12" in patch_schema["$schema"]
    _assert_all_objects_forbid_additional_properties(scene_schema, "scene_spec_v1")
    _assert_all_objects_forbid_additional_properties(patch_schema, "repair_patch_v1")


def test_validate_scene_spec_canonical_and_rejections() -> None:
    scene_a = build_canonical_scene_spec(case_id="drift_A_to_B", source_revision="A")
    assert extract_feeder_graph(scene_a) == CANONICAL_EDGES_A
    assert len(scene_a.sha256()) == 64

    base = scene_a.to_dict()

    # 1. Unknown top-level key
    bad_extra = copy.deepcopy(base)
    bad_extra[" rogue_key"] = 1
    with pytest.raises(ValueError, match="unknown/disallowed keys"):
        validate_scene_spec(bad_extra)

    # 2. NaN / Infinity
    bad_nan_json = json.dumps(base).replace("88.95030198217694", "NaN")
    with pytest.raises(ValueError, match="Non-finite"):
        validate_scene_spec(bad_nan_json)

    # 3. Boolean where number is required
    bad_bool = copy.deepcopy(base)
    bad_bool["racks"][0]["width_m"] = True
    with pytest.raises(ValueError, match="finite number"):
        validate_scene_spec(bad_bool)

    # 4. Duplicate rack ID
    bad_dup = copy.deepcopy(base)
    bad_dup["racks"][1]["rack_id"] = "N1"
    with pytest.raises(ValueError, match="Duplicate rack_id"):
        validate_scene_spec(bad_dup)

    # 5. Out-of-bounds rack coordinate
    bad_coord = copy.deepcopy(base)
    bad_coord["racks"][2]["center_x_m"] = 2.95
    with pytest.raises(ValueError, match="deviates from canonical position"):
        validate_scene_spec(bad_coord)

    # 6. Tampered feeder limit_a
    bad_limit = copy.deepcopy(base)
    bad_limit["feeders"][0]["limit_a"] = 60.0
    with pytest.raises(ValueError, match="does not match trusted limit"):
        validate_scene_spec(bad_limit)

    # 7. Invalid camera pose
    bad_pose = copy.deepcopy(base)
    bad_pose["viewport"]["camera_pose"] = "front_glamour_shot"
    with pytest.raises(ValueError, match="camera_pose"):
        validate_scene_spec(bad_pose)


def test_golden_patch_application_and_scoring() -> None:
    scene0 = build_canonical_scene_spec(case_id="drift_A_to_B", source_revision="A", edges=CANONICAL_EDGES_A)
    scene0_sha = scene0.sha256()

    patch = build_golden_repair_patch(scene0, decision="repair")
    assert patch.decision == "repair"
    assert len(patch.operations) == 2

    scene1 = apply_patch(scene0, patch)
    # scene0 is completely unmutated
    assert scene0.sha256() == scene0_sha
    assert extract_feeder_graph(scene0) == CANONICAL_EDGES_A
    # scene1 has updated edges and preserves source_revision='A'
    assert extract_feeder_graph(scene1) == CANONICAL_EDGES_B
    assert scene1.source_revision == "A"
    assert scene1.sha256() != scene0_sha

    sealed_truth = {
        "old_edges": dict(CANONICAL_EDGES_A),
        "current_edges": dict(CANONICAL_EDGES_B),
        "expected_old_receiver": "N3",
        "expected_new_receiver": "N2",
    }
    s0 = score_graph(scene0, sealed_truth)
    s1 = score_graph(scene1, sealed_truth)
    assert (s0.correct_edges, s0.total_edges) == (2, 4)
    assert (s1.correct_edges, s1.total_edges) == (4, 4)

    run_score = score_twin_repair_run(
        scene0,
        scene1,
        render0_info={"sha256": "aaa"},
        render1_info={"sha256": "bbb", "cable_pixels_changed": True},
        sealed_truth=sealed_truth,
        patch=patch,
    )
    assert run_score["passed"] is True
    assert run_score["graph_score_before"] == "2/4"
    assert run_score["graph_score_after"] == "4/4"
    assert run_score["stale_recommended_receiver"] == "N3"
    assert run_score["repaired_recommended_receiver"] == "N2"
    assert run_score["recommendation_flipped"] is True


def test_section_4c4_all_14_negative_mutations_fail_closed() -> None:
    """Verify every single mutation listed in §4.C4 fails closed while leaving scene0 intact."""
    scene0 = build_canonical_scene_spec(case_id="drift_A_to_B", source_revision="A", edges=CANONICAL_EDGES_A)
    scene0_sha = scene0.sha256()
    golden = build_golden_repair_patch(scene0, decision="repair").to_dict()
    manifest = ImageManifest.default(include_corridors=True, expected_target_revision="B")
    sealed_truth = {"current_edges": dict(CANONICAL_EDGES_B), "expected_new_receiver": "N2"}

    # 1. Unchanged repair patch (empty operations with decision='repair')
    m1 = copy.deepcopy(golden)
    m1["operations"] = []
    m1["discrepancies"] = []
    with pytest.raises(ValueError, match="requires at least one validated operation"):
        validate_repair_patch(m1, scene0, manifest)

    # 2. Inverse patch (from='F2', to='F1' on N2 when N2 is currently F1)
    m2 = copy.deepcopy(golden)
    m2["operations"][0] = {"op": "replace_feeder", "rack_id": "N2", "from": "F2", "to": "F1"}
    m2["discrepancies"][0]["observed_feeder"] = "F2"
    m2["discrepancies"][0]["required_feeder"] = "F1"
    with pytest.raises(ValueError, match="does not match current scene|Stale 'from'"):
        validate_repair_patch(m2, scene0, manifest)

    # 3. N4 moved to wrong feeder (lowers graph accuracy instead of improving to 4/4)
    m3 = {
        "schema_version": "repair_patch.v1",
        "case_id": scene0.case_id,
        "basis_scene_sha256": scene0_sha,
        "target_revision_seen": "B",
        "observed_render_edges": [
            {"rack_id": "N4", "feeder_id": "F2", "image_ref": "render0_wiring.png", "bbox_xyxy_px": [900, 150, 1120, 420]}
        ],
        "target_sld_edges": [
            {"rack_id": "N4", "feeder_id": "F1", "image_ref": "current_sld_B.png", "bbox_xyxy_px": [920, 200, 1020, 420]}
        ],
        "discrepancies": [
            {
                "rack_id": "N4",
                "observed_feeder": "F2",
                "required_feeder": "F1",
                "render_evidence_bbox_xyxy_px": [900, 150, 1120, 420],
                "sld_evidence_bbox_xyxy_px": [920, 200, 1020, 420],
                "reason": "Bogus N4 move to F1",
            }
        ],
        "operations": [{"op": "replace_feeder", "rack_id": "N4", "from": "F2", "to": "F1"}],
        "confidence": 0.5,
        "decision": "repair",
    }
    bad_scene1 = apply_patch(scene0, validate_repair_patch(m3, scene0, manifest))
    res_m3 = score_twin_repair_run(scene0, bad_scene1, sealed_truth=sealed_truth)
    assert res_m3["after_correct_edges"] == 1
    assert res_m3["graph_improved"] is False
    assert res_m3["passed"] is False

    # 4. Duplicate rack in operations
    m4 = copy.deepcopy(golden)
    m4["operations"].append({"op": "replace_feeder", "rack_id": "N2", "from": "F1", "to": "F2"})
    with pytest.raises(ValueError, match="Duplicate"):
        validate_repair_patch(m4, scene0, manifest)

    # 5. Forged basis_scene_sha256
    m5 = copy.deepcopy(golden)
    m5["basis_scene_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="basis_scene_sha256"):
        validate_repair_patch(m5, scene0, manifest)

    # 6. Out-of-bounds or placeholder [0, 0, 1, 1] evidence box
    m6a = copy.deepcopy(golden)
    m6a["discrepancies"][0]["render_evidence_bbox_xyxy_px"] = [0, 0, 1, 1]
    with pytest.raises(ValueError, match="degenerate or placeholder"):
        validate_repair_patch(m6a, scene0, manifest)

    m6b = copy.deepcopy(golden)
    m6b["discrepancies"][0]["render_evidence_bbox_xyxy_px"] = [100, 100, 1400, 300]
    with pytest.raises(ValueError, match="exceeds maximum"):
        validate_repair_patch(m6b, scene0, manifest)

    # 7. Evidence from a file never shown to Critic
    m7 = copy.deepcopy(golden)
    m7["target_sld_edges"][0]["image_ref"] = "old_sld_A.png"
    with pytest.raises(ValueError, match="Unwhitelisted|current SLD"):
        validate_repair_patch(m7, scene0, manifest)

    # 8. Extra unknown JSON key
    m8 = copy.deepcopy(golden)
    m8["extra_secret"] = "leak"
    with pytest.raises(ValueError, match="unknown/disallowed keys"):
        validate_repair_patch(m8, scene0, manifest)

    # 9. Non-finite numeric (Infinity / NaN)
    m9_str = json.dumps(golden).replace("0.94", "Infinity")
    with pytest.raises(ValueError, match="Non-finite"):
        validate_repair_patch(m9_str, scene0, manifest)

    # 10. Path traversal in image_ref
    m10 = copy.deepcopy(golden)
    m10["observed_render_edges"][0]["image_ref"] = "../private/truth.json"
    with pytest.raises(ValueError, match="path traversal"):
        validate_repair_patch(m10, scene0, manifest)

    # 11. Wrong case_id or target_revision_seen
    m11a = copy.deepcopy(golden)
    m11a["case_id"] = "wrong_case_id"
    with pytest.raises(ValueError, match="does not match SceneSpec.case_id"):
        validate_repair_patch(m11a, scene0, manifest)

    m11b = copy.deepcopy(golden)
    m11b["target_revision_seen"] = "A"
    with pytest.raises(ValueError, match="does not match expected revision"):
        validate_repair_patch(m11b, scene0, manifest)

    # 12. decision='abstain'
    m12_with_ops = copy.deepcopy(golden)
    m12_with_ops["decision"] = "abstain"
    with pytest.raises(ValueError, match="requires empty operations"):
        validate_repair_patch(m12_with_ops, scene0, manifest)

    abstain_patch = build_golden_repair_patch(scene0, decision="abstain")
    with pytest.raises(ValueError, match="decision='abstain'"):
        apply_patch(scene0, abstain_patch)

    # 13. Stale 'from' feeder (operation 'from' does not match current scene edge)
    m13a = copy.deepcopy(golden)
    m13a["operations"][0] = {"op": "replace_feeder", "rack_id": "N2", "from": "F2", "to": "F1"}
    with pytest.raises(ValueError, match="Stale 'from'"):
        validate_repair_patch(m13a, scene0, manifest)

    scene1 = apply_patch(scene0, validate_repair_patch(golden, scene0, manifest))
    m13b = copy.deepcopy(golden)
    m13b["basis_scene_sha256"] = scene1.sha256()
    with pytest.raises(ValueError, match="rendered scene has|does not match|Stale 'from'"):
        validate_repair_patch(m13b, scene1, manifest)

    # 14. Malformed JSON syntax
    with pytest.raises(ValueError, match="Malformed JSON"):
        validate_repair_patch("{not_valid_json:", scene0, manifest)

    # Verify scene0 was never mutated across all 14 negative tests
    assert scene0.sha256() == scene0_sha


def test_control_a_to_a_no_change_and_rejects_canned_swap() -> None:
    scene_control = build_canonical_scene_spec(case_id="control_A_to_A", source_revision="A", edges=CANONICAL_EDGES_A)
    no_change_patch = build_golden_repair_patch(scene_control, decision="no_change")
    assert no_change_patch.decision == "no_change"
    assert len(no_change_patch.operations) == 0

    scene_after = apply_patch(scene_control, no_change_patch)
    assert scene_after.sha256() == scene_control.sha256()

    control_truth = {
        "old_edges": dict(CANONICAL_EDGES_A),
        "current_edges": dict(CANONICAL_EDGES_A),
        "expected_old_receiver": "N3",
        "expected_new_receiver": "N3",
    }
    # If an agent tries to apply the canned N2/N3 swap on the A->A control case, graph accuracy drops 4/4 -> 2/4 and fails!
    canned_swap = build_golden_repair_patch(scene_control, decision="repair")
    corrupted_scene = apply_patch(scene_control, canned_swap)
    res_corrupted = score_twin_repair_run(scene_control, corrupted_scene, sealed_truth=control_truth)
    assert res_corrupted["before_correct_edges"] == 4
    assert res_corrupted["after_correct_edges"] == 2
    assert res_corrupted["graph_improved"] is False
    assert res_corrupted["passed"] is False
