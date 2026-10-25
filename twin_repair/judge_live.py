"""Judge-created live challenges for the BlackboxDC Twin Repair demo.

Each judge click produces a fresh, blinded challenge:

- a newly rasterized ``current_sld.png`` (1280x800 RGB) for a server-selected N2/N3 feeder mapping,
  verified by the independent PNG topology parser before it can be used;
- a ``public_state.json`` that carries no answer-bearing fields (no revision letter, no target graph);
- a sealed ``private/truth.json`` that is only read by the scorer after the Critic's patch is frozen.

The run then executes through the unchanged ``run_twin_repair`` state machine, and the result is
post-processed into separate ``visual_repair_status`` / ``action_status`` / ``execution_source``
fields. Nothing here fabricates a provider ID, a PASS, or a canonical number.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import re
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from PIL import Image

from blackbox.render_sld import parse_sld_png_topology, render_sld_png
from twin_repair.challenge import (
    _copy_or_render_rear_images,
    _stamp_sld_revision_block,
    scan_for_truth_leakage,
)
from twin_repair.contracts import (
    CANONICAL_EDGES_A,
    CANONICAL_FEEDER_LIMITS_A,
    CANONICAL_RACK_CENTERS_X_M,
    CANONICAL_RACK_IDS,
    VIEWPORT_HEIGHT_PX,
    VIEWPORT_WIDTH_PX,
    build_canonical_scene_spec,
)
from twin_repair.managed_roles import MockTwinRepairProvider
from twin_repair.physics_adapter import (
    DEFAULT_RACK_CURRENTS_A,
    DEFAULT_RACK_MAX_CURRENTS_A,
    build_world_from_graph,
    compare_stale_and_repaired_actions,
    evaluate_actions,
    recommended_action,
)
from twin_repair.report import derive_outcome_fields

REPO_ROOT = Path(__file__).resolve().parent.parent
RECORDED_LIVE_RUN_DIR = REPO_ROOT / "runs" / "live1"

RUN_ID_RE = re.compile(r"^jr-[0-9a-f]{12}$")
FEEDERS: Tuple[str, str] = ("F1", "F2")
LOCKED_EDGES: Dict[str, str] = {"N1": "F1", "N4": "F2"}
DRAWING_PRESETS: Dict[str, Tuple[str, str]] = {"A": ("F1", "F2"), "B": ("F2", "F1")}
MAX_DRAWING_BYTES = 512 * 1024  # provider limit in blackbox.managed (MAX_IMAGE_BYTES)
NEUTRAL_SLD_NAME = "current_sld.png"

# Public request/answer fields that must never reach a role packet for judge challenges.
JUDGE_FORBIDDEN_PUBLIC_KEYS: frozenset[str] = frozenset(
    {
        "revision_current",
        "current_edges",
        "target_edges",
        "selected_feeders",
        "selection",
        "expected_receiver",
        "expected_decision",
        "judge_selection",
        "target_revision",
    }
)

DEFAULT_MAX_TEMP_C = 90.0
DEFAULT_ACTION_A = 8.0

STAGE_LABELS: Dict[str, str] = {
    "DRAWING_READY": "New as-built drawing generated",
    "QUEUED": "Queued for Argon",
    "RUNNING": "Worker started the run",
    "COMPLETED": "Result published",
    "FAILED": "Run failed; no recommendation issued",
    "CREATED": "Run created",
    "INPUT_LOCKED": "Inputs hashed and locked",
    "BUILDER_RUNNING": "Builder agent reading the old drawing",
    "SCENE_VALIDATED": "Old twin scene validated",
    "RENDER0_VALIDATED": "Old twin rendered (render0)",
    "CRITIC_RUNNING": "Critic agent comparing new drawing with render0",
    "PATCH_VALIDATED": "Critic patch validated",
    "RENDER1_VALIDATED": "Repaired twin re-rendered (render1)",
    "SCORED": "Independent verifier scored the result",
    "PROPOSAL_PENDING_ENGINEER_REVIEW": "Proposal waiting for engineer review",
    "PASS": "Verified",
    "CONTROL_NO_CHANGE": "Verified: no change needed",
    "REJECTED": "Rejected by verifier",
    "ABSTAINED": "Critic abstained",
    "TIMEOUT": "Provider timed out",
    "PROVIDER_ERROR": "Provider error",
    "INTERNAL_ERROR": "Server error",
}
TERMINAL_STATES = frozenset({"PASS", "CONTROL_NO_CHANGE", "REJECTED", "ABSTAINED", "TIMEOUT", "PROVIDER_ERROR", "INTERNAL_ERROR"})


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def new_run_id() -> str:
    return f"jr-{secrets.token_hex(6)}"


def is_valid_run_id(run_id: str) -> bool:
    return bool(RUN_ID_RE.match(str(run_id or "")))


def selected_mapping(n2: str, n3: str) -> Dict[str, str]:
    """Return the full N1..N4 mapping for the bounded demo (N1->F1 and N4->F2 are locked)."""
    if n2 not in FEEDERS or n3 not in FEEDERS:
        raise ValueError("N2 and N3 must each be 'F1' or 'F2'")
    return {"N1": LOCKED_EDGES["N1"], "N2": n2, "N3": n3, "N4": LOCKED_EDGES["N4"]}


def build_public_state(case_id: str) -> Dict[str, Any]:
    """Public telemetry and limits shared by Builder and Critic; deliberately answer-free."""
    return {
        "schema_version": "twin_repair.public_state.v1",
        "case_id": case_id,
        "revision_old": "A",
        "racks": [
            {
                "rack_id": rk,
                "current_a": DEFAULT_RACK_CURRENTS_A[rk],
                "max_current_a": DEFAULT_RACK_MAX_CURRENTS_A[rk],
                "center_x_m": CANONICAL_RACK_CENTERS_X_M[rk],
                "sensor_id": {"N1": "SNS_K1", "N2": "SNS_K3", "N3": "SNS_K4", "N4": "SNS_K2"}[rk],
            }
            for rk in CANONICAL_RACK_IDS
        ],
        "feeders": {
            "F1": {"feeder_id": "F1", "limit_a": CANONICAL_FEEDER_LIMITS_A["F1"], "style": "blue"},
            "F2": {"feeder_id": "F2", "limit_a": CANONICAL_FEEDER_LIMITS_A["F2"], "style": "orange"},
        },
        "viewport": {
            "width_px": VIEWPORT_WIDTH_PX,
            "height_px": VIEWPORT_HEIGHT_PX,
            "allowed_camera_poses": ["rear_inspection", "wiring_oblique"],
        },
        "scene_bounds": {
            "center_x_min_m": -0.5,
            "center_x_max_m": 3.5,
            "width_m": 0.6,
            "height_m": 2.0,
            "depth_m": 1.0,
        },
        "action_size_a": DEFAULT_ACTION_A,
        "allowed_offload_Sign_targets": ["N2", "N3"],
        "max_temp_c": DEFAULT_MAX_TEMP_C,
    }


def assert_judge_packet_is_blind(payload: Any, label: str) -> None:
    """Fail closed if a judge-run packet contains answer-bearing keys or case hints."""
    violations: List[str] = []

    def _walk(obj: Any, path: str) -> None:
        if isinstance(obj, Mapping):
            for k, v in obj.items():
                if str(k) in JUDGE_FORBIDDEN_PUBLIC_KEYS:
                    violations.append(f"{path}.{k}")
                _walk(v, f"{path}.{k}")
        elif isinstance(obj, (list, tuple)):
            for i, v in enumerate(obj):
                _walk(v, f"{path}[{i}]")
        elif isinstance(obj, str):
            for hint in ("drift_A_to_B", "control_A_to_A", "current_sld_B", "current_sld_A"):
                if hint in obj:
                    violations.append(f"{path}: contains {hint!r}")

    _walk(payload, label)
    scan_for_truth_leakage(payload if isinstance(payload, (Mapping, list)) else str(payload), raise_on_leak=True)
    if violations:
        raise ValueError(f"Judge packet leak detected: {violations}")


def _image_record(path: Path, source: str) -> Dict[str, Any]:
    with Image.open(path) as im:
        width, height, mode = im.width, im.height, im.mode
    return {
        "sha256": _sha256_file(path),
        "size_bytes": path.stat().st_size,
        "width_px": width,
        "height_px": height,
        "mode": mode,
        "source": source,
        "created_at_utc": utc_now_iso(),
    }


def build_judge_challenge(run_dir: Path, mapping: Mapping[str, str]) -> Dict[str, Any]:
    """Create a fresh blinded challenge in ``run_dir/input`` and preview copy in ``run_dir/public``."""
    mapping = {str(k): str(v) for k, v in dict(mapping).items()}
    if sorted(mapping) != list(CANONICAL_RACK_IDS) or any(v not in FEEDERS for v in mapping.values()):
        raise ValueError(f"Invalid mapping {mapping!r}")
    if any(mapping[rk] != fd for rk, fd in LOCKED_EDGES.items()):
        raise ValueError("N1 must stay on F1 and N4 must stay on F2 in the bounded demo")

    run_dir = Path(run_dir).resolve()
    if not is_valid_run_id(run_dir.name):
        raise ValueError(f"Run directory name must be an opaque judge run ID, got {run_dir.name!r}")
    input_dir = run_dir / "input"
    pub_dir = input_dir / "public"
    priv_dir = input_dir / "private"
    pub_dir.mkdir(parents=True, exist_ok=True)
    priv_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(priv_dir, 0o700)

    case_id = f"case-{secrets.token_hex(6)}"
    drawing_no = secrets.token_hex(3).upper()

    old_sld = pub_dir / "old_sld_A.png"
    render_sld_png("case_A", old_sld, svg_output_path=priv_dir / "old_sld_A.svg")
    _stamp_sld_revision_block(old_sld, "A (STALE BASELINE)", "SHEET 1 OF 1 | PRIOR TWIN BASELINE")

    current_sld = pub_dir / NEUTRAL_SLD_NAME
    # The SVG twin of the drawing encodes the mapping in plain text, so it stays private.
    render_sld_png(case_id, current_sld, svg_output_path=priv_dir / "current_sld.svg", mapping=mapping)
    _stamp_sld_revision_block(current_sld, "CURRENT AS-BUILT", f"SHEET 1 OF 1 | DRAWING NO. {drawing_no}")

    parsed_old = parse_sld_png_topology(old_sld)["mapping"]
    parsed_current = parse_sld_png_topology(current_sld)["mapping"]
    if parsed_old != dict(CANONICAL_EDGES_A):
        raise ValueError(f"old_sld_A.png failed the independent parser check: {parsed_old}")
    if parsed_current != mapping:
        raise ValueError(f"current_sld.png parsed as {parsed_current}, expected the selected private graph")

    rec = _image_record(current_sld, "server_rendered_judge_challenge")
    if (rec["width_px"], rec["height_px"], rec["mode"]) != (1280, 800, "RGB") or rec["size_bytes"] > MAX_DRAWING_BYTES:
        raise ValueError(f"current_sld.png violates provider image limits: {rec}")

    thermal, _rgb = _copy_or_render_rear_images(pub_dir)

    public_state = build_public_state(case_id)
    assert_judge_packet_is_blind(public_state, "public_state")
    _write_json(pub_dir / "public_state.json", public_state)

    old_edges = dict(CANONICAL_EDGES_A)
    truth = {
        "schema_version": "twin_repair.truth.v1",
        "judge_challenge": True,
        "case_id": case_id,
        "revision_old": "A",
        "old_edges": old_edges,
        "current_edges": dict(mapping),
        "expected_decision": "no_change" if mapping == old_edges else "repair",
        "physics_oracle": compare_stale_and_repaired_actions(old_edges, mapping, public_state),
        "drawing_no": drawing_no,
        "created_at_utc": utc_now_iso(),
    }
    _write_json(priv_dir / "truth.json", truth)
    os.chmod(priv_dir / "truth.json", 0o600)

    _write_json(
        input_dir / "case_manifest.json",
        {
            "schema_version": "twin_repair.judge_case.v1",
            "case_id": case_id,
            "revision_old": "A",
            "revision_current": "CURRENT",
            "public_assets_sha256": {
                "old_sld_A.png": _sha256_file(old_sld),
                NEUTRAL_SLD_NAME: rec["sha256"],
                "old_thermal_rear.png": _sha256_file(thermal),
                "public_state.json": _sha256_file(pub_dir / "public_state.json"),
            },
        },
    )

    run_pub = run_dir / "public"
    run_pub.mkdir(parents=True, exist_ok=True)
    shutil.copy2(current_sld, run_pub / NEUTRAL_SLD_NAME)
    shutil.copy2(old_sld, run_pub / "old_sld_A.png")
    shutil.copy2(thermal, run_pub / "old_thermal_rear.png")

    input_manifest = {
        "schema_version": "twin_repair.input_manifest.v1",
        "run_id": run_dir.name,
        "case_id": case_id,
        "drawing_no": drawing_no,
        "images": {
            NEUTRAL_SLD_NAME: rec,
            "old_sld_A.png": _image_record(old_sld, "server_rendered_stale_baseline"),
            "old_thermal_rear.png": _image_record(thermal, "fixed_rear_thermal_capture"),
        },
        "public_state_sha256": _sha256_file(pub_dir / "public_state.json"),
        "created_at_utc": utc_now_iso(),
    }
    _write_json(run_dir / "input_manifest.json", input_manifest)
    return {"run_id": run_dir.name, "case_id": case_id, "drawing_no": drawing_no, "drawing_sha256": rec["sha256"]}


def prepare_recorded_builder_scene(run_dir: Path, case_id: str) -> Tuple[Path, Dict[str, Any]]:
    """Copy the recorded live Builder scene (live1) and rebind only its opaque case_id."""
    src = RECORDED_LIVE_RUN_DIR / "scene0.json"
    if not src.is_file():
        raise FileNotFoundError("Recorded Builder scene runs/live1/scene0.json is unavailable")
    scene = json.loads(src.read_text(encoding="utf-8"))
    scene["case_id"] = case_id
    dst = Path(run_dir) / "input" / "prepared_scene0.json"
    _write_json(dst, scene)
    b_prov = _read_json(RECORDED_LIVE_RUN_DIR / "roles" / "builder_provenance.json")
    note = {
        "source_file": "runs/live1/scene0.json",
        "source_sha256": _sha256_file(src),
        "source_builder_interaction_id": b_prov.get("interaction_id"),
        "source_builder_returned_model": b_prov.get("returned_model"),
        "modification": "case_id rebound to this challenge; all geometry and edges unchanged",
    }
    return dst, note


class JudgeMockProvider(MockTwinRepairProvider):
    """Offline rehearsal provider: reads the sealed truth to emit a patch. Never Argon-verified."""

    def __init__(self, truth_edges: Mapping[str, str], mode: str = "normal") -> None:
        super().__init__(mode=mode)
        self.truth_edges = {str(k): str(v) for k, v in dict(truth_edges).items()}

    def run_critic(self, **kwargs: Any) -> Any:
        patch, raw, prov = super().run_critic(**kwargs)
        if patch is None or self.mode != "normal" or str(patch.get("decision")) == "abstain":
            return patch, raw, prov
        sld_name = Path(kwargs["current_sld_path"]).name
        r_boxes = {e["rack_id"]: e["bbox_xyxy_px"] for e in patch["observed_render_edges"]}
        s_boxes = {e["rack_id"]: e["bbox_xyxy_px"] for e in patch["target_sld_edges"]}
        observed = {e["rack_id"]: e["feeder_id"] for e in patch["observed_render_edges"]}
        disc, ops = [], []
        for rk in CANONICAL_RACK_IDS:
            if observed[rk] != self.truth_edges[rk]:
                disc.append(
                    {
                        "rack_id": rk,
                        "observed_feeder": observed[rk],
                        "required_feeder": self.truth_edges[rk],
                        "render_evidence_bbox_xyxy_px": r_boxes[rk],
                        "sld_evidence_bbox_xyxy_px": s_boxes[rk],
                        "reason": f"Mock rehearsal: render0 shows {rk} on {observed[rk]}, drawing shows {self.truth_edges[rk]}.",
                    }
                )
                ops.append({"op": "replace_feeder", "rack_id": rk, "from": observed[rk], "to": self.truth_edges[rk]})
        patch = copy.deepcopy(patch)
        patch["target_revision_seen"] = "UNKNOWN"
        patch["target_sld_edges"] = [
            {"rack_id": rk, "feeder_id": self.truth_edges[rk], "image_ref": sld_name, "bbox_xyxy_px": s_boxes[rk]}
            for rk in CANONICAL_RACK_IDS
        ]
        patch["discrepancies"] = disc
        patch["operations"] = ops
        patch["decision"] = "repair" if ops else "no_change"
        raw = (json.dumps(patch, indent=2, sort_keys=True) + "\n").encode("utf-8")
        prov = dataclasses.replace(prov, retrieved_sha256=hashlib.sha256(raw).hexdigest())
        return patch, raw, prov


def _verify_input_binding(run_dir: Path, card: Mapping[str, Any]) -> Optional[str]:
    """Check the Critic's provider manifest saw exactly this run's drawing bytes."""
    manifest = _read_json(run_dir / "input_manifest.json")
    expected = ((manifest.get("images") or {}).get(NEUTRAL_SLD_NAME) or {}).get("sha256")
    critic = _read_json(run_dir / "roles" / "critic_provenance.json")
    if not critic:
        return None
    sent = {m.get("name"): m.get("sha256") for m in critic.get("image_manifest") or [] if isinstance(m, Mapping)}
    if sent.get(NEUTRAL_SLD_NAME) != expected:
        return "Critic input image hash does not match this run's drawing"
    served = run_dir / "public" / NEUTRAL_SLD_NAME
    if not served.is_file() or _sha256_file(served) != expected:
        return "Browser-visible drawing does not match the drawing sent to the Critic"
    return None


def finalize_judge_result(run_dir: Path, *, mode: str, prepared_note: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Merge separated outcome fields into result_public.json for a finished judge run."""
    run_dir = Path(run_dir)
    card = _read_json(run_dir / "result_public.json")
    run_data = {
        **card,
        "gates": card.get("gates") or {},
        "patch": _read_json(run_dir / "patch.json") or card.get("patch") or {},
        "score": _read_json(run_dir / "score.json"),
        "action_comparison": _read_json(run_dir / "action_comparison.json"),
        "builder_provenance": _read_json(run_dir / "roles" / "builder_provenance.json"),
        "critic_provenance": _read_json(run_dir / "roles" / "critic_provenance.json"),
    }
    outcome = derive_outcome_fields(run_data)
    binding_error = _verify_input_binding(run_dir, card)
    if binding_error and outcome["visual_repair_status"] in ("REPAIRED", "NO_CHANGE"):
        outcome.update(visual_repair_status="REJECTED", action_status="INSUFFICIENT_EVIDENCE", safe_receivers=[], recommended_receiver=None)
        card["terminal_state"] = "REJECTED"
        card["error"] = binding_error
        card["passed"] = False
    if mode == "mock":
        outcome["execution_source"] = "mock_fixture"
    card.update(outcome)
    card["judge_mode"] = mode
    if prepared_note:
        card["prepared_builder_source"] = dict(prepared_note)
    card["input_manifest"] = _read_json(run_dir / "input_manifest.json")
    card.pop("run_dir", None)
    _write_json(run_dir / "result_public.json", card)
    return card


def counterfactual_actions(graph: Mapping[str, str], public_state: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Deterministic operator what-ifs (no action / move 8 A to N2 / N3) on a given wiring graph."""
    ps = public_state or build_public_state("counterfactual")
    outcomes = evaluate_actions(dict(graph), ps)
    rec = recommended_action(outcomes)
    world = build_world_from_graph(dict(graph), ps)
    n4_now = None
    try:
        from blackbox.domain import simulate_world

        traj = simulate_world(world, migration=None)
        n4_now = round(float(traj.temps_C["N4"][0]), 2)
    except Exception:
        n4_now = None
    return {
        "graph": dict(graph),
        "n4_temp_now_c": n4_now,
        "max_temp_c": float(ps.get("max_temp_c", DEFAULT_MAX_TEMP_C)),
        "action_size_a": float(ps.get("action_size_a", DEFAULT_ACTION_A)),
        "recommended_receiver": rec.target_rack if rec else None,
        "safe_receivers": sorted(o.target_rack for o in outcomes if o.safe and o.target_rack),
        "outcomes": [o.to_dict() for o in outcomes],
    }


def stale_twin_preview() -> Dict[str, Any]:
    """What the deployed (old) twin says before any new drawing is checked."""
    return counterfactual_actions(dict(CANONICAL_EDGES_A))


def read_stage_events(run_dir: Path) -> List[Dict[str, Any]]:
    """Translate persisted events.jsonl into safe stage summaries and live role progress."""
    stages: List[Dict[str, Any]] = []
    path = Path(run_dir) / "events.jsonl"
    if not path.is_file():
        return stages
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        kind = ev.get("event")
        if kind == "state_transition":
            st = str(ev.get("state", ""))
            item = {"kind": "stage", "state": st, "label": STAGE_LABELS.get(st, st), "at_utc": ev.get("timestamp_utc")}
            if ev.get("decision"):
                item["decision"] = ev.get("decision")
            if ev.get("reason"):
                item["reason"] = str(ev.get("reason"))[:300]
            stages.append(item)
        elif kind in ("judge_status", "role_progress"):
            item = {k: ev.get(k) for k in ("state", "role", "interaction_id", "environment_id", "status", "elapsed_s", "step_count", "step_types", "mode", "reason")}
            item = {k: v for k, v in item.items() if v is not None}
            item["kind"] = "stage" if kind == "judge_status" else "role"
            if kind == "judge_status":
                item["label"] = STAGE_LABELS.get(str(ev.get("state")), str(ev.get("state")))
            item["at_utc"] = ev.get("timestamp_utc")
            prev = stages[-1] if stages else None
            if (
                item["kind"] == "role"
                and prev is not None
                and prev.get("kind") == "role"
                and all(prev.get(k) == item.get(k) for k in ("role", "interaction_id", "status", "step_count"))
            ):
                # Polling heartbeats only advance elapsed time; keep one updating entry per state.
                item["first_at_utc"] = prev.get("first_at_utc", prev.get("at_utc"))
                stages[-1] = item
                continue
            stages.append(item)
    return stages


def append_run_event(run_dir: Path, event: Mapping[str, Any]) -> None:
    from blackbox.managed import append_journal_event

    append_journal_event(Path(run_dir) / "events.jsonl", dict(event))


def execute_judge_run(run_dir: Path, *, mode: str, provider: Any = None) -> Dict[str, Any]:
    """Run the unchanged Twin Repair state machine on a prepared judge challenge.

    ``mode``: ``full`` (Builder + Critic live), ``fast`` (recorded live Builder scene, Critic live),
    or ``mock`` (offline fixture rehearsal, never Argon-verified).
    """
    from twin_repair.managed_roles import LiveManagedTwinRepairProvider
    from twin_repair.orchestrator import run_twin_repair

    run_dir = Path(run_dir).resolve()
    if mode not in ("full", "fast", "mock"):
        raise ValueError(f"Unsupported judge mode {mode!r}")
    case_id = str(_read_json(run_dir / "input" / "public" / "public_state.json").get("case_id", ""))
    prepared_path: Optional[Path] = None
    prepared_note: Optional[Dict[str, Any]] = None
    if mode == "fast":
        prepared_path, prepared_note = prepare_recorded_builder_scene(run_dir, case_id)

    if provider is None:
        if mode == "mock":
            truth = _read_json(run_dir / "input" / "private" / "truth.json")
            provider = JudgeMockProvider(truth["current_edges"])
        else:
            provider = LiveManagedTwinRepairProvider()
    if hasattr(provider, "progress_fn"):
        provider.progress_fn = lambda info: append_run_event(run_dir, {"event": "role_progress", **dict(info)})

    provider_mode = {"full": "live", "fast": "prepared_builder", "mock": "mock"}[mode]
    run_twin_repair(
        run_dir / "input",
        provider_mode=provider_mode,
        out_dir=run_dir,
        provider=provider,
        prepared_scene0_path=prepared_path,
    )
    return finalize_judge_result(run_dir, mode=mode, prepared_note=prepared_note)


def write_failure_result(run_dir: Path, *, mode: str, terminal_state: str, reason: str) -> Dict[str, Any]:
    """Record a visible terminal failure; never reuse another run's result."""
    run_dir = Path(run_dir)
    existing = _read_json(run_dir / "result_public.json")
    if existing.get("terminal_state") in TERMINAL_STATES and existing.get("visual_repair_status"):
        return existing
    card = {
        "schema_version": "twin_repair_result_public.v1",
        "run_id": run_dir.name,
        "terminal_state": terminal_state,
        "passed": False,
        "argon_verified": False,
        "visual_repair_status": "REJECTED",
        "action_status": "INSUFFICIENT_EVIDENCE",
        "safe_receivers": [],
        "recommended_receiver": None,
        "execution_source": {"full": "full_live_two_agents", "fast": "prepared_builder_live_critic", "mock": "mock_fixture"}.get(mode, mode),
        "judge_mode": mode,
        "error": reason[:500],
        "input_manifest": _read_json(run_dir / "input_manifest.json"),
        "generated_at_utc": utc_now_iso(),
    }
    _write_json(run_dir / "result_public.json", card)
    return card
