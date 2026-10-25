"""As-built drift challenge fixture generator, numeric oracle, and privacy scanner for Twin Repair.

Implements Phase A (§2.A2, §2.A3, §9.1) of `11_TWIN_REPAIR_ADDON_IMPLEMENTATION_PLAN_EN.md`:
- `reference_case()`
- `build_challenge_fixture(out_dir, *, seed=20261025, case_type="drift_A_to_B")`
- `scan_for_truth_leakage(target, *, raise_on_leak=True)`
- CLI: `python -m twin_repair.challenge build --seed 20261025 --out runs/fixtures/drift1`
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from PIL import Image, ImageDraw, ImageFont

from blackbox.domain import (
    DEFAULT_MAX_TEMP_C,
    DEFAULT_MIGRATION_AMPS,
    TOTAL_WORLD_CURRENT_A,
)
from blackbox.render_sld import (
    HEADER_BG_COLOR,
    INK_COLOR,
    SUB_INK_COLOR,
    parse_sld_png_topology,
    render_sld_png,
)
from blackbox.scene_bridge import capture_3d_rear_aisle, verify_visual_artifact
from twin_repair.contracts import (
    CANONICAL_EDGES_A,
    CANONICAL_EDGES_B,
    CANONICAL_FEEDER_LIMITS_A,
    CANONICAL_RACK_CENTERS_X_M,
    CANONICAL_RACK_IDS,
    DEFAULT_SLD_BRANCH_BBOXES_PX,
    VIEWPORT_HEIGHT_PX,
    VIEWPORT_WIDTH_PX,
    build_canonical_scene_spec,
    canonical_json_bytes,
    sha256_bytes,
)
from twin_repair.physics_adapter import (
    DEFAULT_RACK_CURRENTS_A,
    DEFAULT_RACK_MAX_CURRENTS_A,
    compare_stale_and_repaired_actions,
    compute_feeder_currents,
    evaluate_actions,
    recommended_action,
)


CHALLENGE_SCHEMA_VERSION: str = "twin_repair.challenge.v1"
DEFAULT_CHALLENGE_SEED: int = 20261025

FORBIDDEN_PUBLIC_KEYS: frozenset[str] = frozenset(
    {
        "truth",
        "private_truth",
        "sealed_truth",
        "current_edges",
        "old_edges",
        "target_edges",
        "safe_receiver",
        "wrong_receiver",
        "expected_new_receiver",
        "expected_old_receiver",
        "expected_patch",
        "desired_patch",
        "reference_graph",
        "physics_oracle",
        "sld_branch_bboxes_px",
        "expected_pre_score",
        "expected_post_score",
    }
)

FORBIDDEN_PUBLIC_SUBSTRINGS: Tuple[str, ...] = (
    "feeder reassignment N2",
    "swap N2 and N3",
    "expected_new_receiver",
    "private/truth.json",
)


def _stamp_sld_revision_block(png_path: Path, revision_label: str, subtitle_label: str) -> None:
    """Stamp the explicit revision identifier in the SLD title block without touching bus/branch geometry."""
    with Image.open(png_path) as im:
        img = im.convert("RGB")
    draw = ImageDraw.Draw(img)
    try:
        font_bold = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 13)
        font_sub = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
    except OSError:
        font_bold = ImageFont.load_default()
        font_sub = font_bold

    # Clear only the right-side revision cell inside the header box (y=28..104, x=850..1250)
    draw.rectangle([852, 34, 1248, 102], fill=HEADER_BG_COLOR)
    draw.text((860, 44), f"REV: {revision_label}", fill=INK_COLOR, font=font_bold)
    draw.text((860, 72), subtitle_label, fill=SUB_INK_COLOR, font=font_sub)
    img.save(png_path, format="PNG", optimize=True)


def reference_case() -> Dict[str, Any]:
    """Compute and assert all Phase A3 (§2.A3) independent numeric oracle invariants."""
    rack_currents = dict(DEFAULT_RACK_CURRENTS_A)
    total_current_a = sum(rack_currents.values())
    if abs(total_current_a - TOTAL_WORLD_CURRENT_A) > 1e-9:
        raise AssertionError(f"Expected sum(rack currents) = 68 A, got {total_current_a}")

    edges_a = dict(CANONICAL_EDGES_A)
    edges_b = dict(CANONICAL_EDGES_B)
    if tuple(edges_a[rk] for rk in CANONICAL_RACK_IDS) != ("F1", "F1", "F2", "F2"):
        raise AssertionError(f"Unexpected A edges: {edges_a}")
    if tuple(edges_b[rk] for rk in CANONICAL_RACK_IDS) != ("F1", "F2", "F1", "F2"):
        raise AssertionError(f"Unexpected B edges: {edges_b}")

    init_feeders_a = compute_feeder_currents(edges_a, rack_currents)
    init_feeders_b = compute_feeder_currents(edges_b, rack_currents)
    if (init_feeders_a["F1"], init_feeders_a["F2"]) != (40.0, 28.0):
        raise AssertionError(f"Unexpected initial feeder currents in A: {init_feeders_a}")
    if (init_feeders_b["F1"], init_feeders_b["F2"]) != (40.0, 28.0):
        raise AssertionError(f"Unexpected initial feeder currents in B: {init_feeders_b}")

    outcomes_a = {o.action_id: o for o in evaluate_actions(edges_a)}
    outcomes_b = {o.action_id: o for o in evaluate_actions(edges_b)}

    # B + transfer N4->N3 by 8 A: F1 = 48 A > 44 A; reject
    b_to_n3 = outcomes_b["MIGRATE_N4_TO_N3_8A"]
    if abs(b_to_n3.f1_current_a - 48.0) > 1e-6 or b_to_n3.f1_current_a <= b_to_n3.f1_limit_a or b_to_n3.safe:
        raise AssertionError(f"Expected B + N4->N3 to overload F1 at 48 A > 44 A and be rejected: {b_to_n3}")

    # B + transfer N4->N2 by 8 A: F1 = 40 A; F2 = 28 A; accept
    b_to_n2 = outcomes_b["MIGRATE_N4_TO_N2_8A"]
    if abs(b_to_n2.f1_current_a - 40.0) > 1e-6 or abs(b_to_n2.f2_current_a - 28.0) > 1e-6 or not b_to_n2.safe:
        raise AssertionError(f"Expected B + N4->N2 to keep F1=40 A, F2=28 A and be accepted: {b_to_n2}")

    # No action at 180 s: N4 connector > 90 C
    no_action_b = outcomes_b["NO_ACTION"]
    if no_action_b.n4_temp_180s_c <= DEFAULT_MAX_TEMP_C or no_action_b.safe:
        raise AssertionError(f"Expected NO_ACTION N4(180s) > 90 C, got {no_action_b.n4_temp_180s_c}")

    # Correct 8 A transfer at 180 s: N4 connector < 90 C
    if b_to_n2.n4_temp_180s_c >= DEFAULT_MAX_TEMP_C or not b_to_n2.thermal_safe:
        raise AssertionError(f"Expected 8 A transfer N4(180s) < 90 C, got {b_to_n2.n4_temp_180s_c}")

    rec_a = recommended_action(list(outcomes_a.values()))
    rec_b = recommended_action(list(outcomes_b.values()))
    if rec_a is None or rec_a.target_rack != "N3":
        raise AssertionError(f"Expected stale A recommended receiver N3, got {rec_a}")
    if rec_b is None or rec_b.target_rack != "N2":
        raise AssertionError(f"Expected current B recommended receiver N2, got {rec_b}")

    return {
        "passed": True,
        "total_current_a": total_current_a,
        "edges_A": edges_a,
        "edges_B": edges_b,
        "initial_feeder_currents_A": init_feeders_a,
        "initial_feeder_currents_B": init_feeders_b,
        "no_action_n4_temp_180s_c": no_action_b.n4_temp_180s_c,
        "offload_8a_n4_temp_180s_c": b_to_n2.n4_temp_180s_c,
        "max_temp_limit_c": DEFAULT_MAX_TEMP_C,
        "stale_recommended_receiver": rec_a.target_rack,
        "current_recommended_receiver": rec_b.target_rack,
        "outcomes_A": {k: v.to_dict() for k, v in outcomes_a.items()},
        "outcomes_B": {k: v.to_dict() for k, v in outcomes_b.items()},
    }


def _normalize_case_type(case_type: str) -> Tuple[str, str, str]:
    raw = str(case_type).strip()
    if raw in ("drift_A_to_B", "A_to_B", "drift", "primary"):
        return ("drift_A_to_B", "A", "B")
    if raw in ("control_A_to_A", "A_to_A", "control", "no_change"):
        return ("control_A_to_A", "A", "A")
    raise ValueError(f"Unsupported case_type: {case_type!r}. Expected 'drift_A_to_B' or 'control_A_to_A'.")


def _copy_or_render_rear_images(out_dir: Path) -> Tuple[Path, Path]:
    repo_root = Path(__file__).resolve().parent.parent
    cached_thermal = repo_root / "artifacts" / "public" / "rear_aisle_thermal.png"
    cached_rgb = repo_root / "artifacts" / "public" / "rear_aisle_rgb.png"

    thermal_dst = out_dir / "old_thermal_rear.png"
    rgb_dst = out_dir / "old_rgb_rear.png"

    if cached_thermal.is_file() and cached_rgb.is_file():
        shutil.copyfile(cached_thermal, thermal_dst)
        shutil.copyfile(cached_rgb, rgb_dst)
    else:
        capture_3d_rear_aisle(out_dir)
        gen_thermal = out_dir / "rear_aisle_thermal.png"
        gen_rgb = out_dir / "rear_aisle_rgb.png"
        if gen_thermal.is_file():
            shutil.move(str(gen_thermal), str(thermal_dst))
        if gen_rgb.is_file():
            shutil.move(str(gen_rgb), str(rgb_dst))

    verify_visual_artifact(thermal_dst)
    verify_visual_artifact(rgb_dst)
    return thermal_dst, rgb_dst


def scan_for_truth_leakage(
    target: Union[Path, str, Mapping[str, Any], Sequence[Any]],
    *,
    raise_on_leak: bool = True,
) -> Dict[str, Any]:
    """Scan public files or JSON structures for private truth keys or target answers."""
    violations: List[str] = []

    def _scan_obj(obj: Any, path_prefix: str) -> None:
        if isinstance(obj, Mapping):
            for k, v in obj.items():
                ks = str(k)
                if ks in FORBIDDEN_PUBLIC_KEYS:
                    violations.append(f"{path_prefix}.{ks}: forbidden private key")
                _scan_obj(v, f"{path_prefix}.{ks}")
        elif isinstance(obj, (list, tuple)):
            for idx, item in enumerate(obj):
                _scan_obj(item, f"{path_prefix}[{idx}]")
        elif isinstance(obj, str):
            for sub in FORBIDDEN_PUBLIC_SUBSTRINGS:
                if sub in obj:
                    violations.append(f"{path_prefix}: contains forbidden substring {sub!r}")

    if isinstance(target, (Path, str)):
        is_existing_dir = False
        is_existing_file = False
        p: Optional[Path] = None
        if isinstance(target, Path) or ("\n" not in target and len(target) <= 512):
            try:
                p = Path(target)
                is_existing_dir = p.is_dir()
                is_existing_file = not is_existing_dir and p.is_file()
            except (OSError, ValueError):
                is_existing_dir = False
                is_existing_file = False

        if is_existing_dir and p is not None:
            for child in sorted(p.rglob("*")):
                if "private" in child.parts or child.name in ("truth.json", "case_manifest.json", "scene_A.json"):
                    continue
                if child.is_file() and child.suffix == ".json":
                    data = json.loads(child.read_text(encoding="utf-8"))
                    _scan_obj(data, child.name)
        elif is_existing_file and p is not None:
            if p.suffix == ".json":
                data = json.loads(p.read_text(encoding="utf-8"))
                _scan_obj(data, p.name)
            else:
                text = p.read_text(encoding="utf-8", errors="ignore")
                _scan_obj(text, p.name)
        else:
            _scan_obj(str(target), "root")
    else:
        _scan_obj(target, "root")

    leaked = len(violations) > 0
    if leaked and raise_on_leak:
        raise ValueError(f"Truth leakage detected in public assets: {violations}")
    return {
        "leaked": leaked,
        "violations": violations,
    }


def build_challenge_fixture(
    out_dir: Union[Path, str],
    *,
    seed: int = DEFAULT_CHALLENGE_SEED,
    case_type: str = "drift_A_to_B",
) -> Dict[str, Any]:
    """Generate the public/private challenge fixture for `drift_A_to_B` or `control_A_to_A`."""
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a non-negative integer")

    case_id, rev_old, rev_curr = _normalize_case_type(case_type)
    out_path = Path(out_dir).resolve()
    out_path.mkdir(parents=True, exist_ok=True)

    # 1. Render old SLD A and current SLD (B for drift, A for control)
    old_sld_path = out_path / "old_sld_A.png"
    render_sld_png("case_A", old_sld_path)
    _stamp_sld_revision_block(
        old_sld_path,
        revision_label="A (STALE BASELINE)",
        subtitle_label="SHEET 1 OF 1 | PRIOR TWIN BASELINE",
    )

    current_sld_path = out_path / "current_sld_B.png"
    if rev_curr == "B":
        render_sld_png("case_B", current_sld_path)
        _stamp_sld_revision_block(
            current_sld_path,
            revision_label="B (AS-BUILT CURRENT)",
            subtitle_label="SHEET 1 OF 1 | REVISED AS-BUILT SCHEDULE",
        )
    else:
        render_sld_png("case_A", current_sld_path)
        _stamp_sld_revision_block(
            current_sld_path,
            revision_label="A (STALE BASELINE)",
            subtitle_label="SHEET 1 OF 1 | PRIOR TWIN BASELINE",
        )

    # Verify SLD legibility and topology via pixel parser
    parsed_old_sld = parse_sld_png_topology(old_sld_path)
    parsed_curr_sld = parse_sld_png_topology(current_sld_path)
    if parsed_old_sld["mapping"] != CANONICAL_EDGES_A:
        raise ValueError(f"old_sld_A.png failed pixel topology check: {parsed_old_sld}")
    expected_curr_edges = CANONICAL_EDGES_B if rev_curr == "B" else CANONICAL_EDGES_A
    if parsed_curr_sld["mapping"] != expected_curr_edges:
        raise ValueError(f"current_sld_B.png failed pixel topology check: {parsed_curr_sld}")

    # 2. Copy or render rear thermal + RGB images
    thermal_path, rgb_path = _copy_or_render_rear_images(out_path)

    # 3. Build public_state.json (strictly free of current B edges, safe receiver, or desired patch)
    public_state: Dict[str, Any] = {
        "schema_version": "twin_repair.public_state.v1",
        "case_id": case_id,
        "seed": int(seed),
        "revision_old": rev_old,
        "revision_current": rev_curr,
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
        "action_size_a": DEFAULT_MIGRATION_AMPS,
        "allowed_offload_Sign_targets": ["N2", "N3"],
        "max_temp_c": DEFAULT_MAX_TEMP_C,
    }
    scan_for_truth_leakage(public_state, raise_on_leak=True)

    public_state_path = out_path / "public_state.json"
    public_state_path.write_bytes(canonical_json_bytes(public_state))

    # 4. Build baseline SceneSpec A (`scene_A.json`)
    scene_a = build_canonical_scene_spec(
        case_id=case_id,
        source_revision=rev_old,
        edges=CANONICAL_EDGES_A,
    )
    scene_a_path = out_path / "scene_A.json"
    scene_a_path.write_bytes(scene_a.canonical_bytes())

    # 5. Build sealed private/truth.json (mode 0700 directory)
    private_dir = out_path / "private"
    private_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(private_dir, 0o700)

    old_edges = dict(CANONICAL_EDGES_A)
    current_edges = dict(expected_curr_edges)
    action_comp = compare_stale_and_repaired_actions(old_edges, current_edges, public_state)

    truth_payload: Dict[str, Any] = {
        "schema_version": "twin_repair.truth.v1",
        "case_id": case_id,
        "case_type": case_id,
        "seed": int(seed),
        "revision_old": rev_old,
        "revision_current": rev_curr,
        "maintenance_note": (
            "feeder reassignment N2<->N3 at 09:10"
            if rev_curr == "B"
            else "control verification: no feeder reassignment"
        ),
        "old_edges": old_edges,
        "current_edges": current_edges,
        "sld_branch_bboxes_px": {rk: list(box) for rk, box in DEFAULT_SLD_BRANCH_BBOXES_PX.items()},
        "expected_decision": "repair" if rev_curr == "B" else "no_change",
        "expected_pre_score": 2 if rev_curr == "B" else 4,
        "expected_post_score": 4,
        "expected_old_receiver": "N3",
        "expected_new_receiver": "N2" if rev_curr == "B" else "N3",
        "physics_oracle": action_comp,
    }
    truth_path = private_dir / "truth.json"
    truth_path.write_bytes(canonical_json_bytes(truth_payload))
    os.chmod(truth_path, 0o600)

    # 6. Build case_manifest.json with SHA-256 of every asset
    public_assets = {
        "old_sld_A.png": sha256_bytes(old_sld_path.read_bytes()),
        "current_sld_B.png": sha256_bytes(current_sld_path.read_bytes()),
        "old_thermal_rear.png": sha256_bytes(thermal_path.read_bytes()),
        "old_rgb_rear.png": sha256_bytes(rgb_path.read_bytes()),
        "public_state.json": sha256_bytes(public_state_path.read_bytes()),
        "scene_A.json": sha256_bytes(scene_a_path.read_bytes()),
    }
    manifest_payload: Dict[str, Any] = {
        "schema_version": CHALLENGE_SCHEMA_VERSION,
        "case_id": case_id,
        "seed": int(seed),
        "revision_old": rev_old,
        "revision_current": rev_curr,
        "renderer_version": "static3d.v2_parameterized",
        "public_assets_sha256": public_assets,
        "truth_sha256": sha256_bytes(truth_path.read_bytes()),
    }
    manifest_path = out_path / "case_manifest.json"
    manifest_path.write_bytes(canonical_json_bytes(manifest_payload))

    # Final privacy scan across public directory
    scan_for_truth_leakage(out_path, raise_on_leak=True)

    return {
        "out_dir": str(out_path),
        "case_id": case_id,
        "seed": int(seed),
        "revision_old": rev_old,
        "revision_current": rev_curr,
        "manifest_path": str(manifest_path),
        "public_state_path": str(public_state_path),
        "scene_a_path": str(scene_a_path),
        "truth_path": str(truth_path),
        "old_sld_path": str(old_sld_path),
        "current_sld_path": str(current_sld_path),
        "thermal_path": str(thermal_path),
        "rgb_path": str(rgb_path),
        "manifest": manifest_payload,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="BlackboxDC Twin Repair challenge fixture builder")
    sub = parser.add_subparsers(dest="command", required=True)

    build_p = sub.add_parser("build", help="Build a deterministic Twin Repair challenge fixture")
    build_p.add_argument("--seed", type=int, default=DEFAULT_CHALLENGE_SEED, help="Deterministic integer seed")
    build_p.add_argument("--out", type=Path, required=True, help="Output directory for generated fixture")
    build_p.add_argument(
        "--case-type",
        type=str,
        default="drift_A_to_B",
        choices=("drift_A_to_B", "control_A_to_A"),
        help="Challenge case variant",
    )

    args = parser.parse_args(argv)
    if args.command == "build":
        ref = reference_case()
        res = build_challenge_fixture(args.out, seed=args.seed, case_type=args.case_type)
        summary = {
            "status": "ok",
            "case_id": res["case_id"],
            "out_dir": res["out_dir"],
            "oracle_verified": ref["passed"],
            "public_assets_sha256": res["manifest"]["public_assets_sha256"],
            "truth_sha256": res["manifest"]["truth_sha256"],
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
