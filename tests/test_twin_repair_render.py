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
    """Verify Phase F web endpoints, 4-state UI, live quota, judge override, and HTTP 403 asset security."""
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
    assert "Repair 3D Twin" in r_index.text

    assert client.get("/app.js").status_code == 200
    assert client.get("/styles.css").status_code == 200

    # 2. Cases endpoint
    r_cases = client.get("/cases")
    assert r_cases.status_code == 200
    cases_data = r_cases.json()
    case_ids = {c["case_id"] for c in cases_data["cases"]}
    assert case_ids == {"drift_A_to_B", "control_A_to_A"}
    assert cases_data["public_live_quota_remaining"] == 2

    # 3. POST /runs for drift_A_to_B (mock)
    r_drift = client.post("/runs", json={"case_id": "drift_A_to_B", "mode": "mock"})
    assert r_drift.status_code == 200
    drift_payload = r_drift.json()
    drift_run_id = drift_payload["run_id"]
    drift_card = drift_payload["result"]
    assert drift_card["terminal_state"] == "PASS"
    for gate_key in REQUIRED_GATE_KEYS:
        assert gate_key in drift_card
        assert isinstance(drift_card[gate_key], bool)
    assert drift_card["cable_pixels_changed"] is True
    assert drift_card["graph_improved"] is True
    assert drift_card["human_review_required"] is True
    assert (
        drift_card["headlines"]["result_caption"]
        == "Argon corrected the twin's wiring before the operator relied on its old recommendation."
    )

    # 4. POST /runs for control_A_to_A (mock)
    r_ctrl = client.post("/runs", json={"case_id": "control_A_to_A", "mode": "mock"})
    assert r_ctrl.status_code == 200
    ctrl_card = r_ctrl.json()["result"]
    assert ctrl_card["terminal_state"] == "CONTROL_NO_CHANGE"
    assert ctrl_card["cable_pixels_changed"] is False
    assert (
        ctrl_card["headlines"]["result_caption"]
        == "The drawing and twin already agree; no repair was made."
    )

    # 5. GET /runs/{run_id}
    r_get_run = client.get(f"/runs/{drift_run_id}")
    assert r_get_run.status_code == 200
    assert r_get_run.json()["run_id"] == drift_run_id

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

    # 7. Public live quota (2 runs allowed without judge code, 3rd returns HTTP 429, judge code overrides)
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
