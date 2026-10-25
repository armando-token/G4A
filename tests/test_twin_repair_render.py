"""Tests for Phase B (Parameterized Three.js 3D Twin & Render Bridge) and Phase F (4-State Web App)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from twin_repair.contracts import build_canonical_scene_spec
from twin_repair.render_bridge import compare_wiring_renders, main as render_bridge_main, render_scene_spec
from twin_repair.report import REQUIRED_GATE_KEYS
from twin_repair.web.app import create_app


REPO_ROOT = Path(__file__).resolve().parent.parent
STATIC3D_DIR = REPO_ROOT / "blackbox" / "static3d"


def _make_spec(revision: str) -> dict:
    spec = build_canonical_scene_spec(source_revision=revision)
    return spec.to_dict() if hasattr(spec, "to_dict") else dict(spec)


def test_legacy_capture_mjs_backward_compatibility(tmp_path: Path) -> None:
    """Verify legacy no-spec `node capture.mjs <out_dir>` remains 100% compatible."""
    out_dir = tmp_path / "legacy_out"
    proc = subprocess.run(
        ["node", str(STATIC3D_DIR / "capture.mjs"), str(out_dir)],
        cwd=str(STATIC3D_DIR),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert (out_dir / "rear_aisle_rgb.png").is_file()
    assert (out_dir / "rear_aisle_thermal.png").is_file()
    assert (out_dir / "render_meta.json").is_file()
    meta = json.loads((out_dir / "render_meta.json").read_text(encoding="utf-8"))
    assert meta.get("status") == "ok"
    assert "metadataByMode" in meta
    assert "rgb" in meta["metadataByMode"]
    assert "thermal" in meta["metadataByMode"]
    assert proc.returncode == 0


def test_parameterized_render_scene_spec_and_wiring_diff(tmp_path: Path) -> None:
    """Verify Spec A vs Spec B 3D renders produce distinct N2/N3 cable pixels and <=3px hotspot registration."""
    spec_a_path = tmp_path / "scene0.json"
    spec_b_path = tmp_path / "scene1.json"
    spec_a_path.write_text(json.dumps(_make_spec("A"), indent=2), encoding="utf-8")
    spec_b_path.write_text(json.dumps(_make_spec("B"), indent=2), encoding="utf-8")

    out_a = tmp_path / "render0"
    out_b = tmp_path / "render1"

    res_a = render_scene_spec(spec_a_path, out_a)
    res_b = render_scene_spec(spec_b_path, out_b)

    for res, out_dir in ((res_a, out_a), (res_b, out_b)):
        assert (out_dir / "render_wiring.png").is_file()
        assert (out_dir / "render_rgb_rear.png").is_file()
        assert (out_dir / "render_thermal_rear.png").is_file()
        assert (out_dir / "render_meta.json").is_file()
        assert float(res["registration_error_px"]) <= 3.0

    diff = compare_wiring_renders(
        out_a / "render_wiring.png",
        out_b / "render_wiring.png",
        out_a / "render_meta.json",
        out_b / "render_meta.json",
    )
    assert diff["sha256_differs"] is True
    assert diff["changed_pixels_by_rack"]["N2"] > 100
    assert diff["changed_pixels_by_rack"]["N3"] > 100
    assert diff["changed_pixels_by_rack"]["N1"] <= 10
    assert diff["changed_pixels_by_rack"]["N4"] <= 10
    assert float(diff["hotspot_shift_px"]) <= 3.0
    assert diff["cable_pixels_changed"] is True

    # Also verify control A -> A comparison yields cable_pixels_changed == False
    diff_control = compare_wiring_renders(
        out_a / "render_wiring.png",
        out_a / "render_wiring.png",
        out_a / "render_meta.json",
        out_a / "render_meta.json",
    )
    assert diff_control["sha256_differs"] is False
    assert diff_control["cable_pixels_changed"] is False


def test_render_bridge_cli_entrypoint(tmp_path: Path) -> None:
    """Verify `python -m twin_repair.render_bridge --spec ... --out-dir ...` works via main()."""
    spec_path = tmp_path / "cli_spec.json"
    spec_path.write_text(json.dumps(_make_spec("A"), indent=2), encoding="utf-8")
    out_dir = tmp_path / "cli_out"
    rc = render_bridge_main(["--spec", str(spec_path), "--out-dir", str(out_dir)])
    assert rc == 0
    assert (out_dir / "render_wiring.png").is_file()


def test_web_app_routes_and_security_gates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify Phase F web endpoints, authentic live1 assets, no-credential live rejection, and HTTP 403 security."""
    import hashlib

    monkeypatch.setenv("BLACKBOX_JUDGE_CODE", "SECRET_JUDGE_2026")
    app = create_app(runs_root=tmp_path / "runs", max_public_live_runs=2, cooldown_s=0.0)
    client = TestClient(app)

    # 1. Health & static UI
    r_health = client.get("/health")
    assert r_health.status_code == 200
    assert r_health.json()["status"] == "ok"

    r_index = client.get("/?present=1")
    assert r_index.status_code == 200
    assert "The connector is hot. Is the wiring map still correct?" in r_index.text
    assert "View verified run" in r_index.text
    assert "Recorded live Argon run" in r_index.text

    assert client.get("/app.js").status_code == 200
    assert client.get("/styles.css").status_code == 200

    # 2. Authentic runs/live1 verification (exact SHA-256 against dossier)
    r_live_rec = client.get("/runs/live1")
    assert r_live_rec.status_code == 200
    live_rec_json = r_live_rec.json()
    assert live_rec_json["provenance_badge"] == "Recorded live Argon run"
    assert live_rec_json["result"]["run_id"] == "twin-repair-a9a617846fdc"
    assert live_rec_json["result"]["argon_verified"] is True
    assert live_rec_json["result"]["passed"] is True

    expected_shas = {
        "current_sld_B.png": "186249a7cd93520e20d65e1234f7cd3a032eaf32b6bc3ffb570afffb4d4cba7c",
        "render0/render_wiring.png": "878ea0b344dc8d728d250b3ffd08c815dc75c1361d7b8b0bba0588da19e97cf9",
        "render1/render_wiring.png": "6e2544c677aa6314e574e8176befbe14a973ecf6ea9bc159aa28d1cad792673d",
    }
    for asset_rel, exp_sha in expected_shas.items():
        r_asset = client.get(f"/runs/live1/assets/{asset_rel}")
        assert r_asset.status_code == 200
        assert hashlib.sha256(r_asset.content).hexdigest() == exp_sha

    # 3. Cases endpoint
    r_cases = client.get("/cases")
    assert r_cases.status_code == 200
    cases_data = r_cases.json()
    case_ids = {c["case_id"] for c in cases_data["cases"]}
    assert case_ids == {"drift_A_to_B", "control_A_to_A"}
    assert cases_data["public_live_quota_remaining"] == 2

    # 4. POST /runs for drift_A_to_B (mock) genuinely runs orchestrator with argon_verified=False
    r_drift = client.post("/runs", json={"case_id": "drift_A_to_B", "mode": "mock"})
    assert r_drift.status_code == 200
    drift_payload = r_drift.json()
    drift_run_id = drift_payload["run_id"]
    drift_card = drift_payload["result"]
    assert drift_card["terminal_state"] == "PASS"
    assert drift_card["provider_mode"] == "mock"
    assert drift_card["argon_verified"] is False
    for gate_key in REQUIRED_GATE_KEYS:
        assert gate_key in drift_card
        assert isinstance(drift_card[gate_key], bool)
    assert drift_card["cable_pixels_changed"] is True
    assert drift_card["graph_improved"] is True
    assert drift_card["human_review_required"] is True

    # 5. POST /runs for control_A_to_A (mock)
    r_ctrl = client.post("/runs", json={"case_id": "control_A_to_A", "mode": "mock"})
    assert r_ctrl.status_code == 200
    ctrl_card = r_ctrl.json()["result"]
    assert ctrl_card["terminal_state"] == "CONTROL_NO_CHANGE"
    assert ctrl_card["argon_verified"] is False
    assert ctrl_card["cable_pixels_changed"] is False

    # 6. Public asset allowlist vs HTTP 403 Forbidden on private/truth.json and traversal
    for allowed_asset in (
        "current_sld_B.png",
        "old_sld_A.png",
        "old_thermal_rear.png",
        "render0/render_wiring.png",
        "render1/render_wiring.png",
        "result_public.json",
        "patch.json",
    ):
        resp_ok = client.get(f"/runs/{drift_run_id}/assets/{allowed_asset}")
        assert resp_ok.status_code == 200, f"Expected 200 for {allowed_asset}"

    for forbidden_asset in (
        "private/truth.json",
        "truth.json",
        "score.json",
        "../truth.json",
        "render0/../../private/truth.json",
    ):
        resp_bad = client.get(f"/runs/{drift_run_id}/assets/{forbidden_asset}")
        assert resp_bad.status_code == 403, f"Expected 403 for {forbidden_asset}, got {resp_bad.status_code}"

    # 7. Without credentials, POST /runs with mode="live" MUST fail closed (never fabricate PASS or argon_verified)
    import sys

    web_app_mod = sys.modules["twin_repair.web.app"]
    monkeypatch.setattr(
        web_app_mod,
        "resolve_auth_config",
        lambda: {"mode": "mock", "ready": False, "reason": "No GEMINI_API_KEY configured"},
    )
    r_no_creds = client.post("/runs", json={"case_id": "drift_A_to_B", "mode": "live"})
    assert r_no_creds.status_code == 503
    no_creds_data = r_no_creds.json()
    assert no_creds_data["passed"] is False
    assert no_creds_data["argon_verified"] is False
    assert no_creds_data["terminal_state"] == "PROVIDER_ERROR"

    # 8. With credentials present, public live quota (2 runs allowed without judge code, 3rd returns HTTP 429, judge code overrides)
    monkeypatch.setattr(
        web_app_mod,
        "resolve_auth_config",
        lambda: {"mode": "ai_studio", "ready": True, "api_key_present": True},
    )
    monkeypatch.setattr(
        web_app_mod,
        "run_twin_repair",
        lambda case_dir, **kw: dict(live_rec_json["result"], run_id=kw.get("run_id", "stub")),
    )
    r_live1 = client.post("/runs", json={"case_id": "drift_A_to_B", "mode": "live"})
    assert r_live1.status_code == 200
    r_live2 = client.post("/runs", json={"case_id": "drift_A_to_B", "mode": "live"})
    assert r_live2.status_code == 200
    r_live3 = client.post("/runs", json={"case_id": "drift_A_to_B", "mode": "live"})
    assert r_live3.status_code == 429
    assert "Live capacity reached; explore a recorded run" in r_live3.json()["error"]

    r_judge = client.post(
        "/runs",
        json={"case_id": "drift_A_to_B", "mode": "live"},
        headers={"X-Judge-Code": "SECRET_JUDGE_2026"},
    )
    assert r_judge.status_code == 200
