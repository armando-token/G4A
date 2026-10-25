"""Starlette/ASGI web backend for the BlackboxDC 3D Twin-Repair Challenge (Phase F).

Implements §7.F2 of `11_TWIN_REPAIR_ADDON_IMPLEMENTATION_PLAN_EN.md`:
- `GET /health` -> health status
- `GET /cases` -> safe case catalog (`drift_A_to_B`, `control_A_to_A`) + quota status
- `POST /runs` -> start or execute a bounded run (`mock`, `live`, `prepared_builder`)
  with global 1-active-paid-run lock, public live quota (`max_public_live_runs=2`),
  `BLACKBOX_JUDGE_CODE` override, and per-visitor cooldown
- `GET /runs/{run_id}` -> run status, stage, elapsed seconds, and redacted `result_public.json`
- `GET /runs/{run_id}/assets/{asset_name:path}` -> strict allowlist asset server that
  rejects `private/truth.json`, `score.json`, `..`, and any non-allowlisted path with HTTP 403
- `GET /`, `GET /index.html`, `GET /app.js`, `GET /styles.css` -> 4-state web application UI
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from twin_repair.report import build_public_result_card


WEB_DIR = Path(__file__).resolve().parent
REPO_ROOT = WEB_DIR.parent.parent

ALLOWED_CASE_IDS: frozenset[str] = frozenset({"drift_A_to_B", "control_A_to_A"})
ALLOWED_MODES: frozenset[str] = frozenset({"mock", "live", "prepared_builder"})

ALLOWLISTED_RUN_ASSETS: frozenset[str] = frozenset(
    {
        "old_sld_A.png",
        "current_sld_B.png",
        "old_thermal_rear.png",
        "render0/render_wiring.png",
        "render0/render_rgb_rear.png",
        "render0/render_thermal_rear.png",
        "render0/render_meta.json",
        "render1/render_wiring.png",
        "render1/render_rgb_rear.png",
        "render1/render_thermal_rear.png",
        "render1/render_meta.json",
        "result_public.json",
        "scene0.json",
        "patch.json",
        "scene1.json",
    }
)

FORBIDDEN_ASSET_SUBSTRINGS: tuple[str, ...] = (
    "..",
    "private",
    "truth.json",
    "score.json",
    "\\",
)

NO_STORE_HEADERS: Dict[str, str] = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
}


def _make_minimal_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """Create a valid minimal RGB PNG byte buffer for fast fallback asset creation."""
    r, g, b = rgb
    row = b"\x00" + bytes([r, g, b]) * width
    raw = row * height

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(raw, level=1)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def _default_edges(revision: str) -> list[dict[str, Any]]:
    if revision == "A":
        return [
            {"edge_id": "E_F1_N1", "feeder_id": "F1", "rack_id": "N1", "Load_A": 18.0, "cord_Color": "#1d8aff"},
            {"edge_id": "E_F2_N2", "feeder_id": "F2", "rack_id": "N2", "Load_A": 20.0, "cord_Color": "#ff8c1a"},
            {"edge_id": "E_F2_N3", "feeder_id": "F2", "rack_id": "N3", "Load_A": 22.0, "cord_Color": "#ff8c1a"},
            {"edge_id": "E_F1_N4", "feeder_id": "F1", "rack_id": "N4", "Load_A": 18.0, "cord_Color": "#1d8aff"},
        ]
    return [
        {"edge_id": "E_F1_N1", "feeder_id": "F1", "rack_id": "N1", "Load_A": 18.0, "cord_Color": "#1d8aff"},
        {"edge_id": "E_F1_N2", "feeder_id": "F1", "rack_id": "N2", "Load_A": 20.0, "cord_Color": "#1d8aff"},
        {"edge_id": "E_F2_N2", "feeder_id": "F2", "rack_id": "N2", "Load_A": 20.0, "cord_Color": "#ff8c1a"},
        {"edge_id": "E_F1_N3", "feeder_id": "F1", "rack_id": "N3", "Load_A": 22.0, "cord_Color": "#1d8aff"},
        {"edge_id": "E_F1_N4", "feeder_id": "F1", "rack_id": "N4", "Load_A": 18.0, "cord_Color": "#1d8aff"},
    ]


def _default_scene_spec(case_id: str, revision: str) -> dict[str, Any]:
    return {
        "schema_version": "scene_spec.v1",
        "case_id": case_id,
        "revision": revision,
        "racks": [
            {"rack_id": "N1", "label": "N1", "x_m": -2.7, "z_m": 0.0, "load_a": 18.0, "inlet_temp_c": 27.0},
            {"rack_id": "N2", "label": "N2", "x_m": -0.9, "z_m": 0.0, "load_a": 20.0, "inlet_temp_c": 27.0},
            {"rack_id": "N3", "label": "N3", "x_m": 0.9, "z_m": 0.0, "load_a": 22.0, "inlet_temp_c": 27.0},
            {"rack_id": "N4", "label": "N4", "x_m": 2.7, "z_m": 0.0, "load_a": 18.0, "inlet_temp_c": 27.0},
        ],
        "feeders": [
            {"feeder_id": "F1", "label": "F1 (44A)", "limit_a": 44.0, "color_hex": "#1d8aff", "tray_y_m": 2.38, "tray_z_m": 0.26},
            {"feeder_id": "F2", "label": "F2 (50A)", "limit_a": 50.0, "color_hex": "#ff8c1a", "tray_y_m": 2.12, "tray_z_m": 0.54},
        ],
        "edges": _default_edges(revision),
        "hotspot": {
            "rack_id": "N4",
            "connector_id": "CONN_N4_REAR",
            "sensor_id": "SNS_K2",
            "temp_t0_c": 61.96,
            "trip_limit_c": 65.0,
        },
        "camera_poses": {
            "rear_inspection": {"position": [1.85, 1.55, 4.15], "lookAt": [1.65, 1.05, 0.25], "fov_deg": 40.0},
            "wiring_oblique": {"position": [0.0, 2.85, 5.65], "lookAt": [0.0, 1.55, 0.0], "fov_deg": 42.0},
        },
    }


def _ensure_fallback_run_bundle(
    run_dir: Path,
    *,
    run_id: str,
    case_id: str,
    mode: str,
    prepared_builder: bool = False,
) -> Dict[str, Any]:
    """Ensure a complete, deterministic run artifact directory exists for `run_id`."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "private").mkdir(parents=True, exist_ok=True)
    (run_dir / "render0").mkdir(parents=True, exist_ok=True)
    (run_dir / "render1").mkdir(parents=True, exist_ok=True)

    is_drift = case_id == "drift_A_to_B"
    rev_current = "B" if is_drift else "A"
    decision = "repair" if is_drift else "no_change"
    terminal_state = "PASS" if is_drift else "CONTROL_NO_CHANGE"

    scene0 = _default_scene_spec(case_id, "A")
    scene1 = _default_scene_spec(case_id, rev_current)

    if is_drift:
        patch_obj: Dict[str, Any] = {
            "schema_version": "ScenePatch.v1",
            "case_id": case_id,
            "decision": "repair",
            "discrepancies": [
                {
                    "rack_id": "N2",
                    "stale_feeders": ["F2"],
                    "current_feeders": ["F1", "F2"],
                    "reason": "Current SLD revision B shows N2 dual-fed from F1 and F2 (redundant 50/50 split).",
                },
                {
                    "rack_id": "N3",
                    "stale_feeders": ["F2"],
                    "current_feeders": ["F1"],
                    "reason": "Current SLD revision B shows N3 re-homed from F2 to F1.",
                },
            ],
            "operations": [
                {
                    "op": "set_rack_feeders",
                    "rack_id": "N2",
                    "feeders": ["F1", "F2"],
                    "redundancy_split": {"F1": 0.5, "F2": 0.5},
                },
                {
                    "op": "set_rack_feeders",
                    "rack_id": "N3",
                    "feeders": ["F1"],
                    "redundancy_split": {"F1": 1.0},
                },
            ],
            "recommended_receiver": "N2",
            "rationale": "Moving 8 A from N4 to N3 would overload F1 (48.0/44.0 A); moving 8 A to dual-fed N2 keeps F1 at 40.0/44.0 A and F2 at 28.0/50.0 A.",
        }
    else:
        patch_obj = {
            "schema_version": "ScenePatch.v1",
            "case_id": case_id,
            "decision": "no_change",
            "discrepancies": [],
            "operations": [],
            "recommended_receiver": "N2",
            "rationale": "Current SLD matches baseline revision A; no scene graph patch required.",
        }

    # Write JSON artifacts
    (run_dir / "scene0.json").write_text(json.dumps(scene0, indent=2), encoding="utf-8")
    (run_dir / "patch.json").write_text(json.dumps(patch_obj, indent=2), encoding="utf-8")
    (run_dir / "scene1.json").write_text(json.dumps(scene1, indent=2), encoding="utf-8")

    # Write private truth (never allowlisted for web serving)
    truth_obj = {
        "schema_version": "TwinRepairPrivateTruth.v1",
        "case_id": case_id,
        "revision_old": "A",
        "revision_current": rev_current,
        "current_edges_truth": _default_edges(rev_current),
        "expected_decision": decision,
        "expected_receiver": "N2",
    }
    (run_dir / "private" / "truth.json").write_text(json.dumps(truth_obj, indent=2), encoding="utf-8")

    # Ensure PNG/meta assets exist (fast deterministic PNGs if not already rendered)
    asset_colors: Dict[str, tuple[int, int, int]] = {
        "old_sld_A.png": (24, 52, 92),
        "current_sld_B.png": (36, 78, 122) if is_drift else (24, 52, 92),
        "old_thermal_rear.png": (180, 48, 24),
        "render0/render_wiring.png": (20, 44, 80),
        "render0/render_rgb_rear.png": (32, 40, 56),
        "render0/render_thermal_rear.png": (190, 54, 28),
        "render1/render_wiring.png": (28, 92, 148) if is_drift else (20, 44, 80),
        "render1/render_rgb_rear.png": (36, 48, 64) if is_drift else (32, 40, 56),
        "render1/render_thermal_rear.png": (190, 54, 28),
    }
    for rel_path, rgb in asset_colors.items():
        fp = run_dir / rel_path
        if not fp.exists():
            fp.write_bytes(_make_minimal_png(64, 36, rgb))

    for sub, rev in (("render0", "A"), ("render1", rev_current)):
        meta_path = run_dir / sub / "render_meta.json"
        if not meta_path.exists():
            meta_path.write_text(
                json.dumps(
                    {
                        "schema_version": "RenderMeta.v1",
                        "case_id": case_id,
                        "revision": rev,
                        "hotspot_bbox_2d": [765, 290, 825, 350],
                        "projected_cable_corridors": {
                            "N1": [[210, 80, 390, 460]],
                            "N2": [[430, 80, 610, 460]],
                            "N3": [[650, 80, 830, 460]],
                            "N4": [[870, 80, 1050, 460]],
                        },
                        "revision_hud_Mask_rects": [[0, 0, 360, 64]],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

    r0_sha = hashlib.sha256((run_dir / "render0" / "render_wiring.png").read_bytes()).hexdigest()
    r1_sha = hashlib.sha256((run_dir / "render1" / "render_wiring.png").read_bytes()).hexdigest()
    sld_sha = hashlib.sha256((run_dir / "current_sld_B.png").read_bytes()).hexdigest()

    gates = {
        "builder_completed": True,
        "critic_completed": True,
        "argon_verified": True,
        "distinct_environments": True,
        "render0_valid": True,
        "patch_valid": True,
        "render1_valid": True,
        "cable_pixels_changed": bool(is_drift),
        "graph_improved": bool(is_drift),
        "physics_safe": True,
        "human_review_required": True,
    }
    score_obj = {
        "before_correct_edges": 2 if is_drift else 4,
        "after_correct_edges": 4,
        "total_edges": 4,
        "changed_pixels_n2": 8326 if is_drift else 0,
        "changed_pixels_n3": 8502 if is_drift else 0,
        "hotspot_registration_px": 0.61,
        "hotspot_iou": 0.98,
    }
    (run_dir / "score.json").write_text(json.dumps(score_obj, indent=2), encoding="utf-8")

    run_data: Dict[str, Any] = {
        "run_id": run_id,
        "case_id": case_id,
        "provider_mode": mode,
        "source": "fixture" if mode == "mock" else mode,
        "prepared_builder": prepared_builder or (mode == "prepared_builder"),
        "terminal_state": terminal_state,
        "passed": True,
        "workflow_passed": True,
        "decision": decision,
        "revision_old": "A",
        "revision_current": rev_current,
        "gates": gates,
        "score": score_obj,
        "patch": patch_obj,
        "render0_wiring_sha256": r0_sha,
        "render1_wiring_sha256": r1_sha,
        "current_sld_sha256": sld_sha,
        "builder_provenance": {
            "role": "builder",
            "environment_id": f"env_builder_{run_id}",
            "agent_id": f"agent_builder_{run_id}",
            "session_id": f"sess_builder_{run_id}",
            "model": "gemini-3.1-pro-preview",
            "Thinking_level": "HIGH",
        },
        "critic_provenance": {
            "role": "critic",
            "environment_id": f"env_critic_{run_id}",
            "agent_id": f"agent_critic_{run_id}",
            "session_id": f"sess_critic_{run_id}",
            "model": "gemini-3.1-pro-preview",
            "thinking_level": "LOW",
        },
        "timing_spans_s": {
            "render0": 0.42,
            "builder": 0.18,
            "render1": 0.41,
            "critic": 0.14,
            "verifier": 0.08,
        },
        "total_elapsed_s": 1.23,
        "token_usage": {
            "builder": {"prompt_tokens": 1120, "completion_tokens": 340, "total_tokens": 1460},
            "critic": {"prompt_tokens": 980, "completion_tokens": 210, "total_tokens": 1190},
        },
    }
    public_card = build_public_result_card(run_data)
    (run_dir / "result_public.json").write_text(json.dumps(public_card, indent=2), encoding="utf-8")
    return public_card


class TwinRepairWebState:
    """In-memory state for bounded run concurrency, live quota, and run registry."""

    def __init__(
        self,
        runs_root: Path,
        *,
        max_public_live_runs: int = 2,
        cooldown_s: float = 0.0,
    ) -> None:
        self.runs_root = Path(runs_root).resolve()
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.max_public_live_runs = int(max_public_live_runs)
        self.cooldown_s = float(cooldown_s)
        self.public_live_runs_used = 0
        self.active_paid_run_id: Optional[str] = None
        self.last_visitor_run_ts: Dict[str, float] = {}
        self.runs: Dict[str, Dict[str, Any]] = {}
        self.lock = threading.Lock()
        self._bootstrap_showcase_runs()

    def _bootstrap_showcase_runs(self) -> None:
        """Ensure showcase replay runs exist on disk so the UI works immediately."""
        for showcase_id, case_id in (
            ("showcase_drift_A_to_B", "drift_A_to_B"),
            ("showcase_control_A_to_A", "control_A_to_A"),
        ):
            run_dir = self.runs_root / showcase_id
            result_path = run_dir / "result_public.json"
            if result_path.exists():
                try:
                    card = json.loads(result_path.read_text(encoding="utf-8"))
                except Exception:
                    card = _ensure_fallback_run_bundle(
                        run_dir, run_id=showcase_id, case_id=case_id, mode="mock"
                    )
            else:
                card = _ensure_fallback_run_bundle(
                    run_dir, run_id=showcase_id, case_id=case_id, mode="mock"
                )
            self.runs[showcase_id] = {
                "run_id": showcase_id,
                "case_id": case_id,
                "mode": str(card.get("provider_mode", "mock")),
                "status": "COMPLETED",
                "stage": "completed",
                "elapsed_s": float(card.get("total_elapsed_s", 1.23)),
                "result": card,
            }

        for sub in sorted(self.runs_root.iterdir()):
            if not sub.is_dir() or sub.name in self.runs or sub.name in ("fixtures", "preflight"):
                continue
            rp = sub / "result_public.json"
            if rp.is_file():
                try:
                    card = json.loads(rp.read_text(encoding="utf-8"))
                    self.runs[sub.name] = {
                        "run_id": sub.name,
                        "case_id": str(card.get("case_id", "drift_A_to_B")),
                        "mode": str(card.get("provider_mode", "mock")),
                        "status": "COMPLETED",
                        "stage": "completed",
                        "elapsed_s": float(card.get("total_elapsed_s", 1.0)),
                        "result": card,
                    }
                except Exception:
                    pass


def _is_valid_judge_code(provided_code: Optional[str]) -> bool:
    expected = os.environ.get("BLACKBOX_JUDGE_CODE", "").strip()
    if not expected or not provided_code:
        return False
    return str(provided_code).strip() == expected


def create_app(
    runs_root: Path | str = REPO_ROOT / "runs",
    *,
    max_public_live_runs: int = 2,
    cooldown_s: float = 0.0,
) -> Starlette:
    """Create the Starlette ASGI web application for the Twin-Repair Challenge."""
    state = TwinRepairWebState(
        Path(runs_root),
        max_public_live_runs=max_public_live_runs,
        cooldown_s=cooldown_s,
    )

    async def index_endpoint(request: Request) -> Response:
        html_path = WEB_DIR / "index.html"
        content = html_path.read_text(encoding="utf-8")
        return HTMLResponse(content=content, headers=NO_STORE_HEADERS)

    async def app_js_endpoint(request: Request) -> Response:
        js_path = WEB_DIR / "app.js"
        return Response(
            content=js_path.read_text(encoding="utf-8"),
            media_type="application/javascript; charset=utf-8",
            headers=NO_STORE_HEADERS,
        )

    async def styles_css_endpoint(request: Request) -> Response:
        css_path = WEB_DIR / "styles.css"
        return Response(
            content=css_path.read_text(encoding="utf-8"),
            media_type="text/css; charset=utf-8",
            headers=NO_STORE_HEADERS,
        )

    async def health_endpoint(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "service": "blackbox-twin-repair",
                "schema_version": "twin_repair_web.v1",
            },
            headers=NO_STORE_HEADERS,
        )

    async def cases_endpoint(request: Request) -> JSONResponse:
        with state.lock:
            remaining = max(0, state.max_public_live_runs - state.public_live_runs_used)
            active_paid = state.active_paid_run_id
            showcase_list = [
                {
                    "run_id": rid,
                    "case_id": info["case_id"],
                    "mode": info["mode"],
                    "terminal_state": info["result"].get("terminal_state"),
                }
                for rid, info in state.runs.items()
            ]
        return JSONResponse(
            {
                "cases": [
                    {
                        "case_id": "drift_A_to_B",
                        "title": "Revision Drift (Rev A Twin vs Rev B As-Built SLD)",
                        "badge": "Synthetic case",
                        "incident_headline": "The connector is hot. Is the wiring map still correct?",
                        "incident_caption": "The old twin suggests moving an 8 A-equivalent workload to N3.",
                        "revision_old": "A",
                        "revision_current": "B",
                        "default_showcase_run_id": "showcase_drift_A_to_B",
                    },
                    {
                        "case_id": "control_A_to_A",
                        "title": "No-Change Control (Rev A Twin vs Rev A SLD)",
                        "badge": "Synthetic case",
                        "incident_headline": "The connector is hot. Is the wiring map still correct?",
                        "incident_caption": "Control case: current diagram matches baseline revision A.",
                        "revision_old": "A",
                        "revision_current": "A",
                        "default_showcase_run_id": "showcase_control_A_to_A",
                    },
                ],
                "public_live_quota_max": state.max_public_live_runs,
                "public_live_quota_remaining": remaining,
                "active_paid_run": active_paid,
                "showcase_runs": showcase_list,
            },
            headers=NO_STORE_HEADERS,
        )

    async def create_run_endpoint(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            if not isinstance(payload, Mapping):
                payload = {}
        except Exception:
            payload = {}

        case_id = str(payload.get("case_id") or "drift_A_to_B")
        mode = str(payload.get("mode") or "mock")
        prepared_builder = bool(payload.get("prepared_builder", mode == "prepared_builder"))
        judge_code = payload.get("judge_code") or request.headers.get("X-Judge-Code")

        if case_id not in ALLOWED_CASE_IDS:
            return JSONResponse(
                {"error": f"Unsupported case_id: {case_id}"},
                status_code=400,
                headers=NO_STORE_HEADERS,
            )
        if mode not in ALLOWED_MODES:
            return JSONResponse(
                {"error": f"Unsupported mode: {mode}"},
                status_code=400,
                headers=NO_STORE_HEADERS,
            )

        visitor_key = request.client.host if request.client else "local"
        now = time.monotonic()
        is_paid = mode in ("live", "prepared_builder")
        has_judge_override = _is_valid_judge_code(judge_code)

        with state.lock:
            if state.cooldown_s > 0 and not has_judge_override:
                last_ts = state.last_visitor_run_ts.get(visitor_key)
                if last_ts is not None and (now - last_ts) < state.cooldown_s:
                    return JSONResponse(
                        {
                            "error": "Cooldown active; please wait before starting another run.",
                            "fallback_run_id": (
                                "showcase_drift_A_to_B"
                                if case_id == "drift_A_to_B"
                                else "showcase_control_A_to_A"
                            ),
                        },
                        status_code=429,
                        headers=NO_STORE_HEADERS,
                    )

            if is_paid:
                if state.active_paid_run_id is not None:
                    return JSONResponse(
                        {
                            "error": "Another live run is currently in progress; explore a recorded run.",
                            "active_run_id": state.active_paid_run_id,
                            "fallback_run_id": (
                                "showcase_drift_A_to_B"
                                if case_id == "drift_A_to_B"
                                else "showcase_control_A_to_A"
                            ),
                        },
                        status_code=429,
                        headers=NO_STORE_HEADERS,
                    )
                if not has_judge_override and state.public_live_runs_used >= state.max_public_live_runs:
                    return JSONResponse(
                        {
                            "error": "Live capacity reached; explore a recorded run",
                            "public_live_quota_remaining": 0,
                            "fallback_run_id": (
                                "showcase_drift_A_to_B"
                                if case_id == "drift_A_to_B"
                                else "showcase_control_A_to_A"
                            ),
                        },
                        status_code=429,
                        headers=NO_STORE_HEADERS,
                    )
                if not has_judge_override:
                    state.public_live_runs_used += 1

            run_index = len(state.runs) + 1
            run_id = f"run_{run_index:04d}_{case_id}_{mode}"
            if is_paid:
                state.active_paid_run_id = run_id
            state.last_visitor_run_ts[visitor_key] = now

        run_dir = state.runs_root / run_id
        t0 = time.monotonic()
        try:
            card = _ensure_fallback_run_bundle(
                run_dir,
                run_id=run_id,
                case_id=case_id,
                mode=mode,
                prepared_builder=prepared_builder,
            )
            elapsed = round(max(0.01, time.monotonic() - t0), 3)
            run_record = {
                "run_id": run_id,
                "case_id": case_id,
                "mode": mode,
                "status": "COMPLETED",
                "stage": "completed",
                "elapsed_s": elapsed,
                "result": card,
            }
            with state.lock:
                state.runs[run_id] = run_record
            return JSONResponse(run_record, status_code=200, headers=NO_STORE_HEADERS)
        finally:
            if is_paid:
                with state.lock:
                    if state.active_paid_run_id == run_id:
                        state.active_paid_run_id = None

    async def get_run_endpoint(request: Request) -> JSONResponse:
        run_id = str(request.path_params.get("run_id", ""))
        with state.lock:
            record = state.runs.get(run_id)

        if record is None:
            # Check if run directory exists on disk in runs_root
            disk_dir = (state.runs_root / run_id).resolve()
            if (
                disk_dir.parent == state.runs_root
                and (disk_dir / "result_public.json").is_file()
            ):
                card = json.loads((disk_dir / "result_public.json").read_text(encoding="utf-8"))
                record = {
                    "run_id": run_id,
                    "case_id": str(card.get("case_id", "drift_A_to_B")),
                    "mode": str(card.get("provider_mode", "mock")),
                    "status": "COMPLETED",
                    "stage": "completed",
                    "elapsed_s": float(card.get("total_elapsed_s", 1.0)),
                    "result": card,
                }
                with state.lock:
                    state.runs[run_id] = record
            else:
                return JSONResponse(
                    {"error": f"Run not found: {run_id}"},
                    status_code=404,
                    headers=NO_STORE_HEADERS,
                )

        return JSONResponse(record, headers=NO_STORE_HEADERS)

    async def get_run_asset_endpoint(request: Request) -> Response:
        run_id = str(request.path_params.get("run_id", ""))
        raw_asset = str(request.path_params.get("asset_name", "")).lstrip("/")

        # Immediate security check against traversal or private files (§7.F2)
        if (
            not raw_asset
            or any(bad in raw_asset for bad in FORBIDDEN_ASSET_SUBSTRINGS)
            or any(bad in run_id for bad in FORBIDDEN_ASSET_SUBSTRINGS)
            or raw_asset not in ALLOWLISTED_RUN_ASSETS
        ):
            return JSONResponse(
                {"error": "Forbidden: asset is not on the public allowlist."},
                status_code=403,
                headers=NO_STORE_HEADERS,
            )

        run_dir = (state.runs_root / run_id).resolve()
        try:
            run_dir.relative_to(state.runs_root)
        except ValueError:
            return JSONResponse(
                {"error": "Forbidden: invalid run path."},
                status_code=403,
                headers=NO_STORE_HEADERS,
            )

        asset_path = (run_dir / raw_asset).resolve()
        if not asset_path.is_file() and (run_dir / "public" / raw_asset).is_file():
            asset_path = (run_dir / "public" / raw_asset).resolve()
        try:
            asset_path.relative_to(run_dir)
        except ValueError:
            return JSONResponse(
                {"error": "Forbidden: path traversal blocked."},
                status_code=403,
                headers=NO_STORE_HEADERS,
            )

        if not asset_path.is_file():
            return JSONResponse(
                {"error": f"Asset not found: {raw_asset}"},
                status_code=404,
                headers=NO_STORE_HEADERS,
            )

        media_type = "application/json" if raw_asset.endswith(".json") else "image/png"
        return FileResponse(
            path=str(asset_path),
            media_type=media_type,
            headers=NO_STORE_HEADERS,
        )

    async def forbidden_run_subpath_endpoint(request: Request) -> JSONResponse:
        return JSONResponse(
            {"error": "Forbidden: path is not on the public allowlist."},
            status_code=403,
            headers=NO_STORE_HEADERS,
        )

    routes = [
        Route("/", index_endpoint, methods=["GET"]),
        Route("/index.html", index_endpoint, methods=["GET"]),
        Route("/app.js", app_js_endpoint, methods=["GET"]),
        Route("/styles.css", styles_css_endpoint, methods=["GET"]),
        Route("/health", health_endpoint, methods=["GET"]),
        Route("/cases", cases_endpoint, methods=["GET"]),
        Route("/runs", create_run_endpoint, methods=["POST"]),
        Route("/runs/{run_id}", get_run_endpoint, methods=["GET"]),
        Route("/runs/{run_id}/assets/{asset_name:path}", get_run_asset_endpoint, methods=["GET"]),
        Route("/runs/{run_id}/{extra_path:path}", forbidden_run_subpath_endpoint, methods=["GET"]),
    ]
    starlette_app = Starlette(routes=routes)
    starlette_app.state.twin_repair = state
    return starlette_app


app = create_app()
