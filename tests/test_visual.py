"""Unit tests for deterministic SLD rendering and shared 3D visual scene generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from blackbox.render_sld import (
    SLD_HEIGHT,
    SLD_WIDTH,
    get_case_sld_mapping,
    render_sld_png,
)
from blackbox.scene_bridge import (
    SCENE_HEIGHT,
    SCENE_WIDTH,
    get_static3d_dir,
    render_deterministic_3d_scene,
    verify_visual_artifact,
)


def test_sld_mapping_and_distinct_renders_for_case_a_and_b(tmp_path: Path) -> None:
    """Verify SLD A and SLD B are deterministic and differ due to N2/N3 feeder swap."""
    map_a = get_case_sld_mapping("case_A")
    map_b = get_case_sld_mapping("case_B")
    assert map_a == {"N1": "F1", "N2": "F2", "N3": "F3", "N4": "F4"}
    assert map_b == {"N1": "F1", "N2": "F3", "N3": "F2", "N4": "F4"}

    sld_a1_path = tmp_path / "case_A" / "sld_a1.png"
    sld_a2_path = tmp_path / "case_A" / "sld_a2.png"
    sld_b_path = tmp_path / "case_B" / "sld_b.png"

    meta_a1 = render_sld_png("case_A", sld_a1_path)
    meta_a2 = render_sld_png("A", sld_a2_path)
    meta_b = render_sld_png("case_B", sld_b_path)

    # Same case renders must be byte-identical
    assert meta_a1["sha256"] == meta_a2["sha256"]
    assert sld_a1_path.read_bytes() == sld_a2_path.read_bytes()

    # Case A and Case B SLD renders must be distinct
    assert meta_a1["sha256"] != meta_b["sha256"]
    assert sld_a1_path.read_bytes() != sld_b_path.read_bytes()

    info_a = verify_visual_artifact(sld_a1_path)
    info_b = verify_visual_artifact(sld_b_path)
    assert info_a["width"] == SLD_WIDTH
    assert info_a["height"] == SLD_HEIGHT
    assert info_b["format"] == "PNG"


def test_shared_3d_scene_byte_identical_across_runs(tmp_path: Path) -> None:
    """Verify shared 3D visual renders are byte-identical across Case A and Case B."""
    scene_a_path = tmp_path / "case_A" / "scene_3d_shared.png"
    scene_b_path = tmp_path / "case_B" / "scene_3d_shared.png"

    res_a = render_deterministic_3d_scene(scene_a_path)
    res_b = render_deterministic_3d_scene(scene_b_path)

    assert res_a["sha256"] == res_b["sha256"]
    assert scene_a_path.read_bytes() == scene_b_path.read_bytes()
    assert res_a["hotspot_node"] == "N2"
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
