"""Unit tests for blackbox.campaign and blackbox.evidence."""

from __future__ import annotations

from pathlib import Path

from blackbox.campaign import run_ab_campaign


def test_run_ab_campaign_end_to_end(tmp_path: Path) -> None:
    artifacts_dir = tmp_path / "artifacts"
    summary = run_ab_campaign(artifacts_root=artifacts_dir)

    assert summary["campaign_pass"] is True
    assert summary["feeder_disambiguation_pass"] is True
    assert summary["auditor_disambiguation_pass"] is True
    assert summary["total_interactions_locked"] == 8
    assert len(summary["accounting"]) == 8

    sums_path = Path(summary["evidence_bundle"]["sha256sums"])
    report_path = Path(summary["evidence_bundle"]["report_md"])
    assert sums_path.exists()
    assert report_path.exists()
    sums_text = sums_path.read_text(encoding="utf-8")
    assert "case_A/sld_case_A.png" in sums_text
    assert "case_B/sld_case_B.png" in sums_text
    assert "public/scene_3d_shared.png" in sums_text
