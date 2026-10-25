"""Tests for judge-created live challenges and the async /api/live endpoints."""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest
from starlette.testclient import TestClient

from blackbox.render_sld import parse_sld_png_topology, render_sld_png
from twin_repair import judge_live
from twin_repair.managed_roles import LiveManagedTwinRepairProvider, load_role_prompt_template
from twin_repair.report import build_public_result_card, derive_outcome_fields
from twin_repair.web.app import create_app
from twin_repair.web.live_api import JudgeLiveService

REPO = Path(__file__).resolve().parent.parent

EXPECTED = {
    ("F1", "F2"): ("NO_CHANGE", "SAFE_RECEIVER_FOUND", ["N3"], "4/4 -> 4/4"),
    ("F2", "F1"): ("REPAIRED", "SAFE_RECEIVER_FOUND", ["N2"], "2/4 -> 4/4"),
    ("F1", "F1"): ("REPAIRED", "NO_SAFE_ACTION", [], "3/4 -> 4/4"),
    ("F2", "F2"): ("REPAIRED", "MULTIPLE_SAFE", ["N2", "N3"], "3/4 -> 4/4"),
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _new_challenge(tmp_path: Path, n2: str, n3: str) -> Path:
    run_dir = tmp_path / judge_live.new_run_id()
    judge_live.build_judge_challenge(run_dir, judge_live.selected_mapping(n2, n3))
    return run_dir


# --------------------------------------------------------------------------- generator / parser


def test_mapping_renderer_roundtrips_all_four_topologies(tmp_path: Path) -> None:
    for n2, n3 in itertools.product(("F1", "F2"), repeat=2):
        mapping = judge_live.selected_mapping(n2, n3)
        out = tmp_path / f"{n2}{n3}.png"
        render_sld_png("judge-test", out, mapping=mapping)
        assert parse_sld_png_topology(out)["mapping"] == mapping


def test_canonical_golden_sld_unchanged(tmp_path: Path) -> None:
    out = tmp_path / "b.png"
    render_sld_png("case_B", out)
    assert parse_sld_png_topology(out)["mapping"] == {"N1": "F1", "N2": "F2", "N3": "F1", "N4": "F2"}


def test_selected_mapping_rejects_out_of_scope_values() -> None:
    with pytest.raises(ValueError):
        judge_live.selected_mapping("F3", "F1")
    with pytest.raises(ValueError):
        judge_live.build_judge_challenge(Path("/tmp/not-a-run-id"), {"N1": "F2", "N2": "F1", "N3": "F2", "N4": "F2"})


def test_each_challenge_is_fresh_and_blind(tmp_path: Path) -> None:
    a = _new_challenge(tmp_path, "F2", "F1")
    b = _new_challenge(tmp_path, "F2", "F1")
    sha_a = _sha(a / "public" / "current_sld.png")
    assert sha_a != _sha(b / "public" / "current_sld.png"), "every click must produce new drawing bytes"
    manifest = json.loads((a / "input_manifest.json").read_text())
    rec = manifest["images"]["current_sld.png"]
    assert rec["sha256"] == sha_a and (rec["width_px"], rec["height_px"]) == (1280, 800)
    assert rec["size_bytes"] <= 512 * 1024

    public_state = json.loads((a / "input" / "public" / "public_state.json").read_text())
    assert "revision_current" not in public_state
    assert public_state["case_id"].startswith("case-")
    judge_live.assert_judge_packet_is_blind(public_state, "public_state")
    # No SVG (which encodes the mapping in text) and no answer-bearing filename in public dirs.
    for pub in (a / "public", a / "input" / "public"):
        names = {p.name for p in pub.iterdir()}
        assert not any(n.endswith(".svg") for n in names)
        assert "current_sld_B.png" not in names and "current_sld_A.png" not in names
    truth = json.loads((a / "input" / "private" / "truth.json").read_text())
    assert truth["judge_challenge"] is True
    assert truth["current_edges"] == {"N1": "F1", "N2": "F2", "N3": "F1", "N4": "F2"}


def test_neutral_critic_prompt_has_no_answer_hints() -> None:
    text = load_role_prompt_template("critic_neutral")
    for hint in ("current_sld_B", "drift_A_to_B", "<A_OR_B>", '"A" or "B"'):
        assert hint not in text
    assert 'target_revision = "UNKNOWN"' in text


class _CapturingInteractions:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        raise RuntimeError("stop after capturing the provider request")


class _CapturingClient:
    def __init__(self) -> None:
        self.interactions = _CapturingInteractions()


def test_exact_critic_provider_request_is_blind(tmp_path: Path) -> None:
    """Scan the real provider request (prompt, mounted files, image bytes), not just our dicts."""
    run_dir = _new_challenge(tmp_path, "F2", "F1")
    client = _CapturingClient()
    provider = LiveManagedTwinRepairProvider(client=client)
    card = judge_live.execute_judge_run(run_dir, mode="fast", provider=provider)
    assert card["terminal_state"] == "PROVIDER_ERROR"
    assert card["visual_repair_status"] == "REJECTED"
    assert card["action_status"] == "INSUFFICIENT_EVIDENCE"
    assert card["passed"] is False and card["argon_verified"] is False

    assert len(client.interactions.calls) == 1
    req = client.interactions.calls[0]
    blob = json.dumps(req)
    truth = json.loads((run_dir / "input" / "private" / "truth.json").read_text())
    for forbidden in ("drift_A_to_B", "control_A_to_A", "current_sld_B", "revision_current", "current_edges", "expected", "truth.json"):
        assert forbidden not in blob, forbidden
    assert '"F2", "N3": "F1"' not in blob and "selected" not in blob
    text_part = req["input"][0]["text"]
    assert "current_sld.png" in text_part and "UNKNOWN" in text_part
    images = [p for p in req["input"] if p.get("type") == "image"]
    sent_sld = hashlib.sha256(base64.b64decode(images[0]["data"])).hexdigest()
    assert sent_sld == _sha(run_dir / "public" / "current_sld.png")
    mounted = {s["target"]: s["content"] for s in req["environment"]["sources"]}
    judge_live.assert_judge_packet_is_blind(json.loads(mounted["public_state.json"]), "mounted_public_state")


# --------------------------------------------------------------------------- outcomes


@pytest.mark.parametrize("n2,n3", list(EXPECTED))
def test_mock_outcome_categories(tmp_path: Path, n2: str, n3: str) -> None:
    run_dir = _new_challenge(tmp_path, n2, n3)
    card = judge_live.execute_judge_run(run_dir, mode="mock")
    visual, action, safe, score = EXPECTED[(n2, n3)]
    assert card["visual_repair_status"] == visual
    assert card["action_status"] == action
    assert card["safe_receivers"] == safe
    assert card["graph_score"]["summary"] == score
    assert card["execution_source"] == "mock_fixture"
    assert card["argon_verified"] is False
    if action == "MULTIPLE_SAFE":
        assert card["tie_break_rule"]
    if visual == "NO_CHANGE":
        assert not (run_dir / "scene1.json").exists() or json.loads((run_dir / "scene1.json").read_text()) == json.loads((run_dir / "scene0.json").read_text())
    # Copy is derived from this run's solver output; never the canonical B numbers on other graphs.
    if (n2, n3) == ("F2", "F2"):
        assert "48" not in card["headlines"]["old_choice_banner"]


@pytest.mark.parametrize("mode,visual", [("abstain", "ABSTAINED"), ("invalid_patch", "REJECTED"), ("forged_hash", "REJECTED"), ("timeout", "REJECTED")])
def test_negative_provider_outcomes_never_show_safe_action(tmp_path: Path, mode: str, visual: str) -> None:
    run_dir = _new_challenge(tmp_path, "F2", "F1")
    truth = json.loads((run_dir / "input" / "private" / "truth.json").read_text())
    provider = judge_live.JudgeMockProvider(truth["current_edges"], mode=mode)
    card = judge_live.execute_judge_run(run_dir, mode="mock", provider=provider)
    assert card["visual_repair_status"] == visual
    assert card["action_status"] == "INSUFFICIENT_EVIDENCE"
    assert card["recommended_receiver"] is None
    assert card["terminal_state"] != "PASS"


def test_input_binding_mismatch_is_rejected(tmp_path: Path) -> None:
    run_dir = _new_challenge(tmp_path, "F2", "F1")
    judge_live.execute_judge_run(run_dir, mode="mock")
    # Tamper with the browser-visible drawing after the run and re-finalize.
    (run_dir / "public" / "current_sld.png").write_bytes((run_dir / "public" / "old_sld_A.png").read_bytes())
    card = judge_live.finalize_judge_result(run_dir, mode="mock")
    assert card["visual_repair_status"] == "REJECTED"
    assert card["action_status"] == "INSUFFICIENT_EVIDENCE"


def test_report_has_no_canonical_fallbacks_without_evidence() -> None:
    card = build_public_result_card({"terminal_state": "PASS", "provider_mode": "live", "patch": {"decision": "repair"}})
    assert card["action_status"] == "INSUFFICIENT_EVIDENCE"
    assert card["action_summary"]["stale_recommended_receiver"] is None
    assert card["action_summary"]["n2_f1_current_a"] is None
    blob = json.dumps(card)
    for canonical in ("48/44", "40/44", "28/50", "52.53"):
        assert canonical not in blob
    assert "changed_pixels_n2_corridor" in card["visual_verification"]
    assert "changed_Pixels_n2_corridor" not in card["visual_verification"]
    assert derive_outcome_fields({"terminal_state": "ABSTAINED"})["visual_repair_status"] == "ABSTAINED"


def test_judge_truth_without_edges_does_not_fall_back(tmp_path: Path) -> None:
    run_dir = _new_challenge(tmp_path, "F2", "F1")
    truth_path = run_dir / "input" / "private" / "truth.json"
    truth = json.loads(truth_path.read_text())
    truth.pop("current_edges")
    truth_path.write_text(json.dumps(truth))
    provider = judge_live.JudgeMockProvider({"N1": "F1", "N2": "F2", "N3": "F1", "N4": "F2"})
    with pytest.raises(ValueError, match="current_edges"):
        judge_live.execute_judge_run(run_dir, mode="mock", provider=provider)


# --------------------------------------------------------------------------- HTTP API


def _client(tmp_path: Path, *, ready: bool = False, executor: Any = None, allow_mock: bool = True) -> TestClient:
    runs = tmp_path / "runs"
    runs.mkdir(exist_ok=True)
    service = JudgeLiveService(runs, allow_mock=allow_mock, auth_ready_fn=lambda: ready, executor=executor)
    return TestClient(create_app(runs_root=runs, judge_service=service))


def _wait_done(client: TestClient, run_id: str, timeout_s: float = 120.0) -> Dict[str, Any]:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        view = client.get(f"/api/live/runs/{run_id}").json()
        if view["state"] in ("COMPLETED", "FAILED"):
            return view
        time.sleep(0.3)
    raise AssertionError("run did not finish")


def test_api_mock_flow_idempotency_assets_and_actions(tmp_path: Path) -> None:
    client = _client(tmp_path)
    cfg = client.get("/api/live/config").json()
    assert cfg["live_available"] is False and cfg["stale_twin"]["recommended_receiver"] == "N3"
    assert "judge_code" not in json.dumps(cfg).replace("judge_code_supported", "")

    assert client.post("/api/live/challenges", json={"drawing": "Z"}).status_code == 400
    assert client.post("/api/live/challenges", json={"n2": "F1", "n3": "F1"}).status_code == 400  # four-state disabled
    created = client.post("/api/live/challenges", json={"drawing": "B"})
    assert created.status_code == 201
    run_id = created.json()["run_id"]
    assert created.json()["state"] == "DRAWING_READY"

    png = client.get(f"/api/live/runs/{run_id}/assets/current_sld.png")
    assert png.status_code == 200 and hashlib.sha256(png.content).hexdigest() == created.json()["drawing"]["sha256"]
    for bad in ("private/truth.json", "input/private/truth.json", "../status.json", "truth.json", "input/public/public_state.json"):
        assert client.get(f"/api/live/runs/{run_id}/assets/{bad}").status_code in (403, 404)

    started = client.post("/api/live/runs", json={"run_id": run_id, "mode": "mock"}, headers={"Idempotency-Key": "k1"})
    assert started.status_code == 202
    again = client.post("/api/live/runs", json={"run_id": run_id, "mode": "mock"}, headers={"Idempotency-Key": "k1"})
    assert again.status_code == 200 and again.json()["idempotent_replay"] is True

    view = _wait_done(client, run_id)
    assert view["state"] == "COMPLETED"
    result = view["result"]
    assert result["visual_repair_status"] == "REPAIRED" and result["recommended_receiver"] == "N2"
    assert result["execution_source"] == "mock_fixture"
    states = [s["state"] for s in view["stages"] if s["kind"] == "stage"]
    assert states[0] == "DRAWING_READY" and "QUEUED" in states and "CRITIC_RUNNING" in states and states[-1] == "COMPLETED"
    assert "truth" not in json.dumps(view).lower().replace("ground", "")

    acts = client.get(f"/api/live/runs/{run_id}/actions").json()
    assert acts["graph"] == {"N1": "F1", "N2": "F2", "N3": "F1", "N4": "F2"}
    by_target = {o["target_rack"]: o for o in acts["outcomes"]}
    assert by_target["N3"]["safe"] is False and by_target["N2"]["safe"] is True
    assert by_target[None]["safe"] is False  # no action exceeds the 90 C case limit

    assert client.get(f"/api/live/runs/{run_id}/assets/render1/render_wiring.png").status_code == 200
    assert client.get("/api/live/runs/jr-000000000000").status_code == 404


def test_api_live_without_credentials_is_503_and_not_started(tmp_path: Path) -> None:
    client = _client(tmp_path, ready=False)
    run_id = client.post("/api/live/challenges", json={"drawing": "A"}).json()["run_id"]
    resp = client.post("/api/live/runs", json={"run_id": run_id, "mode": "full"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["passed"] is False and body["argon_verified"] is False
    assert client.get(f"/api/live/runs/{run_id}").json()["state"] == "DRAWING_READY"


def test_api_mock_disabled_by_default(tmp_path: Path) -> None:
    client = _client(tmp_path, allow_mock=False)
    run_id = client.post("/api/live/challenges", json={"drawing": "A"}).json()["run_id"]
    assert client.post("/api/live/runs", json={"run_id": run_id, "mode": "mock"}).status_code == 403


def test_api_quota_busy_and_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLACKBOX_PUBLIC_LIVE_QUOTA", "1")
    monkeypatch.setenv("BLACKBOX_MAX_PAID_RUNS", "3")
    monkeypatch.setenv("BLACKBOX_JUDGE_CODE", "secret-code")
    gate = {"release": False}

    def slow_crash(run_dir: Path, *, mode: str) -> Dict[str, Any]:
        while not gate["release"]:
            time.sleep(0.05)
        raise RuntimeError("simulated worker crash")

    client = _client(tmp_path, ready=True, executor=slow_crash)
    r1 = client.post("/api/live/challenges", json={"drawing": "B"}).json()["run_id"]
    r2 = client.post("/api/live/challenges", json={"drawing": "B"}).json()["run_id"]
    assert client.post("/api/live/runs", json={"run_id": r1, "mode": "full"}).status_code == 202
    busy = client.post("/api/live/runs", json={"run_id": r2, "mode": "full"})
    assert busy.status_code == 409 and busy.json()["active_run_id"] == r1
    gate["release"] = True
    view = _wait_done(client, r1)
    assert view["state"] == "FAILED"
    assert view["result"]["terminal_state"] == "INTERNAL_ERROR"
    assert view["result"]["visual_repair_status"] == "REJECTED"
    assert view["result"]["passed"] is False

    assert client.post("/api/live/runs", json={"run_id": r2, "mode": "full"}).status_code == 429  # public quota used
    ok = client.post("/api/live/runs", json={"run_id": r2, "mode": "full", "judge_code": "secret-code"})
    assert ok.status_code == 202
    _wait_done(client, r2)


def test_restart_marks_interrupted_runs_failed(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    svc = JudgeLiveService(runs, allow_mock=True, auth_ready_fn=lambda: False, start_worker=False)
    view = svc.create_challenge("F2", "F1", {"drawing": "B"})
    run_dir = svc.run_dir(view["run_id"])
    svc._set_status(run_dir, state="RUNNING", mode="full", started_at=judge_live.utc_now_iso())
    svc2 = JudgeLiveService(runs, allow_mock=True, auth_ready_fn=lambda: False, start_worker=False)
    after = svc2.run_view(view["run_id"])
    assert after["state"] == "FAILED"
    assert after["result"]["visual_repair_status"] == "REJECTED"


def test_recorded_live1_view_is_labeled_replay(tmp_path: Path) -> None:
    if not (REPO / "runs" / "live1" / "result_public.json").is_file():
        pytest.skip("live1 not present")
    svc = JudgeLiveService(REPO / "runs", start_worker=False, auth_ready_fn=lambda: False)
    view = svc.recorded_view("live1")
    res = view["result"]
    assert res["execution_source"] == "recorded_live_replay"
    assert res["visual_repair_status"] == "REPAIRED" and res["recommended_receiver"] == "N2"
    assert res["visual_verification"]["changed_pixels_n2_corridor"] > 0
    assert svc.resolve_asset("live1", "current_sld.png").name == "current_sld_B.png"
    assert svc.resolve_asset("live1", "private/truth.json") is None


def test_role_heartbeats_are_coalesced(tmp_path: Path) -> None:
    from twin_repair.judge_live import read_stage_events

    events = [{"event": "state_transition", "state": "BUILDER_RUNNING", "timestamp_utc": "t0"}]
    for i in range(5):
        events.append({"event": "role_progress", "role": "builder", "interaction_id": "i1", "status": "in_progress", "step_count": 0, "elapsed_s": float(i), "timestamp_utc": f"t{i + 1}"})
    events.append({"event": "role_progress", "role": "builder", "interaction_id": "i1", "status": "completed", "step_count": 7, "elapsed_s": 9.0, "timestamp_utc": "t9"})
    (tmp_path / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    stages = read_stage_events(tmp_path)
    roles = [s for s in stages if s["kind"] == "role"]
    assert [r["status"] for r in roles] == ["in_progress", "completed"]
    assert roles[0]["elapsed_s"] == 4.0 and roles[0]["first_at_utc"] == "t1"
