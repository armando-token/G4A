"""Starlette/ASGI web backend for the BlackboxDC 3D Twin-Repair Challenge (Phase F).

Implements §7.F2 of `11_TWIN_REPAIR_ADDON_IMPLEMENTATION_PLAN_EN.md`:
- `GET /health` -> health status
- `GET /cases` -> safe case catalog (`drift_A_to_B`, `control_A_to_A`), recorded runs, and quota status
- `POST /runs` -> execute a genuine `run_twin_repair` run (`mock`, `live`, `prepared_builder`)
  with global 1-active-paid-run lock, public live quota (`max_public_live_runs=2`),
  `BLACKBOX_JUDGE_CODE` override, and per-visitor cooldown. Never fabricates `PASS` or
  `argon_verified=True`; if `mode="live"` is requested without valid credentials, returns a
  clear error (`HTTP 503`, `passed=False`, `argon_verified=False`).
- `GET /runs/{run_id}` -> run status, stage, elapsed seconds, and redacted `result_public.json`
- `GET /runs/{run_id}/assets/{asset_name:path}` -> strict allowlist asset server that serves
  authentic files from `runs/live1` (and other recorded/executed runs) and rejects
  `private/truth.json`, `score.json`, `..`, and any non-allowlisted path with HTTP 403
- `GET /`, `GET /index.html`, `GET /app.js`, `GET /styles.css` -> 4-state web application UI
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from blackbox.managed import resolve_auth_config
from twin_repair.challenge import build_challenge_fixture
from twin_repair.orchestrator import run_twin_repair


WEB_DIR = Path(__file__).resolve().parent
REPO_ROOT = WEB_DIR.parent.parent
CANONICAL_RUNS_ROOT = (REPO_ROOT / "runs").resolve()

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


def _describe_recorded_label(card: Mapping[str, Any]) -> str:
    mode = str(card.get("provider_mode") or "").lower()
    argon_ok = bool(card.get("argon_verified", False))
    if mode == "live" and argon_ok:
        return "Recorded live Argon run"
    if mode == "live":
        return "Recorded live run (unverified)"
    return "Recorded mock rehearsal run"


class TwinRepairWebState:
    """In-memory state for bounded run concurrency, live quota, and recorded run registry."""

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
        self.run_dirs: Dict[str, Path] = {}
        self.lock = threading.Lock()
        self._load_existing_runs()

    def _register_run_dir(self, run_key: str, run_dir: Path, *, is_recorded: bool = True) -> Optional[Dict[str, Any]]:
        rp = run_dir / "result_public.json"
        if not rp.is_file():
            return None
        try:
            card = json.loads(rp.read_text(encoding="utf-8"))
        except Exception:
            return None
        record = {
            "run_id": run_key,
            "source_run_id": str(card.get("run_id", run_key)),
            "case_id": str(card.get("case_id", "drift_A_to_B")),
            "mode": str(card.get("provider_mode", "mock")),
            "status": "COMPLETED",
            "stage": "completed",
            "recorded": bool(is_recorded),
            "provenance_badge": _describe_recorded_label(card) if is_recorded else str(card.get("provider_mode", "mock")),
            "elapsed_s": float(card.get("total_elapsed_s", 1.0)),
            "result": card,
        }
        self.runs[run_key] = record
        self.run_dirs[run_key] = run_dir.resolve()
        return record

    def _load_existing_runs(self) -> None:
        """Load authentic recorded runs from `runs_root` (and `REPO_ROOT / 'runs'` read-only fallback)."""
        search_roots = [self.runs_root]
        if CANONICAL_RUNS_ROOT != self.runs_root and CANONICAL_RUNS_ROOT.is_dir():
            search_roots.append(CANONICAL_RUNS_ROOT)

        for root_dir in search_roots:
            for sub in sorted(root_dir.iterdir()):
                if not sub.is_dir() or sub.name in ("fixtures", "preflight", "_fixtures"):
                    continue
                if sub.name.startswith("showcase_"):
                    continue
                if sub.name not in self.runs:
                    self._register_run_dir(sub.name, sub, is_recorded=True)

        # Bind canonical showcase aliases directly to the authentic recorded dossiers (`live1` and `control_mock1`)
        if "live1" in self.run_dirs:
            self._register_run_dir("showcase_drift_A_to_B", self.run_dirs["live1"], is_recorded=True)
        elif "mock1" in self.run_dirs:
            self._register_run_dir("showcase_drift_A_to_B", self.run_dirs["mock1"], is_recorded=True)

        if "control_mock1" in self.run_dirs:
            self._register_run_dir("showcase_control_A_to_A", self.run_dirs["control_mock1"], is_recorded=True)

    def resolve_run_dir(self, run_id: str) -> Optional[Path]:
        with self.lock:
            mapped = self.run_dirs.get(run_id)
        if mapped is not None and mapped.is_dir():
            return mapped
        for base in (self.runs_root, CANONICAL_RUNS_ROOT):
            candidate = (base / run_id).resolve()
            try:
                candidate.relative_to(base)
            except ValueError:
                continue
            if candidate.is_dir() and (candidate / "result_public.json").is_file():
                with self.lock:
                    self._register_run_dir(run_id, candidate, is_recorded=True)
                return candidate
        return None

    def ensure_case_fixture_dir(self, case_id: str) -> Path:
        """Return an existing challenge fixture directory or build one deterministically."""
        canonical_map = {
            "drift_A_to_B": CANONICAL_RUNS_ROOT / "fixtures" / "drift1",
            "control_A_to_A": CANONICAL_RUNS_ROOT / "fixtures" / "control1",
        }
        cand = canonical_map.get(case_id)
        if cand is not None and (cand / "public_state.json").is_file():
            return cand

        local_fixture = self.runs_root / "_fixtures" / case_id
        if not (local_fixture / "public_state.json").is_file():
            build_challenge_fixture(local_fixture, seed=20261025, case_type=case_id)
        return local_fixture


def _is_valid_judge_code(provided_code: Optional[str]) -> bool:
    expected = os.environ.get("BLACKBOX_JUDGE_CODE", "").strip()
    if not expected or not provided_code:
        return False
    return str(provided_code).strip() == expected


def create_app(
    runs_root: Path | str = CANONICAL_RUNS_ROOT,
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
                "default_recorded_live_run": "live1" if "live1" in state.runs else None,
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
                    "source_run_id": info.get("source_run_id", rid),
                    "case_id": info["case_id"],
                    "mode": info["mode"],
                    "recorded": info.get("recorded", True),
                    "provenance_badge": info.get("provenance_badge", ""),
                    "argon_verified": bool(info["result"].get("argon_verified", False)),
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
                        "default_showcase_run_id": "live1" if "live1" in state.runs else "showcase_drift_A_to_B",
                    },
                    {
                        "case_id": "control_A_to_A",
                        "title": "No-Change Control (Rev A Twin vs Rev A SLD)",
                        "badge": "Synthetic case",
                        "incident_headline": "The connector is hot. Is the wiring map still correct?",
                        "incident_caption": "Control case: current diagram matches baseline revision A.",
                        "revision_old": "A",
                        "revision_current": "A",
                        "default_showcase_run_id": "control_mock1" if "control_mock1" in state.runs else "showcase_control_A_to_A",
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
        prepared_scene0 = payload.get("prepared_scene0")
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

        fallback_rid = "live1" if case_id == "drift_A_to_B" else "control_mock1"
        visitor_key = request.client.host if request.client else "local"
        now = time.monotonic()
        is_paid = mode in ("live", "prepared_builder")
        has_judge_override = _is_valid_judge_code(judge_code)

        # For live/paid requests, verify credentials BEFORE consuming quota or claiming execution
        if is_paid:
            auth_cfg = resolve_auth_config()
            if not auth_cfg.get("ready"):
                return JSONResponse(
                    {
                        "error": (
                            f"Live Argon execution unavailable: {auth_cfg.get('reason', 'missing credentials')}. "
                            "No live model call was made; inspect the recorded run 'live1' instead."
                        ),
                        "case_id": case_id,
                        "mode": mode,
                        "status": "FAILED",
                        "terminal_state": "PROVIDER_ERROR",
                        "passed": False,
                        "workflow_passed": False,
                        "argon_verified": False,
                        "fallback_run_id": fallback_rid,
                    },
                    status_code=503,
                    headers=NO_STORE_HEADERS,
                )

        with state.lock:
            if state.cooldown_s > 0 and not has_judge_override:
                last_ts = state.last_visitor_run_ts.get(visitor_key)
                if last_ts is not None and (now - last_ts) < state.cooldown_s:
                    return JSONResponse(
                        {
                            "error": "Cooldown active; please wait before starting another run.",
                            "fallback_run_id": fallback_rid,
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
                            "fallback_run_id": fallback_rid,
                        },
                        status_code=429,
                        headers=NO_STORE_HEADERS,
                    )
                if not has_judge_override and state.public_live_runs_used >= state.max_public_live_runs:
                    return JSONResponse(
                        {
                            "error": "Live capacity reached; explore a recorded run",
                            "public_live_quota_remaining": 0,
                            "fallback_run_id": fallback_rid,
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
            case_dir = state.ensure_case_fixture_dir(case_id)
            card = run_twin_repair(
                case_dir,
                provider_mode=mode,
                out_dir=run_dir,
                prepared_scene0_path=prepared_scene0,
                run_id=run_id,
            )
            elapsed = round(max(0.01, time.monotonic() - t0), 3)
            run_record = {
                "run_id": run_id,
                "source_run_id": str(card.get("run_id", run_id)),
                "case_id": case_id,
                "mode": mode,
                "status": "COMPLETED" if card.get("passed") else "FAILED",
                "stage": "completed",
                "recorded": False,
                "provenance_badge": (
                    "Executed live Argon run"
                    if (mode == "live" and card.get("argon_verified"))
                    else f"Executed {mode} run"
                ),
                "elapsed_s": elapsed,
                "result": card,
            }
            with state.lock:
                state.runs[run_id] = run_record
                state.run_dirs[run_id] = run_dir.resolve()
            http_status = 200 if card.get("terminal_state") in ("PASS", "CONTROL_NO_CHANGE") else 502
            return JSONResponse(run_record, status_code=http_status, headers=NO_STORE_HEADERS)
        except Exception as exc:
            return JSONResponse(
                {
                    "error": f"Orchestrator execution failed: {exc.__class__.__name__}: {exc}",
                    "case_id": case_id,
                    "mode": mode,
                    "status": "FAILED",
                    "terminal_state": "PROVIDER_ERROR",
                    "passed": False,
                    "workflow_passed": False,
                    "argon_verified": False,
                    "fallback_run_id": fallback_rid,
                },
                status_code=502,
                headers=NO_STORE_HEADERS,
            )
        finally:
            if is_paid:
                with state.lock:
                    if state.active_paid_run_id == run_id:
                        state.active_paid_run_id = None

    async def get_run_endpoint(request: Request) -> JSONResponse:
        run_id = str(request.path_params.get("run_id", ""))
        state.resolve_run_dir(run_id)
        with state.lock:
            record = state.runs.get(run_id)

        if record is None:
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

        run_dir = state.resolve_run_dir(run_id)
        if run_dir is None:
            return JSONResponse(
                {"error": f"Run not found: {run_id}"},
                status_code=404,
                headers=NO_STORE_HEADERS,
            )

        candidates = [
            (run_dir / raw_asset).resolve(),
            (run_dir / "public" / raw_asset).resolve(),
        ]
        # For control_A_to_A runs (decision == "no_change"), render1 is identical to render0
        if raw_asset.startswith("render1/"):
            r0_equiv = "render0/" + raw_asset[len("render1/"):]
            candidates.append((run_dir / r0_equiv).resolve())

        asset_path: Optional[Path] = None
        for cand in candidates:
            try:
                cand.relative_to(run_dir)
            except ValueError:
                return JSONResponse(
                    {"error": "Forbidden: path traversal blocked."},
                    status_code=403,
                    headers=NO_STORE_HEADERS,
                )
            if cand.is_file():
                asset_path = cand
                break

        if asset_path is None:
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
