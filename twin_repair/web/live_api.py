"""Asynchronous judge live-run API for the BlackboxDC Twin Repair demo.

Routes (all mounted by ``create_app``):

- ``GET  /``                                       -> interactive judge page (``/live`` redirects here)
- ``GET  /api/live/config``                        -> availability, quota, modes (no secrets)
- ``POST /api/live/challenges``                    -> fresh blinded drawing (201)
- ``POST /api/live/runs``                          -> queue a run for a challenge (202, idempotent)
- ``GET  /api/live/runs/{run_id}``                 -> persisted state, stage rail, result
- ``GET  /api/live/runs/{run_id}/actions``         -> deterministic operator counterfactuals
- ``GET  /api/live/runs/{run_id}/assets/{name}``   -> allowlisted PNG/JSON only

A single background worker executes runs one at a time. Run IDs and status are persisted before any
provider call so a refresh or double tap resumes the same run instead of starting a second paid run.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from blackbox.managed import resolve_auth_config
from twin_repair import judge_live
from twin_repair.report import derive_outcome_fields

WEB_DIR = Path(__file__).resolve().parent
REPO_ROOT = WEB_DIR.parent.parent
THREE_DIR = REPO_ROOT / "blackbox" / "static3d" / "node_modules" / "three"

NO_STORE = {"Cache-Control": "no-store, max-age=0"}

LIVE_STATIC_FILES: Dict[str, tuple[Path, str, str]] = {
    "live.js": (WEB_DIR / "live" / "live.js", "application/javascript; charset=utf-8", "no-cache"),
    "live.css": (WEB_DIR / "live" / "live.css", "text/css; charset=utf-8", "no-cache"),
    "viewer3d.js": (WEB_DIR / "live" / "viewer3d.js", "application/javascript; charset=utf-8", "no-cache"),
}

VENDOR_FILES: Dict[str, Path] = {
    "three.module.min.js": THREE_DIR / "build" / "three.module.min.js",
    "three.core.min.js": THREE_DIR / "build" / "three.core.min.js",
    "addons/controls/OrbitControls.js": THREE_DIR / "examples" / "jsm" / "controls" / "OrbitControls.js",
    "addons/environments/RoomEnvironment.js": THREE_DIR / "examples" / "jsm" / "environments" / "RoomEnvironment.js",
    "addons/geometries/RoundedBoxGeometry.js": THREE_DIR / "examples" / "jsm" / "geometries" / "RoundedBoxGeometry.js",
}

LIVE_RUN_ASSETS: frozenset[str] = frozenset(
    {
        "current_sld.png",
        "old_sld_A.png",
        "old_thermal_rear.png",
        "render0/render_wiring.png",
        "render0/render_rgb_rear.png",
        "render0/render_thermal_rear.png",
        "render0/render_meta.json",
        "render1/render_wiring.png",
        "render1/render_rgb_rear.png",
        "render1/render_thermal_rear.png",
        "render1/render_meta.json",
        "scene0.json",
        "scene1.json",
        "patch.json",
        "patch_raw.json",
        "action_comparison.json",
        "result_public.json",
        "input_manifest.json",
    }
)

RECORDED_RUN_IDS: frozenset[str] = frozenset({"live1"})
ACTIVE_STATES = frozenset({"QUEUED", "RUNNING"})
DONE_STATES = frozenset({"COMPLETED", "FAILED"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


class JudgeLiveService:
    """Persisted judge-run store plus a single background worker thread."""

    def __init__(
        self,
        runs_root: Path,
        *,
        allow_mock: Optional[bool] = None,
        auth_ready_fn: Optional[Callable[[], bool]] = None,
        executor: Optional[Callable[..., Dict[str, Any]]] = None,
        start_worker: bool = True,
    ) -> None:
        self.root = Path(runs_root).resolve() / "judge"
        self.root.mkdir(parents=True, exist_ok=True)
        self.recorded_root = Path(runs_root).resolve()
        self.allow_mock = (os.environ.get("BLACKBOX_ALLOW_MOCK_JUDGE_RUNS", "0") == "1") if allow_mock is None else bool(allow_mock)
        self.auth_ready_fn = auth_ready_fn or (lambda: bool(resolve_auth_config().get("ready")))
        self.executor = executor or judge_live.execute_judge_run
        self.public_quota = _env_int("BLACKBOX_PUBLIC_LIVE_QUOTA", 3)
        self.max_paid_runs = _env_int("BLACKBOX_MAX_PAID_RUNS", 25)
        self.judge_reserve = max(1, _env_int("BLACKBOX_JUDGE_RESERVED_RUNS", 1))
        self.max_challenges_per_10min = _env_int("BLACKBOX_MAX_CHALLENGES_PER_10MIN", 40)
        self.four_state_enabled = os.environ.get("BLACKBOX_ENABLE_FOUR_STATE", "0") == "1"
        self.lock = threading.Lock()
        self.queue: "queue.Queue[str]" = queue.Queue()
        self.active_run_id: Optional[str] = None
        self.challenge_times: List[float] = []
        self.ledger_path = self.root / "_ledger.json"
        self.external_busy_fn: Callable[[], Optional[str]] = lambda: None
        self._recover_interrupted_runs()
        self.worker: Optional[threading.Thread] = None
        if start_worker:
            self.worker = threading.Thread(target=self._worker_loop, name="judge-live-worker", daemon=True)
            self.worker.start()

    # ------------------------------------------------------------------ persistence helpers
    def run_dir(self, run_id: str) -> Optional[Path]:
        if not judge_live.is_valid_run_id(run_id):
            return None
        d = (self.root / run_id).resolve()
        if d.parent != self.root or not d.is_dir():
            return None
        return d

    def _status(self, run_dir: Path) -> Dict[str, Any]:
        return _read_json(run_dir / "status.json")

    def _set_status(self, run_dir: Path, **updates: Any) -> Dict[str, Any]:
        st = self._status(run_dir)
        st.update(updates)
        _write_json(run_dir / "status.json", st)
        if "state" in updates:
            judge_live.append_run_event(
                run_dir,
                {"event": "judge_status", "run_id": run_dir.name, "state": updates["state"], "mode": st.get("mode"), "reason": updates.get("reason")},
            )
        return st

    def ledger(self) -> Dict[str, int]:
        led = _read_json(self.ledger_path)
        return {"public_paid_runs": int(led.get("public_paid_runs", 0)), "judge_paid_runs": int(led.get("judge_paid_runs", 0))}

    def quota_view(self) -> Dict[str, int]:
        led = self.ledger()
        total = led["public_paid_runs"] + led["judge_paid_runs"]
        public_cap = min(self.public_quota, max(0, self.max_paid_runs - self.judge_reserve))
        return {
            "public_remaining": max(0, min(public_cap - led["public_paid_runs"], self.max_paid_runs - self.judge_reserve - total)),
            "judge_remaining": max(0, self.max_paid_runs - total),
        }

    def _recover_interrupted_runs(self) -> None:
        for sub in sorted(self.root.iterdir()):
            if not sub.is_dir() or not judge_live.is_valid_run_id(sub.name):
                continue
            st = self._status(sub)
            if st.get("state") in ACTIVE_STATES:
                reason = "The server restarted before this run finished; no result was issued."
                judge_live.write_failure_result(sub, mode=str(st.get("mode", "full")), terminal_state="INTERNAL_ERROR", reason=reason)
                self._set_status(sub, state="FAILED", finished_at=_utc_now(), reason=reason)

    # ------------------------------------------------------------------ worker
    def _worker_loop(self) -> None:
        while True:
            run_id = self.queue.get()
            try:
                self._execute(run_id)
            finally:
                with self.lock:
                    self.active_run_id = None
                self.queue.task_done()

    def _execute(self, run_id: str) -> None:
        run_dir = self.run_dir(run_id)
        if run_dir is None:
            return
        mode = str(self._status(run_dir).get("mode", "full"))
        self._set_status(run_dir, state="RUNNING", started_at=_utc_now())
        try:
            card = self.executor(run_dir, mode=mode)
            self._set_status(run_dir, state="COMPLETED", finished_at=_utc_now(), terminal_state=card.get("terminal_state"))
        except Exception as exc:  # A crash must become a visible terminal failure.
            reason = f"{exc.__class__.__name__}: {exc}"[:400]
            judge_live.write_failure_result(run_dir, mode=mode, terminal_state="INTERNAL_ERROR", reason=reason)
            self._set_status(run_dir, state="FAILED", finished_at=_utc_now(), reason=reason)

    # ------------------------------------------------------------------ views
    def run_view(self, run_id: str) -> Optional[Dict[str, Any]]:
        if run_id in RECORDED_RUN_IDS:
            return self.recorded_view(run_id)
        run_dir = self.run_dir(run_id)
        if run_dir is None:
            return None
        st = self._status(run_dir)
        manifest = _read_json(run_dir / "input_manifest.json")
        drawing = (manifest.get("images") or {}).get("current_sld.png") or {}
        stages = judge_live.read_stage_events(run_dir)
        started = _parse_utc(st.get("started_at"))
        finished = _parse_utc(st.get("finished_at"))
        elapsed = None
        if started is not None:
            elapsed = round((finished or time.time()) - started, 1)
        state = str(st.get("state", "DRAWING_READY"))
        view: Dict[str, Any] = {
            "run_id": run_id,
            "state": state,
            "mode": st.get("mode"),
            "selection": st.get("selection"),
            "created_at": st.get("created_at"),
            "queued_at": st.get("queued_at"),
            "started_at": st.get("started_at"),
            "finished_at": st.get("finished_at"),
            "elapsed_s": elapsed,
            "stage": next((s.get("label") for s in reversed(stages) if s.get("kind") == "stage"), "New as-built drawing generated"),
            "stages": stages,
            "drawing": {
                "url": f"/api/live/runs/{run_id}/assets/current_sld.png",
                "sha256": drawing.get("sha256"),
                "drawing_no": manifest.get("drawing_no"),
            },
            "status_url": f"/api/live/runs/{run_id}",
            "page_url": f"/?run={run_id}",
            "recorded": False,
        }
        if state in DONE_STATES:
            card = _read_json(run_dir / "result_public.json")
            card.pop("run_dir", None)
            view["result"] = card
            view["source"] = card.get("execution_source")
        return view

    def recorded_view(self, run_id: str) -> Optional[Dict[str, Any]]:
        run_dir = (self.recorded_root / run_id).resolve()
        card = _read_json(run_dir / "result_public.json")
        if not card:
            return None
        run_data = {
            **card,
            "provider_mode": "replay",
            "is_replay": True,
            "patch": _read_json(run_dir / "patch.json") or card.get("patch") or {},
            "score": _read_json(run_dir / "score.json") or {"total_edges": card.get("graph_score", {}).get("total_edges", 0), "before_correct_edges": card.get("graph_score", {}).get("before_correct"), "evidence_boxes_valid": True},
            "action_comparison": _read_json(run_dir / "action_comparison.json"),
            "builder_provenance": _read_json(run_dir / "roles" / "builder_provenance.json"),
            "critic_provenance": _read_json(run_dir / "roles" / "critic_provenance.json"),
        }
        outcome = derive_outcome_fields(run_data)
        vis = dict(card.get("visual_verification") or {})
        if "changed_Pixels_n2_corridor" in vis and "changed_pixels_n2_corridor" not in vis:
            vis["changed_pixels_n2_corridor"] = vis.pop("changed_Pixels_n2_corridor")
        result = {**card, **outcome, "visual_verification": vis, "judge_mode": "replay"}
        result.pop("run_dir", None)
        sld_sha = str(vis.get("current_sld_sha256") or card.get("current_sld_sha256") or "")
        return {
            "run_id": run_id,
            "state": "COMPLETED",
            "mode": "replay",
            "recorded": True,
            "selection": {"drawing": "B", "N2": "F2", "N3": "F1"},
            "stages": judge_live.read_stage_events(run_dir),
            "stage": "Recorded live run",
            "elapsed_s": card.get("total_elapsed_s"),
            "created_at": card.get("generated_at_utc"),
            "drawing": {"url": f"/api/live/runs/{run_id}/assets/current_sld.png", "sha256": sld_sha, "drawing_no": None},
            "status_url": f"/api/live/runs/{run_id}",
            "page_url": f"/?run={run_id}",
            "source": "recorded_live_replay",
            "result": result,
        }

    def resolve_asset(self, run_id: str, name: str) -> Optional[Path]:
        if name not in LIVE_RUN_ASSETS:
            return None
        if run_id in RECORDED_RUN_IDS:
            base = (self.recorded_root / run_id).resolve()
            alias = "current_sld_B.png" if name == "current_sld.png" else name
            candidates = [base / alias, base / "public" / alias]
        else:
            base = self.run_dir(run_id)
            if base is None:
                return None
            candidates = [base / name, base / "public" / name]
        for cand in candidates:
            cand = cand.resolve()
            try:
                cand.relative_to(base)
            except ValueError:
                return None
            if "private" in cand.parts or "input" in cand.relative_to(base).parts:
                return None
            if cand.is_file():
                return cand
        return None

    # ------------------------------------------------------------------ actions
    def create_challenge(self, n2: str, n3: str, selection: Mapping[str, Any]) -> Dict[str, Any]:
        now = time.monotonic()
        with self.lock:
            self.challenge_times = [t for t in self.challenge_times if now - t < 600]
            if len(self.challenge_times) >= self.max_challenges_per_10min:
                raise PermissionError("Too many new drawings were requested; please wait a few minutes.")
            self.challenge_times.append(now)
        run_id = judge_live.new_run_id()
        run_dir = self.root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        _write_json(run_dir / "status.json", {"run_id": run_id, "state": "DRAWING_READY", "created_at": _utc_now(), "selection": dict(selection)})
        try:
            judge_live.build_judge_challenge(run_dir, judge_live.selected_mapping(n2, n3))
        except Exception:
            self._set_status(run_dir, state="FAILED", reason="Drawing generation failed its own verification")
            raise
        judge_live.append_run_event(run_dir, {"event": "judge_status", "run_id": run_id, "state": "DRAWING_READY"})
        return self.run_view(run_id) or {}

    def start_run(self, run_id: str, mode: str, *, judge_authorized: bool, idempotency_key: Optional[str]) -> tuple[int, Dict[str, Any]]:
        run_dir = self.run_dir(run_id)
        if run_dir is None:
            return 404, {"error": "Unknown challenge. Create a new drawing first."}
        if mode not in ("full", "fast", "mock"):
            return 400, {"error": f"Unsupported mode: {mode}"}
        if mode == "mock" and not self.allow_mock:
            return 403, {"error": "Offline rehearsal mode is disabled on this server."}
        paid = mode in ("full", "fast")
        with self.lock:
            st = self._status(run_dir)
            if st.get("state") != "DRAWING_READY":
                # Idempotent: an already-started challenge never triggers a second paid run.
                return 200, {**(self.run_view(run_id) or {}), "idempotent_replay": True}
            if paid and not self.auth_ready_fn():
                return 503, {
                    "error": "Live Argon access is not configured on this server. No run was started.",
                    "passed": False,
                    "argon_verified": False,
                }
            busy = self.active_run_id or self.external_busy_fn()
            if busy is not None:
                return 409, {"error": "Another judge run is in progress.", "active_run_id": busy, "active_page_url": f"/?run={busy}"}
            if paid:
                q = self.quota_view()
                if judge_authorized:
                    if q["judge_remaining"] <= 0:
                        return 429, {"error": "The server's paid-run budget is exhausted."}
                elif q["public_remaining"] <= 0:
                    return 429, {"error": "Public live quota is used up. A judge code is required for more live runs."}
                led = self.ledger()
                led["judge_paid_runs" if judge_authorized else "public_paid_runs"] += 1
                _write_json(self.ledger_path, led)
            self.active_run_id = run_id
            self._set_status(
                run_dir,
                state="QUEUED",
                mode=mode,
                queued_at=_utc_now(),
                idempotency_key=(idempotency_key or "")[:80] or None,
                authorized_by="judge_code" if judge_authorized else "public_quota",
            )
        self.queue.put(run_id)
        return 202, {**(self.run_view(run_id) or {}), "state": "QUEUED"}

    def config(self) -> Dict[str, Any]:
        try:
            ready = bool(self.auth_ready_fn())
        except Exception:
            ready = False
        with self.lock:
            active = self.active_run_id
        return {
            "live_available": ready,
            "modes": {
                "full": {"label": "Full live: Builder + Critic agents", "typical_s": 100, "available": ready},
                "fast": {"label": "Fast: recorded Builder scene, Critic live", "typical_s": 55, "available": ready},
                "mock": {"label": "Offline rehearsal (fixture, not Argon)", "typical_s": 10, "available": self.allow_mock},
            },
            "quota": self.quota_view(),
            "judge_code_supported": bool(os.environ.get("BLACKBOX_JUDGE_CODE", "").strip()),
            "active_run_id": active,
            "four_state_enabled": self.four_state_enabled,
            "default_mode": os.environ.get("BLACKBOX_DEFAULT_JUDGE_MODE", "full") if ready else ("mock" if self.allow_mock else "full"),
            "recorded_run_id": "live1" if (self.recorded_root / "live1" / "result_public.json").is_file() else None,
            "stale_twin": judge_live.stale_twin_preview(),
        }


def build_live_routes(service: JudgeLiveService, judge_code_check: Callable[[Optional[str]], bool]) -> List[Route]:
    async def live_page(request: Request) -> Response:
        return FileResponse(WEB_DIR / "live" / "live.html", media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})

    async def legacy_live_redirect(request: Request) -> Response:
        query = request.url.query
        return RedirectResponse(f"/?{query}" if query else "/", status_code=301)

    async def live_static(request: Request) -> Response:
        name = str(request.path_params.get("name", ""))
        entry = LIVE_STATIC_FILES.get(name)
        if entry is None or not entry[0].is_file():
            return JSONResponse({"error": "Not found"}, status_code=404)
        return FileResponse(entry[0], media_type=entry[1], headers={"Cache-Control": entry[2]})

    async def vendor_static(request: Request) -> Response:
        name = str(request.path_params.get("name", ""))
        path = VENDOR_FILES.get(name)
        if path is None or not path.is_file():
            return JSONResponse({"error": "Not found"}, status_code=404)
        return FileResponse(path, media_type="application/javascript; charset=utf-8", headers={"Cache-Control": "public, max-age=604800, immutable"})

    async def config_endpoint(request: Request) -> JSONResponse:
        return JSONResponse(service.config(), headers=NO_STORE)

    async def create_challenge(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        payload = payload if isinstance(payload, Mapping) else {}
        drawing = payload.get("drawing")
        if drawing in judge_live.DRAWING_PRESETS:
            n2, n3 = judge_live.DRAWING_PRESETS[str(drawing)]
        elif service.four_state_enabled and payload.get("n2") in judge_live.FEEDERS and payload.get("n3") in judge_live.FEEDERS:
            n2, n3 = str(payload["n2"]), str(payload["n3"])
        else:
            return JSONResponse({"error": "Choose drawing 'A' or 'B'."}, status_code=400, headers=NO_STORE)
        selection = {"drawing": drawing if drawing in judge_live.DRAWING_PRESETS else "custom", "N2": n2, "N3": n3}
        try:
            view = service.create_challenge(n2, n3, selection)
        except PermissionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=429, headers=NO_STORE)
        except Exception as exc:
            return JSONResponse({"error": f"Drawing generation failed: {exc.__class__.__name__}"}, status_code=500, headers=NO_STORE)
        return JSONResponse(view, status_code=201, headers=NO_STORE)

    async def start_run(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        payload = payload if isinstance(payload, Mapping) else {}
        run_id = str(payload.get("run_id") or "")
        mode = str(payload.get("mode") or "full")
        code = payload.get("judge_code") or request.headers.get("X-Judge-Code")
        status, body = service.start_run(
            run_id,
            mode,
            judge_authorized=judge_code_check(code),
            idempotency_key=request.headers.get("Idempotency-Key"),
        )
        return JSONResponse(body, status_code=status, headers=NO_STORE)

    async def get_run(request: Request) -> JSONResponse:
        view = service.run_view(str(request.path_params.get("run_id", "")))
        if view is None:
            return JSONResponse({"error": "Run not found"}, status_code=404, headers=NO_STORE)
        return JSONResponse(view, headers=NO_STORE)

    async def get_actions(request: Request) -> JSONResponse:
        run_id = str(request.path_params.get("run_id", ""))
        view = service.run_view(run_id)
        if view is None:
            return JSONResponse({"error": "Run not found"}, status_code=404, headers=NO_STORE)
        result = view.get("result") or {}
        if result.get("visual_repair_status") not in ("REPAIRED", "NO_CHANGE"):
            return JSONResponse(
                {"error": "No verified wiring graph for this run; counterfactuals are not available.", "action_status": "INSUFFICIENT_EVIDENCE"},
                status_code=409,
                headers=NO_STORE,
            )
        scene_path = service.resolve_asset(run_id, "scene1.json") or service.resolve_asset(run_id, "scene0.json")
        scene = _read_json(scene_path) if scene_path else {}
        graph = {str(e.get("rack_id")): str(e.get("feeder_id")) for e in scene.get("edges") or [] if isinstance(e, Mapping)}
        if sorted(graph) != ["N1", "N2", "N3", "N4"]:
            return JSONResponse({"error": "Accepted scene graph is incomplete."}, status_code=409, headers=NO_STORE)
        return JSONResponse({"run_id": run_id, "graph_source": scene_path.name if scene_path else None, **judge_live.counterfactual_actions(graph)}, headers=NO_STORE)

    async def get_asset(request: Request) -> Response:
        run_id = str(request.path_params.get("run_id", ""))
        name = str(request.path_params.get("asset_name", ""))
        path = service.resolve_asset(run_id, name)
        if path is None:
            code = 403 if name not in LIVE_RUN_ASSETS else 404
            return JSONResponse({"error": "Asset is not available."}, status_code=code, headers=NO_STORE)
        media = "application/json" if name.endswith(".json") else "image/png"
        return FileResponse(path, media_type=media, headers={"Cache-Control": "private, max-age=300"})

    return [
        Route("/", live_page, methods=["GET"]),
        Route("/index.html", live_page, methods=["GET"]),
        Route("/live", legacy_live_redirect, methods=["GET"]),
        Route("/static/{name}", live_static, methods=["GET"]),
        Route("/vendor/three/{name:path}", vendor_static, methods=["GET"]),
        Route("/api/live/config", config_endpoint, methods=["GET"]),
        Route("/api/live/challenges", create_challenge, methods=["POST"]),
        Route("/api/live/runs", start_run, methods=["POST"]),
        Route("/api/live/runs/{run_id}", get_run, methods=["GET"]),
        Route("/api/live/runs/{run_id}/actions", get_actions, methods=["GET"]),
        Route("/api/live/runs/{run_id}/assets/{asset_name:path}", get_asset, methods=["GET"]),
    ]
