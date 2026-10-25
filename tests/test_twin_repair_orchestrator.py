"""End-to-end unit and integration tests for `twin_repair` Phases D & E orchestrator, roles, and report card."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from twin_repair.challenge import build_challenge_fixture, scan_for_truth_leakage
from twin_repair.managed_roles import (
    MockTwinRepairProvider,
    build_builder_agents_md,
    build_critic_agents_md,
    load_role_prompt_template,
)
from twin_repair.orchestrator import TIMING_SPAN_KEYS, main as orchestrator_main, run_twin_repair
from twin_repair.report import REQUIRED_GATE_KEYS


def test_role_prompts_and_agents_md_zero_leakage() -> None:
    builder_prompt = load_role_prompt_template("builder")
    critic_prompt = load_role_prompt_template("critic")
    builder_agents = build_builder_agents_md()
    critic_agents = build_critic_agents_md()

    for text in (builder_prompt, critic_prompt, builder_agents, critic_agents):
        res = scan_for_truth_leakage(text, raise_on_leak=True)
        assert res["leaked"] is False

    # Builder prompt must never mention Revision B or swapping N2 and N3
    assert "swap N2 and N3" not in builder_prompt
    assert "current_sld_B" not in builder_prompt
    assert "/workspace/scene_spec.json" in builder_prompt
    assert "/workspace/repair_patch.json" in critic_prompt


def test_golden_mock_drift_run_end_to_end(tmp_path: Path) -> None:
    case_dir = tmp_path / "drift_case"
    build_challenge_fixture(case_dir, seed=20261025, case_type="drift_A_to_B")

    run_dir = tmp_path / "run_mock_drift"
    rc = orchestrator_main(["mock", "--case", str(case_dir), "--out", str(run_dir)])
    assert rc == 0

    result_path = run_dir / "result_public.json"
    assert result_path.is_file()
    result = json.loads(result_path.read_text(encoding="utf-8"))

    assert result["terminal_state"] == "PASS"
    assert result["passed"] is True
    assert result["workflow_passed"] is True
    assert result["mock_rehearsal_passed"] is True
    assert result["provider_mode"] == "mock"
    assert result["source"] == "fixture"

    # All 11 required gate booleans present at top level and in result["gates"]
    for gk in REQUIRED_GATE_KEYS:
        assert gk in result
        assert gk in result["gates"]

    assert result["builder_completed"] is True
    assert result["critic_completed"] is True
    assert result["argon_verified"] is False  # Mock fixture is never claimed as live Argon
    assert result["distinct_environments"] is True
    assert result["render0_valid"] is True
    assert result["patch_valid"] is True
    assert result["render1_valid"] is True
    assert result["cable_pixels_changed"] is True
    assert result["graph_improved"] is True
    assert result["physics_safe"] is True
    assert result["human_review_required"] is True

    # Graph score & physics flip (2/4 -> 4/4, N3 -> N2)
    assert result["graph_score"]["before"] == "2/4"
    assert result["graph_score"]["after"] == "4/4"
    assert result["graph_score"]["summary"] == "2/4 -> 4/4"

    act = result["action_summary"]
    assert act["stale_recommended_receiver"] == "N3"
    assert act["repaired_recommended_receiver"] == "N2"
    assert act["n3_f1_current_a"] == 48.0
    assert act["n2_f1_current_a"] == 40.0
    assert act["n2_f2_current_a"] == 28.0
    assert 52.0 <= act["n4_repaired_temp_180s_c"] <= 53.5

    # Timing spans & dossier files
    for span_key in TIMING_SPAN_KEYS:
        assert span_key in result["timing_spans_s"]

    expected_files = [
        "manifest.json",
        "public/old_sld_A.png",
        "public/current_sld_B.png",
        "public/old_thermal_rear.png",
        "public/public_state.json",
        "private/truth.json",
        "roles/builder_provenance.json",
        "roles/critic_provenance.json",
        "scene0.json",
        "render0/render_wiring.png",
        "render0/render_rgb_rear.png",
        "render0/render_thermal_rear.png",
        "render0/render_meta.json",
        "patch.json",
        "scene1.json",
        "render1/render_wiring.png",
        "render1/render_rgb_rear.png",
        "render1/render_thermal_rear.png",
        "render1/render_meta.json",
        "score.json",
        "action_comparison.json",
        "result_public.json",
        "RESULTS_TWIN_REPAIR.md",
        "events.jsonl",
    ]
    for rel in expected_files:
        assert (run_dir / rel).is_file(), f"Missing dossier artifact: {rel}"

    # Verify hidden-oracle separation on public result card
    leak_check = scan_for_truth_leakage(result, raise_on_leak=True)
    assert leak_check["leaked"] is False

    # Verify replay mode on completed dossier
    replay_dir = tmp_path / "run_replay"
    rc_replay = orchestrator_main(["replay", "--case", str(run_dir), "--out", str(replay_dir)])
    assert rc_replay == 0
    replay_res = json.loads((replay_dir / "result_public.json").read_text(encoding="utf-8"))
    assert replay_res["provider_mode"] == "replay"
    assert replay_res["is_replay"] is True


def test_control_a_to_a_no_change_end_to_end(tmp_path: Path) -> None:
    control_dir = tmp_path / "control_case"
    build_challenge_fixture(control_dir, seed=20261025, case_type="control_A_to_A")

    out_dir = tmp_path / "run_control"
    res = run_twin_repair(control_dir, provider_mode="mock", out_dir=out_dir)

    assert res["terminal_state"] == "CONTROL_NO_CHANGE"
    assert res["passed"] is True
    assert res["workflow_passed"] is True
    assert res["patch"]["decision"] == "no_change"
    assert res["patch"]["operations"] == []
    assert res["graph_score"]["before"] == "4/4"
    assert res["graph_score"]["after"] == "4/4"
    assert "already agree; no repair was made" in res["headlines"]["result_caption"]


@pytest.mark.parametrize(
    ("adverse_mode", "expected_terminal_state"),
    [
        ("abstain", "ABSTAINED"),
        ("timeout", "TIMEOUT"),
        ("missing_remote_output", "PROVIDER_ERROR"),
        ("same_environment_id", "REJECTED"),
        ("invalid_patch", "REJECTED"),
        ("forged_hash", "REJECTED"),
    ],
)
def test_fail_closed_negative_runs(
    tmp_path: Path,
    adverse_mode: str,
    expected_terminal_state: str,
) -> None:
    case_dir = tmp_path / "drift_case"
    if not case_dir.exists():
        build_challenge_fixture(case_dir, seed=20261025, case_type="drift_A_to_B")

    out_dir = tmp_path / f"run_{adverse_mode}"
    provider = MockTwinRepairProvider(mode=adverse_mode)
    res = run_twin_repair(
        case_dir,
        provider_mode="mock",
        out_dir=out_dir,
        provider=provider,
    )

    assert res["terminal_state"] == expected_terminal_state
    assert res["passed"] is False
    assert res["workflow_passed"] is False
    assert (out_dir / "scene0.json").is_file()
    assert (out_dir / "render0" / "render_wiring.png").is_file()
    assert (out_dir / "events.jsonl").is_file()

    if adverse_mode == "same_environment_id":
        assert res["distinct_environments"] is False
    if adverse_mode in ("invalid_patch", "forged_hash"):
        assert res["patch_valid"] is False
