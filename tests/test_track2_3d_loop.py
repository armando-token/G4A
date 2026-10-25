"""Unit tests for the Track Two 3D scene generate -> render -> inspect -> critique -> patch loop."""

from __future__ import annotations

import json
from pathlib import Path

from blackbox.track2_3d_loop import (
    ImprovementReport,
    SceneConfig,
    apply_scene_patch,
    compute_render_metrics,
    critique_and_propose_patch,
    render_parametrized_3d_scene,
    run_track2_3d_loop,
)


def test_track2_3d_end_to_end_measurable_improvement(tmp_path: Path) -> None:
    """Verify the full 3D loop renders initial and patched PNGs and proves measurable improvement."""
    report = run_track2_3d_loop(output_dir=tmp_path)
    assert isinstance(report, ImprovementReport)
    assert report.measurable_improvement_verified is True

    v1_png = Path(report.initial_metrics.image_path)
    v2_png = Path(report.patched_metrics.image_path)
    report_json = tmp_path / "track2_3d_improvement_report.json"

    assert v1_png.exists()
    assert v2_png.exists()
    assert report_json.exists()
    assert report.initial_metrics.sha256 != report.patched_metrics.sha256

    # Verify critique identified concrete framing, occlusion, and thermal range deficiencies
    assert len(report.critique.identified_deficiencies) >= 2
    assert report.critique.inspected_image_sha256 == report.initial_metrics.sha256
    assert report.patched_config.cable_tray_occlusion < report.initial_config.cable_tray_occlusion

    # Verify measurable improvement across key diagnostic metrics
    assert report.patched_metrics.hotspot_contrast_ratio > report.initial_metrics.hotspot_contrast_ratio
    assert report.patched_metrics.pdu_framing_score > report.initial_metrics.pdu_framing_score
    assert report.patched_metrics.overall_diagnostic_score > report.initial_metrics.overall_diagnostic_score
    assert report.deltas["overall_diagnostic_score_delta"] > 10.0

    saved_data = json.loads(report_json.read_text(encoding="utf-8"))
    assert saved_data["measurable_improvement_verified"] is True


def test_track2_3d_deterministic_reproducibility(tmp_path: Path) -> None:
    """Verify rendering and metric computation are 100% deterministic across repeated runs."""
    cfg = SceneConfig(version="v1_test")
    img_a = tmp_path / "run_a.png"
    img_b = tmp_path / "run_b.png"

    render_parametrized_3d_scene(cfg, img_a)
    render_parametrized_3d_scene(cfg, img_b)

    m_a = compute_render_metrics(img_a, config=cfg)
    m_b = compute_render_metrics(img_b, config=cfg)

    assert m_a.sha256 == m_b.sha256
    assert m_a.hotspot_contrast_ratio == m_b.hotspot_contrast_ratio
    assert m_a.pdu_framing_score == m_b.pdu_framing_score
    assert m_a.luminance_entropy == m_b.luminance_entropy
    assert m_a.edge_sharpness == m_b.edge_sharpness
    assert m_a.overall_diagnostic_score == m_b.overall_diagnostic_score

    critique = critique_and_propose_patch(cfg, m_a)
    patched_cfg = apply_scene_patch(cfg, critique.proposed_patch)
    assert patched_cfg.version == "v2_patched"
    assert patched_cfg.fov_deg == 44.0
