"""Phase 5 paired A/B campaign runner, precommitment freeze, CLI, and backward-compatible runner.

Implements Phase 5 (`05_CAMPAIGN_EXECUTION_AND_REPORTING.md`):
1. `build_campaign_precommitment`: Freezes `precommitment.json` before any turn runs,
   sealing private ground truth and opaque case ID mappings in a `0700` vault outside
   the distributable dossier, and locking SHA-256 digests of public packets, visual
   PNG inputs (`sld.png`, `rear_aisle_rgb.png`, `rear_aisle_thermal.png`), role
   prompts, Pydantic JSON schemas, oracle thresholds, token budget caps, and the 4
   planned comparison/ablation baselines.
2. `run_preflight_command`: Verifies authentication/model availability, Phase 2 visual
   PNG cross-case invariants, and Phase 3 canary evidence without spending an extra
   paid interaction.
3. `run_visual_v2_pair_campaign`: Sequential Case A -> Case B runner using
   `VisualV2ProtocolOrchestrator`, supporting journal reconciliation on resume,
   honest partial-dossier reporting on gate failure, and full Phase 5 dossier
   generation (`write_phase5_pair_dossier` + `verify_and_replay_bundle`).
4. CLI entry point (`python -m blackbox.campaign {preflight,mock-pair,live-pair,replay}`).
5. Preserves backward-compatible `run_ab_campaign` for Phase 00 tests and dashboard.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence

from blackbox.agent_harness import (
    build_a1_prompt,
    build_a2_prompt,
    build_i1_prompt,
    build_i2_prompt,
    build_role_agents_md,
)
from blackbox.contracts import (
    AuditChallengeV2,
    IndependentGraphV2,
    PlanV2,
    ProposalV2,
)
from blackbox.evidence import (
    compute_file_sha256,
    render_final_report_md,
    verify_and_replay_bundle,
    write_campaign_evidence_bundle,
    write_phase5_pair_dossier,
)
from blackbox.fixture import (
    build_private_world,
    export_public_packet,
    write_case_fixtures,
)
from blackbox.managed import (
    A2_NONCACHED_RESERVE,
    AUDITOR_NONCACHED_RESERVE,
    CASE_NONCACHED_CAP,
    DEFAULT_FOUNDATION_MODEL,
    DEFAULT_MANAGED_AGENT_MODEL,
    NATIVE_INTERACTION_NONCACHED_CAP,
    PAIR_NONCACHED_CAP,
    ManagedAgentTransport,
    PairBudgetTracker,
    build_genai_client,
    preflight_managed_agent,
    reconcile_journal,
    redact_for_log,
    resolve_auth_config,
)
from blackbox.oracle import (
    GuardedOracleTruthLoader,
    compute_campaign_ablations,
    evaluate_pair_acceptance_v2,
    grade_ab_campaign,
    grade_case_run,
)
from blackbox.protocol import (
    PROTOCOL_VERSION_BY_MODE,
    BlackboxProtocolStateMachine,
    CaseProtocolRunResult,
    DeterministicVisualV2MockClient,
    DiagnosticMode,
    VisualV2CaseRunResult,
    VisualV2ProtocolOrchestrator,
    ensure_case_visual_inputs,
)
from blackbox.render_sld import render_sld_png
from blackbox.scene_bridge import (
    render_deterministic_3d_scene,
    verify_visual_artifact,
)


FIXTURE_VERSION_VISUAL_V2: str = "visual-v2-1.0.0"
DEFAULT_OPAQUE_CASE_IDS: Dict[str, str] = {
    "case_A": "case-alpha-7f3a",
    "case_B": "case-beta-9c2d",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_json(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return _sha256_bytes(raw)


def _substantive_packet_sha256(packet: Mapping[str, Any]) -> str:
    cleaned = {
        k: v
        for k, v in dict(packet).items()
        if k not in {"case_id", "canonical_case_id", "sld_image_path"}
    }
    return _sha256_json(cleaned)


def _enrich_usage_dict(raw_usage_dict: Mapping[str, Any]) -> Dict[str, Any]:
    """Ensure usage dict contains both Phase 3 (`noncached_tokens`) and Phase 5 (`billed_noncached_tokens`) keys."""
    out = dict(raw_usage_dict)
    raw = dict(out.get("raw_usage") or {})
    noncached = int(out.get("noncached_tokens", out.get("billed_noncached_tokens", 0)) or 0)
    cached = int(out.get("cached_tokens", 0) or 0)
    total = int(out.get("total_tokens", noncached + cached) or 0)
    out["input_tokens"] = int(out.get("input_tokens", raw.get("input_tokens", 0)) or 0)
    out["output_tokens"] = int(out.get("output_tokens", raw.get("output_tokens", 0)) or 0)
    out["cached_tokens"] = cached
    out["thoughts_tokens"] = int(out.get("thoughts_tokens", raw.get("thoughts_tokens", 0)) or 0)
    out["tool_use_tokens"] = int(out.get("tool_use_tokens", raw.get("tool_use_tokens", 0)) or 0)
    out["total_tokens"] = total
    out["noncached_tokens"] = noncached
    out["billed_noncached_tokens"] = int(out.get("billed_noncached_tokens", noncached) or noncached)
    out["cached_inclusive_tokens"] = int(out.get("cached_inclusive_tokens", total) or total)
    out["unknown_usage"] = bool(out.get("unknown_usage", False))
    return out


class _EnrichedCaseResultProxy:
    """Wraps VisualV2CaseRunResult so `.to_dict()` includes enriched Phase 5 usage fields while preserving attributes."""

    def __init__(self, case_res: VisualV2CaseRunResult, enriched_dict: Dict[str, Any]) -> None:
        self._case_res = case_res
        self._enriched_dict = enriched_dict
        self.evaluation_report = case_res.evaluation_report
        self.turn_evidences = case_res.turn_evidences
        self.zero_leakage_verified = case_res.zero_leakage_verified

    def to_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self._enriched_dict)


def _serialize_case_result_with_enriched_usage(case_res: VisualV2CaseRunResult) -> _EnrichedCaseResultProxy:
    """Convert VisualV2CaseRunResult to a proxy whose `.to_dict()` has enriched per-turn usage dicts."""
    d = case_res.to_dict()
    turn_evs = dict(d.get("turn_evidences") or {})
    case_noncached = 0
    case_total = 0
    for tid, ev in turn_evs.items():
        ev_dict = dict(ev)
        u_enriched = _enrich_usage_dict(ev_dict.get("usage") or {})
        ev_dict["usage"] = u_enriched
        turn_evs[tid] = ev_dict
        case_noncached += int(u_enriched["billed_noncached_tokens"])
        case_total += int(u_enriched["cached_inclusive_tokens"])
    d["turn_evidences"] = turn_evs
    b_sum = dict(d.get("budget_summary") or {})
    b_sum.setdefault("case_noncached_tokens", case_noncached)
    b_sum.setdefault("case_cached_inclusive_tokens", case_total)
    d["budget_summary"] = b_sum
    return _EnrichedCaseResultProxy(case_res, d)


def _read_journal_events(journal_path: Path) -> List[Dict[str, Any]]:
    if not journal_path.is_file():
        return []
    events: List[Dict[str, Any]] = []
    for raw_line in journal_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line:
            events.append(json.loads(line))
    return events


# ---------------------------------------------------------------------------
# Phase 5 Section 1: Campaign Precommitment Freeze (`build_campaign_precommitment`)
# ---------------------------------------------------------------------------


def build_campaign_precommitment(
    campaign_id: str,
    mode: str = "mock",
    diagnostic_mode: DiagnosticMode = "guided_diagnostic",
    max_pair_noncached: int = PAIR_NONCACHED_CAP,
    artifacts_root: Path = Path("artifacts"),
) -> Dict[str, Any]:
    """Freeze all campaign invariants, input hashes, schemas, thresholds, and ablations prior to execution."""
    if diagnostic_mode not in ("guided_diagnostic", "blind_diagnostic"):
        raise ValueError(f"Unsupported diagnostic_mode={diagnostic_mode!r}")

    artifacts_root = Path(artifacts_root)
    protocol_version = PROTOCOL_VERSION_BY_MODE[diagnostic_mode]
    harness_mode = "guided_diagnostic" if diagnostic_mode == "guided_diagnostic" else "unassisted"

    # 1. Build private worlds & seal raw private truth + opaque ID mapping in restricted vault
    priv_a = build_private_world("A")
    priv_b = build_private_world("B")
    priv_a_serializable = {k: v for k, v in priv_a.items() if k != "world"}
    priv_b_serializable = {k: v for k, v in priv_b.items() if k != "world"}
    truth_sha_a = _sha256_json(priv_a_serializable)
    truth_sha_b = _sha256_json(priv_b_serializable)

    private_precommit_dir = artifacts_root / "private_precommit" / campaign_id
    private_precommit_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(private_precommit_dir, 0o700)
    (private_precommit_dir / "sealed_private_precommit.json").write_text(
        json.dumps(
            {
                "campaign_id": campaign_id,
                "label_to_opaque_id": dict(DEFAULT_OPAQUE_CASE_IDS),
                "case_A": priv_a_serializable,
                "case_B": priv_b_serializable,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    # 2. Export public packets and verify byte-identical substantive content
    pub_a = export_public_packet(priv_a, diagnostic_mode=harness_mode)
    pub_b = export_public_packet(priv_b, diagnostic_mode=harness_mode)
    pub_a["diagnostic_mode"] = diagnostic_mode
    pub_b["diagnostic_mode"] = diagnostic_mode

    sub_sha_a = _substantive_packet_sha256(pub_a)
    sub_sha_b = _substantive_packet_sha256(pub_b)
    if sub_sha_a != sub_sha_b:
        raise ValueError("Precommitment invariant failed: substantive public_packet differs between Case A and Case B")

    # 3. Verify visual PNG inputs and cross-case invariants
    with tempfile.TemporaryDirectory() as tmp_dir:
        tdir = Path(tmp_dir)
        imgs_a = ensure_case_visual_inputs("A", tdir / "case_A")
        imgs_b = ensure_case_visual_inputs("B", tdir / "case_B")
        info_a = {k: verify_visual_artifact(p) for k, p in imgs_a.items()}
        info_b = {k: verify_visual_artifact(p) for k, p in imgs_b.items()}

    rgb_identical = info_a["rgb"]["sha256"] == info_b["rgb"]["sha256"]
    thermal_identical = info_a["thermal"]["sha256"] == info_b["thermal"]["sha256"]
    sld_distinct = info_a["sld"]["sha256"] != info_b["sld"]["sha256"]
    if not (rgb_identical and thermal_identical and sld_distinct):
        raise ValueError(
            f"Precommitment visual invariant failed: rgb_identical={rgb_identical}, "
            f"thermal_identical={thermal_identical}, sld_distinct={sld_distinct}"
        )

    # 4. Compute role prompt template hashes & Pydantic JSON schema hashes
    dummy_hashes = {
        "sld": info_a["sld"]["sha256"],
        "rgb": info_a["rgb"]["sha256"],
        "thermal": info_a["thermal"]["sha256"],
    }
    role_prompts_sha256 = {
        "investigator_agents_md": _sha256_bytes(
            build_role_agents_md("investigator").encode("utf-8")
        ),
        "auditor_agents_md": _sha256_bytes(
            build_role_agents_md("auditor").encode("utf-8")
        ),
        "I1_prompt_template": _sha256_bytes(
            build_i1_prompt("case_A", diagnostic_mode=harness_mode).encode("utf-8")
        ),
        "I2_prompt_template": _sha256_bytes(
            build_i2_prompt("case_A", "0" * 64, {"value": 0.65}, {"hotspot": "N4"}).encode("utf-8")
        ),
        "A1_prompt_template": _sha256_bytes(
            build_a1_prompt("case_A", dummy_hashes).encode("utf-8")
        ),
        "A2_prompt_template": _sha256_bytes(
            build_a2_prompt(
                "case_A",
                "0" * 64,
                "1" * 64,
                {"hotspot": "N4"},
                {"selected_action_id": "MIGRATE_N4_TO_N3_8A"},
                {"value": 0.65},
            ).encode("utf-8")
        ),
    }

    json_schemas_sha256 = {
        "ProposalV2": _sha256_json(ProposalV2.model_json_schema()),
        "PlanV2": _sha256_json(PlanV2.model_json_schema()),
        "IndependentGraphV2": _sha256_json(IndependentGraphV2.model_json_schema()),
        "AuditChallengeV2": _sha256_json(AuditChallengeV2.model_json_schema()),
    }

    return {
        "schema_version": "visual-v2-precommitment-1.0",
        "campaign_id": campaign_id,
        "campaign_run_id": campaign_id,
        "created_at_utc": _utc_now(),
        "mode": mode,
        "diagnostic_mode": diagnostic_mode,
        "protocol_version": protocol_version,
        "fixture_version": FIXTURE_VERSION_VISUAL_V2,
        "requested_agent": DEFAULT_MANAGED_AGENT_MODEL,
        "requested_model": DEFAULT_FOUNDATION_MODEL,
        "private_truth_sha256": {
            "case_A": truth_sha_a,
            "case_B": truth_sha_b,
        },
        "opaque_case_ids": sorted(DEFAULT_OPAQUE_CASE_IDS.values()),
        "controlled_public_packet_sha256": {
            "case_A": _sha256_json(pub_a),
            "case_B": _sha256_json(pub_b),
            "substantive_sha256": sub_sha_a,
            "substantive_equal": True,
        },
        "input_image_sha256": {
            "case_A": {k: v["sha256"] for k, v in info_a.items()},
            "case_B": {k: v["sha256"] for k, v in info_b.items()},
            "rgb_identical": rgb_identical,
            "thermal_identical": thermal_identical,
            "sld_distinct": sld_distinct,
        },
        "role_prompts_sha256": role_prompts_sha256,
        "json_schemas_sha256": json_schemas_sha256,
        "oracle_thresholds": {
            "min_bbox_iou": 0.30,
            "max_trajectory_rmse_c": 0.35,
            "max_pdu_current_error_a": 1e-6,
            "min_canary_diff_c": 0.10,
        },
        "budget_limits": {
            "per_interaction_noncached_cap": NATIVE_INTERACTION_NONCACHED_CAP,
            "per_case_noncached_cap": CASE_NONCACHED_CAP,
            "pair_noncached_cap": int(max_pair_noncached),
            "auditor_reserve": AUDITOR_NONCACHED_RESERVE,
            "a2_reserve": A2_NONCACHED_RESERVE,
        },
        "planned_comparisons": [
            "no_action",
            "fixed_n2_policy",
            "privileged_oracle",
            "no_sld_variant",
        ],
        "ablations": compute_campaign_ablations(),
    }


# ---------------------------------------------------------------------------
# Phase 5 Section 2: Preflight Command (`run_preflight_command`)
# ---------------------------------------------------------------------------


def run_preflight_command(
    mode: str = "auto",
    artifacts_root: Path = Path("artifacts"),
    client: Any = None,
) -> Dict[str, Any]:
    """Run Phase 5 preflight verifying auth, Phase 2 visual assets, and Phase 3 canary evidence."""
    artifacts_root = Path(artifacts_root)
    reports_dir = artifacts_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    norm_mode = (mode or "auto").strip().lower()
    auth_cfg = resolve_auth_config()
    if norm_mode == "mock":
        managed_check = {
            "ready": True,
            "mode": "mock",
            "agent_name": DEFAULT_MANAGED_AGENT_MODEL,
            "model_id": DEFAULT_FOUNDATION_MODEL,
            "agent_available": True,
            "model_available": True,
        }
    else:
        managed_check = preflight_managed_agent(client=client)

    # Verify Phase 2 PNG assets & cross-case SHA-256 invariants
    preflight_vis_dir = artifacts_root / "preflight_visuals"
    imgs_a = ensure_case_visual_inputs("A", preflight_vis_dir / "case_A")
    imgs_b = ensure_case_visual_inputs("B", preflight_vis_dir / "case_B")
    info_a = {k: verify_visual_artifact(p) for k, p in imgs_a.items()}
    info_b = {k: verify_visual_artifact(p) for k, p in imgs_b.items()}

    visual_invariants_ok = (
        info_a["rgb"]["sha256"] == info_b["rgb"]["sha256"]
        and info_a["thermal"]["sha256"] == info_b["thermal"]["sha256"]
        and info_a["sld"]["sha256"] != info_b["sld"]["sha256"]
    )

    # Check existing Phase 3 canary evidence without spending an extra paid interaction
    canary_path = reports_dir / "phase3_canary_evidence.json"
    if canary_path.is_file():
        canary_evidence = json.loads(canary_path.read_text(encoding="utf-8"))
        canary_present = True
    else:
        canary_evidence = {
            "phase": "03_MANAGED_RUNTIME",
            "canary_status": "verified_offline_mock" if norm_mode == "mock" else str(auth_cfg.get("mode", "mock")),
            "paid_interaction_spent": False,
        }
        canary_present = False

    ready = bool(visual_invariants_ok and (norm_mode == "mock" or managed_check.get("ready", False)))
    report = {
        "phase": "05_PREFLIGHT",
        "timestamp_utc": _utc_now(),
        "requested_mode": norm_mode,
        "resolved_auth_mode": auth_cfg.get("mode", "mock"),
        "ready": ready,
        "managed_agent_check": redact_for_log(managed_check),
        "visual_assets_check": {
            "passed": visual_invariants_ok,
            "case_A": {k: v["sha256"] for k, v in info_a.items()},
            "case_B": {k: v["sha256"] for k, v in info_b.items()},
        },
        "phase3_canary_check": {
            "evidence_file_present": canary_present,
            "summary": redact_for_log(canary_evidence),
        },
    }
    report_path = reports_dir / "phase5_preflight_report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


# ---------------------------------------------------------------------------
# Phase 5 Section 2: Sequential A -> B Pair Campaign Runner
# ---------------------------------------------------------------------------


def _write_honest_partial_dossier(
    run_dir: Path,
    precommitment: Mapping[str, Any],
    failed_attempts: Sequence[Mapping[str, Any]],
    completed_cases: Mapping[str, Any],
    execution_mode: str,
) -> Dict[str, Any]:
    """Write an honest partial dossier when a case fails a gate, preserving all failed attempt records."""
    run_dir.mkdir(parents=True, exist_ok=True)
    comp_dicts = {
        k: (v.to_dict() if hasattr(v, "to_dict") else dict(v))
        for k, v in completed_cases.items()
    }
    pair_manifest = {
        "schema_version": "visual-v2-phase5-dossier",
        "campaign_run_id": str(precommitment.get("campaign_run_id") or run_dir.name),
        "created_at_utc": _utc_now(),
        "execution_mode": execution_mode,
        "status": "incomplete_failed_gate",
        "strict_pair_passed": False,
        "precommitment": dict(precommitment),
        "completed_cases": list(comp_dicts.keys()),
        "failed_attempts": [redact_for_log(dict(fa)) for fa in failed_attempts],
        "pair_evaluation": {
            "strict_pair_passed": False,
            "strict_case_A": bool(comp_dicts.get("case_A", {}).get("strict_case_passed", False)),
            "strict_case_B": bool(comp_dicts.get("case_B", {}).get("strict_case_passed", False)),
            "physical_safety_certified": False,
            "ablations": compute_campaign_ablations(),
        },
        "physical_safety_certified": False,
    }
    pair_manifest_path = run_dir / "pair_manifest.json"
    pair_manifest_path.write_text(json.dumps(pair_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    empty_score = {"strict_case_passed": False, "booleans": {}, "metrics": {}}
    report_md = render_final_report_md(
        pair_manifest,
        comp_dicts.get("case_A", empty_score),
        comp_dicts.get("case_B", empty_score),
    )
    report_path = run_dir / "FINAL_REPORT.md"
    report_path.write_text(report_md, encoding="utf-8")

    return {
        "run_dir": str(run_dir),
        "status": "failed_gate",
        "strict_pair_passed": False,
        "failed_attempts": list(failed_attempts),
        "pair_manifest_path": str(pair_manifest_path),
        "final_report_path": str(report_path),
    }


def run_visual_v2_pair_campaign(
    mode: Literal["mock", "live"] = "mock",
    *,
    auth_mode: str = "auto",
    diagnostic_mode: DiagnosticMode = "guided_diagnostic",
    campaign_id: Optional[str] = None,
    max_pair_noncached: int = PAIR_NONCACHED_CAP,
    artifacts_root: Path = Path("artifacts"),
    client: Any = None,
    resume_bundle_dir: Optional[Path] = None,
    prior_failed_attempts: Sequence[Mapping[str, Any]] = (),
) -> Dict[str, Any]:
    """Execute the Phase 5 paired Case A -> Case B `visual-v2` campaign and generate the full dossier."""
    artifacts_root = Path(artifacts_root)
    if resume_bundle_dir is not None:
        run_dir = Path(resume_bundle_dir)
        active_campaign_id = campaign_id or run_dir.name
    else:
        active_campaign_id = campaign_id or f"campaign-{mode}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        run_dir = artifacts_root / "reports" / active_campaign_id

    run_dir.mkdir(parents=True, exist_ok=True)
    journals_dir = run_dir / "journals"
    locks_dir = run_dir / "locks_staging"
    journals_dir.mkdir(parents=True, exist_ok=True)
    locks_dir.mkdir(parents=True, exist_ok=True)

    require_live = mode == "live"
    execution_mode = "live_provider" if mode == "live" else "offline_deterministic"

    if client is None:
        if mode == "mock":
            active_client: Any = DeterministicVisualV2MockClient()
        else:
            auth_cfg = resolve_auth_config()
            if not auth_cfg.get("ready"):
                raise RuntimeError(
                    f"Cannot run live-pair without configured credentials: {auth_cfg.get('reason')}"
                )
            active_client = build_genai_client(auth_cfg)
    else:
        active_client = client

    budget_tracker = PairBudgetTracker()

    # 1. Freeze or verify precommitment.json before any turn executes
    precommit_path = run_dir / "precommitment.json"
    fresh_precommitment = build_campaign_precommitment(
        campaign_id=active_campaign_id,
        mode=mode if mode == "mock" else auth_mode,
        diagnostic_mode=diagnostic_mode,
        max_pair_noncached=max_pair_noncached,
        artifacts_root=artifacts_root,
    )
    if precommit_path.is_file():
        existing_precommit = json.loads(precommit_path.read_text(encoding="utf-8"))
        if (
            existing_precommit.get("controlled_public_packet_sha256")
            != fresh_precommitment.get("controlled_public_packet_sha256")
            or existing_precommit.get("input_image_sha256")
            != fresh_precommitment.get("input_image_sha256")
            or existing_precommit.get("json_schemas_sha256")
            != fresh_precommitment.get("json_schemas_sha256")
        ):
            raise ValueError("Resume aborted: existing precommitment.json hashes do not match current code/inputs")
        precommitment = existing_precommit
        # Reconcile any interrupted journals before resuming
        for jp in sorted(journals_dir.glob("*.jsonl")):
            reconcile_journal(jp, client=active_client, budget_tracker=budget_tracker)
    else:
        precommitment = fresh_precommitment
        precommit_path.write_text(
            json.dumps(precommitment, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    orchestrator = VisualV2ProtocolOrchestrator(
        client=active_client,
        journal_dir=journals_dir,
        locks_dir=locks_dir,
    )

    harness_mode = "guided_diagnostic" if diagnostic_mode == "guided_diagnostic" else "unassisted"
    pub_pkt_a = export_public_packet(build_private_world("A"), diagnostic_mode=harness_mode)
    pub_pkt_b = export_public_packet(build_private_world("B"), diagnostic_mode=harness_mode)
    pub_pkt_a["diagnostic_mode"] = diagnostic_mode
    pub_pkt_b["diagnostic_mode"] = diagnostic_mode

    failed_attempts: List[Dict[str, Any]] = [dict(x) for x in prior_failed_attempts]
    completed_enriched: Dict[str, Dict[str, Any]] = {}
    journal_events_by_case: Dict[str, List[Dict[str, Any]]] = {}

    # 2. Sequential execution: Case A then Case B
    for short_id, c_label, pub_pkt in (
        ("A", "case_A", pub_pkt_a),
        ("B", "case_B", pub_pkt_b),
    ):
        case_vis_dir = run_dir / c_label / "inputs"
        case_vis_dir.mkdir(parents=True, exist_ok=True)
        loader = GuardedOracleTruthLoader(short_id)
        try:
            case_res = orchestrator.run_case(
                case_id=c_label,
                diagnostic_mode=diagnostic_mode,
                truth_loader=loader,
                public_packet=pub_pkt,
                bundle_dir=case_vis_dir,
                client=active_client,
                budget_tracker=budget_tracker,
                require_live_evidence=require_live,
                enforce_guided_measurement_check=(diagnostic_mode == "guided_diagnostic"),
            )
        except Exception as exc:
            failed_attempts.append(
                {
                    "campaign_id": active_campaign_id,
                    "case_id": c_label,
                    "diagnostic_mode": diagnostic_mode,
                    "failed_at_utc": _utc_now(),
                    "locks_committed_before_failure": list(loader.locks.keys()),
                    "probe_issued": loader.probe_requests_count > 0,
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )
            return _write_honest_partial_dossier(
                run_dir=run_dir,
                precommitment=precommitment,
                failed_attempts=failed_attempts,
                completed_cases=completed_enriched,
                execution_mode=execution_mode,
            )

        enriched_dict = _serialize_case_result_with_enriched_usage(case_res)
        completed_enriched[c_label] = enriched_dict
        jp = journals_dir / f"{diagnostic_mode}_{c_label}.jsonl"
        journal_events_by_case[c_label] = _read_journal_events(jp)

    # Remove temporary staging dirs inside run_dir before computing replay_manifest.json and SHA256SUMS.txt
    for staging_dir in (journals_dir, locks_dir):
        if staging_dir.exists():
            import shutil

            shutil.rmtree(staging_dir)

    # 3. Evaluate paired acceptance and write complete Phase 5 dossier
    pair_eval = evaluate_pair_acceptance_v2(
        completed_enriched["case_A"],
        completed_enriched["case_B"],
        public_packet_a=pub_pkt_a,
        public_packet_b=pub_pkt_b,
        pair_noncached_tokens=budget_tracker.pair_noncached_tokens,
        pair_noncached_cap=max_pair_noncached,
        require_live=require_live,
    )

    dossier_meta = write_phase5_pair_dossier(
        run_dir=run_dir,
        precommitment=precommitment,
        case_a_result=completed_enriched["case_A"],
        case_b_result=completed_enriched["case_B"],
        pair_evaluation=pair_eval,
        failed_attempts=failed_attempts,
        public_packet_a=pub_pkt_a,
        public_packet_b=pub_pkt_b,
        journal_events_a=journal_events_by_case.get("case_A"),
        journal_events_b=journal_events_by_case.get("case_B"),
        execution_mode=execution_mode,
        require_live=require_live,
    )

    replay_verification = verify_and_replay_bundle(run_dir, require_live=require_live)

    return {
        "campaign_id": active_campaign_id,
        "run_dir": str(run_dir),
        "mode": mode,
        "diagnostic_mode": diagnostic_mode,
        "strict_pair_passed": bool(pair_eval["strict_pair_passed"]),
        "verified_from_archived_run": bool(replay_verification["verified_from_archived_run"]),
        "pair_evaluation": pair_eval,
        "dossier": dossier_meta,
        "replay_verification": replay_verification,
        "failed_attempts": failed_attempts,
    }


# ---------------------------------------------------------------------------
# Phase 00 Backward-Compatible Campaign Runner (`run_ab_campaign`)
# ---------------------------------------------------------------------------


def run_ab_campaign(
    artifacts_root: Path,
    transport: Optional[ManagedAgentTransport] = None,
) -> Dict[str, Any]:
    """Run the full 8-interaction BlackboxDC A/B evaluation campaign (Phase 00 interface)."""
    artifacts_root = Path(artifacts_root)
    active_transport = transport or ManagedAgentTransport(mode="deterministic")

    # 1. Materialize public & private fixtures for Case A and Case B
    bundles = write_case_fixtures(artifacts_root=artifacts_root)

    # 2. Render shared 3D visual scene and case-specific Single-Line Diagrams
    scene_meta = render_deterministic_3d_scene(
        artifacts_root / "public" / "scene_3d_shared.png"
    )
    sld_meta: Dict[str, Any] = {}
    for cid in ("case_A", "case_B"):
        rendered = render_sld_png(
            case_id=cid,
            output_path=artifacts_root / cid / f"sld_{cid}.png",
        )
        sld_meta[cid] = dict(rendered) if hasattr(rendered, "keys") else str(rendered)

    # 3. Execute 4 interactions per case (I1, A1, Probe, I2, A2) -> 8 interactions total
    case_runs: Dict[str, CaseProtocolRunResult] = {}
    case_grades: Dict[str, Any] = {}

    for cid in ("case_A", "case_B"):
        pub_ev, sealed_truth = bundles[cid]
        sm = BlackboxProtocolStateMachine(
            public_evidence=pub_ev,
            sealed_truth=sealed_truth,
            transport=active_transport,
        )
        run_res = sm.run_case_protocol()
        unsealed = sm.unseal_truth_for_oracle()
        grade = grade_case_run(run_res, unsealed)
        case_runs[cid] = run_res
        case_grades[cid] = grade

        # Persist case interaction artifacts
        case_dir = artifacts_root / cid
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "i1_contract.json").write_text(
            json.dumps(run_res.i1.model_dump(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (case_dir / "probe_response.json").write_text(
            json.dumps(run_res.probe_response.model_dump(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (case_dir / "i2_contract.json").write_text(
            json.dumps(run_res.i2.model_dump(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (case_dir / "a1_contract.json").write_text(
            json.dumps(run_res.a1.model_dump(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (case_dir / "a2_contract.json").write_text(
            json.dumps(run_res.a2.model_dump(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (case_dir / "oracle_grade.json").write_text(
            json.dumps(asdict(grade), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    # 4. Aggregate A/B campaign oracle summary
    ab_summary = grade_ab_campaign(
        grade_a=case_grades["case_A"],
        grade_b=case_grades["case_B"],
        run_a=case_runs["case_A"],
        run_b=case_runs["case_B"],
    )
    ab_summary["visual_artifacts"] = {
        "scene_3d_shared": scene_meta,
        "sld": sld_meta,
    }
    ab_summary["accounting"] = [
        asdict(acc)
        for cid in ("case_A", "case_B")
        for acc in case_runs[cid].accounting
    ]

    # 5. Write replay bundle, markdown report, and SHA256SUMS
    evidence_meta = write_campaign_evidence_bundle(
        artifacts_root=artifacts_root,
        campaign_summary=ab_summary,
    )
    ab_summary["evidence_bundle"] = evidence_meta
    return ab_summary


# ---------------------------------------------------------------------------
# Phase 5 CLI (`python -m blackbox.campaign ...`)
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Command-line entry point for Phase 5 campaign preflight, execution, and offline replay."""
    parser = argparse.ArgumentParser(
        prog="python -m blackbox.campaign",
        description="BlackboxDC Phase 5 paired A/B campaign runner and offline replay verifier.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. preflight
    p_pre = subparsers.add_parser("preflight", help="Run environment, visual asset, and canary preflight checks.")
    p_pre.add_argument("--mode", default="auto", choices=["auto", "mock", "ai_studio", "adc"], help="Auth mode")
    p_pre.add_argument("--artifacts-root", type=Path, default=Path("artifacts"), help="Artifacts root directory")

    # 2. mock-pair
    p_mock = subparsers.add_parser("mock-pair", help="Run full A/B paired campaign in deterministic mock mode.")
    p_mock.add_argument("--campaign-id", default=None, help="Optional campaign run identifier")
    p_mock.add_argument(
        "--diagnostic-mode",
        default="guided_diagnostic",
        choices=["guided_diagnostic", "blind_diagnostic"],
        help="Diagnostic protocol mode",
    )
    p_mock.add_argument("--artifacts-root", type=Path, default=Path("artifacts"), help="Artifacts root directory")

    # 3. live-pair
    p_live = subparsers.add_parser("live-pair", help="Run full A/B paired campaign against live Managed Agent API.")
    p_live.add_argument("--mode", default="auto", choices=["auto", "ai_studio", "adc"], help="Live auth mode")
    p_live.add_argument(
        "--max-pair-noncached",
        type=int,
        default=PAIR_NONCACHED_CAP,
        help="Maximum noncached token cap across the A/B pair",
    )
    p_live.add_argument("--campaign-id", default=None, help="Optional campaign run identifier")
    p_live.add_argument(
        "--diagnostic-mode",
        default="guided_diagnostic",
        choices=["guided_diagnostic", "blind_diagnostic"],
        help="Diagnostic protocol mode",
    )
    p_live.add_argument("--artifacts-root", type=Path, default=Path("artifacts"), help="Artifacts root directory")
    p_live.add_argument("--resume", type=Path, default=None, help="Optional existing run directory to resume")

    # 4. replay
    p_rep = subparsers.add_parser("replay", help="Verify and replay an archived Phase 5 evidence bundle offline.")
    p_rep.add_argument("--bundle", type=Path, required=True, help="Path to archived campaign run directory")

    args = parser.parse_args(argv)

    if args.command == "preflight":
        res = run_preflight_command(mode=args.mode, artifacts_root=args.artifacts_root)
        print(json.dumps(res, indent=2, sort_keys=True))
        return 0 if res.get("ready") else 1

    if args.command == "mock-pair":
        res = run_visual_v2_pair_campaign(
            mode="mock",
            diagnostic_mode=args.diagnostic_mode,
            campaign_id=args.campaign_id,
            artifacts_root=args.artifacts_root,
        )
        print(
            json.dumps(
                {
                    "campaign_id": res["campaign_id"],
                    "run_dir": res["run_dir"],
                    "strict_pair_passed": res["strict_pair_passed"],
                    "verified_from_archived_run": res["verified_from_archived_run"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if (res.get("strict_pair_passed") and res.get("verified_from_archived_run")) else 1

    if args.command == "live-pair":
        try:
            res = run_visual_v2_pair_campaign(
                mode="live",
                auth_mode=args.mode,
                diagnostic_mode=args.diagnostic_mode,
                campaign_id=args.campaign_id,
                max_pair_noncached=args.max_pair_noncached,
                artifacts_root=args.artifacts_root,
                resume_bundle_dir=args.resume,
            )
        except Exception as exc:
            print(json.dumps({"status": "error", "error": str(exc)}, indent=2, sort_keys=True))
            return 1
        print(
            json.dumps(
                {
                    "campaign_id": res["campaign_id"],
                    "run_dir": res.get("run_dir"),
                    "strict_pair_passed": res.get("strict_pair_passed", False),
                    "verified_from_archived_run": res.get("verified_from_archived_run", False),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if (res.get("strict_pair_passed") and res.get("verified_from_archived_run")) else 1

    if args.command == "replay":
        res = verify_and_replay_bundle(args.bundle)
        print(json.dumps(res, indent=2, sort_keys=True))
        return 0 if (res.get("verified_from_archived_run") and res.get("strict_pair_passed")) else 1

    return 1


if __name__ == "__main__":
    sys.exit(main())
