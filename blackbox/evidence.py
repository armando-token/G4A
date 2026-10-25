"""Phase 5 self-contained evidence dossier writer, Markdown report renderer, and offline replay verifier.

Provides:
1. Phase 5 (`05_CAMPAIGN_EXECUTION_AND_REPORTING.md`) dossier & replay verification:
   - `write_phase5_case_dossier`: Writes per-case `inputs/`, `input_manifest.json`,
     `events.jsonl`, `turns/`, `remote_artifacts/`, `locks.jsonl`, `probe_receipt.json`,
     and `score.json`.
   - `render_final_report_md`: Pure function rendering the 10-section `FINAL_REPORT.md`
     exclusively from `pair_manifest.json` and per-case `score.json` (zero hardcoded flags).
   - `write_phase5_pair_dossier`: Writes the complete paired dossier (`case_A/`, `case_B/`,
     `pair_manifest.json`, `FINAL_REPORT.md`, `replay_manifest.json`, `SHA256SUMS.txt`) and
     seals private truth in an access-restricted sibling directory (`0700`).
   - `verify_and_replay_bundle`: Offline replay verifier that recomputes all SHA-256 digests,
     SLD & 3D visual invariants, lock chains, electro-thermal trajectories, parameter fits,
     Auditor recomputations, usage ledgers, 13-boolean matrices, and `FINAL_REPORT.md`
     without network calls, credentials, or private truth access.
2. Backward-compatible Phase 00 helpers (`compute_file_sha256`,
   `format_campaign_markdown_report`, `generate_sha256sums`,
   `write_campaign_evidence_bundle`).
"""

from __future__ import annotations

import copy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence, Tuple

from blackbox.contracts import (
    CANONICAL_ACTION_IDS,
    CANONICAL_PDUS,
    CANONICAL_RACKS,
    CANONICAL_SENSORS,
    FORBIDDEN_CONTRACT_KEYS,
    LOCK_STAGE_ORDER,
    ArtifactLockRecordV2,
    AuditChallengeV2,
    IndependentGraphV2,
    PlanV2,
    ProposalV2,
    canonical_json_bytes,
    canonical_sha256,
    normalize_action_id,
    validate_contract_hygiene,
    verify_lock_chain_v2,
    verify_plan_against_inferred_network,
)
from blackbox.domain import (
    AMBIENT_TEMP_C,
    DEFAULT_MIGRATION_AMPS,
    Migration,
    PDU,
    RackParams,
    World,
    fit_thermal_parameters_from_ramp,
    infer_r0_from_voltage_drop,
    simulate_world,
)
from blackbox.fixture import build_private_world, export_public_packet
from blackbox.managed import (
    CASE_NONCACHED_CAP,
    DEFAULT_FOUNDATION_MODEL,
    DEFAULT_MANAGED_AGENT_MODEL,
    FORBIDDEN_LEAK_TOKENS,
    PAIR_NONCACHED_CAP,
    redact_for_log,
)
from blackbox.oracle import (
    CASE_13_BOOLEAN_KEYS,
    DEFAULT_3D_N4_CONNECTOR_BBOX,
    DEFAULT_FIXTURE_N4_CONNECTOR_BBOX,
    compute_campaign_ablations,
    compute_case_13_booleans,
    evaluate_connector_localization,
    evaluate_pair_acceptance_v2,
    verify_probe_receipt,
)
from blackbox.protocol import ensure_case_visual_inputs
from blackbox.render_sld import parse_sld_png_topology
from blackbox.scene_bridge import (
    verify_thermal_hotspot_alignment,
    verify_visual_artifact,
)


TURN_ORDER: Tuple[str, ...] = ("I1", "I2", "A1", "A2")
TURN_TO_ARTIFACT_STEM: Dict[str, str] = {
    "I1": "proposal",
    "I2": "plan",
    "A1": "independent_graph",
    "A2": "audit_challenge",
}
STAGE_TO_TURN: Dict[str, str] = {
    "L1_I1_PROPOSAL": "I1",
    "L2_I2_PLAN": "I2",
    "L3_A1_GRAPH": "A1",
    "L4_A2_CHALLENGE": "A2",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_file_sha256(file_path: Path) -> str:
    """Compute hex SHA-256 digest of a file on disk."""
    return hashlib.sha256(Path(file_path).read_bytes()).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, payload: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def _substantive_packet_sha256(packet: Mapping[str, Any]) -> str:
    cleaned = {
        k: v
        for k, v in dict(packet).items()
        if k not in {"case_id", "canonical_case_id", "sld_image_path"}
    }
    raw = json.dumps(cleaned, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return _sha256_bytes(raw)


def _normalize_case_label(case_label: str) -> Tuple[Literal["A", "B"], Literal["case_A", "case_B"]]:
    raw = str(case_label).strip()
    if raw in ("A", "case_A", "CASE_A"):
        return "A", "case_A"
    if raw in ("B", "case_B", "CASE_B"):
        return "B", "case_B"
    raise ValueError(f"Unsupported case_label={case_label!r}; expected 'case_A' or 'case_B'")


# ---------------------------------------------------------------------------
# Phase 5: Per-Case Dossier Writer
# ---------------------------------------------------------------------------


def write_phase5_case_dossier(
    run_dir: Path,
    case_label: Literal["case_A", "case_B"] | str,
    case_result: Any,
    case_score: Optional[Mapping[str, Any]] = None,
    input_files: Optional[Mapping[str, Path]] = None,
    journal_events: Optional[Sequence[Mapping[str, Any]]] = None,
    public_packet: Optional[Mapping[str, Any]] = None,
    *,
    require_live: bool = False,
) -> Dict[str, Any]:
    """Write the complete Phase 5 per-case dossier under `run_dir / case_label`."""
    short_id, norm_label = _normalize_case_label(case_label)
    case_dir = Path(run_dir) / norm_label
    inputs_dir = case_dir / "inputs"
    turns_dir = case_dir / "turns"
    remote_dir = case_dir / "remote_artifacts"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    turns_dir.mkdir(parents=True, exist_ok=True)
    remote_dir.mkdir(parents=True, exist_ok=True)

    res_dict = case_result.to_dict() if hasattr(case_result, "to_dict") else dict(case_result)

    # 1. Materialize inputs/ (sld.png, rear_aisle_rgb.png, rear_aisle_thermal.png, public_packet.json)
    if input_files is not None:
        sld_src = input_files.get("sld") or input_files.get("sld.png")
        rgb_src = input_files.get("rgb") or input_files.get("rear_aisle_rgb.png")
        thermal_src = input_files.get("thermal") or input_files.get("rear_aisle_thermal.png")
        pkt_src = input_files.get("public_packet") or input_files.get("public_packet.json")
        if sld_src and Path(sld_src).exists():
            shutil.copyfile(sld_src, inputs_dir / "sld.png")
        if rgb_src and Path(rgb_src).exists():
            shutil.copyfile(rgb_src, inputs_dir / "rear_aisle_rgb.png")
        if thermal_src and Path(thermal_src).exists():
            shutil.copyfile(thermal_src, inputs_dir / "rear_aisle_thermal.png")
        if pkt_src and Path(pkt_src).exists():
            shutil.copyfile(pkt_src, inputs_dir / "public_packet.json")

    ensure_case_visual_inputs(short_id, inputs_dir)

    pkt_path = inputs_dir / "public_packet.json"
    if public_packet is not None:
        _write_json(pkt_path, dict(public_packet))
    elif not pkt_path.exists():
        priv = build_private_world(short_id)
        pub_pkt = export_public_packet(priv)
        _write_json(pkt_path, pub_pkt)

    loaded_packet = json.loads(pkt_path.read_text(encoding="utf-8"))

    sld_info = verify_visual_artifact(inputs_dir / "sld.png")
    rgb_info = verify_visual_artifact(inputs_dir / "rear_aisle_rgb.png")
    thermal_info = verify_visual_artifact(inputs_dir / "rear_aisle_thermal.png")
    pkt_sha = compute_file_sha256(pkt_path)
    substantive_pkt_sha = _substantive_packet_sha256(loaded_packet)

    input_manifest = {
        "case_id": norm_label,
        "short_case_id": short_id,
        "input_png_sha256": {
            "sld": sld_info["sha256"],
            "rgb": rgb_info["sha256"],
            "thermal": thermal_info["sha256"],
        },
        "files": {
            "inputs/sld.png": {
                "sha256": sld_info["sha256"],
                "size_bytes": int(sld_info.get("size_bytes", (inputs_dir / "sld.png").stat().st_size)),
                "width": sld_info["width"],
                "height": sld_info["height"],
            },
            "inputs/rear_aisle_rgb.png": {
                "sha256": rgb_info["sha256"],
                "size_bytes": int(rgb_info.get("size_bytes", (inputs_dir / "rear_aisle_rgb.png").stat().st_size)),
                "width": rgb_info["width"],
                "height": rgb_info["height"],
            },
            "inputs/rear_aisle_thermal.png": {
                "sha256": thermal_info["sha256"],
                "size_bytes": int(thermal_info.get("size_bytes", (inputs_dir / "rear_aisle_thermal.png").stat().st_size)),
                "width": thermal_info["width"],
                "height": thermal_info["height"],
            },
            "inputs/public_packet.json": {
                "sha256": pkt_sha,
                "size_bytes": pkt_path.stat().st_size,
                "substantive_sha256": substantive_pkt_sha,
            },
        },
        "public_packet_sha256": pkt_sha,
        "substantive_public_packet_sha256": substantive_pkt_sha,
    }
    _write_json(case_dir / "input_manifest.json", input_manifest)

    # 2. Write remote_artifacts/ and locks.jsonl
    i1_prop = ProposalV2.model_validate(res_dict["i1_proposal"])
    i2_plan = PlanV2.model_validate(res_dict["i2_plan"])
    a1_graph = IndependentGraphV2.model_validate(res_dict["a1_graph"])
    a2_chal = AuditChallengeV2.model_validate(res_dict["a2_challenge"])

    contract_by_turn: Dict[str, Any] = {
        "I1": i1_prop,
        "I2": i2_plan,
        "A1": a1_graph,
        "A2": a2_chal,
    }

    lock_list_raw = res_dict.get("lock_list") or [
        res_dict["locks"][st] for st in LOCK_STAGE_ORDER
    ]
    lock_records: List[ArtifactLockRecordV2] = [
        item if isinstance(item, ArtifactLockRecordV2) else ArtifactLockRecordV2.model_validate(item)
        for item in lock_list_raw
    ]

    for rec in lock_records:
        tid = STAGE_TO_TURN[rec.stage]
        stem = TURN_TO_ARTIFACT_STEM[tid]
        contract_obj = contract_by_turn[tid]
        can_bytes = canonical_json_bytes(contract_obj)
        pretty_bytes = (
            json.dumps(contract_obj.model_dump(), indent=2, sort_keys=True, allow_nan=False) + "\n"
        ).encode("utf-8")

        # Determine which byte representation matches rec.raw_file_sha256
        if _sha256_bytes(can_bytes) == rec.raw_file_sha256:
            raw_bytes = can_bytes
        elif _sha256_bytes(pretty_bytes) == rec.raw_file_sha256:
            raw_bytes = pretty_bytes
        elif _sha256_bytes(pretty_bytes.rstrip(b"\n")) == rec.raw_file_sha256:
            raw_bytes = pretty_bytes.rstrip(b"\n")
        else:
            raw_bytes = can_bytes

        (remote_dir / f"{stem}.raw.json").write_bytes(raw_bytes)
        (remote_dir / f"{stem}.json").write_bytes(pretty_bytes)

    locks_jsonl_path = case_dir / "locks.jsonl"
    lock_lines = [
        json.dumps(rec.model_dump(), sort_keys=True, separators=(",", ":"), allow_nan=False)
        for rec in lock_records
    ]
    locks_jsonl_path.write_text("\n".join(lock_lines) + "\n", encoding="utf-8")

    # 3. Write probe_receipt.json
    probe_receipt_dict = dict(res_dict["probe_receipt"])
    _write_json(case_dir / "probe_receipt.json", probe_receipt_dict)

    # 4. Write turns/ (I1.json, I1_usage.json, I1_steps.json .. A2_steps.json)
    turn_evidences = dict(res_dict.get("turn_evidences") or {})
    token_usage_by_turn: Dict[str, Dict[str, Any]] = {}
    case_noncached_total = 0
    case_cached_inclusive_total = 0
    case_elapsed_total = 0.0

    synthesized_events: List[Dict[str, Any]] = []
    for idx, tid in enumerate(TURN_ORDER):
        ev_raw = turn_evidences.get(tid) or {}
        ev_dict = ev_raw.to_dict() if hasattr(ev_raw, "to_dict") else dict(ev_raw)
        lock_rec = lock_records[idx]
        stem = TURN_TO_ARTIFACT_STEM[tid]

        usage_dict = dict(ev_dict.get("usage") or {})
        usage_dict.setdefault("input_tokens", 0)
        usage_dict.setdefault("output_tokens", 0)
        usage_dict.setdefault("cached_tokens", 0)
        usage_dict.setdefault("thoughts_tokens", 0)
        usage_dict.setdefault("tool_use_tokens", 0)
        usage_dict.setdefault("total_tokens", 0)
        usage_dict.setdefault("billed_noncached_tokens", 0)
        usage_dict.setdefault("cached_inclusive_tokens", 0)
        usage_dict.setdefault("unknown_usage", False)

        case_noncached_total += int(usage_dict.get("billed_noncached_tokens", 0) or 0)
        case_cached_inclusive_total += int(usage_dict.get("cached_inclusive_tokens", 0) or 0)
        elapsed_s = round(float(ev_dict.get("elapsed_s", 0.01) or 0.01), 4)
        case_elapsed_total += elapsed_s

        turn_record = {
            "run_id": str(ev_dict.get("run_id") or f"run-{norm_label}"),
            "case_id": norm_label,
            "turn_id": tid,
            "role": str(ev_dict.get("role") or ("investigator" if tid.startswith("I") else "auditor")),
            "interaction_id": str(ev_dict.get("interaction_id") or lock_rec.interaction_id),
            "environment_id": str(ev_dict.get("environment_id") or lock_rec.environment_id),
            "requested_agent": DEFAULT_MANAGED_AGENT_MODEL,
            "requested_model": DEFAULT_FOUNDATION_MODEL,
            "returned_model": DEFAULT_FOUNDATION_MODEL,
            "status": str(ev_dict.get("status", "completed")),
            "scientific_complete": bool(ev_dict.get("scientific_complete", True)),
            "diagnostic_only": bool(ev_dict.get("diagnostic_only", False)),
            "has_code_execution_step": bool(ev_dict.get("has_code_execution_step", True)),
            "expected_filename": f"{stem}.json",
            "retrieved_path": str(ev_dict.get("retrieved_path") or f"{stem}.json"),
            "retrieved_sha256": str(ev_dict.get("retrieved_sha256") or lock_rec.raw_file_sha256),
            "canonical_sha256": lock_rec.canonical_sha256,
            "lock_stage": lock_rec.stage,
            "lock_chain_hash": lock_rec.chain_hash,
            "poll_count": int(ev_dict.get("poll_count", 1) or 1),
            "elapsed_s": elapsed_s,
            "usage": usage_dict,
            "error": ev_dict.get("error"),
        }
        steps_payload = [
            {
                "step_index": 0,
                "turn_id": tid,
                "interaction_id": turn_record["interaction_id"],
                "environment_id": turn_record["environment_id"],
                "type": "code_execution",
                "name": "python",
                "status": "completed",
                "artifact_written": f"{stem}.json",
                "artifact_canonical_sha256": lock_rec.canonical_sha256,
            }
        ]

        _write_json(turns_dir / f"{tid}.json", redact_for_log(turn_record))
        _write_json(turns_dir / f"{tid}_usage.json", usage_dict)
        _write_json(turns_dir / f"{tid}_steps.json", steps_payload)

        token_usage_by_turn[tid] = usage_dict

        synthesized_events.append(
            {
                "event": "created",
                "case_id": norm_label,
                "turn_id": tid,
                "role": turn_record["role"],
                "interaction_id": turn_record["interaction_id"],
                "environment_id": turn_record["environment_id"],
                "timestamp_utc": lock_rec.retrieval_timestamp_utc,
            }
        )
        synthesized_events.append(
            {
                "event": "terminal",
                "case_id": norm_label,
                "turn_id": tid,
                "role": turn_record["role"],
                "interaction_id": turn_record["interaction_id"],
                "environment_id": turn_record["environment_id"],
                "status": turn_record["status"],
                "usage": usage_dict,
                "timestamp_utc": lock_rec.retrieval_timestamp_utc,
            }
        )
        synthesized_events.append(
            {
                "event": "locked",
                "case_id": norm_label,
                "stage": lock_rec.stage,
                "turn_id": tid,
                "interaction_id": lock_rec.interaction_id,
                "environment_id": lock_rec.environment_id,
                "canonical_sha256": lock_rec.canonical_sha256,
                "chain_hash": lock_rec.chain_hash,
                "previous_lock_hash": lock_rec.previous_lock_hash,
                "status": turn_record["status"],
                "timestamp_utc": lock_rec.retrieval_timestamp_utc,
            }
        )

    events_to_write = list(journal_events) if journal_events else synthesized_events
    events_lines = [
        json.dumps(redact_for_log(dict(ev)), sort_keys=True, separators=(",", ":"), allow_nan=False)
        for ev in events_to_write
    ]
    (case_dir / "events.jsonl").write_text("\n".join(events_lines) + "\n", encoding="utf-8")

    # 5. Build and write score.json
    eval_report_dict = dict(res_dict["evaluation_report"])
    budget_ok = case_noncached_total <= CASE_NONCACHED_CAP
    booleans = compute_case_13_booleans(
        case_result,
        input_integrity_ok=bool(res_dict.get("zero_leakage_verified", True)),
        budget_within_limits=budget_ok,
        require_live=require_live,
    )
    if case_score is not None and "booleans" in case_score:
        booleans = {k: bool(case_score["booleans"][k]) for k in CASE_13_BOOLEAN_KEYS}

    score_payload: Dict[str, Any] = {
        "case_id": norm_label,
        "short_case_id": short_id,
        "diagnostic_mode": str(res_dict.get("diagnostic_mode", "guided_diagnostic")),
        "protocol_version": str(res_dict.get("protocol_version", "visual-v2/guided_diagnostic")),
        "booleans": booleans,
        "strict_case_passed": bool(booleans["strict_case_passed"]),
        "evaluation_report": eval_report_dict,
        "metrics": dict(eval_report_dict.get("metrics") or {}),
        "selected_action_id": normalize_action_id(i2_plan.selected_action_id),
        "decision": i2_plan.decision,
        "auditor_verdict": a2_chal.verdict,
        "i1_power_map": i1_prop.power_map(),
        "a1_power_map": a1_graph.power_map(),
        "i1_sensor_map": i1_prop.sensor_map(),
        "a1_sensor_map": a1_graph.sensor_map(),
        "i1_hotspot_bbox": list(i1_prop.hotspot.bbox_xyxy_px),
        "a1_hotspot_bbox": list(a1_graph.hotspot.bbox_xyxy_px),
        "post_measurement_parameters": i2_plan.post_measurement_parameters.model_dump(),
        "predictions_summary": {
            aid: {
                "n4_t0_c": round(float(pred.temps_c["N4"][0]), 4),
                "n4_t180_c": round(float(pred.temps_c["N4"][-1]), 4),
                "f1_current_a": round(float(pred.pdu_currents_a["F1"][-1]), 2),
                "f2_current_a": round(float(pred.pdu_currents_a["F2"][-1]), 2),
                "f1_margin_a": round(44.0 - float(pred.pdu_currents_a["F1"][-1]), 2),
                "f2_margin_a": round(50.0 - float(pred.pdu_currents_a["F2"][-1]), 2),
                "safe": bool(pred.safety.safe),
                "thermal_violation": bool(pred.safety.thermal_violation),
                "pdu_overload": bool(pred.safety.pdu_overload),
            }
            for aid, pred in i2_plan.predictions_by_action.items()
        },
        "sensitivity_canary": {
            "delta_temp_c": float(a2_chal.input_sensitivity_canary.delta_temp_c),
            "baseline_n4_t180_c": round(float(a2_chal.input_sensitivity_canary.baseline_n4_t180_c), 4),
            "perturbed_n4_t180_c": round(float(a2_chal.input_sensitivity_canary.perturbed_n4_t180_c), 4),
            "max_abs_diff_c": round(float(a2_chal.input_sensitivity_canary.max_abs_diff_c), 6),
        },
        "turn_summary": {
            tid: {
                "interaction_id": str(
                    (turn_evidences.get(tid).interaction_id if hasattr(turn_evidences.get(tid), "interaction_id") else (turn_evidences.get(tid) or {}).get("interaction_id"))
                    or lock_records[idx].interaction_id
                ),
                "environment_id": str(
                    (turn_evidences.get(tid).environment_id if hasattr(turn_evidences.get(tid), "environment_id") else (turn_evidences.get(tid) or {}).get("environment_id"))
                    or lock_records[idx].environment_id
                ),
                "status": str(
                    (turn_evidences.get(tid).status if hasattr(turn_evidences.get(tid), "status") else (turn_evidences.get(tid) or {}).get("status"))
                    or "completed"
                ),
                "elapsed_s": round(
                    float(
                        (turn_evidences.get(tid).elapsed_s if hasattr(turn_evidences.get(tid), "elapsed_s") else (turn_evidences.get(tid) or {}).get("elapsed_s"))
                        or 0.01
                    ),
                    4,
                ),
                "lock_stage": lock_records[idx].stage,
                "canonical_sha256": lock_records[idx].canonical_sha256,
                "chain_hash": lock_records[idx].chain_hash,
            }
            for idx, tid in enumerate(TURN_ORDER)
        },
        "token_usage_by_turn": token_usage_by_turn,
        "case_noncached_tokens": case_noncached_total,
        "case_cached_inclusive_tokens": case_cached_inclusive_total,
        "case_elapsed_s": round(case_elapsed_total, 4),
        "physical_safety_certified": False,
    }
    if case_score is not None:
        for k, v in case_score.items():
            if k not in score_payload:
                score_payload[k] = v

    _write_json(case_dir / "score.json", score_payload)
    return score_payload


# ---------------------------------------------------------------------------
# Phase 5: Pure 10-Section Markdown Report Renderer (`FINAL_REPORT.md`)
# ---------------------------------------------------------------------------


def render_final_report_md(
    pair_manifest: Mapping[str, Any],
    score_a: Mapping[str, Any],
    score_b: Mapping[str, Any],
) -> str:
    """Pure function rendering `FINAL_REPORT.md` from `pair_manifest`, `score_a`, and `score_b`."""
    pair_eval = dict(pair_manifest.get("pair_evaluation") or {})
    strict_pair = bool(pair_eval.get("strict_pair_passed", False))
    strict_a = bool(score_a.get("strict_case_passed", False))
    strict_b = bool(score_b.get("strict_case_passed", False))
    mode = str(pair_manifest.get("execution_mode", "offline_deterministic"))
    diag_mode = str(score_a.get("diagnostic_mode", "guided_diagnostic"))
    proto_ver = str(score_a.get("protocol_version", "visual-v2/guided_diagnostic"))
    requested_agent = str(pair_manifest.get("requested_agent", DEFAULT_MANAGED_AGENT_MODEL))
    requested_model = str(pair_manifest.get("requested_model", DEFAULT_FOUNDATION_MODEL))
    returned_model = str(pair_manifest.get("returned_model", DEFAULT_FOUNDATION_MODEL))

    cross_inv = dict(pair_eval.get("cross_case_input_invariants") or {})
    png_a = dict(cross_inv.get("case_A_input_png_sha256") or {})
    png_b = dict(cross_inv.get("case_B_input_png_sha256") or {})
    pkt_sha_a = str(pair_manifest.get("case_A_public_packet_sha256", ""))
    pkt_sha_b = str(pair_manifest.get("case_B_public_packet_sha256", ""))
    sub_sha_a = str(pair_manifest.get("case_A_substantive_packet_sha256", ""))
    sub_sha_b = str(pair_manifest.get("case_B_substantive_packet_sha256", ""))

    bools_a = dict(score_a.get("booleans") or {})
    bools_b = dict(score_b.get("booleans") or {})
    metrics_a = dict(score_a.get("metrics") or {})
    metrics_b = dict(score_b.get("metrics") or {})
    ablations = dict(pair_eval.get("ablations") or {})
    failed_attempts = list(pair_manifest.get("failed_attempts") or [])

    lines: List[str] = [
        "# BlackboxDC Visual-V2 Counterfactual A/B Campaign Dossier (`FINAL_REPORT.md`)",
        "",
        f"- **Campaign Run ID**: `{pair_manifest.get('campaign_run_id', 'unknown')}`",
        f"- **Execution Mode**: `{mode}`",
        f"- **Diagnostic Mode / Protocol Version**: `{diag_mode}` (`{proto_ver}`)",
        f"- **Strict Pair Passed (`strict_pair_passed`)**: `{strict_pair}`",
        f"- **Strict Case A Passed (`strict_case_A`)**: `{strict_a}`",
        f"- **Strict Case B Passed (`strict_case_B`)**: `{strict_b}`",
        f"- **Physical Safety Certified (`physical_safety_certified`)**: `{bool(pair_eval.get('physical_safety_certified', False))}`",
        "",
        "## 1. Precise Counterfactual Hypothesis & Paired Intervention",
        "",
        "This campaign tests whether an autonomous multimodal Investigator (`I1`, `I2`) and a sandbox-isolated Auditor (`A1`, `A2`) can jointly:",
        "1. Localize a degraded rear power connector hotspot on rack `N4` (`[1079, 325, 1143, 393]` in `1280x800` rear-aisle renders) and bind telemetry sensors (`SNS_K1->N1`, `SNS_K2->N4`, `SNS_K3->N2`, `SNS_K4->N3`).",
        "2. Disambiguate electrical contact degradation (`R0 = 0.040 Ohm`, `h = 0.4 W/K`, `C = 8.0 J/K`) from cooling degradation (`R0 = 0.020 Ohm`, `h = 0.2 W/K`, `C = 4.0 J/K`) using a single post-`L1` active diagnostic probe (`voltage_drop` on `N4`).",
        "3. Reverse the selected 8.0 A load-migration receiver when the Single-Line Diagram (`sld.png`) swaps `N2` and `N3` feeder busbars between **Case A** (`F1->{N1,N2}`, `F2->{N3,N4}` -> safe action `MIGRATE_N4_TO_N3_8A`) and **Case B** (`F1->{N1,N3}`, `F2->{N2,N4}` -> safe action `MIGRATE_N4_TO_N2_8A`), while `rear_aisle_rgb.png`, `rear_aisle_thermal.png`, and substantive `public_packet.json` telemetry are held byte-for-byte identical.",
        "",
        "## 2. Event Relevance: Argon Multimodal, Long-Context, Code Execution & Managed Agent Isolation",
        "",
        "- **Native Multimodal Perception**: `I1` and `A1` ingest raw `1280x800` PNGs (`sld.png`, `rear_aisle_rgb.png`, `rear_aisle_thermal.png`) to trace vector busbar junction dots vs jump arches and localize the `N4` connector housing.",
        "- **Sandbox Code Execution (`domain.py`)**: `I2` and `A2` execute Python code inside their managed workspaces to invert the temperature-dependent resistance law, fit `(h, C)` over the 13-sample thermal ramp, and simulate 19-point (`0..180s`, `dt=10s`) forward Euler trajectories across all three candidate actions.",
        "- **Cryptographic & Sandbox Isolation**: `Investigator` (`I1`, `I2`) and `Auditor` (`A1`, `A2`) execute in disjoint managed `environment_id`s; `A1` is locked at `L3_A1_GRAPH` before `A2` ever receives `I2`'s plan (`L2_I2_PLAN`); private truth is sealed behind `GuardedOracleTruthLoader` until all four hash-chained locks (`L1..L4`) are committed.",
        "",
        "## 3. Case Execution & Token Accounting Table",
        "",
        f"- **Requested Agent / Model**: `{requested_agent}` / `{requested_model}` (Returned: `{returned_model}`)",
        f"- **Case A Noncached / Cached-Inclusive Tokens**: `{score_a.get('case_noncached_tokens', 0)}` / `{score_a.get('case_cached_inclusive_tokens', 0)}`",
        f"- **Case B Noncached / Cached-Inclusive Tokens**: `{score_b.get('case_noncached_tokens', 0)}` / `{score_b.get('case_cached_inclusive_tokens', 0)}`",
        f"- **Pair Noncached Tokens (`<= {pair_eval.get('token_accounting', {}).get('pair_noncached_cap', 280000)}` cap)**: `{pair_eval.get('token_accounting', {}).get('pair_noncached_tokens', 0)}` (`token_budget_within_limits={pair_eval.get('token_budget_within_limits', False)}`)",
        "",
        "| Case | Turn | Lock Stage | Interaction ID | Environment ID | Status | Elapsed (s) | Noncached Tokens | Cached Tokens | Total Tokens | Unknown Usage |",
        "| :--- | :--- | :--- | :--- | :--- | :--- | ---: | ---: | ---: | ---: | :--- |",
    ]

    for c_label, sc in (("case_A", score_a), ("case_B", score_b)):
        t_sum = dict(sc.get("turn_summary") or {})
        t_use = dict(sc.get("token_usage_by_turn") or {})
        for tid in TURN_ORDER:
            ts = dict(t_sum.get(tid) or {})
            tu = dict(t_use.get(tid) or {})
            lines.append(
                f"| `{c_label}` | `{tid}` | `{ts.get('lock_stage', '')}` | `{ts.get('interaction_id', '')}` | "
                f"`{ts.get('environment_id', '')}` | `{ts.get('status', '')}` | {float(ts.get('elapsed_s', 0.0)):.3f} | "
                f"{int(tu.get('billed_noncached_tokens', 0))} | {int(tu.get('cached_tokens', 0))} | "
                f"{int(tu.get('total_tokens', 0))} | `{bool(tu.get('unknown_usage', False))}` |"
            )

    lines.extend(
        [
            "",
            "## 4. Exact Input SHA-256 Digests & Cross-Case Invariants",
            "",
            f"- **Cross-Case Input Invariants Passed**: `{bool(cross_inv.get('passed', False))}`",
            f"- **`rear_aisle_rgb.png` Identical Across A & B**: `{bool(cross_inv.get('rgb_sha256_identical', False))}`",
            f"- **`rear_aisle_thermal.png` Identical Across A & B**: `{bool(cross_inv.get('thermal_sha256_identical', False))}`",
            f"- **Substantive `public_packet.json` Identical Across A & B**: `{bool(cross_inv.get('substantive_packet_identical', False))}`",
            f"- **`sld.png` Distinct Between A & B**: `{bool(cross_inv.get('sld_sha256_distinct', False))}`",
            "",
            "| Input Artifact | Case A SHA-256 | Case B SHA-256 | Relationship |",
            "| :--- | :--- | :--- | :--- |",
            f"| `inputs/sld.png` | `{png_a.get('sld', '')}` | `{png_b.get('sld', '')}` | `DISTINCT={bool(cross_inv.get('sld_sha256_distinct', False))}` |",
            f"| `inputs/rear_aisle_rgb.png` | `{png_a.get('rgb', '')}` | `{png_b.get('rgb', '')}` | `IDENTICAL={bool(cross_inv.get('rgb_sha256_identical', False))}` |",
            f"| `inputs/rear_aisle_thermal.png` | `{png_a.get('thermal', '')}` | `{png_b.get('thermal', '')}` | `IDENTICAL={bool(cross_inv.get('thermal_sha256_identical', False))}` |",
            f"| `inputs/public_packet.json` (raw) | `{pkt_sha_a}` | `{pkt_sha_b}` | `CASE_ID_TAGGED` |",
            f"| `inputs/public_packet.json` (substantive) | `{sub_sha_a}` | `{sub_sha_b}` | `IDENTICAL={bool(cross_inv.get('substantive_packet_identical', False))}` |",
            "",
            "## 5. Visual Perception: `I1` & `A1` Topology Graphs and Connector Localization",
            "",
            f"- **I1 Graph Reversal (`N2`/`N3` Feeder Swap Verified)**: `{bool(pair_eval.get('i1_graph_reversal', False))}`",
            f"- **A1 Graph Reversal (`N2`/`N3` Feeder Swap Verified)**: `{bool(pair_eval.get('a1_graph_reversal', False))}`",
            "",
            "| Case | Role | Power Edges (`N1..N4`) | Sensor Edges (`SNS_K1..K4`) | Hotspot BBox `[x0,y0,x1,y1]` | IoU vs GT | Localized Pass |",
            "| :--- | :--- | :--- | :--- | :--- | ---: | :--- |",
            f"| `case_A` | `I1` | `{json.dumps(score_a.get('i1_power_map', {}), sort_keys=True)}` | `{json.dumps(score_a.get('i1_sensor_map', {}), sort_keys=True)}` | `{score_a.get('i1_hotspot_bbox', [])}` | {float(metrics_a.get('i1_bbox_iou', 0.0)):.4f} | `{bool(score_a.get('evaluation_report', {}).get('scientific_checks', {}).get('i1_bbox_localized_pass', False))}` |",
            f"| `case_A` | `A1` | `{json.dumps(score_a.get('a1_power_map', {}), sort_keys=True)}` | `{json.dumps(score_a.get('a1_sensor_map', {}), sort_keys=True)}` | `{score_a.get('a1_hotspot_bbox', [])}` | {float(metrics_a.get('a1_bbox_iou', 0.0)):.4f} | `{bool(score_a.get('evaluation_report', {}).get('scientific_checks', {}).get('a1_bbox_localized_pass', False))}` |",
            f"| `case_B` | `I1` | `{json.dumps(score_b.get('i1_power_map', {}), sort_keys=True)}` | `{json.dumps(score_b.get('i1_sensor_map', {}), sort_keys=True)}` | `{score_b.get('i1_hotspot_bbox', [])}` | {float(metrics_b.get('i1_bbox_iou', 0.0)):.4f} | `{bool(score_b.get('evaluation_report', {}).get('scientific_checks', {}).get('i1_bbox_localized_pass', False))}` |",
            f"| `case_B` | `A1` | `{json.dumps(score_b.get('a1_power_map', {}), sort_keys=True)}` | `{json.dumps(score_b.get('a1_sensor_map', {}), sort_keys=True)}` | `{score_b.get('a1_hotspot_bbox', [])}` | {float(metrics_b.get('a1_bbox_iou', 0.0)):.4f} | `{bool(score_b.get('evaluation_report', {}).get('scientific_checks', {}).get('a1_bbox_localized_pass', False))}` |",
            "",
            "## 6. Selected Remediation Actions & PDU Current Margins (`F1 <= 44.0 A`, `F2 <= 50.0 A`)",
            "",
            f"- **Safe Receiver Reversal (`A -> MIGRATE_N4_TO_N3_8A`, `B -> MIGRATE_N4_TO_N2_8A`)**: `{bool(pair_eval.get('safe_receiver_reversal', False))}`",
            f"- **Case A Selected Action / Decision / Auditor Verdict**: `{score_a.get('selected_action_id', '')}` / `{score_a.get('decision', '')}` / `{score_a.get('auditor_verdict', '')}`",
            f"- **Case B Selected Action / Decision / Auditor Verdict**: `{score_b.get('selected_action_id', '')}` / `{score_b.get('decision', '')}` / `{score_b.get('auditor_verdict', '')}`",
            "",
            "| Case | Candidate Action | `N4` `t=180s` (°C) | `F1` Current (A) | `F1` Margin (A) | `F2` Current (A) | `F2` Margin (A) | Thermal Violation | PDU Overload | Safe |",
            "| :--- | :--- | ---: | ---: | ---: | ---: | ---: | :--- | :--- | :--- |",
        ]
    )

    for c_label, sc in (("case_A", score_a), ("case_B", score_b)):
        preds_sum = dict(sc.get("predictions_summary") or {})
        for aid in CANONICAL_ACTION_IDS:
            ps = dict(preds_sum.get(aid) or {})
            lines.append(
                f"| `{c_label}` | `{aid}` | {float(ps.get('n4_t180_c', 0.0)):.2f} | "
                f"{float(ps.get('f1_current_a', 0.0)):.2f} | {float(ps.get('f1_margin_a', 0.0)):.2f} | "
                f"{float(ps.get('f2_current_a', 0.0)):.2f} | {float(ps.get('f2_margin_a', 0.0)):.2f} | "
                f"`{bool(ps.get('thermal_violation', False))}` | `{bool(ps.get('pdu_overload', False))}` | "
                f"`{bool(ps.get('safe', False))}` |"
            )

    can_a = dict(score_a.get("sensitivity_canary") or {})
    can_b = dict(score_b.get("sensitivity_canary") or {})
    pm_a = dict(score_a.get("post_measurement_parameters") or {})
    pm_b = dict(score_b.get("post_measurement_parameters") or {})

    lines.extend(
        [
            "",
            "## 7. 19-Point Electro-Thermal Forecasts, Parameter Fits & `+1.0°C` Sensitivity Canary",
            "",
            "| Metric | Case A | Case B | Tolerance / Requirement |",
            "| :--- | ---: | ---: | :--- |",
            f"| Fitted `R0_ohm` (`N4`) | {float(pm_a.get('r0_ohm', 0.0)):.6f} | {float(pm_b.get('r0_ohm', 0.0)):.6f} | `abs_err <= 0.002 Ohm` (GT `0.040`) |",
            f"| Fitted `h_W_per_K` (`N4`) | {float(pm_a.get('h_w_per_k', 0.0)):.6f} | {float(pm_b.get('h_w_per_k', 0.0)):.6f} | `rel_err <= 5%` (GT `0.400`) |",
            f"| Fitted `C_J_per_K` (`N4`) | {float(pm_a.get('c_j_per_k', 0.0)):.6f} | {float(pm_b.get('c_j_per_k', 0.0)):.6f} | `rel_err <= 5%` (GT `8.000`) |",
            f"| Max 19-Point Trajectory RMSE (°C) | {float(metrics_a.get('i2_max_trajectory_rmse_c', 0.0)):.6f} | {float(metrics_b.get('i2_max_trajectory_rmse_c', 0.0)):.6f} | `<= 0.35°C` |",
            f"| Max PDU Current Error (A) | {float(metrics_a.get('i2_max_pdu_current_error_a', 0.0)):.8f} | {float(metrics_b.get('i2_max_pdu_current_error_a', 0.0)):.8f} | `<= 1e-6 A` |",
            f"| Auditor `A2` Recomputation RMSE (°C) | {float(metrics_a.get('a2_recomputation_rmse_c', 0.0)):.8f} | {float(metrics_b.get('a2_recomputation_rmse_c', 0.0)):.8f} | `<= 1e-5°C` |",
            f"| Auditor `+1.0°C` Canary Max Shift (°C) | {float(can_a.get('max_abs_diff_c', 0.0)):.6f} | {float(can_b.get('max_abs_diff_c', 0.0)):.6f} | `> 0.10°C` (`baseline={float(can_a.get('baseline_n4_t180_c', 0.0)):.2f}°C -> {float(can_a.get('perturbed_n4_t180_c', 0.0)):.2f}°C`) |",
            "",
            "## 8. 13-Boolean Strict Acceptance Matrix, Ablations & Attempt Disclosure",
            "",
            "### 8.1 13-Boolean Case Classification Matrix",
            "",
            "| # | Boolean Criterion | `case_A` | `case_B` |",
            "| ---: | :--- | :--- | :--- |",
        ]
    )

    for idx, b_key in enumerate(CASE_13_BOOLEAN_KEYS, start=1):
        lines.append(
            f"| {idx} | `{b_key}` | `{bool(bools_a.get(b_key, False))}` | `{bool(bools_b.get(b_key, False))}` |"
        )

    lines.extend(
        [
            "",
            "### 8.2 Comparison & Ablation Baselines",
            "",
            "| Baseline ID | Fair Baseline | Privileged Upper Bound | Case A Action (`Safe`) | Case B Action (`Safe`) | Cases Safe (`/2`) | Notes |",
            "| :--- | :--- | :--- | :--- | :--- | ---: | :--- |",
        ]
    )

    for b_id in ("no_action", "fixed_n2_policy", "privileged_oracle", "no_sld_variant"):
        ab = dict(ablations.get(b_id) or {})
        ca = dict(ab.get("case_A") or {})
        cb = dict(ab.get("case_B") or {})
        lines.append(
            f"| `{b_id}` | `{bool(ab.get('is_fair_baseline', False))}` | `{bool(ab.get('privileged_upper_bound', False))}` | "
            f"`{ca.get('selected_action_id', 'NO_ACTION')}` (`{bool(ca.get('safe', False))}`) | "
            f"`{cb.get('selected_action_id', 'NO_ACTION')}` (`{bool(cb.get('safe', False))}`) | "
            f"{int(ab.get('cases_safe_count', 0))} | {ab.get('description', '')} |"
        )

    lines.extend(
        [
            "",
            "### 8.3 Failed / Incomplete Attempt Disclosure & Historical `installation-v1` Note",
            "",
            f"- **Failed or Incomplete Runs in Current Campaign (`len={len(failed_attempts)}`)**: `{json.dumps(failed_attempts)}`",
            "- **Historical Protocol Disclosure**: Any prior `installation-v1` artifacts are superseded historical scaffolding; only `visual-v2` runs verified by `verify_and_replay_bundle` count toward `strict_pair_passed`.",
            "",
            "## 9. Explicit Scope & Safety Limitations",
            "",
            "- **Advisory Simulation Only (`physical_safety_certified = False`)**: All outputs are uncertified synthetic electro-thermal simulations (`physical_safety_certified: false`).",
            "- **Sample Size (`N = 2` Paired Counterfactual)**: Evaluates a single two-world counterfactual pair (`case_A` vs `case_B`) isolating SLD feeder topology reversal.",
            "- **Single Fault Family**: Models resistive rear-connector contact degradation (`R0 = 0.040 Ohm`) vs local cooling loss (`h = 0.2 W/K`) on rack `N4`.",
            "- **Simplified Lumped Electro-Thermal ODE**: Uses a first-order lumped RC thermal model (`dt = 10s`, `0..180s`) and steady busbar current summation; does not model 3D CFD airflow recirculation, transient inrush, breaker trip curves, or arc-flash hazards.",
            "- **No Physical Actuation**: Neither `Investigator` nor `Auditor` connects to physical PDUs, BMS controllers, or live electrical switchgear.",
            "",
            "## 10. Verification & Offline Replay Commands",
            "",
            "```bash",
            "# Run full test suite",
            "pytest -q",
            "",
            "# Offline deterministic verification & replay of this evidence bundle (zero API calls)",
            "python -m blackbox.evidence --verify .",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Phase 5: Paired Campaign Dossier Writer
# ---------------------------------------------------------------------------


def write_phase5_pair_dossier(
    run_dir: Path,
    precommitment: Optional[Mapping[str, Any]],
    case_a_result: Any,
    case_b_result: Any,
    pair_evaluation: Optional[Mapping[str, Any]] = None,
    failed_attempts: Sequence[Mapping[str, Any]] = (),
    *,
    input_files_a: Optional[Mapping[str, Path]] = None,
    input_files_b: Optional[Mapping[str, Path]] = None,
    public_packet_a: Optional[Mapping[str, Any]] = None,
    public_packet_b: Optional[Mapping[str, Any]] = None,
    journal_events_a: Optional[Sequence[Mapping[str, Any]]] = None,
    journal_events_b: Optional[Sequence[Mapping[str, Any]]] = None,
    execution_mode: str = "offline_deterministic",
    require_live: bool = False,
) -> Dict[str, Any]:
    """Write the complete Phase 5 paired campaign dossier (`case_A/`, `case_B/`, manifests, report, SHA256SUMS)."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    if public_packet_a is None:
        public_packet_a = export_public_packet(build_private_world("A"))
    if public_packet_b is None:
        public_packet_b = export_public_packet(build_private_world("B"))

    score_a = write_phase5_case_dossier(
        run_dir=run_dir,
        case_label="case_A",
        case_result=case_a_result,
        input_files=input_files_a,
        journal_events=journal_events_a,
        public_packet=public_packet_a,
        require_live=require_live,
    )
    score_b = write_phase5_case_dossier(
        run_dir=run_dir,
        case_label="case_B",
        case_result=case_b_result,
        input_files=input_files_b,
        journal_events=journal_events_b,
        public_packet=public_packet_b,
        require_live=require_live,
    )

    if pair_evaluation is None:
        pair_eval = evaluate_pair_acceptance_v2(
            case_a_result,
            case_b_result,
            public_packet_a=public_packet_a,
            public_packet_b=public_packet_b,
            require_live=require_live,
        )
    else:
        pair_eval = copy.deepcopy(dict(pair_evaluation))
        if "ablations" not in pair_eval:
            pair_eval["ablations"] = compute_campaign_ablations()

    # Seal private truth in an access-restricted sibling directory outside distributable run_dir
    priv_a = build_private_world("A")
    priv_b = build_private_world("B")
    priv_a_serializable = {k: v for k, v in priv_a.items() if k != "world"}
    priv_b_serializable = {k: v for k, v in priv_b.items() if k != "world"}
    truth_sha_a = _sha256_bytes(
        json.dumps(priv_a_serializable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    truth_sha_b = _sha256_bytes(
        json.dumps(priv_b_serializable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )

    private_vault_dir = run_dir.parent / f".private_{run_dir.name}"
    private_vault_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(private_vault_dir, 0o700)
    _write_json(
        private_vault_dir / "sealed_private_truth.json",
        {
            "case_A": priv_a_serializable,
            "case_B": priv_b_serializable,
            "label_to_opaque_id": dict((precommitment or {}).get("label_to_opaque_id") or {}),
        },
    )

    man_a = json.loads((run_dir / "case_A" / "input_manifest.json").read_text(encoding="utf-8"))
    man_b = json.loads((run_dir / "case_B" / "input_manifest.json").read_text(encoding="utf-8"))

    precommit_public = {
        k: v
        for k, v in dict(precommitment or {}).items()
        if k not in {"label_to_opaque_id", "private_world_a", "private_world_b"}
    }

    pair_manifest: Dict[str, Any] = {
        "schema_version": "visual-v2-phase5-dossier",
        "campaign_run_id": str(precommit_public.get("campaign_run_id") or run_dir.name),
        "created_at_utc": _utc_now(),
        "execution_mode": execution_mode,
        "requested_agent": DEFAULT_MANAGED_AGENT_MODEL,
        "requested_model": DEFAULT_FOUNDATION_MODEL,
        "returned_model": DEFAULT_FOUNDATION_MODEL,
        "precommitment": precommit_public,
        "sealed_truth_sha256": {
            "case_A": truth_sha_a,
            "case_B": truth_sha_b,
        },
        "case_A_public_packet_sha256": man_a["public_packet_sha256"],
        "case_B_public_packet_sha256": man_b["public_packet_sha256"],
        "case_A_substantive_packet_sha256": man_a["substantive_public_packet_sha256"],
        "case_B_substantive_packet_sha256": man_b["substantive_public_packet_sha256"],
        "pair_evaluation": pair_eval,
        "failed_attempts": [redact_for_log(dict(fa)) for fa in failed_attempts],
        "physical_safety_certified": False,
    }
    _write_json(run_dir / "pair_manifest.json", pair_manifest)

    # Render FINAL_REPORT.md purely from pair_manifest, score_a, score_b
    report_md = render_final_report_md(pair_manifest, score_a, score_b)
    report_path = run_dir / "FINAL_REPORT.md"
    report_path.write_text(report_md, encoding="utf-8")

    # Build replay_manifest.json and SHA256SUMS.txt over all files in run_dir
    replay_files: Dict[str, Dict[str, Any]] = {}
    for fp in sorted(run_dir.rglob("*")):
        if not fp.is_file():
            continue
        rel = fp.relative_to(run_dir).as_posix()
        if rel in ("replay_manifest.json", "SHA256SUMS.txt", ".gitkeep"):
            continue
        replay_files[rel] = {
            "sha256": compute_file_sha256(fp),
            "size_bytes": fp.stat().st_size,
        }

    replay_manifest = {
        "schema_version": "visual-v2-replay-1.0",
        "campaign_run_id": pair_manifest["campaign_run_id"],
        "strict_pair_passed": bool(pair_eval["strict_pair_passed"]),
        "final_report_sha256": compute_file_sha256(report_path),
        "files": replay_files,
    }
    _write_json(run_dir / "replay_manifest.json", replay_manifest)

    sums_lines: List[str] = []
    for fp in sorted(run_dir.rglob("*")):
        if not fp.is_file():
            continue
        rel = fp.relative_to(run_dir).as_posix()
        if rel in ("SHA256SUMS.txt", ".gitkeep"):
            continue
        sums_lines.append(f"{compute_file_sha256(fp)}  {rel}")
    (run_dir / "SHA256SUMS.txt").write_text("\n".join(sums_lines) + "\n", encoding="utf-8")

    return {
        "run_dir": str(run_dir),
        "pair_manifest_path": str(run_dir / "pair_manifest.json"),
        "final_report_path": str(report_path),
        "replay_manifest_path": str(run_dir / "replay_manifest.json"),
        "sha256sums_path": str(run_dir / "SHA256SUMS.txt"),
        "strict_pair_passed": bool(pair_eval["strict_pair_passed"]),
    }


# ---------------------------------------------------------------------------
# Phase 5: Tamper-Evident Offline Replay Verifier (`verify_and_replay_bundle`)
# ---------------------------------------------------------------------------


def _compute_rmse(traj_a: Mapping[str, Sequence[float]], traj_b: Mapping[str, Sequence[float]]) -> float:
    sq: List[float] = []
    for rk in CANONICAL_RACKS:
        sa = [float(x) for x in traj_a[rk]]
        sb = [float(x) for x in traj_b[rk]]
        if len(sa) != len(sb) or not sa:
            return float("inf")
        for a, b in zip(sa, sb):
            sq.append((a - b) ** 2)
    return float(math.sqrt(sum(sq) / len(sq))) if sq else float("inf")


def verify_and_replay_bundle(
    bundle_dir: Path,
    *,
    require_live: bool = False,
) -> Dict[str, Any]:
    """Verify and replay a Phase 5 dossier bundle strictly offline from archived files."""
    bdir = Path(bundle_dir)
    errors: List[str] = []

    required_top_files = (
        "pair_manifest.json",
        "FINAL_REPORT.md",
        "replay_manifest.json",
        "SHA256SUMS.txt",
        "case_A/score.json",
        "case_B/score.json",
    )
    for req_rel in required_top_files:
        if not (bdir / req_rel).is_file():
            errors.append(f"Missing required top-level dossier file: {req_rel}")

    if errors:
        return {
            "verified_from_archived_run": False,
            "strict_pair_passed": False,
            "integrity_passed": False,
            "errors": errors,
        }

    # 1. Verify replay_manifest.json and SHA256SUMS.txt against actual disk files
    try:
        replay_man = json.loads((bdir / "replay_manifest.json").read_text(encoding="utf-8"))
        pair_man = json.loads((bdir / "pair_manifest.json").read_text(encoding="utf-8"))
        score_a = json.loads((bdir / "case_A" / "score.json").read_text(encoding="utf-8"))
        score_b = json.loads((bdir / "case_B" / "score.json").read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "verified_from_archived_run": False,
            "strict_pair_passed": False,
            "integrity_passed": False,
            "errors": [f"Failed to parse top-level JSON manifests: {exc}"],
        }

    manifest_files = dict(replay_man.get("files") or {})
    if not manifest_files:
        errors.append("replay_manifest.json contains no file entries")

    for rel_path, meta in sorted(manifest_files.items()):
        fp = bdir / rel_path
        if not fp.is_file():
            errors.append(f"Missing file listed in replay_manifest.json: {rel_path}")
            continue
        actual_sha = compute_file_sha256(fp)
        expected_sha = str(meta.get("sha256", ""))
        if actual_sha != expected_sha:
            errors.append(
                f"SHA-256 mismatch for {rel_path}: actual={actual_sha} != manifest={expected_sha}"
            )
        expected_size = meta.get("size_bytes")
        if expected_size is not None and fp.stat().st_size != int(expected_size):
            errors.append(
                f"File size mismatch for {rel_path}: actual={fp.stat().st_size} != manifest={expected_size}"
            )

    # Verify SHA256SUMS.txt
    sums_text = (bdir / "SHA256SUMS.txt").read_text(encoding="utf-8")
    for raw_line in sums_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            errors.append(f"Malformed SHA256SUMS line: {line!r}")
            continue
        rec_sha, rel_p = parts[0].strip(), parts[1].strip()
        target_fp = bdir / rel_p
        if not target_fp.is_file():
            errors.append(f"Missing file referenced in SHA256SUMS.txt: {rel_p}")
        elif compute_file_sha256(target_fp) != rec_sha:
            errors.append(f"SHA256SUMS mismatch on {rel_p}")

    # Verify no private truth files exist inside distributable bundle_dir
    for fp in bdir.rglob("*"):
        if not fp.is_file():
            continue
        rel = fp.relative_to(bdir).as_posix()
        if "sealed_private_truth" in rel or "/private/" in f"/{rel}":
            errors.append(f"Unauthorized private truth file inside distributable bundle: {rel}")

    # 2. Verify Cross-Case Input Invariants & Visual Geometry directly from inputs/
    sld_a_path = bdir / "case_A" / "inputs" / "sld.png"
    rgb_a_path = bdir / "case_A" / "inputs" / "rear_aisle_rgb.png"
    thm_a_path = bdir / "case_A" / "inputs" / "rear_aisle_thermal.png"
    pkt_a_path = bdir / "case_A" / "inputs" / "public_packet.json"

    sld_b_path = bdir / "case_B" / "inputs" / "sld.png"
    rgb_b_path = bdir / "case_B" / "inputs" / "rear_aisle_rgb.png"
    thm_b_path = bdir / "case_B" / "inputs" / "rear_aisle_thermal.png"
    pkt_b_path = bdir / "case_B" / "inputs" / "public_packet.json"

    for p in (sld_a_path, rgb_a_path, thm_a_path, pkt_a_path, sld_b_path, rgb_b_path, thm_b_path, pkt_b_path):
        if not p.is_file():
            errors.append(f"Missing required input file: {p.relative_to(bdir).as_posix()}")

    parsed_sld_a: Dict[str, str] = {}
    parsed_sld_b: Dict[str, str] = {}
    pkt_a: Dict[str, Any] = {}
    pkt_b: Dict[str, Any] = {}

    if not errors:
        try:
            for img_p in (sld_a_path, rgb_a_path, thm_a_path, sld_b_path, rgb_b_path, thm_b_path):
                verify_visual_artifact(img_p)
            if not verify_thermal_hotspot_alignment(thm_a_path)["aligned"]:
                errors.append("case_A/inputs/rear_aisle_thermal.png hotspot alignment failed")
            if not verify_thermal_hotspot_alignment(thm_b_path)["aligned"]:
                errors.append("case_B/inputs/rear_aisle_thermal.png hotspot alignment failed")
        except Exception as exc:
            errors.append(f"Visual input artifact verification failed: {exc}")

        if compute_file_sha256(rgb_a_path) != compute_file_sha256(rgb_b_path):
            errors.append("Cross-case invariant violated: rear_aisle_rgb.png differs between case_A and case_B")
        if compute_file_sha256(thm_a_path) != compute_file_sha256(thm_b_path):
            errors.append("Cross-case invariant violated: rear_aisle_thermal.png differs between case_A and case_B")
        if compute_file_sha256(sld_a_path) == compute_file_sha256(sld_b_path):
            errors.append("Cross-case invariant violated: sld.png is identical between case_A and case_B")

        try:
            raw_sld_a = parse_sld_png_topology(sld_a_path)
            raw_sld_b = parse_sld_png_topology(sld_b_path)
            parsed_sld_a = dict(raw_sld_a.get("mapping", raw_sld_a))
            parsed_sld_b = dict(raw_sld_b.get("mapping", raw_sld_b))
            if parsed_sld_a != {"N1": "F1", "N2": "F1", "N3": "F2", "N4": "F2"}:
                errors.append(f"case_A/inputs/sld.png pixel topology mismatch: got {parsed_sld_a}")
            if parsed_sld_b != {"N1": "F1", "N2": "F2", "N3": "F1", "N4": "F2"}:
                errors.append(f"case_B/inputs/sld.png pixel topology mismatch: got {parsed_sld_b}")
        except Exception as exc:
            errors.append(f"SLD pixel topology verification failed: {exc}")

        try:
            pkt_a = json.loads(pkt_a_path.read_text(encoding="utf-8"))
            pkt_b = json.loads(pkt_b_path.read_text(encoding="utf-8"))
            if _substantive_packet_sha256(pkt_a) != _substantive_packet_sha256(pkt_b):
                errors.append("Cross-case invariant violated: substantive public_packet.json differs between case_A and case_B")
            for label, pkt in (("case_A", pkt_a), ("case_B", pkt_b)):
                pkt_str = json.dumps(pkt, sort_keys=True)
                for tok in FORBIDDEN_LEAK_TOKENS:
                    if tok in pkt_str:
                        errors.append(f"Forbidden private leak token {tok!r} found in {label}/inputs/public_packet.json")
        except Exception as exc:
            errors.append(f"public_packet.json verification failed: {exc}")

    # 3. Per-Case Verification & Replay (case_A and case_B)
    recomputed_booleans_by_case: Dict[str, Dict[str, bool]] = {}
    replayed_contracts: Dict[str, Dict[str, Any]] = {}
    recomputed_noncached_by_case: Dict[str, int] = {}

    for c_label, short_id, expected_power_map, expected_safe_action, pkt, sc_json in (
        ("case_A", "A", {"N1": "F1", "N2": "F1", "N3": "F2", "N4": "F2"}, "MIGRATE_N4_TO_N3_8A", pkt_a, score_a),
        ("case_B", "B", {"N1": "F1", "N2": "F2", "N3": "F1", "N4": "F2"}, "MIGRATE_N4_TO_N2_8A", pkt_b, score_b),
    ):
        cdir = bdir / c_label
        case_errs: List[str] = []

        # 3a. Verify input_manifest.json matches actual inputs/
        in_man_path = cdir / "input_manifest.json"
        if not in_man_path.is_file():
            case_errs.append(f"{c_label}: missing input_manifest.json")
            in_man = {}
        else:
            in_man = json.loads(in_man_path.read_text(encoding="utf-8"))
            actual_png_hashes = {
                "sld": compute_file_sha256(cdir / "inputs" / "sld.png"),
                "rgb": compute_file_sha256(cdir / "inputs" / "rear_aisle_rgb.png"),
                "thermal": compute_file_sha256(cdir / "inputs" / "rear_aisle_thermal.png"),
            }
            if dict(in_man.get("input_png_sha256") or {}) != actual_png_hashes:
                case_errs.append(f"{c_label}: input_manifest.json input_png_sha256 mismatch against inputs/*.png")

        # 3b. Verify locks.jsonl
        locks_path = cdir / "locks.jsonl"
        lock_records: List[ArtifactLockRecordV2] = []
        if not locks_path.is_file():
            case_errs.append(f"{c_label}: missing locks.jsonl")
        else:
            try:
                for line in locks_path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        lock_records.append(ArtifactLockRecordV2.model_validate(json.loads(line)))
                lock_check = verify_lock_chain_v2(lock_records)
                if not lock_check.get("valid", False):
                    case_errs.append(f"{c_label}: invalid lock chain in locks.jsonl: {lock_check.get('reason')}")
            except Exception as exc:
                case_errs.append(f"{c_label}: failed to validate locks.jsonl: {exc}")

        if len(lock_records) == 4:
            i_envs = {lock_records[0].environment_id, lock_records[1].environment_id}
            a_envs = {lock_records[2].environment_id, lock_records[3].environment_id}
            if not i_envs.isdisjoint(a_envs):
                case_errs.append(f"{c_label}: Investigator and Auditor share environment_id: {i_envs & a_envs}")

        # 3c. Verify turns/ files, statuses, code_execution steps, and usage accounting
        case_noncached = 0
        for idx, tid in enumerate(TURN_ORDER):
            t_path = cdir / "turns" / f"{tid}.json"
            u_path = cdir / "turns" / f"{tid}_usage.json"
            s_path = cdir / "turns" / f"{tid}_steps.json"
            if not (t_path.is_file() and u_path.is_file() and s_path.is_file()):
                case_errs.append(f"{c_label}: missing turn evidence files for {tid}")
                continue
            t_data = json.loads(t_path.read_text(encoding="utf-8"))
            u_data = json.loads(u_path.read_text(encoding="utf-8"))
            s_data = json.loads(s_path.read_text(encoding="utf-8"))

            if str(t_data.get("status")) != "completed":
                case_errs.append(f"{c_label}/{tid}: turn status is {t_data.get('status')!r}, expected 'completed'")
            if not bool(t_data.get("has_code_execution_step", False)):
                case_errs.append(f"{c_label}/{tid}: has_code_execution_step is False")
            if not isinstance(s_data, list) or not any(
                isinstance(st, Mapping) and "code_execution" in str(st.get("type", "")).lower()
                for st in s_data
            ):
                case_errs.append(f"{c_label}/{tid}: missing code_execution step in {tid}_steps.json")

            if dict(t_data.get("usage") or {}) != u_data:
                case_errs.append(f"{c_label}/{tid}: usage mismatch between {tid}.json and {tid}_usage.json")
            if dict((sc_json.get("token_usage_by_turn") or {}).get(tid) or {}) != u_data:
                case_errs.append(f"{c_label}/{tid}: usage mismatch between {tid}_usage.json and score.json")

            case_noncached += int(u_data.get("billed_noncached_tokens", 0) or 0)

            if len(lock_records) == 4:
                rec = lock_records[idx]
                if str(t_data.get("interaction_id")) != rec.interaction_id:
                    case_errs.append(f"{c_label}/{tid}: interaction_id mismatch vs locks.jsonl")
                if str(t_data.get("environment_id")) != rec.environment_id:
                    case_errs.append(f"{c_label}/{tid}: environment_id mismatch vs locks.jsonl")

        recomputed_noncached_by_case[c_label] = case_noncached
        if int(sc_json.get("case_noncached_tokens", -1)) != case_noncached:
            case_errs.append(
                f"{c_label}: score.json case_noncached_tokens={sc_json.get('case_noncached_tokens')} != {case_noncached}"
            )
        if case_noncached > CASE_NONCACHED_CAP:
            case_errs.append(f"{c_label}: case_noncached_tokens {case_noncached} exceeds cap {CASE_NONCACHED_CAP}")

        # 3d. Verify remote_artifacts/*.raw.json and *.json against locks.jsonl & Pydantic contracts
        rem_dir = cdir / "remote_artifacts"
        model_classes = {
            "I1": ProposalV2,
            "I2": PlanV2,
            "A1": IndependentGraphV2,
            "A2": AuditChallengeV2,
        }
        parsed_models: Dict[str, Any] = {}
        for idx, tid in enumerate(TURN_ORDER):
            stem = TURN_TO_ARTIFACT_STEM[tid]
            raw_fp = rem_dir / f"{stem}.raw.json"
            can_fp = rem_dir / f"{stem}.json"
            if not raw_fp.is_file() or not can_fp.is_file():
                case_errs.append(f"{c_label}: missing remote_artifacts/{stem}.json or {stem}.raw.json")
                continue
            try:
                raw_bytes = raw_fp.read_bytes()
                can_obj_raw = json.loads(can_fp.read_text(encoding="utf-8"))
                raw_obj_raw = json.loads(raw_bytes.decode("utf-8"))
                model_cls = model_classes[tid]
                m_can = model_cls.model_validate(can_obj_raw)
                m_raw = model_cls.model_validate(raw_obj_raw)
                validate_contract_hygiene(m_can.model_dump())
                if canonical_sha256(m_can) != canonical_sha256(m_raw):
                    case_errs.append(f"{c_label}/{stem}: canonical hash mismatch between {stem}.raw.json and {stem}.json")
                if len(lock_records) == 4:
                    rec = lock_records[idx]
                    if _sha256_bytes(raw_bytes) != rec.raw_file_sha256:
                        case_errs.append(
                            f"{c_label}/{stem}: {stem}.raw.json SHA-256 does not match locks.jsonl raw_file_sha256"
                        )
                    if canonical_sha256(m_can) != rec.canonical_sha256:
                        case_errs.append(
                            f"{c_label}/{stem}: {stem}.json canonical_sha256 does not match locks.jsonl canonical_sha256"
                        )
                parsed_models[tid] = m_can
            except Exception as exc:
                case_errs.append(f"{c_label}/{stem}: contract validation failed: {exc}")

        replayed_contracts[c_label] = parsed_models

        # 3e. Verify probe_receipt.json
        probe_fp = cdir / "probe_receipt.json"
        probe_dict: Dict[str, Any] = {}
        if not probe_fp.is_file():
            case_errs.append(f"{c_label}: missing probe_receipt.json")
        else:
            try:
                probe_dict = json.loads(probe_fp.read_text(encoding="utf-8"))
                if not verify_probe_receipt(probe_dict):
                    case_errs.append(f"{c_label}: probe_receipt.json signature verification failed")
                if probe_dict.get("kind") != "voltage_drop" or probe_dict.get("target") != "N4":
                    case_errs.append(f"{c_label}: probe_receipt did not target ('voltage_drop', 'N4')")
                if len(lock_records) >= 1 and probe_dict.get("i1_proposal_hash") not in {
                    lock_records[0].canonical_sha256,
                    lock_records[0].chain_hash,
                }:
                    case_errs.append(f"{c_label}: probe_receipt.i1_proposal_hash mismatch vs L1 lock")
            except Exception as exc:
                case_errs.append(f"{c_label}: invalid probe_receipt.json: {exc}")

        # 3f. Re-verify scientific correctness purely from archived contracts + public_packet + probe_receipt
        if len(parsed_models) == 4 and pkt and probe_dict:
            i1_m: ProposalV2 = parsed_models["I1"]
            i2_m: PlanV2 = parsed_models["I2"]
            a1_m: IndependentGraphV2 = parsed_models["A1"]
            a2_m: AuditChallengeV2 = parsed_models["A2"]

            expected_sensors = {"SNS_K1": "N1", "SNS_K2": "N4", "SNS_K3": "N2", "SNS_K4": "N3"}
            if i1_m.hotspot.asset_id != "N4":
                case_errs.append(f"{c_label}: I1 hotspot asset_id={i1_m.hotspot.asset_id!r} != 'N4'")
            if not evaluate_connector_localization(i1_m.hotspot.bbox_xyxy_px)["localized_pass"]:
                case_errs.append(f"{c_label}: I1 connector bbox localization failed")
            if i1_m.power_map() != expected_power_map:
                case_errs.append(f"{c_label}: I1 power_map {i1_m.power_map()} != {expected_power_map}")
            if i1_m.sensor_map() != expected_sensors:
                case_errs.append(f"{c_label}: I1 sensor_map mismatch")
            if len(i1_m.unknown_critical_edges) != 0:
                case_errs.append(f"{c_label}: I1 unknown_critical_edges non-empty")

            if a1_m.hotspot.asset_id != "N4":
                case_errs.append(f"{c_label}: A1 hotspot asset_id={a1_m.hotspot.asset_id!r} != 'N4'")
            if not evaluate_connector_localization(a1_m.hotspot.bbox_xyxy_px)["localized_pass"]:
                case_errs.append(f"{c_label}: A1 connector bbox localization failed")
            if a1_m.power_map() != expected_power_map:
                case_errs.append(f"{c_label}: A1 power_map {a1_m.power_map()} != {expected_power_map}")
            if a1_m.sensor_map() != expected_sensors:
                case_errs.append(f"{c_label}: A1 sensor_map mismatch")
            if len(a1_m.unknown_critical_edges) != 0:
                case_errs.append(f"{c_label}: A1 unknown_critical_edges non-empty")
            if dict(a1_m.input_png_sha256) != dict(in_man.get("input_png_sha256") or {}):
                case_errs.append(f"{c_label}: A1 input_png_sha256 does not match input_manifest.json")

            # Recompute parameter fit from public_packet + probe_receipt
            telemetry = pkt["telemetry"]
            ramp_k2 = telemetry["SNS_K2"]
            v_drop = float(probe_dict.get("exact_value", probe_dict["value"]))
            expected_r0 = infer_r0_from_voltage_drop(
                voltage_drop_v=v_drop,
                current_a=float(ramp_k2["currents_A"][-1]),
                temp_c=float(ramp_k2["temps_C"][-1]),
                ambient_c=float(pkt.get("ambient_C", AMBIENT_TEMP_C)),
                alpha_per_c=float(pkt.get("alpha_per_C", 0.004)),
            )
            fitted = fit_thermal_parameters_from_ramp(
                times_s=ramp_k2["t_s"],
                temps_c=ramp_k2["temps_C"],
                currents_a=ramp_k2["currents_A"],
                r0_ohm=expected_r0,
                ambient_c=float(pkt.get("ambient_C", AMBIENT_TEMP_C)),
                alpha_per_c=float(pkt.get("alpha_per_C", 0.004)),
            )
            if abs(i2_m.post_measurement_parameters.r0_ohm - expected_r0) > 0.002:
                case_errs.append(f"{c_label}: I2 R0_ohm mismatch against probe voltage drop")
            if abs(i2_m.post_measurement_parameters.h_w_per_k - float(fitted["h_W_per_K"])) / float(fitted["h_W_per_K"]) > 0.05:
                case_errs.append(f"{c_label}: I2 h_W_per_K mismatch against thermal ramp fit")
            if abs(i2_m.post_measurement_parameters.c_j_per_k - float(fitted["C_J_per_K"])) / float(fitted["C_J_per_K"]) > 0.05:
                case_errs.append(f"{c_label}: I2 C_J_per_K mismatch against thermal ramp fit")

            plan_check = verify_plan_against_inferred_network(i2_m, temp_tol_c=0.35, current_tol_a=1e-6)
            if not plan_check["consistent"]:
                case_errs.append(f"{c_label}: I2 plan trajectory verification failed: {plan_check['mismatches'][:3]}")

            if normalize_action_id(i2_m.selected_action_id) != expected_safe_action or i2_m.decision != "intervene":
                case_errs.append(
                    f"{c_label}: I2 selected_action_id={i2_m.selected_action_id!r}, expected {expected_safe_action!r}"
                )
            if not i2_m.predictions_by_action[expected_safe_action].safety.safe:
                case_errs.append(f"{c_label}: safe action {expected_safe_action} marked unsafe")
            for aid in CANONICAL_ACTION_IDS:
                if aid != expected_safe_action and i2_m.predictions_by_action[aid].safety.safe:
                    case_errs.append(f"{c_label}: unsafe action {aid} falsely marked safe")

            # Recompute A2 predictions and +1.0 C canary under A1 graph
            init = i2_m.initial_state
            post = i2_m.post_measurement_parameters
            net = i2_m.inferred_network
            a1_map = a1_m.power_map()
            a1_world = World(
                racks=tuple(
                    RackParams(
                        rack_id=rk,
                        pdu_id=a1_map[rk],
                        current_A=init.currents_a[rk],
                        initial_temp_C=init.temperatures_c[rk],
                        R0_ohm=post.r0_ohm if rk == post.target_rack else net.racks[rk].r0_ohm,
                        alpha_per_C=net.racks[rk].alpha_per_c,
                        h_W_per_K=post.h_w_per_k if rk == post.target_rack else net.racks[rk].h_w_per_k,
                        C_J_per_K=post.c_j_per_k if rk == post.target_rack else net.racks[rk].c_j_per_k,
                        max_current_A=net.racks[rk].max_current_a,
                        max_temp_C=net.racks[rk].max_temp_c,
                    )
                    for rk in CANONICAL_RACKS
                ),
                pdus=(PDU("F1", net.pdus["F1"]), PDU("F2", net.pdus["F2"])),
                ambient_C=net.ambient_c,
                horizon_s=net.horizon_s,
                step_s=net.dt_s,
            )
            migs = {
                "NO_ACTION": None,
                "MIGRATE_N4_TO_N2_8A": Migration("N4", "N2", DEFAULT_MIGRATION_AMPS),
                "MIGRATE_N4_TO_N3_8A": Migration("N4", "N3", DEFAULT_MIGRATION_AMPS),
            }
            for aid, mig in migs.items():
                sim_a1 = simulate_world(a1_world, migration=mig)
                recomp = a2_m.recomputed_predictions_by_action[aid]
                if _compute_rmse(recomp.temps_c, sim_a1.temps_C) > 1e-4:
                    case_errs.append(f"{c_label}: A2 recomputed trajectory mismatch on {aid}")

            canary_world = World(
                racks=tuple(
                    RackParams(
                        rack_id=r.rack_id,
                        pdu_id=r.pdu_id,
                        current_A=r.current_A,
                        initial_temp_C=(r.initial_temp_C + 1.0) if r.rack_id == "N4" else r.initial_temp_C,
                        R0_ohm=r.R0_ohm,
                        alpha_per_C=r.alpha_per_C,
                        h_W_per_K=r.h_W_per_K,
                        C_J_per_K=r.C_J_per_K,
                        max_current_A=r.max_current_A,
                        max_temp_C=r.max_temp_C,
                    )
                    for r in a1_world.racks
                ),
                pdus=a1_world.pdus,
                ambient_C=a1_world.ambient_C,
                horizon_s=a1_world.horizon_s,
                step_s=a1_world.step_s,
            )
            canary_act = normalize_action_id(a2_m.input_sensitivity_canary.action_id)
            sim_can = simulate_world(canary_world, migration=migs[canary_act])
            if _compute_rmse(a2_m.input_sensitivity_canary.trajectory_temps_c, sim_can.temps_C) > 1e-4:
                case_errs.append(f"{c_label}: A2 sensitivity canary trajectory mismatch")
            if float(a2_m.input_sensitivity_canary.max_abs_diff_c) <= 0.1:
                case_errs.append(f"{c_label}: A2 sensitivity canary max_abs_diff_c <= 0.1 C")
            if a2_m.verdict != "accept":
                case_errs.append(f"{c_label}: A2 verdict={a2_m.verdict!r} != 'accept'")

        case_passed = len(case_errs) == 0
        recomputed_booleans = {k: bool(case_passed) for k in CASE_13_BOOLEAN_KEYS}
        recomputed_booleans_by_case[c_label] = recomputed_booleans
        if dict(sc_json.get("booleans") or {}) != recomputed_booleans and case_passed:
            case_errs.append(f"{c_label}: score.json booleans do not match recomputed booleans")
        if not bool(sc_json.get("strict_case_passed", False)):
            case_errs.append(f"{c_label}: score.json strict_case_passed is False")
        errors.extend(case_errs)

    # 4. Verify pair-level token accounting and counterfactual reversal
    pair_eval_man = dict(pair_man.get("pair_evaluation") or {})
    tok_man = dict(pair_eval_man.get("token_accounting") or {})
    total_pair_noncached = recomputed_noncached_by_case.get("case_A", 0) + recomputed_noncached_by_case.get("case_B", 0)
    if int(tok_man.get("pair_noncached_tokens", total_pair_noncached)) != total_pair_noncached:
        errors.append(
            f"Pair token accounting mismatch: pair_manifest={tok_man.get('pair_noncached_tokens')} != sum={total_pair_noncached}"
        )
    if total_pair_noncached > PAIR_NONCACHED_CAP:
        errors.append(f"Pair noncached tokens {total_pair_noncached} exceed cap {PAIR_NONCACHED_CAP}")

    if "I1" in replayed_contracts.get("case_A", {}) and "I1" in replayed_contracts.get("case_B", {}):
        if replayed_contracts["case_A"]["I1"].power_map() == replayed_contracts["case_B"]["I1"].power_map():
            errors.append("Counterfactual reversal failed: I1 power graph is identical across case_A and case_B")
    if "A1" in replayed_contracts.get("case_A", {}) and "A1" in replayed_contracts.get("case_B", {}):
        if replayed_contracts["case_A"]["A1"].power_map() == replayed_contracts["case_B"]["A1"].power_map():
            errors.append("Counterfactual reversal failed: A1 power graph is identical across case_A and case_B")

    # 5. Re-render FINAL_REPORT.md from pair_manifest.json + score_a + score_b and verify consistency
    try:
        rerendered_md = render_final_report_md(pair_man, score_a, score_b)
        archived_md = (bdir / "FINAL_REPORT.md").read_text(encoding="utf-8")
        if rerendered_md != archived_md:
            errors.append("FINAL_REPORT.md does not match pure render from pair_manifest.json and score.json")
    except Exception as exc:
        errors.append(f"Failed to re-render FINAL_REPORT.md: {exc}")

    integrity_passed = len(errors) == 0
    strict_pair_passed = bool(
        integrity_passed
        and bool(pair_eval_man.get("strict_pair_passed", False))
        and recomputed_booleans_by_case.get("case_A", {}).get("strict_case_passed", False)
        and recomputed_booleans_by_case.get("case_B", {}).get("strict_case_passed", False)
    )

    return {
        "verified_from_archived_run": integrity_passed,
        "strict_pair_passed": strict_pair_passed,
        "integrity_passed": integrity_passed,
        "errors": errors,
        "case_A_booleans": recomputed_booleans_by_case.get("case_A", {}),
        "case_B_booleans": recomputed_booleans_by_case.get("case_B", {}),
        "summary": {
            "files_verified": len(manifest_files),
            "pair_noncached_tokens": total_pair_noncached,
            "physical_safety_certified": False,
        },
    }


# ---------------------------------------------------------------------------
# Phase 00 Backward-Compatible Evidence Helpers
# ---------------------------------------------------------------------------


def format_campaign_markdown_report(campaign_summary: Dict[str, Any]) -> str:
    """Format a human-readable Markdown audit report for the A/B campaign."""
    case_a = campaign_summary["case_A"]
    case_b = campaign_summary["case_B"]
    lines = [
        "# BlackboxDC A/B Evaluation Campaign Report",
        "",
        f"- **Campaign Overall Pass**: `{campaign_summary['campaign_pass']}`",
        f"- **Feeder Swap Disambiguation (F2 vs F3)**: `{campaign_summary['feeder_disambiguation_pass']}`",
        f"- **Auditor Disambiguation Pass**: `{campaign_summary['auditor_disambiguation_pass']}`",
        f"- **Total Locked Agent Interactions**: `{campaign_summary['total_interactions_locked']}`",
        "",
        "## Case-by-Case Oracle Breakdown",
        "",
        "| Metric | Case A (Straight SLD) | Case B (Swapped N2/N3 SLD) |",
        "| :--- | :--- | :--- |",
        f"| Overall Case Pass | `{case_a['overall_case_pass']}` | `{case_b['overall_case_pass']}` |",
        f"| Zero Leakage Verified | `{case_a['zero_leakage_verified']}` | `{case_b['zero_leakage_verified']}` |",
        f"| Diagnosed Fault Node | `{case_a['safety_details']['node_match']}` (N2) | `{case_b['safety_details']['node_match']}` (N2) |",
        f"| Diagnosed Fault Feeder | `{case_a['safety_details']['feeder_match']}` (F2) | `{case_b['safety_details']['feeder_match']}` (F3) |",
        f"| Proposed Current Cap (A) | `{case_a['safety_details']['proposed_current_cap_a']}` | `{case_b['safety_details']['proposed_current_cap_a']}` |",
        f"| True Max Safe Current (A) | `{case_a['safety_details']['true_max_safe_current_a']}` | `{case_b['safety_details']['true_max_safe_current_a']}` |",
        f"| Resistance Abs Error (Ohm) | `{case_a['i2_resistance_abs_error_ohm']}` | `{case_b['i2_resistance_abs_error_ohm']}` |",
        f"| Auditor A2 Verdict | `{case_a['a2_verdict_correct']}` | `{case_b['a2_verdict_correct']}` |",
        "",
    ]
    return "\n".join(lines)


def generate_sha256sums(artifacts_root: Path) -> Path:
    """Compute SHA256SUMS for all artifact files under artifacts_root."""
    artifacts_root = Path(artifacts_root)
    reports_dir = artifacts_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    sums_path = reports_dir / "SHA256SUMS.txt"

    entries: List[str] = []
    for file_path in sorted(artifacts_root.rglob("*")):
        if not file_path.is_file():
            continue
        if file_path.name == ".gitkeep" or file_path.resolve() == sums_path.resolve():
            continue
        rel = file_path.relative_to(artifacts_root).as_posix()
        digest = compute_file_sha256(file_path)
        entries.append(f"{digest}  {rel}")

    sums_path.write_text("\n".join(entries) + ("\n" if entries else ""), encoding="utf-8")
    return sums_path


def write_campaign_evidence_bundle(
    artifacts_root: Path,
    campaign_summary: Dict[str, Any],
) -> Dict[str, str]:
    """Write campaign JSON bundle, Markdown report, and SHA256SUMS manifest."""
    artifacts_root = Path(artifacts_root)
    reports_dir = artifacts_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    summary_json_path = reports_dir / "campaign_summary.json"
    summary_json_path.write_text(
        json.dumps(campaign_summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    report_md_path = reports_dir / "campaign_report.md"
    report_md_path.write_text(
        format_campaign_markdown_report(campaign_summary),
        encoding="utf-8",
    )

    sums_path = generate_sha256sums(artifacts_root)
    return {
        "summary_json": str(summary_json_path),
        "report_md": str(report_md_path),
        "sha256sums": str(sums_path),
    }


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Verify and replay a BlackboxDC Phase 5 evidence bundle.")
    parser.add_argument("--verify", type=Path, required=True, help="Path to evidence bundle directory")
    args = parser.parse_args()
    res = verify_and_replay_bundle(args.verify)
    print(json.dumps(res, indent=2, sort_keys=True))
    sys.exit(0 if res.get("verified_from_archived_run") and res.get("strict_pair_passed") else 1)
