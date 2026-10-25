"""Unit tests for Phase 2 SLD geometry/topology, 3D WebGL capture, hotspot alignment, and visual bundle invariants."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from blackbox.render_sld import (
    SLD_HEIGHT,
    SLD_WIDTH,
    get_case_sld_mapping,
    parse_sld_png_topology,
    parse_sld_svg_topology,
    render_sld_png,
    render_sld_svg,
)
from blackbox.scene_bridge import (
    MAX_PNG_BYTES as SCENE_MAX_PNG_BYTES,
    SCENE_HEIGHT,
    SCENE_WIDTH,
    build_phase2_visual_bundle,
    capture_3d_rear_aisle,
    get_static3d_dir,
    project_connector_bbox,
    project_world_to_pixel,
    render_deterministic_3d_scene,
    verify_thermal_hotspot_alignment,
    verify_visual_artifact,
)


def _extract_svg_text_elements(svg_text: str) -> list[tuple[str, str, str]]:
    """Extract (x, y, text) tuples from all <text> elements in an SVG."""
    root = ET.fromstring(svg_text)
    items: list[tuple[str, str, str]] = []
    for elem in root.iter():
        if elem.tag.split("}")[-1] == "text":
            items.append(
                (
                    elem.attrib.get("x", ""),
                    elem.attrib.get("y", ""),
                    (elem.text or "").strip(),
                )
            )
    return items


def test_sld_geometry_topology_and_svg_text_invariance(tmp_path: Path) -> None:
    """Verify Case A and Case B SLD graph edges via OCR-agnostic SVG & PNG geometry parsers and text invariance."""
    expected_map_a = {"N1": "F1", "N2": "F1", "N3": "F2", "N4": "F2"}
    expected_map_b = {"N1": "F1", "N2": "F2", "N3": "F1", "N4": "F2"}
    expected_edges_a = {"F1-N1", "F1-N2", "F2-N3", "F2-N4"}
    expected_edges_b = {"F1-N1", "F1-N3", "F2-N2", "F2-N4"}

    assert get_case_sld_mapping("A") == expected_map_a
    assert get_case_sld_mapping("case_A") == expected_map_a
    assert get_case_sld_mapping("B") == expected_map_b
    assert get_case_sld_mapping("case_B") == expected_map_b

    svg_a = render_sld_svg("A")
    svg_b = render_sld_svg("B")
    assert svg_a != svg_b

    # All <text> elements in SVG A and SVG B must be 100% identical
    texts_a = _extract_svg_text_elements(svg_a)
    texts_b = _extract_svg_text_elements(svg_b)
    assert len(texts_a) > 0
    assert texts_a == texts_b

    # Verify required title/footer disclaimers and absence of case labels
    assert "ACTIVE REVISION" in svg_a
    assert "TRAINING / SIMULATION — NOT FOR CONSTRUCTION" in svg_a
    assert "CASE A" not in svg_a.upper()
    assert "CASE B" not in svg_b.upper()

    # OCR-agnostic SVG topology verification
    svg_top_a = parse_sld_svg_topology(svg_a)
    svg_top_b = parse_sld_svg_topology(svg_b)
    assert svg_top_a["mapping"] == expected_map_a
    assert svg_top_a["edges"] == expected_edges_a
    assert svg_top_a["arches_over_F2"] == ["N1", "N2"]

    assert svg_top_b["mapping"] == expected_map_b
    assert svg_top_b["edges"] == expected_edges_b
    assert svg_top_b["arches_over_F2"] == ["N1", "N3"]

    # Render PNGs and check dimensions, mode, size < 512 KiB, and OCR-agnostic PNG pixel topology
    sld_a1_path = tmp_path / "case_A" / "sld.png"
    sld_a2_path = tmp_path / "case_A" / "sld_repeat.png"
    sld_b_path = tmp_path / "case_B" / "sld.png"

    meta_a1 = render_sld_png("A", sld_a1_path)
    meta_a2 = render_sld_png("case_A", sld_a2_path)
    meta_b = render_sld_png("B", sld_b_path)

    assert meta_a1["sha256"] == meta_a2["sha256"]
    assert meta_a1["sha256"] != meta_b["sha256"]

    for p in (sld_a1_path, sld_b_path):
        info = verify_visual_artifact(p)
        assert info["width"] == 1280
        assert info["height"] == 800
        assert info["mode"] == "RGB"
        assert info["size_bytes"] < SCENE_MAX_PNG_BYTES

    png_top_a = parse_sld_png_topology(sld_a1_path)
    png_top_b = parse_sld_png_topology(sld_b_path)
    assert png_top_a["mapping"] == expected_map_a
    assert png_top_a["edges"] == expected_edges_a
    assert png_top_b["mapping"] == expected_map_b
    assert png_top_b["edges"] == expected_edges_b


def test_camera_projection_and_connector_bbox() -> None:
    """Verify pinhole projection of N4 fault connector center (2.57, 1.2, 0.555) and bounding box."""
    px, py = project_world_to_pixel((2.57, 1.2, 0.555))
    assert px == pytest.approx(1110.36, abs=0.02)
    assert py == pytest.approx(358.99, abs=0.02)

    bbox = project_connector_bbox()
    assert bbox == [1079, 325, 1143, 393]


def test_capture_3d_rear_aisle_and_hotspot_alignment(tmp_path: Path) -> None:
    """Verify 3D WebGL capture dimensions, RGB mode, recapture determinism, and <=3.0px thermal hotspot alignment."""
    cap_dir = tmp_path / "capture"
    res = capture_3d_rear_aisle(cap_dir, verify_recapture=True)

    assert res["recapture_verified"] is True
    for key in ("rgb", "thermal"):
        info = res[key]
        assert info["width"] == 1280
        assert info["height"] == 800
        assert info["mode"] == "RGB"
        assert info["size_bytes"] < SCENE_MAX_PNG_BYTES

    rear_thermal_path = Path(res["rear_aisle_thermal_path"])
    assert rear_thermal_path.exists()

    alignment = verify_thermal_hotspot_alignment(rear_thermal_path)
    assert alignment["aligned"] is True
    assert alignment["pixel_distance"] <= 3.0
    assert 0.0 < alignment["hot_area_fraction"] < 0.03


def test_build_phase2_visual_bundle_and_five_cross_case_invariants(tmp_path: Path) -> None:
    """Verify build_phase2_visual_bundle enforces all 5 cross-case SHA-256 invariants and writes input_manifest.json."""
    artifacts_root = tmp_path / "artifacts"
    manifest = build_phase2_visual_bundle(artifacts_root, reviewer_initials="AGY")

    input_manifest_path = artifacts_root / "input_manifest.json"
    assert input_manifest_path.exists()
    on_disk = json.loads(input_manifest_path.read_text(encoding="utf-8"))
    assert on_disk == manifest

    assert manifest["phase"] == "02_VISUAL_ASSETS"
    assert manifest["degraded_2d_fallback"] is False
    assert manifest["human_inspection"]["checklist_passed"] is True

    def _sha(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    case_a = artifacts_root / "case_A"
    case_b = artifacts_root / "case_B"

    # Verify all 5 cross-case SHA-256 invariants directly from disk bytes
    assert _sha(case_a / "rgb.png") == _sha(case_b / "rgb.png")
    assert _sha(case_a / "thermal.png") == _sha(case_b / "thermal.png")
    assert _sha(case_a / "sld.png") != _sha(case_b / "sld.png")
    assert _sha(case_a / "telemetry.json") == _sha(case_b / "telemetry.json")
    assert _sha(case_a / "records.json") == _sha(case_b / "records.json")

    assert all(manifest["cross_case_invariants"].values())


def test_shared_3d_scene_byte_identical_across_runs(tmp_path: Path) -> None:
    """Verify backward-compatible shared 3D helper produces byte-identical renders."""
    scene_a_path = tmp_path / "case_A" / "scene_3d_shared.png"
    scene_b_path = tmp_path / "case_B" / "scene_3d_shared.png"

    res_a = render_deterministic_3d_scene(scene_a_path)
    res_b = render_deterministic_3d_scene(scene_b_path)

    assert res_a["sha256"] == res_b["sha256"]
    assert scene_a_path.read_bytes() == scene_b_path.read_bytes()
    assert res_a["width"] == SCENE_WIDTH
    assert res_a["height"] == SCENE_HEIGHT

    verified = verify_visual_artifact(scene_a_path)
    assert verified["exists"] is True
    assert verified["sha256"] == res_a["sha256"]


def test_static3d_scaffold_files_exist() -> None:
    """Verify the static3d Three.js + Playwright scaffold files are present."""
    static3d_dir = get_static3d_dir()
    assert static3d_dir.exists()
    assert (static3d_dir / "package.json").exists()
    assert (static3d_dir / "scene.html").exists()
    assert (static3d_dir / "scene.js").exists()
    assert (static3d_dir / "capture.mjs").exists()
