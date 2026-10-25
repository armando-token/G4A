"""Phase 5 campaign tests: precommitment freeze, mock-pair CLI, and all 11 tamper regressions."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
from typing import Any, Dict

import pytest
from PIL import Image

from blackbox.campaign import (
    build_campaign_precommitment,
    main as campaign_cli_main,
    run_ab_campaign,
    run_preflight_command,
    run_visual_v2_pair_campaign,
)
from blackbox.evidence import verify_and_replay_bundle
from blackbox.oracle import CASE_13_BOOLEAN_KEYS
from blackbox.protocol import DeterministicVisualV2MockClient


def test_run_ab_campaign_end_to_end(tmp_path: Path) -> None:
    """Backward-compatible Phase 00 end-to-end A/B campaign test."""
    artifacts_dir = tmp_path / "artifacts"
    summary = run_ab_campaign(artifacts_root=artifacts_dir)

    assert summary["campaign_pass"] is True
    assert summary["feeder_disambiguation_pass"] is True
    assert summary["auditor_disambiguation_pass"] is True
    assert summary["total_interactions_locked"] == 8

    assert summary["case_A"]["overall_case_pass"] is True
    assert summary["case_B"]["overall_case_pass"] is True
    assert summary["case_A"]["safety_details"]["feeder_match"] is True
    assert summary["case_B"]["safety_details"]["feeder_match"] is True

    assert (artifacts_dir / "reports" / "campaign_summary.json").exists()
    assert (artifacts_dir / "reports" / "campaign_report.md").exists()
    assert (artifacts_dir / "reports" / "SHA256SUMS.txt").exists()


def test_build_campaign_precommitment_and_preflight(tmp_path: Path) -> None:
    """Verify Phase 5 precommitment freeze and preflight check in mock mode."""
    artifacts_dir = tmp_path / "artifacts"
    pre = build_campaign_precommitment(
        campaign_id="precommit-test-01",
        mode="mock",
        diagnostic_mode="guided_diagnostic",
        max_pair_noncached=280_000,
        artifacts_root=artifacts_dir,
    )
    assert pre["campaign_id"] == "precommit-test-01"
    assert pre["protocol_version"] == "visual-v2/guided_diagnostic"
    assert pre["controlled_public_packet_sha256"]["substantive_equal"] is True
    assert pre["input_image_sha256"]["rgb_identical"] is True
    assert pre["input_image_sha256"]["thermal_identical"] is True
    assert pre["input_image_sha256"]["sld_distinct"] is True
    assert set(pre["json_schemas_sha256"].keys()) == {
        "ProposalV2",
        "PlanV2",
        "IndependentGraphV2",
        "AuditChallengeV2",
    }
    assert set(pre["planned_comparisons"]) == {
        "no_action",
        "fixed_n2_policy",
        "privileged_oracle",
        "no_sld_variant",
    }
    assert set(pre["ablations"].keys()) == {
        "no_action",
        "fixed_n2_policy",
        "privileged_oracle",
        "no_sld_variant",
    }

    # Verify private truth vault was sealed outside reports/
    vault_file = artifacts_dir / "private_precommit" / "precommit-test-01" / "sealed_private_precommit.json"
    assert vault_file.is_file()

    preflight_res = run_preflight_command(mode="mock", artifacts_root=artifacts_dir)
    assert preflight_res["ready"] is True
    assert preflight_res["visual_assets_check"]["passed"] is True
    assert (artifacts_dir / "reports" / "phase5_preflight_report.json").is_file()


@pytest.fixture(scope="module")
def reference_bundle_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build one clean, verified Phase 5 mock-pair bundle to clone across tamper tests."""
    root = tmp_path_factory.mktemp("phase5_ref_bundle")
    artifacts_root = root / "artifacts"
    res = run_visual_v2_pair_campaign(
        mode="mock",
        diagnostic_mode="guided_diagnostic",
        campaign_id="ref-pair-01",
        artifacts_root=artifacts_root,
    )
    assert res["strict_pair_passed"] is True
    assert res["verified_from_archived_run"] is True
    return Path(res["run_dir"])


def _clone_bundle(src_dir: Path, dst_parent: Path, name: str) -> Path:
    dst = dst_parent / name
    shutil.copytree(src_dir, dst)
    return dst


def test_mock_pair_campaign_and_cli_replay(reference_bundle_dir: Path, tmp_path: Path) -> None:
    """Verify full dossier structure, 13 booleans per case, and CLI entry points."""
    bdir = reference_bundle_dir
    assert (bdir / "precommitment.json").is_file()
    assert (bdir / "pair_manifest.json").is_file()
    assert (bdir / "FINAL_REPORT.md").is_file()
    assert (bdir / "replay_manifest.json").is_file()
    assert (bdir / "SHA256SUMS.txt").is_file()

    for c_label in ("case_A", "case_B"):
        sc = json.loads((bdir / c_label / "score.json").read_text(encoding="utf-8"))
        assert sc["strict_case_passed"] is True
        assert sc["physical_safety_certified"] is False
        assert sc["case_noncached_tokens"] > 0
        for b_key in CASE_13_BOOLEAN_KEYS:
            assert sc["booleans"][b_key] is True

    replay = verify_and_replay_bundle(bdir)
    assert replay["verified_from_archived_run"] is True
    assert replay["strict_pair_passed"] is True
    assert replay["errors"] == []

    # Test CLI subcommands: preflight, mock-pair, replay
    cli_artifacts = tmp_path / "cli_artifacts"
    assert campaign_cli_main(["preflight", "--mode", "mock", "--artifacts-root", str(cli_artifacts)]) == 0
    assert (
        campaign_cli_main(
            [
                "mock-pair",
                "--campaign-id",
                "cli-mock-01",
                "--artifacts-root",
                str(cli_artifacts),
            ]
        )
        == 0
    )
    cli_bundle = cli_artifacts / "reports" / "cli-mock-01"
    assert campaign_cli_main(["replay", "--bundle", str(cli_bundle)]) == 0


def test_campaign_partial_dossier_on_gate_failure(tmp_path: Path) -> None:
    """Verify that a gate failure stops the case, preserves failed_attempts, and writes an honest partial dossier."""
    def _bad_i1(d: Dict[str, Any]) -> Dict[str, Any]:
        d["selected_measurement"]["type"] = "thermal_recheck"
        return d

    bad_client = DeterministicVisualV2MockClient(artifact_mutators={"I1": _bad_i1})
    res = run_visual_v2_pair_campaign(
        mode="mock",
        diagnostic_mode="guided_diagnostic",
        campaign_id="failed-gate-01",
        artifacts_root=tmp_path / "artifacts",
        client=bad_client,
    )
    assert res["strict_pair_passed"] is False
    assert len(res["failed_attempts"]) == 1
    assert res["failed_attempts"][0]["case_id"] == "case_A"
    assert res["failed_attempts"][0]["probe_issued"] is False
    assert (Path(res["run_dir"]) / "pair_manifest.json").is_file()
    assert (Path(res["run_dir"]) / "FINAL_REPORT.md").is_file()


# ---------------------------------------------------------------------------
# Phase 5 Section 5: All 11 Required Tamper / Regression Tests
# ---------------------------------------------------------------------------


def test_tamper_01_truncate_turn(reference_bundle_dir: Path, tmp_path: Path) -> None:
    """1. Truncate a turn (`case_A/turns/I2.json` removed from 4-turn sequence) -> replay verification fails."""
    bdir = _clone_bundle(reference_bundle_dir, tmp_path, "tamper_01")
    target = bdir / "case_A" / "turns" / "I2.json"
    target.unlink()

    res = verify_and_replay_bundle(bdir)
    assert res["verified_from_archived_run"] is False
    assert res["strict_pair_passed"] is False
    assert len(res["errors"]) > 0


def test_tamper_02_alter_sld_one_byte(reference_bundle_dir: Path, tmp_path: Path) -> None:
    """2. Alter SLD one byte (`case_A/inputs/sld.png`) -> replay verification fails."""
    bdir = _clone_bundle(reference_bundle_dir, tmp_path, "tamper_02")
    sld_path = bdir / "case_A" / "inputs" / "sld.png"
    data = bytearray(sld_path.read_bytes())
    data[-10] ^= 0xFF
    sld_path.write_bytes(bytes(data))

    res = verify_and_replay_bundle(bdir)
    assert res["verified_from_archived_run"] is False
    assert res["strict_pair_passed"] is False


def test_tamper_03_swap_ab_sld(reference_bundle_dir: Path, tmp_path: Path) -> None:
    """3. Swap A/B SLD image (`case_A/inputs/sld.png` <-> `case_B/inputs/sld.png`) -> replay verification fails."""
    bdir = _clone_bundle(reference_bundle_dir, tmp_path, "tamper_03")
    sld_a = bdir / "case_A" / "inputs" / "sld.png"
    sld_b = bdir / "case_B" / "inputs" / "sld.png"
    bytes_a = sld_a.read_bytes()
    bytes_b = sld_b.read_bytes()
    sld_a.write_bytes(bytes_b)
    sld_b.write_bytes(bytes_a)

    res = verify_and_replay_bundle(bdir)
    assert res["verified_from_archived_run"] is False
    assert res["strict_pair_passed"] is False


def test_tamper_04_delete_a2_artifact(reference_bundle_dir: Path, tmp_path: Path) -> None:
    """4. Delete A2 artifact (`case_B/remote_artifacts/audit_challenge.json`) -> replay verification fails."""
    bdir = _clone_bundle(reference_bundle_dir, tmp_path, "tamper_04")
    (bdir / "case_B" / "remote_artifacts" / "audit_challenge.json").unlink()

    res = verify_and_replay_bundle(bdir)
    assert res["verified_from_archived_run"] is False
    assert res["strict_pair_passed"] is False


def test_tamper_05_tamper_forecast(reference_bundle_dir: Path, tmp_path: Path) -> None:
    """5. Tamper forecast (`case_A/remote_artifacts/plan.json` trajectory value altered) -> replay verification fails."""
    bdir = _clone_bundle(reference_bundle_dir, tmp_path, "tamper_05")
    plan_path = bdir / "case_A" / "remote_artifacts" / "plan.json"
    plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
    plan_data["predictions_by_action"]["MIGRATE_N4_TO_N3_8A"]["temps_c"]["N4"][-1] = 52.0
    plan_path.write_text(json.dumps(plan_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    res = verify_and_replay_bundle(bdir)
    assert res["verified_from_archived_run"] is False
    assert res["strict_pair_passed"] is False


def test_tamper_06_alter_usage_snapshot(reference_bundle_dir: Path, tmp_path: Path) -> None:
    """6. Alter usage snapshot (`case_A/turns/I1_usage.json`) -> replay verification fails."""
    bdir = _clone_bundle(reference_bundle_dir, tmp_path, "tamper_06")
    usage_path = bdir / "case_A" / "turns" / "I1_usage.json"
    u_data = json.loads(usage_path.read_text(encoding="utf-8"))
    u_data["billed_noncached_tokens"] = int(u_data.get("billed_noncached_tokens", 2050)) + 500
    u_data["noncached_tokens"] = int(u_data.get("noncached_tokens", 2050)) + 500
    usage_path.write_text(json.dumps(u_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    res = verify_and_replay_bundle(bdir)
    assert res["verified_from_archived_run"] is False
    assert res["strict_pair_passed"] is False


def test_tamper_07_make_ab_rgb_differ(reference_bundle_dir: Path, tmp_path: Path) -> None:
    """7. Make A/B RGB differ (`case_B/inputs/rear_aisle_rgb.png` modified) -> replay verification fails."""
    bdir = _clone_bundle(reference_bundle_dir, tmp_path, "tamper_07")
    rgb_b_path = bdir / "case_B" / "inputs" / "rear_aisle_rgb.png"
    with Image.open(rgb_b_path) as im:
        img = im.copy()
    img.putpixel((10, 10), (254, 1, 1))
    img.save(rgb_b_path, format="PNG")

    res = verify_and_replay_bundle(bdir)
    assert res["verified_from_archived_run"] is False
    assert res["strict_pair_passed"] is False


def test_tamper_08_make_ab_graph_same(reference_bundle_dir: Path, tmp_path: Path) -> None:
    """8. Make A/B graph same (`case_B/remote_artifacts/independent_graph.json` given Case A edges) -> fails."""
    bdir = _clone_bundle(reference_bundle_dir, tmp_path, "tamper_08")
    graph_a = json.loads(
        (bdir / "case_A" / "remote_artifacts" / "independent_graph.json").read_text(encoding="utf-8")
    )
    graph_b_path = bdir / "case_B" / "remote_artifacts" / "independent_graph.json"
    graph_b = json.loads(graph_b_path.read_text(encoding="utf-8"))
    graph_b["edges"] = graph_a["edges"]
    graph_b_path.write_text(json.dumps(graph_b, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    res = verify_and_replay_bundle(bdir)
    assert res["verified_from_archived_run"] is False
    assert res["strict_pair_passed"] is False


def test_tamper_09_incorrect_safe_receiver(reference_bundle_dir: Path, tmp_path: Path) -> None:
    """9. Set incorrect safe receiver (`MIGRATE_N4_TO_N2_8A` in Case A `plan.json`) -> fails."""
    bdir = _clone_bundle(reference_bundle_dir, tmp_path, "tamper_09")
    plan_a_path = bdir / "case_A" / "remote_artifacts" / "plan.json"
    plan_a = json.loads(plan_a_path.read_text(encoding="utf-8"))
    plan_a["selected_action_id"] = "MIGRATE_N4_TO_N2_8A"
    plan_a_path.write_text(json.dumps(plan_a, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    res = verify_and_replay_bundle(bdir)
    assert res["verified_from_archived_run"] is False
    assert res["strict_pair_passed"] is False


def test_tamper_10_incomplete_marked_completed_in_summary(
    reference_bundle_dir: Path, tmp_path: Path
) -> None:
    """10. Mark turn `I2.json` status as `incomplete` while `score.json` claims `completed` -> fails."""
    bdir = _clone_bundle(reference_bundle_dir, tmp_path, "tamper_10")
    i2_turn_path = bdir / "case_A" / "turns" / "I2.json"
    i2_turn = json.loads(i2_turn_path.read_text(encoding="utf-8"))
    i2_turn["status"] = "incomplete"
    i2_turn["scientific_complete"] = False
    i2_turn["diagnostic_only"] = True
    i2_turn_path.write_text(json.dumps(i2_turn, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    res = verify_and_replay_bundle(bdir)
    assert res["verified_from_archived_run"] is False
    assert res["strict_pair_passed"] is False


def test_tamper_11_corrupt_locks_jsonl(reference_bundle_dir: Path, tmp_path: Path) -> None:
    """11. Corrupt `locks.jsonl` (`chain_hash` altered) -> replay verification fails."""
    bdir = _clone_bundle(reference_bundle_dir, tmp_path, "tamper_11")
    locks_path = bdir / "case_A" / "locks.jsonl"
    lines = [json.loads(line) for line in locks_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    lines[1]["chain_hash"] = "f" * 64
    locks_path.write_text(
        "\n".join(json.dumps(rec, sort_keys=True, separators=(",", ":")) for rec in lines) + "\n",
        encoding="utf-8",
    )

    res = verify_and_replay_bundle(bdir)
    assert res["verified_from_archived_run"] is False
    assert res["strict_pair_passed"] is False
