"""Unit tests for blackbox.dashboard offline replay loader."""

from __future__ import annotations

from pathlib import Path

from blackbox.dashboard import load_or_generate_replay_state


def test_dashboard_offline_replay_state(tmp_path: Path) -> None:
    artifacts_dir = tmp_path / "artifacts"
    state = load_or_generate_replay_state(artifacts_root=artifacts_dir)

    assert state["summary"]["campaign_pass"] is True
    assert "case_A" in state["cases"]
    assert "case_B" in state["cases"]
    assert state["cases"]["case_A"]["i2"]["diagnosed_fault_feeder"] == "F2"
    assert state["cases"]["case_B"]["i2"]["diagnosed_fault_feeder"] == "F3"
    assert Path(state["scene_3d_path"]).exists()
    assert Path(state["sha256sums_path"]).exists()
