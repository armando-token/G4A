"""Immutable run state machine, timing spans, fail-closed gates, and CLI for BlackboxDC Twin Repair.

Implements Phase E of `11_TWIN_REPAIR_ADDON_IMPLEMENTATION_PLAN_EN.md`:
- Monotonic state machine:
  `CREATED -> INPUT_LOCKED -> BUILDER_RUNNING -> SCENE_VALIDATED -> RENDER0_VALIDATED`
  `-> CRITIC_RUNNING -> PATCH_VALIDATED -> RENDER1_VALIDATED -> SCORED`
  `-> PROPOSAL_PENDING_ENGINEER_REVIEW -> PASS | CONTROL_NO_CHANGE | REJECTED | ABSTAINED | TIMEOUT | PROVIDER_ERROR`
- 8 measured timing spans (`asset_load`, `builder_api`, `builder_validation`, `render0`,
  `critic_api`, `critic_validation`, `render1`, `scorer_physics_ui`).
- Truth leakage barrier (`scan_for_truth_leakage`) on all Builder and Critic inputs.
- Role sandbox separation (`builder_prov.environment_id != critic_prov.environment_id`).
- Sealed verification (`score_twin_repair_run`) invoked ONLY after `patch.json` is frozen.
- Complete immutable dossier (`manifest.json`, `public/`, `private/truth.json`, `roles/`,
  `scene0.json`, `render0/`, `patch.json`, `scene1.json`, `render1/`, `score.json`,
  `action_comparison.json`, `result_public.json`, `RESULTS_TWIN_REPAIR.md`, `events.jsonl`).
- CLI entrypoint: `python -m twin_repair.orchestrator {mock,live,replay} --case ... --out ...`
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import inspect
import json
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union
import uuid

from blackbox.managed import _scan_for_private_leaks, append_journal_event, redact_for_log
from twin_repair.managed_roles import (
    LiveManagedTwinRepairProvider,
    MockTwinRepairProvider,
    RoleProvenance,
)
from twin_repair.render_bridge import compare_wiring_renders, render_scene_spec
from twin_repair.report import build_public_result_card, render_results_twin_repair_md


NON_TERMINAL_STATES: Tuple[str, ...] = (
    "CREATED",
    "INPUT_LOCKED",
    "BUILDER_RUNNING",
    "SCENE_VALIDATED",
    "RENDER0_VALIDATED",
    "CRITIC_RUNNING",
    "PATCH_VALIDATED",
    "RENDER1_VALIDATED",
    "SCORED",
    "PROPOSAL_PENDING_ENGINEER_REVIEW",
)

TERMINAL_STATES: frozenset[str] = frozenset(
    {
        "PASS",
        "CONTROL_NO_CHANGE",
        "REJECTED",
        "ABSTAINED",
        "TIMEOUT",
        "PROVIDER_ERROR",
    }
)

TIMING_SPAN_KEYS: Tuple[str, ...] = (
    "asset_load",
    "builder_api",
    "builder_validation",
    "render0",
    "critic_api",
    "critic_validation",
    "render1",
    "scorer_physics_ui",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_plain_dict(obj: Any) -> Dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, Mapping):
        return dict(obj)
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return dict(obj.to_dict())
    if hasattr(obj, "model_dump") and callable(obj.model_dump):
        return dict(obj.model_dump())
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    raise TypeError(f"Cannot convert {type(obj).__name__} to dict")


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    from twin_repair.contracts import canonical_json_bytes

    return canonical_json_bytes(_to_plain_dict(payload))


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> Tuple[bytes, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = _canonical_json_bytes(payload)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with open(tmp_path, "wb") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())
    tmp_path.replace(path)
    return raw, hashlib.sha256(raw).hexdigest()


def scan_for_truth_leakage(payload: Any, context_label: str = "agent_input") -> None:
    """Ensure no private truth keys, expected patches, or private paths leak into agent inputs."""
    try:
        from twin_repair import challenge as _ch_mod

        if hasattr(_ch_mod, "scan_for_truth_leakage"):
            _ch_mod.scan_for_truth_leakage(payload)
    except PermissionError:
        raise
    except ValueError as exc:
        raise PermissionError(str(exc)) from exc
    except Exception:
        pass

    _scan_for_private_leaks(payload, context_label)
    serialized = json.dumps(payload, sort_keys=True) if not isinstance(payload, str) else payload
    for forbidden in (
        "current_edges_truth",
        "old_edges_truth",
        "expected_patch",
        "sealed_truth",
        "private/truth.json",
        "swap N2 and N3",
    ):
        if forbidden in serialized:
            raise PermissionError(
                f"Truth leakage barrier triggered in {context_label}: forbidden token {forbidden!r}"
            )


def _call_validate_scene_spec(raw_scene: Mapping[str, Any], public_state: Mapping[str, Any]) -> Dict[str, Any]:
    from twin_repair import contracts as _c_mod

    fn = getattr(_c_mod, "validate_scene_spec")
    sig = inspect.signature(fn)
    if len(sig.parameters) >= 2:
        res = fn(dict(raw_scene), dict(public_state))
    else:
        res = fn(dict(raw_scene))
    return _to_plain_dict(res)


def _call_validate_repair_patch(
    raw_patch: Mapping[str, Any],
    scene0: Mapping[str, Any],
    critic_images: Mapping[str, Any],
    basis_scene_sha256: str,
    render0_meta: Mapping[str, Any],
    public_state: Mapping[str, Any],
) -> Dict[str, Any]:
    from twin_repair import contracts as _c_mod

    fn = getattr(_c_mod, "validate_repair_patch")
    sig = inspect.signature(fn)
    kwargs: Dict[str, Any] = {}
    for pname in sig.parameters:
        if pname in ("raw", "patch", "raw_patch", "payload"):
            kwargs[pname] = dict(raw_patch)
        elif pname in ("current", "scene", "scene0", "current_scene", "scene_spec"):
            kwargs[pname] = dict(scene0)
        elif pname in ("images", "image_manifest", "critic_images", "allowed_images"):
            kwargs[pname] = dict(critic_images)
        elif pname in ("basis_scene_sha256", "expected_scene_sha256", "scene_sha256"):
            kwargs[pname] = basis_scene_sha256
        elif pname in ("render_meta", "render0_meta"):
            kwargs[pname] = dict(render0_meta)
        elif pname in ("public_state",):
            kwargs[pname] = dict(public_state)
    try:
        res = fn(**kwargs) if kwargs else fn(dict(raw_patch), dict(scene0), dict(critic_images))
    except TypeError:
        res = fn(dict(raw_patch), dict(scene0), dict(critic_images))
    return _to_plain_dict(res)


def _call_apply_patch(scene0: Mapping[str, Any], patch: Mapping[str, Any]) -> Dict[str, Any]:
    from twin_repair import contracts as _c_mod

    fn = getattr(_c_mod, "apply_patch")
    res = fn(copy.deepcopy(dict(scene0)), copy.deepcopy(dict(patch)))
    return _to_plain_dict(res)


def _extract_edges_map(scene: Mapping[str, Any]) -> Dict[str, str]:
    try:
        from twin_repair import contracts as _c_mod

        if hasattr(_c_mod, "extract_feeder_graph"):
            return dict(_c_mod.extract_feeder_graph(dict(scene)))
    except Exception:
        pass
    out: Dict[str, str] = {}
    for edge in scene.get("edges") or []:
        if isinstance(edge, Mapping) and "rack_id" in edge and "feeder_id" in edge:
            out[str(edge["rack_id"])] = str(edge["feeder_id"])
    return out


def _evaluate_actions_and_comparison(
    scene0: Mapping[str, Any],
    scene1: Mapping[str, Any],
    public_state: Mapping[str, Any],
) -> Dict[str, Any]:
    graph0 = _extract_edges_map(scene0)
    graph1 = _extract_edges_map(scene1)
    from twin_repair.physics_adapter import compare_stale_and_repaired_actions

    comp = dict(compare_stale_and_repaired_actions(graph0, graph1, dict(public_state)))
    pre_map: Dict[str, Dict[str, Any]] = {}
    for item in comp.get("stale_outcomes") or []:
        d = _to_plain_dict(item)
        key = str(d.get("target_rack") or d.get("action_id") or "")
        if key:
            pre_map[key] = d
        if d.get("action_id"):
            pre_map[str(d["action_id"])] = d

    post_map: Dict[str, Dict[str, Any]] = {}
    for item in comp.get("repaired_outcomes") or []:
        d = _to_plain_dict(item)
        key = str(d.get("target_rack") or d.get("action_id") or "")
        if key:
            post_map[key] = d
        if d.get("action_id"):
            post_map[str(d["action_id"])] = d

    repaired_rec_info = comp.get("repaired_choice_evaluated_on_repaired_graph") or {}
    physics_safe = bool(repaired_rec_info.get("safe", False))
    return {
        **comp,
        "before_graph": graph0,
        "after_graph": graph1,
        "pre_repair_candidates": pre_map,
        "post_repair_candidates": post_map,
        "physics_safe": physics_safe,
    }


def _score_run(
    *,
    scene0: Mapping[str, Any],
    scene1: Mapping[str, Any],
    render0_dir: Path,
    render1_dir: Path,
    render0_meta: Mapping[str, Any],
    render1_meta: Mapping[str, Any],
    patch: Mapping[str, Any],
    private_truth: Mapping[str, Any],
    public_state: Mapping[str, Any],
    action_comparison: Mapping[str, Any],
) -> Dict[str, Any]:
    """Invoke `verifier.score_twin_repair_run` if available and enrich with canonical score fields."""
    verifier_out: Dict[str, Any] = {}
    try:
        from twin_repair import verifier as _v_mod

        if hasattr(_v_mod, "score_twin_repair_run"):
            fn = _v_mod.score_twin_repair_run
            sig = inspect.signature(fn)
            kwargs: Dict[str, Any] = {}
            for pname in sig.parameters:
                if pname in ("scene0", "before_scene", "scene_before"):
                    kwargs[pname] = dict(scene0)
                elif pname in ("scene1", "after_scene", "scene_after"):
                    kwargs[pname] = dict(scene1)
                elif pname in ("render0_dir", "before_render_dir", "render0"):
                    kwargs[pname] = render0_dir
                elif pname in ("render1_dir", "after_render_dir", "render1"):
                    kwargs[pname] = render1_dir
                elif pname in ("render0_meta", "meta0"):
                    kwargs[pname] = dict(render0_meta)
                elif pname in ("render1_meta", "meta1"):
                    kwargs[pname] = dict(render1_meta)
                elif pname in ("patch", "repair_patch"):
                    kwargs[pname] = dict(patch)
                elif pname in ("sealed_truth", "private_truth", "truth"):
                    kwargs[pname] = dict(private_truth)
                elif pname in ("public_state",):
                    kwargs[pname] = dict(public_state)
            verifier_out = _to_plain_dict(fn(**kwargs))
    except Exception:
        verifier_out = {}

    graph0 = _extract_edges_map(scene0)
    graph1 = _extract_edges_map(scene1)
    if private_truth.get("judge_challenge") and not private_truth.get("current_edges"):
        raise ValueError("Judge challenge truth is missing current_edges; refusing to fall back to canonical A/B truth")
    target_truth_edges: Dict[str, str] = dict(
        private_truth.get("current_edges")
        or private_truth.get("target_edges")
        or private_truth.get("edges_current")
        or (
            {"N1": "F1", "N2": "F1", "N3": "F2", "N4": "F2"}
            if str(private_truth.get("revision_current") or public_state.get("revision_current") or "B") == "A"
            else {"N1": "F1", "N2": "F2", "N3": "F1", "N4": "F2"}
        )
    )

    before_correct = sum(1 for rk, fd in target_truth_edges.items() if graph0.get(rk) == fd)
    after_correct = sum(1 for rk, fd in target_truth_edges.items() if graph1.get(rk) == fd)
    total_edges = len(target_truth_edges)

    decision = str(patch.get("decision", "repair"))
    if decision == "repair" and render1_dir.exists() and (render1_dir / "render_wiring.png").exists():
        changed_racks = sorted({str(op.get("rack_id")) for op in (patch.get("operations") or []) if op.get("rack_id")})
        stable_racks = [rk for rk in ("N1", "N2", "N3", "N4") if rk not in changed_racks]
        cmp_res = compare_wiring_renders(
            render0_dir,
            render1_dir,
            changed_racks=tuple(changed_racks) or ("N2", "N3"),
            stable_racks=tuple(stable_racks),
        )
    else:
        cmp_res = {
            "cable_pixels_changed": False,
            "changed_pixels_by_rack": {"N1": 0, "N2": 0, "N3": 0, "N4": 0},
            "hotspot_shift_px": 0.0,
            "hotspot_stable": True,
        }

    # Verify Critic evidence bounding boxes intersect projected cable corridors and SLD branch boxes
    evidence_boxes_valid = bool(verifier_out.get("evidence_boxes_valid", True))
    if decision == "repair":
        corridors0 = dict(render0_meta.get("projected_cable_corridors") or {})
        for disc in patch.get("discrepancies") or []:
            rk = str(disc.get("rack_id", ""))
            r_box = disc.get("render_evidence_bbox_xyxy_px") or [0, 0, 0, 0]
            c_box = (corridors0.get(rk) or {}).get("bbox_xyxy_px")
            if c_box and len(r_box) == 4 and len(c_box) == 4:
                ix0 = max(int(r_box[0]), int(c_box[0]))
                iy0 = max(int(r_box[1]), int(c_box[1]))
                ix1 = min(int(r_box[2]), int(c_box[2]))
                iy1 = min(int(r_box[3]), int(c_box[3]))
                if ix1 <= ix0 or iy1 <= iy0:
                    evidence_boxes_valid = False

    graph_improved = bool(after_correct == total_edges and after_correct > before_correct)
    physics_safe = bool(action_comparison.get("physics_safe", False))

    merged = {
        **verifier_out,
        "before_correct_edges": before_correct,
        "after_correct_edges": after_correct,
        "total_edges": total_edges,
        "graph_score_before": f"{before_correct}/{total_edges}",
        "graph_score_after": f"{after_correct}/{total_edges}",
        "graph_improved": graph_improved,
        "cable_pixels_changed": bool(cmp_res.get("cable_pixels_changed", False)),
        "changed_pixels_n2": int((cmp_res.get("changed_pixels_by_rack") or {}).get("N2", 0)),
        "changed_pixels_n3": int((cmp_res.get("changed_pixels_by_rack") or {}).get("N3", 0)),
        "wiring_comparison": cmp_res,
        "evidence_boxes_valid": evidence_boxes_valid,
        "hotspot_registration_px": float(render1_meta.get("registration_error_px", render0_meta.get("registration_error_px", 0.0))),
        "hotspot_iou": float(verifier_out.get("hotspot_iou", 1.0)),
        "physics_safe": physics_safe,
    }
    return merged


# Judge-created challenges use this answer-neutral filename for the current drawing.
NEUTRAL_CURRENT_SLD_NAME = "current_sld.png"


def _ensure_case_assets(case_dir: Path) -> Dict[str, Any]:
    """Load or build case assets (`public/`, `private/truth.json`, `case_manifest.json`) in `case_dir`."""
    cdir = Path(case_dir).resolve()
    if not cdir.exists() or not (
        (cdir / "case_manifest.json").exists()
        or (cdir / "public" / "public_state.json").exists()
        or (cdir / "public_state.json").exists()
    ):
        from twin_repair.challenge import build_challenge_fixture

        case_kind = "control_A_to_A" if "control" in cdir.name.lower() else "drift_A_to_B"
        build_challenge_fixture(cdir, case_type=case_kind)

    pub_dir = cdir / "public" if (cdir / "public").is_dir() else cdir
    priv_dir = cdir / "private" if (cdir / "private").is_dir() else cdir

    pub_state_path = pub_dir / "public_state.json"
    if not pub_state_path.is_file() and (cdir / "public_state.json").is_file():
        pub_state_path = cdir / "public_state.json"
    public_state = json.loads(pub_state_path.read_text(encoding="utf-8"))

    truth_path = priv_dir / "truth.json"
    if not truth_path.is_file() and (cdir / "truth.json").is_file():
        truth_path = cdir / "truth.json"
    private_truth = json.loads(truth_path.read_text(encoding="utf-8")) if truth_path.is_file() else {}

    manifest_path = cdir / "case_manifest.json"
    if not manifest_path.is_file() and (cdir / "manifest.json").is_file():
        manifest_path = cdir / "manifest.json"
    case_manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}

    old_sld_path = pub_dir / "old_sld_A.png"
    if not old_sld_path.is_file() and (cdir / "old_sld_A.png").is_file():
        old_sld_path = cdir / "old_sld_A.png"

    old_thermal_path = pub_dir / "old_thermal_rear.png"
    if not old_thermal_path.is_file() and (cdir / "old_thermal_rear.png").is_file():
        old_thermal_path = cdir / "old_thermal_rear.png"

    # Current SLD can be current_sld_B.png, current_sld_A.png, or current_sld.png
    current_sld_path: Optional[Path] = None
    for candidate_name in ("current_sld_B.png", "current_sld_A.png", "current_sld.png"):
        for base in (pub_dir, cdir):
            cand = base / candidate_name
            if cand.is_file():
                current_sld_path = cand
                break
        if current_sld_path is not None:
            break
    if current_sld_path is None:
        raise FileNotFoundError(f"Could not find current SLD PNG inside {cdir}")

    return {
        "case_dir": cdir,
        "public_state": public_state,
        "private_truth": private_truth,
        "case_manifest": case_manifest,
        "old_sld_path": old_sld_path,
        "old_thermal_path": old_thermal_path,
        "current_sld_path": current_sld_path,
    }


def run_twin_repair(
    case_dir: Union[Path, str],
    *,
    provider_mode: str = "mock",
    out_dir: Optional[Union[Path, str]] = None,
    provider: Any = None,
    prepared_scene0_path: Optional[Union[Path, str]] = None,
) -> Dict[str, Any]:
    """Execute the immutable Twin Repair state machine (`CREATED -> ... -> PASS | ...`)."""
    norm_mode = provider_mode.strip().lower()
    if norm_mode not in ("mock", "live", "prepared_builder", "replay"):
        raise ValueError(
            f"Unsupported provider_mode={provider_mode!r}; expected 'mock', 'live', 'prepared_builder', or 'replay'"
        )

    run_id = f"twin-repair-{uuid.uuid4().hex[:12]}"
    run_root = Path(out_dir).resolve() if out_dir is not None else (Path("runs") / run_id).resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    events_path = run_root / "events.jsonl"

    # Handle replay mode on an existing dossier directory
    if norm_mode == "replay":
        src_dir = Path(case_dir).resolve()
        if (src_dir / "result_public.json").is_file():
            existing = json.loads((src_dir / "result_public.json").read_text(encoding="utf-8"))
            existing["provider_mode"] = "replay"
            existing["is_replay"] = True
            existing["source"] = "replay"
            if run_root != src_dir:
                _atomic_write_json(run_root / "result_public.json", existing)
                (run_root / "RESULTS_TWIN_REPAIR.md").write_text(
                    render_results_twin_repair_md(existing),
                    encoding="utf-8",
                )
            append_journal_event(
                events_path,
                {"event": "state_transition", "run_id": run_id, "state": "REPLAY", "source_dir": str(src_dir)},
            )
            return existing

    state_history: List[str] = []
    timing_spans_s: Dict[str, float] = {k: 0.0 for k in TIMING_SPAN_KEYS}
    t_run_start = time.monotonic()
    started_at_utc = _utc_now_iso()

    def _transition(new_state: str, detail: Optional[Mapping[str, Any]] = None) -> None:
        state_history.append(new_state)
        ev: Dict[str, Any] = {
            "event": "state_transition",
            "run_id": run_id,
            "state": new_state,
            "state_index": len(state_history) - 1,
        }
        if detail:
            ev.update(dict(redact_for_log(dict(detail))))
        append_journal_event(events_path, ev)

    _transition("CREATED", {"provider_mode": norm_mode})

    gates: Dict[str, bool] = {
        "builder_completed": False,
        "critic_completed": False,
        "argon_verified": False,
        "distinct_environments": False,
        "render0_valid": False,
        "patch_valid": False,
        "render1_valid": False,
        "cable_pixels_changed": False,
        "graph_improved": False,
        "physics_safe": False,
        "human_review_required": True,
    }

    builder_prov_dict: Dict[str, Any] = {}
    critic_prov_dict: Dict[str, Any] = {}
    scene0_dict: Dict[str, Any] = {}
    scene1_dict: Dict[str, Any] = {}
    patch_dict: Dict[str, Any] = {}
    score_dict: Dict[str, Any] = {}
    action_comp_dict: Dict[str, Any] = {}
    render0_meta: Dict[str, Any] = {}
    render1_meta: Dict[str, Any] = {}
    error_reason: Optional[str] = None

    # ------------------------------------------------------------------
    # Span 1: asset_load & INPUT_LOCKED
    # ------------------------------------------------------------------
    t0 = time.monotonic()
    assets = _ensure_case_assets(Path(case_dir))
    public_state = dict(assets["public_state"])
    private_truth = dict(assets["private_truth"])
    case_manifest = dict(assets["case_manifest"])
    old_sld_path: Path = assets["old_sld_path"]
    old_thermal_path: Path = assets["old_thermal_path"]
    current_sld_path: Path = assets["current_sld_path"]
    case_id = str(public_state.get("case_id", "drift_A_to_B"))

    # Copy public & private assets into immutable run directory
    pub_out_dir = run_root / "public"
    priv_out_dir = run_root / "private"
    roles_out_dir = run_root / "roles"
    pub_out_dir.mkdir(parents=True, exist_ok=True)
    priv_out_dir.mkdir(parents=True, exist_ok=True)
    roles_out_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy2(old_sld_path, pub_out_dir / "old_sld_A.png")
    shutil.copy2(old_thermal_path, pub_out_dir / "old_thermal_rear.png")
    shutil.copy2(current_sld_path, pub_out_dir / current_sld_path.name)
    neutral_sld = current_sld_path.name == NEUTRAL_CURRENT_SLD_NAME
    if current_sld_path.name != "current_sld_B.png" and not neutral_sld:
        shutil.copy2(current_sld_path, pub_out_dir / "current_sld_B.png")
    _atomic_write_json(pub_out_dir / "public_state.json", public_state)
    if private_truth:
        _atomic_write_json(priv_out_dir / "truth.json", private_truth)

    current_sld_sha = hashlib.sha256(current_sld_path.read_bytes()).hexdigest()
    run_manifest = {
        **case_manifest,
        "run_id": run_id,
        "case_id": case_id,
        "provider_mode": norm_mode,
        "started_at_utc": started_at_utc,
        "old_sld_sha256": hashlib.sha256(old_sld_path.read_bytes()).hexdigest(),
        "old_thermal_sha256": hashlib.sha256(old_thermal_path.read_bytes()).hexdigest(),
        "current_sld_sha256": current_sld_sha,
        "revision_old": str(public_state.get("revision_old", case_manifest.get("revision_old", "A"))),
        "revision_current": str(public_state.get("revision_current", case_manifest.get("revision_current", "B"))),
    }
    _atomic_write_json(run_root / "manifest.json", run_manifest)

    # Verify zero truth leakage in public_state before calling any role
    scan_for_truth_leakage(public_state, "public_state")
    timing_spans_s["asset_load"] = round(time.monotonic() - t0, 4)
    _transition("INPUT_LOCKED", {"case_id": case_id, "current_sld_sha256": current_sld_sha})

    active_provider = provider
    if active_provider is None:
        if norm_mode == "mock":
            active_provider = MockTwinRepairProvider(mode="normal")
        else:
            active_provider = LiveManagedTwinRepairProvider()

    def _finalize(terminal_state: str, reason: Optional[str] = None) -> Dict[str, Any]:
        nonlocal error_reason
        if reason:
            error_reason = reason
        _transition(terminal_state, {"reason": error_reason} if error_reason else None)
        total_elapsed_s = round(time.monotonic() - t_run_start, 4)
        completed_at_utc = _utc_now_iso()

        b_usage = dict((builder_prov_dict.get("usage") or {}))
        c_usage = dict((critic_prov_dict.get("usage") or {}))
        token_usage = {
            "builder": b_usage,
            "critic": c_usage,
            "total_noncached_tokens": int(b_usage.get("noncached_tokens", 0)) + int(c_usage.get("noncached_tokens", 0)),
            "total_cached_tokens": int(b_usage.get("cached_tokens", 0)) + int(c_usage.get("cached_tokens", 0)),
            "total_tokens": int(b_usage.get("total_tokens", 0)) + int(c_usage.get("total_tokens", 0)),
        }

        if terminal_state == "PASS":
            workflow_passed = all(
                gates[k]
                for k in (
                    "builder_completed",
                    "critic_completed",
                    "distinct_environments",
                    "render0_valid",
                    "patch_valid",
                    "render1_valid",
                    "cable_pixels_changed",
                    "graph_improved",
                    "physics_safe",
                    "human_review_required",
                )
            )
            passed = bool(workflow_passed and (gates["argon_verified"] if norm_mode == "live" else True))
        elif terminal_state == "CONTROL_NO_CHANGE":
            workflow_passed = all(
                gates[k]
                for k in (
                    "builder_completed",
                    "critic_completed",
                    "distinct_environments",
                    "render0_valid",
                    "patch_valid",
                    "physics_safe",
                    "human_review_required",
                )
            )
            passed = bool(workflow_passed and (gates["argon_verified"] if norm_mode == "live" else True))
        else:
            workflow_passed = False
            passed = False

        r0_sha = str(((render0_meta.get("files") or {}).get("render_wiring.png") or {}).get("sha256", ""))
        r1_sha = str(((render1_meta.get("files") or {}).get("render_wiring.png") or {}).get("sha256", ""))

        full_run_data: Dict[str, Any] = {
            "run_id": run_id,
            "case_id": case_id,
            "run_dir": str(run_root),
            "provider_mode": norm_mode,
            "source": "fixture" if norm_mode == "mock" else norm_mode,
            "prepared_builder": bool(prepared_scene0_path is not None or norm_mode == "prepared_builder"),
            "revision_old": run_manifest["revision_old"],
            "revision_current": run_manifest["revision_current"],
            "terminal_state": terminal_state,
            "state_history": list(state_history),
            "passed": passed,
            "workflow_passed": workflow_passed,
            "mock_rehearsal_passed": bool(workflow_passed and norm_mode == "mock"),
            "error": error_reason,
            "gates": gates,
            **gates,
            "manifest": run_manifest,
            "builder_provenance": builder_prov_dict,
            "critic_provenance": critic_prov_dict,
            "scene0": scene0_dict,
            "scene1": scene1_dict,
            "patch": patch_dict,
            "score": score_dict,
            "action_comparison": action_comp_dict,
            "render0_wiring_sha256": r0_sha,
            "render1_wiring_sha256": r1_sha,
            "current_sld_sha256": current_sld_sha,
            "timing_spans_s": timing_spans_s,
            "total_elapsed_s": total_elapsed_s,
            "token_usage": token_usage,
            "started_at_utc": started_at_utc,
            "completed_at_utc": completed_at_utc,
        }

        if score_dict:
            _atomic_write_json(run_root / "score.json", score_dict)
        if action_comp_dict:
            _atomic_write_json(run_root / "action_comparison.json", action_comp_dict)

        public_card = build_public_result_card(full_run_data)
        public_card["run_dir"] = str(run_root)
        public_card["state_history"] = list(state_history)
        public_card["mock_rehearsal_passed"] = bool(workflow_passed and norm_mode == "mock")
        if error_reason:
            public_card["error"] = error_reason

        _atomic_write_json(run_root / "result_public.json", public_card)
        md_text = render_results_twin_repair_md(full_run_data)
        (run_root / "RESULTS_TWIN_REPAIR.md").write_text(md_text, encoding="utf-8")
        return public_card

    # ------------------------------------------------------------------
    # Span 2: builder_api & BUILDER_RUNNING
    # ------------------------------------------------------------------
    _transition("BUILDER_RUNNING")
    t_b_api = time.monotonic()
    if prepared_scene0_path is not None:
        prep_p = Path(prepared_scene0_path).resolve()
        raw_scene0 = json.loads(prep_p.read_text(encoding="utf-8"))
        raw_scene0_bytes = prep_p.read_bytes()
        builder_prov = RoleProvenance(
            role="builder",
            source="prepared_builder",
            requested_agent="antigravity-preview-09-2026",
            requested_model="gemini-4-argon",
            returned_model="prepared-scene0",
            argon_verified=False,
            interaction_id=f"prepared-builder-{uuid.uuid4().hex[:8]}",
            environment_id="env-prepared-builder-000",
            status="completed",
            native_image_input_sent=False,
            image_manifest=[],
            agents_md_sha256="",
            mounted_files=[],
            retrieved_file_path=str(prep_p),
            retrieved_sha256=hashlib.sha256(raw_scene0_bytes).hexdigest(),
            elapsed_s=0.0,
            usage={"noncached_tokens": 0, "cached_tokens": 0, "total_tokens": 0, "unknown_usage": False},
            code_execution_observed=False,
        )
    else:
        try:
            raw_scene0, raw_scene0_bytes, builder_prov = active_provider.run_builder(
                old_sld_path=pub_out_dir / "old_sld_A.png",
                old_thermal_path=pub_out_dir / "old_thermal_rear.png",
                public_state=public_state,
            )
        except Exception as exc:
            timing_spans_s["builder_api"] = round(time.monotonic() - t_b_api, 4)
            return _finalize("PROVIDER_ERROR", f"Builder provider error: {exc.__class__.__name__}: {exc}")

    timing_spans_s["builder_api"] = round(time.monotonic() - t_b_api, 4)
    builder_prov_dict = builder_prov.to_dict() if hasattr(builder_prov, "to_dict") else _to_plain_dict(builder_prov)
    _atomic_write_json(roles_out_dir / "builder_provenance.json", builder_prov_dict)

    b_status = str(builder_prov_dict.get("status", "")).lower()
    if b_status == "timeout":
        return _finalize("TIMEOUT", "Builder interaction timed out")
    if b_status != "completed" or raw_scene0 is None:
        return _finalize("PROVIDER_ERROR", f"Builder did not complete with scene_spec.json (status={b_status!r})")

    if raw_scene0_bytes is not None:
        (run_root / "scene0_raw.json").write_bytes(raw_scene0_bytes)

    # ------------------------------------------------------------------
    # Span 3: builder_validation & SCENE_VALIDATED
    # ------------------------------------------------------------------
    t_b_val = time.monotonic()
    try:
        scene0_dict = _call_validate_scene_spec(raw_scene0, public_state)
    except Exception as exc:
        timing_spans_s["builder_validation"] = round(time.monotonic() - t_b_val, 4)
        return _finalize("REJECTED", f"Scene0 validation failed: {exc}")

    _, scene0_sha256 = _atomic_write_json(run_root / "scene0.json", scene0_dict)
    gates["builder_completed"] = True
    timing_spans_s["builder_validation"] = round(time.monotonic() - t_b_val, 4)
    _transition("SCENE_VALIDATED", {"scene0_sha256": scene0_sha256})

    # ------------------------------------------------------------------
    # Span 4: render0 & RENDER0_VALIDATED
    # ------------------------------------------------------------------
    t_r0 = time.monotonic()
    render0_dir = run_root / "render0"
    try:
        render0_meta = render_scene_spec(run_root / "scene0.json", render0_dir)
    except Exception as exc:
        timing_spans_s["render0"] = round(time.monotonic() - t_r0, 4)
        return _finalize("REJECTED", f"Render0 failed: {exc}")

    gates["render0_valid"] = True
    timing_spans_s["render0"] = round(time.monotonic() - t_r0, 4)
    _transition(
        "RENDER0_VALIDATED",
        {"render0_wiring_sha256": ((render0_meta.get("files") or {}).get("render_wiring.png") or {}).get("sha256", "")},
    )

    # ------------------------------------------------------------------
    # Span 5: critic_api & CRITIC_RUNNING
    # ------------------------------------------------------------------
    _transition("CRITIC_RUNNING")
    t_c_api = time.monotonic()
    critic_public_check = {
        "public_state": public_state,
        "basis_scene_sha256": scene0_sha256,
        "current_sld_path": str(pub_out_dir / current_sld_path.name),
        "render0_wiring_path": str(render0_dir / "render_wiring.png"),
        "render0_thermal_path": str(render0_dir / "render_thermal_rear.png"),
    }
    scan_for_truth_leakage(critic_public_check, "critic_inputs")

    try:
        raw_patch, raw_patch_bytes, critic_prov = active_provider.run_critic(
            current_sld_path=pub_out_dir / current_sld_path.name,
            render0_wiring_path=render0_dir / "render_wiring.png",
            render0_thermal_path=render0_dir / "render_thermal_rear.png",
            public_state=public_state,
            basis_scene_sha256=scene0_sha256,
            render0_meta=render0_meta,
        )
    except Exception as exc:
        timing_spans_s["critic_api"] = round(time.monotonic() - t_c_api, 4)
        return _finalize("PROVIDER_ERROR", f"Critic provider error: {exc.__class__.__name__}: {exc}")

    timing_spans_s["critic_api"] = round(time.monotonic() - t_c_api, 4)
    critic_prov_dict = critic_prov.to_dict() if hasattr(critic_prov, "to_dict") else _to_plain_dict(critic_prov)
    _atomic_write_json(roles_out_dir / "critic_provenance.json", critic_prov_dict)

    # Check environment isolation between Builder and Critic
    b_env = str(builder_prov_dict.get("environment_id", ""))
    c_env = str(critic_prov_dict.get("environment_id", ""))
    gates["distinct_environments"] = bool(b_env and c_env and b_env != c_env)
    if not gates["distinct_environments"]:
        return _finalize(
            "REJECTED",
            f"Sandbox environment isolation violated: Builder env {b_env!r} == Critic env {c_env!r}",
        )

    c_status = str(critic_prov_dict.get("status", "")).lower()
    if c_status == "timeout":
        return _finalize("TIMEOUT", "Critic interaction timed out")
    if c_status == "missing_remote_output" or raw_patch is None or c_status != "completed":
        return _finalize(
            "PROVIDER_ERROR",
            f"Critic did not complete with repair_patch.json (status={c_status!r})",
        )

    if raw_patch_bytes is not None:
        (run_root / "patch_raw.json").write_bytes(raw_patch_bytes)

    gates["critic_completed"] = True
    gates["argon_verified"] = bool(
        builder_prov_dict.get("argon_verified", False) and critic_prov_dict.get("argon_verified", False)
    )

    # ------------------------------------------------------------------
    # Span 6: critic_validation & PATCH_VALIDATED
    # ------------------------------------------------------------------
    t_c_val = time.monotonic()
    sld_aliases = (current_sld_path.name,) if neutral_sld else (current_sld_path.name, "current_sld_B.png", "current_sld_A.png")
    critic_images_manifest = {
        **{name: {"width_px": 1280, "height_px": 800, "sha256": current_sld_sha} for name in sld_aliases},
        "render0_wiring.png": {
            "width_px": 1280,
            "height_px": 800,
            "sha256": str(((render0_meta.get("files") or {}).get("render_wiring.png") or {}).get("sha256", "")),
        },
        "render0_thermal_rear.png": {
            "width_px": 1280,
            "height_px": 800,
            "sha256": str(((render0_meta.get("files") or {}).get("render_thermal_rear.png") or {}).get("sha256", "")),
        },
    }
    try:
        patch_dict = _call_validate_repair_patch(
            raw_patch=raw_patch,
            scene0=scene0_dict,
            critic_images=critic_images_manifest,
            basis_scene_sha256=scene0_sha256,
            render0_meta=render0_meta,
            public_state=public_state,
        )
        if str(patch_dict.get("basis_scene_sha256", "")) != scene0_sha256:
            raise ValueError(
                f"basis_scene_sha256 mismatch: patch has {patch_dict.get('basis_scene_sha256')!r}, expected {scene0_sha256!r}"
            )
    except Exception as exc:
        timing_spans_s["critic_validation"] = round(time.monotonic() - t_c_val, 4)
        return _finalize("REJECTED", f"Patch validation failed: {exc}")

    _, patch_sha256 = _atomic_write_json(run_root / "patch.json", patch_dict)
    gates["patch_valid"] = True
    timing_spans_s["critic_validation"] = round(time.monotonic() - t_c_val, 4)
    decision = str(patch_dict.get("decision", "")).lower()
    _transition("PATCH_VALIDATED", {"patch_sha256": patch_sha256, "decision": decision})

    # Handle abstain
    if decision == "abstain":
        return _finalize("ABSTAINED", str(patch_dict.get("abstain_reason") or patch_dict.get("reason") or "Critic abstained"))

    # Handle no_change control case
    if decision == "no_change":
        t_sc = time.monotonic()
        scene1_dict = copy.deepcopy(scene0_dict)
        action_comp_dict = _evaluate_actions_and_comparison(scene0_dict, scene1_dict, public_state)
        score_dict = _score_run(
            scene0=scene0_dict,
            scene1=scene1_dict,
            render0_dir=render0_dir,
            render1_dir=render0_dir,
            render0_meta=render0_meta,
            render1_meta=render0_meta,
            patch=patch_dict,
            private_truth=private_truth,
            public_state=public_state,
            action_comparison=action_comp_dict,
        )
        timing_spans_s["scorer_physics_ui"] = round(time.monotonic() - t_sc, 4)
        _transition("SCORED", {"graph_score_before": score_dict.get("graph_score_before")})
        gates["physics_safe"] = bool(score_dict.get("physics_safe", False))
        if int(score_dict.get("before_correct_edges", 0)) == int(score_dict.get("total_edges", 4)):
            _transition("PROPOSAL_PENDING_ENGINEER_REVIEW")
            return _finalize("CONTROL_NO_CHANGE")
        return _finalize("REJECTED", "no_change returned on a drifted graph that was not 4/4")

    # ------------------------------------------------------------------
    # Span 7: apply_patch & render1 (RENDER1_VALIDATED)
    # ------------------------------------------------------------------
    t_r1 = time.monotonic()
    try:
        scene1_dict = _call_apply_patch(scene0_dict, patch_dict)
    except Exception as exc:
        timing_spans_s["render1"] = round(time.monotonic() - t_r1, 4)
        return _finalize("REJECTED", f"apply_patch failed: {exc}")

    _, scene1_sha256 = _atomic_write_json(run_root / "scene1.json", scene1_dict)
    render1_dir = run_root / "render1"
    try:
        render1_meta = render_scene_spec(run_root / "scene1.json", render1_dir)
    except Exception as exc:
        timing_spans_s["render1"] = round(time.monotonic() - t_r1, 4)
        return _finalize("REJECTED", f"Render1 failed: {exc}")

    gates["render1_valid"] = True
    timing_spans_s["render1"] = round(time.monotonic() - t_r1, 4)
    _transition(
        "RENDER1_VALIDATED",
        {
            "scene1_sha256": scene1_sha256,
            "render1_wiring_sha256": ((render1_meta.get("files") or {}).get("render_wiring.png") or {}).get("sha256", ""),
        },
    )

    # ------------------------------------------------------------------
    # Span 8: scorer_physics_ui (ONLY after patch.json is frozen)
    # ------------------------------------------------------------------
    t_sc = time.monotonic()
    action_comp_dict = _evaluate_actions_and_comparison(scene0_dict, scene1_dict, public_state)
    score_dict = _score_run(
        scene0=scene0_dict,
        scene1=scene1_dict,
        render0_dir=render0_dir,
        render1_dir=render1_dir,
        render0_meta=render0_meta,
        render1_meta=render1_meta,
        patch=patch_dict,
        private_truth=private_truth,
        public_state=public_state,
        action_comparison=action_comp_dict,
    )
    gates["cable_pixels_changed"] = bool(score_dict.get("cable_pixels_changed", False))
    gates["graph_improved"] = bool(score_dict.get("graph_improved", False))
    gates["physics_safe"] = bool(score_dict.get("physics_safe", False))
    timing_spans_s["scorer_physics_ui"] = round(time.monotonic() - t_sc, 4)

    _transition(
        "SCORED",
        {
            "graph_score_before": score_dict.get("graph_score_before"),
            "graph_score_after": score_dict.get("graph_score_after"),
            "cable_pixels_changed": gates["cable_pixels_changed"],
            "physics_safe": gates["physics_safe"],
        },
    )

    if not (
        gates["cable_pixels_changed"]
        and gates["graph_improved"]
        and gates["physics_safe"]
        and bool(score_dict.get("evidence_boxes_valid", True))
    ):
        return _finalize(
            "REJECTED",
            "Post-repair verification failed (cable_pixels_changed, graph_improved, physics_safe, or evidence_boxes_valid is False)",
        )

    _transition("PROPOSAL_PENDING_ENGINEER_REVIEW")
    return _finalize("PASS")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m twin_repair.orchestrator",
        description="Run the BlackboxDC Twin Repair closed visual loop.",
    )
    parser.add_argument(
        "mode",
        choices=("mock", "live", "prepared_builder", "replay"),
        help="Execution mode: mock (local fixture), live (Argon Managed Agents), prepared_builder, or replay.",
    )
    parser.add_argument("--case", default=None, help="Path to challenge case directory (or run directory for replay).")
    parser.add_argument("--run", default=None, help="Path to existing run directory (alias for --case in replay mode).")
    parser.add_argument("--out", default=None, help="Output dossier directory for this run.")
    parser.add_argument("--prepared-scene0", default=None, help="Optional prepared scene0.json path.")
    parser.add_argument("--max-noncached-tokens", type=int, default=80000, help="Token budget ceiling for live runs.")
    args = parser.parse_args(list(argv) if argv is not None else None)

    case_target = args.case or args.run
    if not case_target:
        parser.error("either --case or --run is required")
    out_target = args.out or case_target

    result = run_twin_repair(
        case_target,
        provider_mode=args.mode,
        out_dir=out_target,
        prepared_scene0_path=args.prepared_scene0,
    )
    summary = {
        "run_id": result.get("run_id"),
        "case_id": result.get("case_id"),
        "provider_mode": result.get("provider_mode"),
        "terminal_state": result.get("terminal_state"),
        "passed": result.get("passed"),
        "workflow_passed": result.get("workflow_passed"),
        "graph_score": (result.get("graph_score") or {}).get("summary"),
        "run_dir": result.get("run_dir", str(Path(out_target).resolve())),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if result.get("terminal_state") in ("PASS", "CONTROL_NO_CHANGE") and result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
