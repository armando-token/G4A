"""Streamlit web application with offline replay and 6-panel visual causal audit for BlackboxDC."""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw

from blackbox.campaign import run_ab_campaign
from blackbox.evidence import (
    CaseView,
    PairView,
    TurnView,
    load_pair_view,
    read_cached_bundle_bytes,
    resolve_safe_bundle_path,
)


def load_or_generate_replay_state(artifacts_root: Path) -> Dict[str, Any]:
    """Load existing campaign replay bundle from disk or generate a fresh deterministic bundle."""
    artifacts_root = Path(artifacts_root)
    summary_file = artifacts_root / "reports" / "campaign_summary.json"
    if not summary_file.exists():
        run_ab_campaign(artifacts_root=artifacts_root)
    summary_data = json.loads(summary_file.read_text(encoding="utf-8"))

    cases: Dict[str, Dict[str, Any]] = {}
    for cid in ("case_A", "case_B"):
        case_dir = artifacts_root / cid
        cases[cid] = {
            "i1": json.loads((case_dir / "i1_contract.json").read_text(encoding="utf-8")),
            "probe": json.loads((case_dir / "probe_response.json").read_text(encoding="utf-8")),
            "i2": json.loads((case_dir / "i2_contract.json").read_text(encoding="utf-8")),
            "a1": json.loads((case_dir / "a1_contract.json").read_text(encoding="utf-8")),
            "a2": json.loads((case_dir / "a2_contract.json").read_text(encoding="utf-8")),
            "oracle": json.loads((case_dir / "oracle_grade.json").read_text(encoding="utf-8")),
            "sld_path": str(case_dir / f"sld_{cid}.png"),
        }

    return {
        "summary": summary_data,
        "cases": cases,
        "scene_3d_path": str(artifacts_root / "public" / "scene_3d_shared.png"),
        "sha256sums_path": str(artifacts_root / "reports" / "SHA256SUMS.txt"),
    }


def resolve_default_bundle_dir(
    artifacts_root: Path = Path("artifacts"),
    argv: Optional[Sequence[str]] = None,
) -> Path:
    """Resolve the default visual-v2 dossier bundle directory from env, CLI argv, or artifacts."""
    env_dir = os.environ.get("BLACKBOX_BUNDLE_DIR", "").strip()
    if env_dir:
        return Path(env_dir).resolve()

    args = list(sys.argv[1:] if argv is None else argv)
    for idx, token in enumerate(args):
        if token == "--bundle" and idx + 1 < len(args):
            return Path(args[idx + 1]).resolve()
        if token.startswith("--bundle="):
            return Path(token.split("=", 1)[1]).resolve()

    root = Path(artifacts_root)
    if not root.is_absolute():
        repo_candidate = Path(__file__).resolve().parent.parent / root
        if repo_candidate.exists():
            root = repo_candidate

    live_dir = root / "reports" / "phase5_live_pair_dossier"
    if live_dir.exists():
        return live_dir.resolve()

    mock_dir = root / "reports" / "phase5_mock_pair_dossier"
    return mock_dir.resolve()


def render_bbox_overlay_png(
    image_bytes: bytes,
    *,
    i1_bbox: Optional[Sequence[int]] = None,
    a1_bbox: Optional[Sequence[int]] = None,
    crosshair_xy: Optional[Tuple[int, int]] = None,
) -> bytes:
    """Render Investigator (I1) and Auditor (A1) bounding boxes and optional crosshair onto a PNG copy."""
    if i1_bbox is None and a1_bbox is None and crosshair_xy is None:
        return image_bytes

    with Image.open(io.BytesIO(image_bytes)) as base_img:
        canvas = base_img.convert("RGBA")

    draw = ImageDraw.Draw(canvas)
    width, height = canvas.size

    if i1_bbox is not None and len(i1_bbox) == 4:
        x1, y1, x2, y2 = [int(v) for v in i1_bbox]
        draw.rectangle([x1, y1, x2, y2], outline=(0, 220, 255, 255), width=3)
        draw.text((max(0, x1), max(0, y1 - 16)), "I1 bbox", fill=(0, 220, 255, 255))

    if a1_bbox is not None and len(a1_bbox) == 4:
        x1, y1, x2, y2 = [int(v) for v in a1_bbox]
        draw.rectangle([x1, y1, x2, y2], outline=(255, 195, 0, 255), width=3)
        draw.text((max(0, x1), min(height - 16, y2 + 4)), "A1 bbox", fill=(255, 195, 0, 255))

    if crosshair_xy is not None:
        cx, cy = int(crosshair_xy[0]), int(crosshair_xy[1])
        cx = max(0, min(width - 1, cx))
        cy = max(0, min(height - 1, cy))
        draw.line([(cx, 0), (cx, height - 1)], fill=(255, 255, 255, 180), width=1)
        draw.line([(0, cy), (width - 1, cy)], fill=(255, 255, 255, 180), width=1)
        draw.ellipse([cx - 5, cy - 5, cx + 5, cy + 5], outline=(255, 80, 80, 255), width=2)

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="PNG")
    return out.getvalue()


def render_sld_highlight_png(
    image_bytes: bytes,
    *,
    highlight_branches: bool = False,
) -> bytes:
    """Return raw sld.png bytes or draw an offline UI highlight around the N2/N3 feeder swap region."""
    if not highlight_branches:
        return image_bytes

    with Image.open(io.BytesIO(image_bytes)) as base_img:
        canvas = base_img.convert("RGBA")

    draw = ImageDraw.Draw(canvas)
    # Highlight the N2/N3 feeder junction region (x=420..800, y=185..430)
    draw.rectangle([420, 185, 800, 430], outline=(255, 140, 0, 255), width=4)
    draw.text(
        (426, 165),
        "OFFLINE UI OVERLAY: N2/N3 Feeder Junction Swap Region (Not sent to agent)",
        fill=(255, 140, 0, 255),
    )
    # Highlight F1/F2 busbar label zone
    draw.rectangle([70, 160, 410, 340], outline=(0, 190, 255, 255), width=3)
    draw.text(
        (76, 140),
        "F1 / F2 Busbar Limits",
        fill=(0, 190, 255, 255),
    )

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="PNG")
    return out.getvalue()


def _extract_case_incident_summary(case_view: CaseView) -> Dict[str, Any]:
    packet = case_view.public_packet
    plan = case_view.remote_artifacts.get("plan", {})
    proposal = case_view.remote_artifacts.get("proposal", {})
    inferred_net = plan.get("inferred_network", {})
    pdu_limits = packet.get("pdu_limits_A") or inferred_net.get("pdus") or {}
    rack_limits = packet.get("rack_limits_A") or {}
    max_temp_c = inferred_net.get("max_temp_c", packet.get("max_temp_C"))
    migration_quantum_a = packet.get("migration_quantum_A")
    allowed_receivers = list(packet.get("allowed_receivers", []))
    hotspot = proposal.get("hotspot", {})
    hotspot_asset = hotspot.get("asset_id", "")
    hotspot_surface = hotspot.get("surface", "")

    sensor_edges = proposal.get("sensor_edges", [])
    bound_sensor = ""
    for edge in sensor_edges:
        if edge.get("rack_id") == hotspot_asset or edge.get("dst") == hotspot_asset:
            bound_sensor = str(edge.get("sensor_id") or edge.get("src") or "")
            break

    telemetry = packet.get("telemetry", {})
    sensor_entry = telemetry.get(bound_sensor) if bound_sensor else None
    t0_sensor_temp_c: Optional[float] = None
    if isinstance(sensor_entry, dict):
        if sensor_entry.get("temp_t0_C") is not None:
            t0_sensor_temp_c = float(sensor_entry["temp_t0_C"])
        elif sensor_entry.get("temps_C"):
            t0_sensor_temp_c = float(sensor_entry["temps_C"][-1])
    elif isinstance(sensor_entry, list) and sensor_entry:
        t0_sensor_temp_c = float(sensor_entry[-1])

    return {
        "enabled": True,
        "rack_count": len(rack_limits) if rack_limits else len(inferred_net.get("racks", {})),
        "pdu_count": len(pdu_limits),
        "pdu_limits_a": dict(pdu_limits),
        "rack_limits_a": dict(rack_limits),
        "max_temp_c": max_temp_c,
        "migration_quantum_a": migration_quantum_a,
        "allowed_receivers": allowed_receivers,
        "hotspot_asset": hotspot_asset,
        "hotspot_surface": hotspot_surface,
        "bound_sensor": bound_sensor,
        "t0_sensor_temp_c": t0_sensor_temp_c,
        "probe_receipt": dict(case_view.probe_receipt),
        "physical_safety_certified": False,
        "advisory_only": True,
        "glossary": {
            "PDU": "Power Distribution Unit — upstream electrical busbar (F1, F2) with strict current ceiling.",
            "Rack": "Server Cabinet (N1..N4) drawing compute load from its assigned PDU feeder.",
            "SLD": "Electrical Single-Line Diagram (sld.png) specifying which PDU feeds each server rack.",
            "Lock Chain": "Four-stage SHA-256 commitment ledger (L1..L4) sealing agent outputs before grading.",
        },
    }


def _build_turn_timeline_rows(case_view: CaseView) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    inv_env_id = case_view.turns["I1"].environment_id if "I1" in case_view.turns else ""
    for turn_key in ("I1", "I2", "A1", "A2"):
        if turn_key not in case_view.turns:
            continue
        t_view: TurnView = case_view.turns[turn_key]
        rows.append(
            {
                "turn_id": t_view.turn_id,
                "role": t_view.role,
                "status": t_view.status,
                "interaction_id": t_view.interaction_id,
                "environment_id": t_view.environment_id,
                "environment_id_short": t_view.environment_id_short,
                "elapsed_s": t_view.elapsed_s,
                "lock_stage": t_view.lock_stage,
                "lock_hash": t_view.lock_hash,
                "chain_hash": t_view.chain_hash,
                "artifact_filename": t_view.artifact_filename,
                "artifact_sha256": t_view.artifact_sha256,
                "received_plan": t_view.received_plan,
                "received_probe_receipt": t_view.received_probe_receipt,
                "disjoint_from_investigator_env": (
                    t_view.environment_id != inv_env_id if turn_key in ("A1", "A2") else False
                ),
            }
        )
    return rows


def _build_case_decision_physics(case_view: CaseView) -> Dict[str, Any]:
    plan = case_view.remote_artifacts.get("plan", {})
    challenge = case_view.remote_artifacts.get("audit_challenge", {})
    inferred_net = plan.get("inferred_network", {})
    pdu_limits = inferred_net.get("pdus") or case_view.public_packet.get("pdu_limits_A") or {}
    max_temp_c = inferred_net.get("max_temp_c", case_view.public_packet.get("max_temp_C"))

    predictions_by_action = plan.get("predictions_by_action", {})
    recomputed_by_action = challenge.get("recomputed_predictions_by_action", {})
    score_summary = case_view.score.get("predictions_summary", {})

    action_rows: List[Dict[str, Any]] = []
    trajectories: Dict[str, Any] = {}

    for action_id in sorted(predictions_by_action.keys()):
        pred = predictions_by_action[action_id]
        recomp = recomputed_by_action.get(action_id, {})
        summary = score_summary.get(action_id, {})

        temps_c = pred.get("temps_c", {})
        pdu_currents = pred.get("pdu_currents_a", {})
        time_s = pred.get("time_s", [])
        safety = pred.get("safety", {})

        n4_series = temps_c.get("N4", [])
        f1_series = pdu_currents.get("F1", [])
        f2_series = pdu_currents.get("F2", [])

        t180_n4_c = float(n4_series[-1]) if n4_series else float(summary.get("n4_t180_c", 0.0))
        f1_amp = float(f1_series[-1]) if f1_series else float(summary.get("f1_current_a", 0.0))
        f2_amp = float(f2_series[-1]) if f2_series else float(summary.get("f2_current_a", 0.0))

        recomp_n4_series = recomp.get("temps_c", {}).get("N4", [])
        recomp_t180_n4_c = float(recomp_n4_series[-1]) if recomp_n4_series else t180_n4_c

        action_rows.append(
            {
                "action_id": action_id,
                "is_selected": action_id == case_view.selected_action_id,
                "n4_t180_c": t180_n4_c,
                "a2_recomputed_n4_t180_c": recomp_t180_n4_c,
                "max_temp_limit_c": max_temp_c,
                "f1_current_a": f1_amp,
                "f1_limit_a": pdu_limits.get("F1"),
                "f2_current_a": f2_amp,
                "f2_limit_a": pdu_limits.get("F2"),
                "thermal_violation": bool(safety.get("thermal_violation", summary.get("thermal_violation", False))),
                "pdu_overload": bool(safety.get("pdu_overload", summary.get("pdu_overload", False))),
                "safe": bool(safety.get("safe", summary.get("safe", False))),
                "value_sources": "I2 plan.json (verified against A2 audit_challenge.json & local oracle)",
            }
        )

        trajectories[action_id] = {
            "time_s": list(time_s),
            "temps_c": {k: list(v) for k, v in temps_c.items()},
            "pdu_currents_a": {k: list(v) for k, v in pdu_currents.items()},
        }

    proposal = case_view.remote_artifacts.get("proposal", {})
    ind_graph = case_view.remote_artifacts.get("independent_graph", {})

    return {
        "enabled": True,
        "case_id": case_view.case_id,
        "selected_action_id": case_view.selected_action_id,
        "decision": case_view.decision,
        "auditor_verdict": case_view.auditor_verdict,
        "blocking_graph_uncertainty": case_view.blocking_graph_uncertainty,
        "unknown_critical_edges_i1": list(proposal.get("unknown_critical_edges", [])),
        "unknown_critical_edges_a1": list(ind_graph.get("unknown_critical_edges", [])),
        "post_measurement_parameters": dict(plan.get("post_measurement_parameters", {})),
        "pdu_limits_a": dict(pdu_limits),
        "max_temp_c": max_temp_c,
        "candidate_action_table": action_rows,
        "trajectories": trajectories,
        "sensitivity_canary": dict(challenge.get("input_sensitivity_canary", {})),
    }


def build_six_panel_dashboard_model(
    pair_view: PairView,
    *,
    selected_case: str = "case_A",
    overlay_mode: str = "both",
    highlight_sld: bool = False,
    crosshair_xy: Optional[Tuple[int, int]] = None,
) -> Dict[str, Any]:
    """Build a pure, unit-testable dictionary representing all 6 panels of the visual causal audit."""
    if not pair_view.is_complete or pair_view.status == "incomplete":
        present_cases = list(pair_view.cases.keys())
        return {
            "banner": {
                "status": "INCOMPLETE",
                "headline": f"INCOMPLETE RUN — FAILED GATE: {pair_view.first_failed_gate or 'unknown'}",
                "verified_from_archived_run": False,
                "strict_pair_passed": False,
                "first_failed_gate": pair_view.first_failed_gate,
                "failed_attempts": list(pair_view.failed_attempts),
                "physical_safety_certified": False,
                "advisory_notice": "SYNTHETIC SIMULATION ONLY — physical_safety_certified = False — NO PHYSICAL ACTUATION",
            },
            "panel_1_incident": {"enabled": False, "present_cases": present_cases},
            "panel_2_physical_view": {"enabled": False, "present_cases": present_cases},
            "panel_3_sld_reversal": {"enabled": False, "present_cases": present_cases},
            "panel_4_agent_timeline": {"enabled": False, "present_cases": present_cases},
            "panel_5_decision_physics": {"enabled": False, "present_cases": present_cases},
            "panel_6_proof_limits": {
                "enabled": False,
                "strict_pair_passed": False,
                "first_failed_gate": pair_view.first_failed_gate,
                "failed_attempts": list(pair_view.failed_attempts),
                "physical_safety_certified": False,
            },
        }

    active_case_key = selected_case if selected_case in pair_view.cases else "case_A"
    case_a = pair_view.cases["case_A"]
    case_b = pair_view.cases["case_B"]
    active_case = pair_view.cases[active_case_key]

    # Panel 1: Incident and Stakes
    panel_1 = _extract_case_incident_summary(active_case)

    # Panel 2: Physical View
    i1_bbox = active_case.remote_artifacts["proposal"]["hotspot"]["bbox_xyxy_px"]
    a1_bbox = active_case.remote_artifacts["independent_graph"]["hotspot"]["bbox_xyxy_px"]
    draw_i1 = i1_bbox if overlay_mode in ("i1", "both") else None
    draw_a1 = a1_bbox if overlay_mode in ("a1", "both") else None

    rgb_raw = read_cached_bundle_bytes(
        str(active_case.image_paths["rgb"]),
        active_case.image_sha256["rgb"],
    )
    thermal_raw = read_cached_bundle_bytes(
        str(active_case.image_paths["thermal"]),
        active_case.image_sha256["thermal"],
    )
    rgb_rendered = render_bbox_overlay_png(
        rgb_raw,
        i1_bbox=draw_i1,
        a1_bbox=draw_a1,
        crosshair_xy=crosshair_xy,
    )
    thermal_rendered = render_bbox_overlay_png(
        thermal_raw,
        i1_bbox=draw_i1,
        a1_bbox=draw_a1,
        crosshair_xy=crosshair_xy,
    )

    panel_2 = {
        "enabled": True,
        "selected_case": active_case_key,
        "overlay_mode": overlay_mode,
        "crosshair_xy": crosshair_xy,
        "i1_bbox_xyxy_px": list(i1_bbox),
        "a1_bbox_xyxy_px": list(a1_bbox),
        "i1_bbox_iou": active_case.metrics.get("i1_bbox_iou"),
        "a1_bbox_iou": active_case.metrics.get("a1_bbox_iou"),
        "rgb_sha256": active_case.image_sha256["rgb"],
        "thermal_sha256": active_case.image_sha256["thermal"],
        "rgb_identical_across_ab": bool(pair_view.cross_case_input_invariants.get("rgb_sha256_identical")),
        "thermal_identical_across_ab": bool(pair_view.cross_case_input_invariants.get("thermal_sha256_identical")),
        "rgb_png_bytes": rgb_rendered,
        "thermal_png_bytes": thermal_rendered,
        "static_3d_preview_note": "Optional 3D preview is a static offline visualization and is never sent to the agent.",
    }

    # Panel 3: Only One Thing Changes (Counterfactual SLD Intervention)
    sld_a_raw = read_cached_bundle_bytes(
        str(case_a.image_paths["sld"]),
        case_a.image_sha256["sld"],
    )
    sld_b_raw = read_cached_bundle_bytes(
        str(case_b.image_paths["sld"]),
        case_b.image_sha256["sld"],
    )
    sld_a_rendered = render_sld_highlight_png(sld_a_raw, highlight_branches=highlight_sld)
    sld_b_rendered = render_sld_highlight_png(sld_b_raw, highlight_branches=highlight_sld)

    invariants = pair_view.cross_case_input_invariants
    evidence_manifest_rows = [
        {
            "asset": "rear_aisle_rgb.png",
            "case_A_sha256": case_a.image_sha256["rgb"],
            "case_B_sha256": case_b.image_sha256["rgb"],
            "relation": "IDENTICAL" if invariants.get("rgb_sha256_identical") else "MISMATCH",
        },
        {
            "asset": "rear_aisle_thermal.png",
            "case_A_sha256": case_a.image_sha256["thermal"],
            "case_B_sha256": case_b.image_sha256["thermal"],
            "relation": "IDENTICAL" if invariants.get("thermal_sha256_identical") else "MISMATCH",
        },
        {
            "asset": "public_packet.json (substantive)",
            "case_A_sha256": case_a.substantive_packet_sha256,
            "case_B_sha256": case_b.substantive_packet_sha256,
            "relation": "IDENTICAL" if invariants.get("substantive_packet_identical") else "MISMATCH",
        },
        {
            "asset": "sld.png (Single-Line Diagram)",
            "case_A_sha256": case_a.image_sha256["sld"],
            "case_B_sha256": case_b.image_sha256["sld"],
            "relation": "DISTINCT" if invariants.get("sld_sha256_distinct") else "IDENTICAL_ERROR",
        },
    ]

    panel_3 = {
        "enabled": True,
        "highlight_sld": highlight_sld,
        "case_A_sld_sha256": case_a.image_sha256["sld"],
        "case_B_sld_sha256": case_b.image_sha256["sld"],
        "case_A_sld_png_bytes": sld_a_rendered,
        "case_B_sld_png_bytes": sld_b_rendered,
        "case_A_power_map": dict(case_a.score.get("i1_power_map", {})),
        "case_B_power_map": dict(case_b.score.get("i1_power_map", {})),
        "evidence_manifest_rows": evidence_manifest_rows,
        "invariants_passed": bool(invariants.get("passed")),
    }

    # Panel 4: What the Agents Did
    panel_4 = {
        "enabled": True,
        "selected_case": active_case_key,
        "turns_by_case": {
            "case_A": _build_turn_timeline_rows(case_a),
            "case_B": _build_turn_timeline_rows(case_b),
        },
        "active_turns": _build_turn_timeline_rows(active_case),
        "probe_receipt": dict(active_case.probe_receipt),
        "a1_blind_isolation_verified": bool(active_case.booleans.get("auditor_independence")),
        "role_distinction": {
            "Investigator": "Managed agent (I1 -> Scoped Voltage Probe -> I2) inferring topology and RC physics.",
            "Auditor": "Fresh blind managed agent environment (A1 -> A2) extracting graph independently and challenging I2.",
            "Local Oracle": "Offline deterministic verifier grading locked artifacts against sealed physics truth.",
        },
    }

    # Panel 5: Decision and Physics
    decision_a = _build_case_decision_physics(case_a)
    decision_b = _build_case_decision_physics(case_b)
    panel_5 = {
        "enabled": True,
        "selected_case": active_case_key,
        "case_A": decision_a,
        "case_B": decision_b,
        "active": decision_a if active_case_key == "case_A" else decision_b,
        "decision_reversed_across_ab": case_a.selected_action_id != case_b.selected_action_id,
    }

    # Panel 6: Proof and Limits
    boolean_matrix_rows: List[Dict[str, Any]] = []
    for gate_name in case_a.booleans.keys():
        boolean_matrix_rows.append(
            {
                "gate": gate_name,
                "case_A": bool(case_a.booleans.get(gate_name)),
                "case_B": bool(case_b.booleans.get(gate_name)),
            }
        )

    baseline_rows: List[Dict[str, Any]] = []
    for base_id in ("no_action", "fixed_n2_policy", "no_sld_variant", "privileged_oracle"):
        if base_id in pair_view.ablations:
            b_entry = pair_view.ablations[base_id]
            baseline_rows.append(
                {
                    "baseline_id": base_id,
                    "cases_safe_count": b_entry.get("cases_safe_count"),
                    "success_rate": b_entry.get("success_rate"),
                    "is_fair_baseline": b_entry.get("is_fair_baseline"),
                    "privileged_upper_bound": b_entry.get("privileged_upper_bound"),
                    "case_A_action": b_entry.get("case_A", {}).get("selected_action_id"),
                    "case_A_safe": b_entry.get("case_A", {}).get("safe"),
                    "case_B_action": b_entry.get("case_B", {}).get("selected_action_id"),
                    "case_B_safe": b_entry.get("case_B", {}).get("safe"),
                    "description": b_entry.get("description", ""),
                }
            )

    panel_6 = {
        "enabled": True,
        "strict_pair_passed": pair_view.strict_pair_passed,
        "verified_from_archived_run": pair_view.verified_from_archived_run,
        "boolean_matrix_rows": boolean_matrix_rows,
        "token_accounting": dict(pair_view.token_accounting),
        "baseline_rows": baseline_rows,
        "metrics_by_case": {
            "case_A": dict(case_a.metrics),
            "case_B": dict(case_b.metrics),
        },
        "physical_safety_certified": False,
        "disclaimers": [
            "SYNTHETIC SIMULATION ONLY — physical_safety_certified = False.",
            "Advisory workflow only: zero physical actuation or live switchgear connection.",
            "Evaluates a controlled N=2 paired counterfactual world on a single connector-degradation fault family.",
        ],
    }

    banner_status = "VERIFIED ARCHIVED RUN" if pair_view.strict_pair_passed else "FAILED VERIFICATION"
    return {
        "banner": {
            "status": banner_status,
            "headline": (
                f"{banner_status} | campaign_run_id={pair_view.campaign_run_id} | "
                f"execution_mode={pair_view.execution_mode}"
            ),
            "verified_from_archived_run": pair_view.verified_from_archived_run,
            "strict_pair_passed": pair_view.strict_pair_passed,
            "first_failed_gate": pair_view.first_failed_gate,
            "failed_attempts": list(pair_view.failed_attempts),
            "physical_safety_certified": False,
            "advisory_notice": "SYNTHETIC SIMULATION ONLY — physical_safety_certified = False — NO PHYSICAL ACTUATION",
        },
        "panel_1_incident": panel_1,
        "panel_2_physical_view": panel_2,
        "panel_3_sld_reversal": panel_3,
        "panel_4_agent_timeline": panel_4,
        "panel_5_decision_physics": panel_5,
        "panel_6_proof_limits": panel_6,
    }


def render_visual_causal_audit_page(bundle_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Render the Six-Panel Visual Causal Audit Streamlit page from a sealed dossier."""
    import streamlit as st

    st.set_page_config(page_title="BlackboxDC — Visual Causal Audit", layout="wide")

    target_dir = Path(bundle_dir).resolve() if bundle_dir is not None else resolve_default_bundle_dir()
    pair_view = load_pair_view(target_dir, verify_integrity=True, require_live=False)

    st.title("BlackboxDC — Six-Panel Visual Causal Audit")
    st.warning(
        "**SYNTHETIC SIMULATION ONLY — `physical_safety_certified = False` — NO PHYSICAL ACTUATION.** "
        "All outputs are offline advisory simulation artifacts verified from a sealed dossier."
    )

    if not pair_view.is_complete or not pair_view.strict_pair_passed:
        model = build_six_panel_dashboard_model(pair_view)
        st.error(
            f"**{model['banner']['headline']}** — First failed gate: `{pair_view.first_failed_gate}` "
            f"(failed attempts: `{len(pair_view.failed_attempts)}`)"
        )
        if pair_view.failed_attempts:
            with st.expander("Failed Attempt Ledger", expanded=True):
                st.json(pair_view.failed_attempts)
        return {"pair_view": pair_view, "model": model}

    st.success(
        f"**VERIFIED ARCHIVED RUN** (`campaign_run_id={pair_view.campaign_run_id}`, "
        f"`execution_mode={pair_view.execution_mode}`, `strict_pair_passed={pair_view.strict_pair_passed}`)"
    )

    ctrl_col1, ctrl_col2, ctrl_col3, ctrl_col4 = st.columns(4)
    selected_case = ctrl_col1.radio(
        "Active Case Selector",
        options=["case_A", "case_B"],
        index=0,
        horizontal=True,
    )
    overlay_label = ctrl_col2.selectbox(
        "Panel 2 Bounding Box Overlay",
        options=["Both I1 + A1 Boxes", "Raw Agent Input", "I1 Box", "A1 Box"],
        index=0,
    )
    overlay_map = {
        "Both I1 + A1 Boxes": "both",
        "Raw Agent Input": "raw",
        "I1 Box": "i1",
        "A1 Box": "a1",
    }
    overlay_mode = overlay_map[overlay_label]
    highlight_sld = ctrl_col3.checkbox(
        "Panel 3 Explanatory SLD Highlight (Offline UI Only)",
        value=False,
    )
    enable_crosshair = ctrl_col4.checkbox("Enable Shared Pixel Crosshair", value=False)
    crosshair_xy: Optional[Tuple[int, int]] = None
    if enable_crosshair:
        cx_col, cy_col = st.columns(2)
        cx = cx_col.slider("Crosshair X (px)", min_value=0, max_value=1279, value=1111)
        cy = cy_col.slider("Crosshair Y (px)", min_value=0, max_value=799, value=359)
        crosshair_xy = (cx, cy)

    model = build_six_panel_dashboard_model(
        pair_view,
        selected_case=selected_case,
        overlay_mode=overlay_mode,
        highlight_sld=highlight_sld,
        crosshair_xy=crosshair_xy,
    )

    # Panel 1 — Incident and Stakes
    st.header("Panel 1 — Incident and Stakes")
    p1 = model["panel_1_incident"]
    p1_c1, p1_c2, p1_c3, p1_c4 = st.columns(4)
    p1_c1.metric("Server Racks / PDU Busbars", f"{p1['rack_count']} Racks / {p1['pdu_count']} PDUs")
    p1_c2.metric("Hotspot Rack & Sensor", f"{p1['hotspot_asset']} ({p1['bound_sensor']})")
    p1_c3.metric("t = 0 s Sensor Reading", f"{p1['t0_sensor_temp_c']:.2f} °C" if p1["t0_sensor_temp_c"] else "N/A")
    p1_c4.metric("Thermal Limit / Shift Quantum", f"{p1['max_temp_c']} °C / {p1['migration_quantum_a']} A")
    with st.expander("Inline Glossary & Advisory Workflow Explanation", expanded=False):
        for term, definition in p1["glossary"].items():
            st.markdown(f"- **{term}**: {definition}")

    # Panel 2 — Physical View
    st.header("Panel 2 — Physical View (1280×800 Native Multimodal Inputs)")
    p2 = model["panel_2_physical_view"]
    st.caption(
        f"I1 IoU: `{p2['i1_bbox_iou']}` | A1 IoU: `{p2['a1_bbox_iou']}` | "
        f"RGB SHA-256: `{p2['rgb_sha256'][:16]}...` | Thermal SHA-256: `{p2['thermal_sha256'][:16]}...` "
        "(Byte-for-byte identical across Case A and Case B)"
    )
    img_col1, img_col2 = st.columns(2)
    img_col1.image(p2["rgb_png_bytes"], caption=f"{selected_case} rear_aisle_rgb.png ({overlay_label})")
    img_col2.image(p2["thermal_png_bytes"], caption=f"{selected_case} rear_aisle_thermal.png ({overlay_label})")
    scene_3d_candidate = Path(__file__).resolve().parent.parent / "artifacts" / "public" / "scene_3d_shared.png"
    if scene_3d_candidate.exists():
        with st.expander("Optional Static 3D Preview (scene_3d_shared.png — Never Sent to Agent)"):
            st.caption(p2["static_3d_preview_note"])
            st.image(str(scene_3d_candidate), caption="Static 3D Rear-Aisle Preview (Offline UI Visualization Only)")

    # Panel 3 — Only One Thing Changes
    st.header("Panel 3 — Only One Thing Changes (Counterfactual SLD Swap)")
    p3 = model["panel_3_sld_reversal"]
    sld_col1, sld_col2 = st.columns(2)
    sld_col1.image(
        p3["case_A_sld_png_bytes"],
        caption=f"Case A sld.png (SHA-256: {p3['case_A_sld_sha256'][:16]}...) | Power Map: {p3['case_A_power_map']}",
    )
    sld_col2.image(
        p3["case_B_sld_png_bytes"],
        caption=f"Case B sld.png (SHA-256: {p3['case_B_sld_sha256'][:16]}...) | Power Map: {p3['case_B_power_map']}",
    )
    st.subheader("Controlled Evidence Manifest Table")
    st.table(p3["evidence_manifest_rows"])

    # Panel 4 — What the Agents Did
    st.header("Panel 4 — What the Agents Did (Dual-Track Managed Agents & Blind Auditor)")
    p4 = model["panel_4_agent_timeline"]
    st.table(p4["active_turns"])
    with st.expander("Copyable Full Provider Interaction IDs, Environment IDs & Probe Receipt"):
        st.json(
            {
                "selected_case": selected_case,
                "a1_blind_isolation_verified": p4["a1_blind_isolation_verified"],
                "probe_receipt": p4["probe_receipt"],
                "turns": p4["active_turns"],
            }
        )

    # Panel 5 — Decision and Physics
    st.header("Panel 5 — Decision and Physics (Counterfactual Reversal & 19-Point Trajectories)")
    p5_active = model["panel_5_decision_physics"]["active"]
    p5_a = model["panel_5_decision_physics"]["case_A"]
    p5_b = model["panel_5_decision_physics"]["case_B"]
    rev_col1, rev_col2, rev_col3 = st.columns(3)
    rev_col1.metric("Case A Selected Action", p5_a["selected_action_id"])
    rev_col2.metric("Case B Selected Action", p5_b["selected_action_id"])
    rev_col3.metric("Auditor Verdict", p5_active["auditor_verdict"])

    if p5_active["blocking_graph_uncertainty"]:
        st.warning("Blocking Graph Uncertainty Active — Safe Abstention (`NO_ACTION`) Enforced.")

    st.subheader(f"Candidate Action Comparison ({selected_case})")
    st.table(p5_active["candidate_action_table"])

    sel_act = p5_active["selected_action_id"]
    if sel_act in p5_active["trajectories"]:
        traj = p5_active["trajectories"][sel_act]
        chart_col1, chart_col2 = st.columns(2)
        with chart_col1:
            st.caption(f"Rack Temperatures (°C) over 0..180 s under `{sel_act}`")
            st.line_chart(traj["temps_c"])
        with chart_col2:
            st.caption(f"PDU Currents (A) over 0..180 s under `{sel_act}`")
            st.line_chart(traj["pdu_currents_a"])

    # Panel 6 — Proof and Limits
    st.header("Panel 6 — Proof and Limits (13-Boolean Strict Matrix, Baselines & Disclaimers)")
    p6 = model["panel_6_proof_limits"]
    st.subheader("13-Boolean Strict Acceptance Matrix")
    st.table(p6["boolean_matrix_rows"])

    st.subheader("Comparison & Ablation Baselines")
    st.table(p6["baseline_rows"])

    with st.expander("Archived JSON Artifact Inspector & Token Accounting"):
        active_case_view = pair_view.cases[selected_case]
        st.json(
            {
                "token_accounting": p6["token_accounting"],
                "metrics": p6["metrics_by_case"][selected_case],
                "proposal": active_case_view.remote_artifacts["proposal"],
                "probe_receipt": active_case_view.probe_receipt,
                "plan": active_case_view.remote_artifacts["plan"],
                "independent_graph": active_case_view.remote_artifacts["independent_graph"],
                "audit_challenge": active_case_view.remote_artifacts["audit_challenge"],
            }
        )

    for disclaimer in p6["disclaimers"]:
        st.caption(f"• {disclaimer}")

    return {"pair_view": pair_view, "model": model}


def render_streamlit_dashboard(artifacts_root: Optional[Path] = None) -> Dict[str, Any]:
    """Render the legacy Phase 00 replay state or delegate to the visual causal audit page."""
    import streamlit as st

    root = Path(artifacts_root) if artifacts_root else Path("artifacts")
    state = load_or_generate_replay_state(root)
    summary = state["summary"]

    st.set_page_config(page_title="BlackboxDC A/B Evaluation Console", layout="wide")
    st.title("BlackboxDC — Multimodal Causal & Safety Evaluation Console")
    st.caption(
        "Paired World A/B Evaluation | Investigator (I1 -> Probe -> I2) & Independent Auditor (A1 -> A2)"
    )

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Campaign Pass", str(summary["campaign_pass"]))
    col2.metric("Feeder Swap Disambiguated", str(summary["feeder_disambiguation_pass"]))
    col3.metric("Auditor Disambiguated", str(summary["auditor_disambiguation_pass"]))
    col4.metric("Locked Interactions", str(summary["total_interactions_locked"]))

    return state


if __name__ == "__main__":
    render_visual_causal_audit_page()
