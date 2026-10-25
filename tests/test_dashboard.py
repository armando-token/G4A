"""Unit and integration tests for blackbox.dashboard and sealed visual-v2 replay views."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict

import pytest

from blackbox.dashboard import (
    build_six_panel_dashboard_model,
    load_or_generate_replay_state,
    render_bbox_overlay_png,
    render_sld_highlight_png,
    resolve_default_bundle_dir,
)
from blackbox.evidence import load_pair_view, resolve_safe_bundle_path


REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_DOSSIER_DIR = REPO_ROOT / "artifacts" / "reports" / "phase5_live_pair_dossier"
DASHBOARD_SOURCE_PATH = REPO_ROOT / "blackbox" / "dashboard.py"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _refresh_bundle_checksums(bundle_dir: Path) -> None:
    replay_man_path = bundle_dir / "replay_manifest.json"
    if replay_man_path.exists():
        rm = json.loads(replay_man_path.read_text(encoding="utf-8"))
        files_map = dict(rm.get("files") or {})
        for rel_p, meta in list(files_map.items()):
            target = bundle_dir / rel_p
            if target.exists() and isinstance(meta, dict):
                meta["sha256"] = _sha256_file(target)
                meta["size_bytes"] = target.stat().st_size
        rm["files"] = files_map
        replay_man_path.write_text(json.dumps(rm, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    sums_path = bundle_dir / "SHA256SUMS.txt"
    if sums_path.exists():
        new_lines = []
        for raw_line in sums_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) == 2:
                rel_p = parts[1].strip()
                target = bundle_dir / rel_p
                if target.exists():
                    new_lines.append(f"{_sha256_file(target)}  {rel_p}")
        sums_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


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


def test_load_pair_view_and_six_panels_live_dossier() -> None:
    pair_view = load_pair_view(LIVE_DOSSIER_DIR, verify_integrity=True, require_live=True)
    assert pair_view.is_complete is True
    assert pair_view.strict_pair_passed is True
    assert pair_view.verified_from_archived_run is True
    assert pair_view.physical_safety_certified is False

    model_a = build_six_panel_dashboard_model(
        pair_view,
        selected_case="case_A",
        overlay_mode="both",
        highlight_sld=False,
        crosshair_xy=(1111, 359),
    )
    model_b = build_six_panel_dashboard_model(
        pair_view,
        selected_case="case_B",
        overlay_mode="raw",
        highlight_sld=True,
        crosshair_xy=None,
    )

    assert model_a["banner"]["status"] == "VERIFIED ARCHIVED RUN"
    assert model_a["banner"]["strict_pair_passed"] is True
    assert model_a["banner"]["physical_safety_certified"] is False

    # Panel 1
    p1 = model_a["panel_1_incident"]
    assert p1["enabled"] is True
    assert p1["rack_count"] == 4
    assert p1["pdu_count"] == 2
    assert p1["hotspot_asset"] == "N4"
    assert p1["bound_sensor"] == "SNS_K2"
    assert p1["max_temp_c"] == 90.0
    assert p1["migration_quantum_a"] == 8.0
    assert set(p1["glossary"].keys()) >= {"PDU", "Rack", "SLD", "Lock Chain"}

    # Panel 2
    p2_a = model_a["panel_2_physical_view"]
    p2_b = model_b["panel_2_physical_view"]
    assert p2_a["rgb_identical_across_ab"] is True
    assert p2_a["thermal_identical_across_ab"] is True
    assert p2_a["rgb_sha256"] == p2_b["rgb_sha256"]
    assert p2_a["thermal_sha256"] == p2_b["thermal_sha256"]
    assert p2_a["i1_bbox_iou"] > 0.90
    assert p2_a["a1_bbox_iou"] > 0.90
    assert len(p2_a["rgb_png_bytes"]) > 1000
    # Raw overlay mode returns exact unannotated image bytes
    assert hashlib.sha256(p2_b["rgb_png_bytes"]).hexdigest() == p2_b["rgb_sha256"]

    # Panel 3
    p3_a = model_a["panel_3_sld_reversal"]
    p3_b = model_b["panel_3_sld_reversal"]
    assert p3_a["invariants_passed"] is True
    assert p3_a["case_A_sld_sha256"] != p3_a["case_B_sld_sha256"]
    # When highlight_sld=False, raw SLD bytes match the archived SHA-256
    assert hashlib.sha256(p3_a["case_A_sld_png_bytes"]).hexdigest() == p3_a["case_A_sld_sha256"]
    # When highlight_sld=True, explanatory overlay alters display bytes without touching archive
    assert hashlib.sha256(p3_b["case_A_sld_png_bytes"]).hexdigest() != p3_b["case_A_sld_sha256"]

    # Panel 4
    p4_a = model_a["panel_4_agent_timeline"]
    assert p4_a["a1_blind_isolation_verified"] is True
    turns_by_id = {row["turn_id"]: row for row in p4_a["active_turns"]}
    assert set(turns_by_id.keys()) == {"I1", "I2", "A1", "A2"}
    assert turns_by_id["A1"]["received_plan"] is False
    assert turns_by_id["A1"]["received_probe_receipt"] is False
    assert turns_by_id["A1"]["disjoint_from_investigator_env"] is True
    assert turns_by_id["A2"]["received_plan"] is True

    # Panel 5
    p5 = model_a["panel_5_decision_physics"]
    assert p5["decision_reversed_across_ab"] is True
    assert p5["case_A"]["selected_action_id"] == "MIGRATE_N4_TO_N3_8A"
    assert p5["case_B"]["selected_action_id"] == "MIGRATE_N4_TO_N2_8A"
    traj_a = p5["case_A"]["trajectories"]["MIGRATE_N4_TO_N3_8A"]
    assert len(traj_a["time_s"]) == 19
    assert len(traj_a["temps_c"]["N4"]) == 19

    # Panel 6
    p6 = model_a["panel_6_proof_limits"]
    assert len(p6["boolean_matrix_rows"]) == 13
    assert all(r["case_A"] and r["case_B"] for r in p6["boolean_matrix_rows"])
    baseline_ids = [r["baseline_id"] for r in p6["baseline_rows"]]
    assert baseline_ids == ["no_action", "fixed_n2_policy", "no_sld_variant", "privileged_oracle"]


def test_offline_no_credentials_and_repeated_refresh_zero_spend(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_var in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv("BLACKBOX_BUNDLE_DIR", str(LIVE_DOSSIER_DIR))

    resolved = resolve_default_bundle_dir()
    assert resolved == LIVE_DOSSIER_DIR.resolve()

    pair_view = load_pair_view(resolved, verify_integrity=True, require_live=True)
    initial_tokens = pair_view.token_accounting["pair_noncached_tokens"]

    for idx in range(5):
        case_choice = "case_A" if idx % 2 == 0 else "case_B"
        overlay_choice = "both" if idx % 2 == 0 else "raw"
        model = build_six_panel_dashboard_model(
            pair_view,
            selected_case=case_choice,
            overlay_mode=overlay_choice,
            highlight_sld=(idx % 2 == 1),
        )
        assert model["banner"]["strict_pair_passed"] is True
        assert model["panel_6_proof_limits"]["token_accounting"]["pair_noncached_tokens"] == initial_tokens


def test_malicious_bundle_relative_paths_rejected(tmp_path: Path) -> None:
    bundle_copy = tmp_path / "dossier"
    shutil.copytree(LIVE_DOSSIER_DIR, bundle_copy)

    with pytest.raises(ValueError, match="Malicious or out-of-bundle path"):
        resolve_safe_bundle_path(bundle_copy, "../outside.json")

    with pytest.raises(ValueError, match="Malicious or out-of-bundle path"):
        resolve_safe_bundle_path(bundle_copy, "/etc/passwd")

    # Also test malicious path inside replay_manifest.json
    replay_man_path = bundle_copy / "replay_manifest.json"
    replay_man = json.loads(replay_man_path.read_text(encoding="utf-8"))
    replay_man["files"]["../../outside.txt"] = {"sha256": "0" * 64, "size_bytes": 12}
    replay_man_path.write_text(json.dumps(replay_man, indent=2), encoding="utf-8")

    with pytest.raises(ValueError, match="Malicious or out-of-bundle path"):
        load_pair_view(bundle_copy, verify_integrity=True)


def test_missing_media_rejected(tmp_path: Path) -> None:
    bundle_copy = tmp_path / "dossier"
    shutil.copytree(LIVE_DOSSIER_DIR, bundle_copy)

    rgb_file = bundle_copy / "case_A" / "inputs" / "rear_aisle_rgb.png"
    rgb_file.unlink()

    with pytest.raises((FileNotFoundError, ValueError)):
        load_pair_view(bundle_copy, verify_integrity=True)


def test_invalid_score_rejected(tmp_path: Path) -> None:
    bundle_copy = tmp_path / "dossier"
    shutil.copytree(LIVE_DOSSIER_DIR, bundle_copy)

    score_path = bundle_copy / "case_A" / "score.json"
    score_data = json.loads(score_path.read_text(encoding="utf-8"))
    score_data["selected_action_id"] = "NO_ACTION"
    score_path.write_text(json.dumps(score_data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _refresh_bundle_checksums(bundle_copy)

    with pytest.raises(ValueError, match="Bundle integrity/replay verification failed"):
        load_pair_view(bundle_copy, verify_integrity=True)


def test_mixed_ab_hashes_rejected(tmp_path: Path) -> None:
    bundle_copy = tmp_path / "dossier"
    shutil.copytree(LIVE_DOSSIER_DIR, bundle_copy)

    # Copy Case A's SLD over Case B's SLD so they have identical hashes
    shutil.copyfile(
        bundle_copy / "case_A" / "inputs" / "sld.png",
        bundle_copy / "case_B" / "inputs" / "sld.png",
    )
    _refresh_bundle_checksums(bundle_copy)

    with pytest.raises(ValueError, match="Bundle integrity/replay verification failed"):
        load_pair_view(bundle_copy, verify_integrity=True)


def test_run_id_collision_rejected(tmp_path: Path) -> None:
    bundle_copy = tmp_path / "dossier"
    shutil.copytree(LIVE_DOSSIER_DIR, bundle_copy)

    precommit_path = bundle_copy / "precommitment.json"
    precommit = json.loads(precommit_path.read_text(encoding="utf-8"))
    precommit["campaign_run_id"] = "collided-foreign-run-id-999"
    precommit_path.write_text(json.dumps(precommit, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _refresh_bundle_checksums(bundle_copy)

    with pytest.raises(ValueError, match="Run ID collision"):
        load_pair_view(bundle_copy, verify_integrity=True)


def test_incomplete_partial_run_banner_and_disabled_panels(tmp_path: Path) -> None:
    partial_dir = tmp_path / "incomplete_dossier"
    partial_dir.mkdir(parents=True)
    shutil.copytree(LIVE_DOSSIER_DIR / "case_A", partial_dir / "case_A")

    pair_manifest: Dict[str, Any] = {
        "schema_version": "blackbox.pair_dossier.v2",
        "campaign_run_id": "partial-run-001",
        "execution_mode": "antigravity_live",
        "diagnostic_mode": "one_shot_Menu",
        "status": "incomplete",
        "strict_pair_passed": False,
        "verified_from_archived_run": False,
        "first_failed_gate": "auditor_independence",
        "failed_attempts": [
            {
                "attempt": 1,
                "case_id": "case_B",
                "failed_gate": "auditor_independence",
                "reason": "Simulated mid-run abort before Case B completion",
            }
        ],
        "physical_safety_certified": False,
    }
    (partial_dir / "pair_manifest.json").write_text(
        json.dumps(pair_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    pv = load_pair_view(partial_dir, verify_integrity=True)
    assert pv.is_complete is False
    assert pv.status == "incomplete"
    assert pv.strict_pair_passed is False
    assert pv.first_failed_gate == "auditor_independence"
    assert len(pv.failed_attempts) == 1

    model = build_six_panel_dashboard_model(pv)
    assert model["banner"]["status"] == "INCOMPLETE"
    assert model["banner"]["strict_pair_passed"] is False
    assert model["banner"]["first_failed_gate"] == "auditor_independence"
    assert model["panel_1_incident"]["enabled"] is False
    assert model["panel_2_physical_view"]["enabled"] is False
    assert model["panel_3_sld_reversal"]["enabled"] is False
    assert model["panel_4_agent_timeline"]["enabled"] is False
    assert model["panel_5_decision_physics"]["enabled"] is False
    assert model["panel_6_proof_limits"]["enabled"] is False


def test_no_hardcoded_outcomes_in_dashboard_source() -> None:
    source_text = DASHBOARD_SOURCE_PATH.read_text(encoding="utf-8")
    forbidden_literals = (
        "52.53",
        "98.83",
        "48.0",
        "40.0",
        "28.0",
        "MIGRATE_N4_TO_N3_8A",
        "MIGRATE_N4_TO_N2_8A",
        "google.genai",
        "build_private_world",
    )
    for token in forbidden_literals:
        assert token not in source_text, f"Forbidden literal {token!r} found in dashboard.py source"

    tree = ast.parse(source_text, filename=str(DASHBOARD_SOURCE_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float)):
            val_str = str(node.value)
            for token in forbidden_literals:
                assert token not in val_str, f"Forbidden AST constant {val_str!r} in dashboard.py"


def test_streamlit_headless_smoke_healthcheck() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        free_port = sock.getsockname()[1]

    env = dict(os.environ)
    env["GOOGLE_API_KEY"] = ""
    env["GEMINI_API_KEY"] = ""
    env["GOOGLE_APPLICATION_CREDENTIALS"] = ""
    env["BLACKBOX_BUNDLE_DIR"] = str(LIVE_DOSSIER_DIR)
    env["PYTHONPATH"] = str(REPO_ROOT)

    streamlit_bin = REPO_ROOT / ".venv" / "bin" / "streamlit"
    cmd = [
        str(streamlit_bin),
        "run",
        str(DASHBOARD_SOURCE_PATH),
        "--server.headless",
        "true",
        "--server.address",
        "127.0.0.1",
        "--server.port",
        str(free_port),
        "--browser.gatherUsageStats",
        "false",
    ]

    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        health_url = f"http://127.0.0.1:{free_port}/_stcore/health"
        root_url = f"http://127.0.0.1:{free_port}/"
        healthy = False
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(health_url, timeout=1.0) as resp:
                    body = resp.read().decode("utf-8", errors="replace").strip()
                    if resp.status == 200 and body == "ok":
                        healthy = True
                        break
            except Exception:
                time.sleep(0.25)

        assert healthy is True, "Streamlit headless server failed to reach healthy state"

        with urllib.request.urlopen(root_url, timeout=2.0) as root_resp:
            assert root_resp.status == 200
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)
