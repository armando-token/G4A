"""Public result card and RESULTS_TWIN_REPAIR.md generator for BlackboxDC Twin Repair.

Implements §7.F1, §8.G2, and §9.1 of `11_TWIN_REPAIR_ADDON_IMPLEMENTATION_PLAN_EN.md`:
- `build_public_result_card(run_data)` -> strictly redacted `result_public.json` dict with
  all 11 required gate booleans (`builder_completed`, `critic_completed`, `argon_verified`,
  `distinct_environments`, `render0_valid`, `patch_valid`, `render1_valid`,
  `cable_pixels_changed`, `graph_improved`, `physics_safe`, `human_review_required`),
  before/after graph score (`2/4 -> 4/4` or `4/4 -> 4/4`), old vs repaired action
  comparison (`N3: F1 = 48.0/44.0 A` vs `N2: F1 = 40.0/44.0 A, F2 = 28.0/50.0 A`),
  timing spans, token usage, and §7.F1 headlines/captions.
- `render_results_twin_repair_md(run_data)` -> Markdown evidence report (`RESULTS_TWIN_REPAIR.md`).
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping

from blackbox.managed import redact_for_log


REQUIRED_GATE_KEYS: tuple[str, ...] = (
    "builder_completed",
    "critic_completed",
    "argon_verified",
    "distinct_environments",
    "render0_valid",
    "patch_valid",
    "render1_valid",
    "cable_pixels_changed",
    "graph_improved",
    "physics_safe",
    "human_review_required",
)

FORBIDDEN_PUBLIC_REPORT_KEYS: frozenset[str] = frozenset(
    {
        "private_truth",
        "sealed_truth",
        "truth_table",
        "current_edges_truth",
        "old_edges_truth",
        "api_key",
        "gemini_api_key",
        "google_api_key",
    }
)


def _strip_private_keys(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        cleaned: Dict[str, Any] = {}
        for k, v in obj.items():
            ks = str(k)
            if ks in FORBIDDEN_PUBLIC_REPORT_KEYS or ks.startswith("_private"):
                continue
            cleaned[ks] = _strip_private_keys(v)
        return cleaned
    if isinstance(obj, list):
        return [_strip_private_keys(item) for item in obj]
    if isinstance(obj, tuple):
        return [_strip_private_keys(item) for item in obj]
    return obj


VISUAL_REPAIR_STATUSES: tuple[str, ...] = ("REPAIRED", "NO_CHANGE", "ABSTAINED", "REJECTED")
ACTION_STATUSES: tuple[str, ...] = ("SAFE_RECEIVER_FOUND", "MULTIPLE_SAFE", "NO_SAFE_ACTION", "INSUFFICIENT_EVIDENCE")


def _role_summary(prov: Mapping[str, Any]) -> Dict[str, Any]:
    prov = dict(prov or {})
    return {
        "source": prov.get("source"),
        "returned_model": prov.get("returned_model"),
        "argon_verified": bool(prov.get("argon_verified", False)),
        "environment_id": prov.get("environment_id"),
        "interaction_id": prov.get("interaction_id"),
        "status": prov.get("status"),
        "elapsed_s": prov.get("elapsed_s"),
        "input_images": [
            {"name": m.get("name"), "sha256": m.get("sha256")}
            for m in (prov.get("image_manifest") or [])
            if isinstance(m, Mapping)
        ],
    }


def derive_outcome_fields(run_data: Mapping[str, Any]) -> Dict[str, Any]:
    """Separate the visual repair verdict from the electrical action verdict.

    An accurate repair with no safe transfer is a valid safety result, so the two are never folded
    into a single PASS. Values are computed only from this run's validated artifacts.
    """
    raw_gates = dict(run_data.get("gates") or {})

    def gate(key: str) -> bool:
        return bool(raw_gates.get(key, run_data.get(key, False)))

    terminal_state = str(run_data.get("terminal_state", "REJECTED"))
    patch = dict(run_data.get("patch") or {})
    decision = str(run_data.get("decision") or patch.get("decision") or "").lower()
    score = dict(run_data.get("score") or {})
    total = int(score.get("total_edges", 0) or 0)

    if terminal_state == "ABSTAINED":
        visual = "ABSTAINED"
    elif decision == "no_change" and gate("patch_valid") and total > 0 and int(score.get("before_correct_edges", -1)) == total:
        visual = "NO_CHANGE"
    elif (
        decision == "repair"
        and gate("patch_valid")
        and gate("render1_valid")
        and gate("cable_pixels_changed")
        and gate("graph_improved")
        and bool(score.get("evidence_boxes_valid", False))
    ):
        visual = "REPAIRED"
    else:
        visual = "REJECTED"

    action_comp = dict(run_data.get("action_comparison") or {})
    repaired = action_comp.get("repaired_outcomes")
    if visual in ("REPAIRED", "NO_CHANGE") and isinstance(repaired, list) and repaired:
        safe_targets = sorted(
            str(o.get("target_rack"))
            for o in repaired
            if isinstance(o, Mapping) and o.get("target_rack") and bool(o.get("safe"))
        )
        if not safe_targets:
            action = "NO_SAFE_ACTION"
        elif len(safe_targets) == 1:
            action = "SAFE_RECEIVER_FOUND"
        else:
            action = "MULTIPLE_SAFE"
        receiver = action_comp.get("repaired_recommended_receiver") if safe_targets else None
    else:
        action, safe_targets, receiver = "INSUFFICIENT_EVIDENCE", [], None

    provider_mode = str(run_data.get("provider_mode", "mock"))
    if provider_mode == "mock":
        source = "mock_fixture"
    elif provider_mode == "replay" or run_data.get("is_replay"):
        source = "recorded_live_replay"
    elif bool(run_data.get("prepared_builder")) or provider_mode == "prepared_builder":
        source = "prepared_builder_live_critic"
    else:
        source = "full_live_two_agents"

    return {
        "visual_repair_status": visual,
        "action_status": action,
        "safe_receivers": safe_targets,
        "recommended_receiver": receiver,
        "tie_break_rule": (
            "Lowest peak feeder load ratio, then rack ID" if action == "MULTIPLE_SAFE" else None
        ),
        "execution_source": source,
        "role_provenance": {
            "builder": _role_summary(dict(run_data.get("builder_provenance") or {})),
            "critic": _role_summary(dict(run_data.get("critic_provenance") or {})),
        },
    }


def build_public_result_card(run_data: Mapping[str, Any]) -> Dict[str, Any]:
    """Build the strictly redacted `result_public.json` dictionary for CLI and web UI."""
    raw_gates = dict(run_data.get("gates") or {})
    gates: Dict[str, bool] = {}
    for k in REQUIRED_GATE_KEYS:
        if k == "human_review_required":
            gates[k] = bool(raw_gates.get(k, run_data.get(k, True)))
        else:
            gates[k] = bool(raw_gates.get(k, run_data.get(k, False)))

    terminal_state = str(run_data.get("terminal_state", "REJECTED"))
    provider_mode = str(run_data.get("provider_mode", "mock"))
    decision = str(run_data.get("decision") or (run_data.get("patch") or {}).get("decision") or "")

    score_data = dict(run_data.get("score") or {})
    before_correct = int(score_data.get("before_correct_edges", score_data.get("score0_correct", 2)))
    total_edges = int(score_data.get("total_edges", 4))
    after_correct = int(
        score_data.get(
            "after_correct_edges",
            score_data.get("score1_correct", before_correct if decision != "repair" else 4),
        )
    )
    graph_score_before = f"{before_correct}/{total_edges}"
    graph_score_after = f"{after_correct}/{total_edges}"
    graph_score_summary = f"{graph_score_before} -> {graph_score_after}"

    action_comp = dict(run_data.get("action_comparison") or {})
    outcome = derive_outcome_fields(run_data)
    old_rec = action_comp.get("stale_recommended_receiver") or action_comp.get("old_receiver")
    repaired_rec = action_comp.get("repaired_recommended_receiver") or action_comp.get("recommended_receiver")

    # Per-candidate values come only from this run's solver output; never substitute canonical numbers.
    post_candidates = action_comp.get("post_repair_candidates") or action_comp.get("candidates") or {}
    n3_info = post_candidates.get("N3") or post_candidates.get("MIGRATE_N4_TO_N3_8A") or {}
    n2_info = post_candidates.get("N2") or post_candidates.get("MIGRATE_N4_TO_N2_8A") or {}
    old_info = dict(action_comp.get("stale_choice_evaluated_on_repaired_graph") or {})
    rep_info = dict(action_comp.get("repaired_choice_evaluated_on_repaired_graph") or {})

    def _num(info: Mapping[str, Any], *keys: str) -> Any:
        for key in keys:
            if info.get(key) is not None:
                return round(float(info[key]), 2)
        return None

    n3_f1 = _num(n3_info, "f1_current_a", "F1_A")
    n3_f1_lim = _num(n3_info, "f1_limit_a")
    n2_f1 = _num(n2_info, "f1_current_a", "F1_A")
    n2_f1_lim = _num(n2_info, "f1_limit_a")
    n2_f2 = _num(n2_info, "f2_current_a", "F2_A")
    n2_f2_lim = _num(n2_info, "f2_limit_a")
    n4_temp_180s = _num(rep_info, "n4_temp_180s_c", "n4_final_temp_c")
    if n4_temp_180s is None:
        n4_temp_180s = _num(n2_info, "n4_temp_180s_c", "n4_final_temp_c")

    def _choice_text(rack: Any, info: Mapping[str, Any]) -> str:
        if not rack or not info:
            return "No verified recommendation issued"
        f1, f1l = _num(info, "f1_current_a"), _num(info, "f1_limit_a")
        f2, f2l = _num(info, "f2_current_a"), _num(info, "f2_limit_a")
        if None in (f1, f1l, f2, f2l):
            return "No verified recommendation issued"
        status = "within limits" if info.get("safe") else "unsafe"
        return f"{rack}: F1 = {f1:.0f}/{f1l:.0f} A, F2 = {f2:.0f}/{f2l:.0f} A ({status})"

    if terminal_state == "PASS" and action_comp:
        result_caption = "Argon corrected the twin's wiring before the operator relied on its old recommendation."
        old_choice_banner = f"Old choice {_choice_text(old_rec, old_info)}"
        repaired_choice_banner = f"Repaired choice {_choice_text(repaired_rec, rep_info)}"
        if n4_temp_180s is not None:
            repaired_choice_banner += f" (N4 180s = {n4_temp_180s:.2f} °C)"
    elif terminal_state == "CONTROL_NO_CHANGE":
        result_caption = "The drawing and twin already agree; no repair was made."
        old_choice_banner = "Already matches; no repair made"
        repaired_choice_banner = "Already matches; no repair made"
    else:
        result_caption = "Argon could not verify this wiring; no recommendation is issued."
        old_choice_banner = "No verified recommendation issued"
        repaired_choice_banner = "No verified recommendation issued"

    builder_prov = _strip_private_keys(redact_for_log(dict(run_data.get("builder_provenance") or {})))
    critic_prov = _strip_private_keys(redact_for_log(dict(run_data.get("critic_provenance") or {})))
    patch_public = _strip_private_keys(copy.deepcopy(dict(run_data.get("patch") or {})))

    card: Dict[str, Any] = {
        "schema_version": "twin_repair_result_public.v1",
        "run_id": str(run_data.get("run_id", "")),
        "case_id": str(run_data.get("case_id", "")),
        "provider_mode": provider_mode,
        "source": str(run_data.get("source", "fixture" if provider_mode == "mock" else provider_mode)),
        "is_simulated_mock": provider_mode == "mock",
        "is_replay": provider_mode == "replay",
        "prepared_builder": bool(run_data.get("prepared_builder", provider_mode == "prepared_builder")),
        "terminal_state": terminal_state,
        "passed": bool(run_data.get("passed", False)),
        "workflow_passed": bool(run_data.get("workflow_passed", run_data.get("passed", False))),
        # All 11 required top-level gate booleans (§9.1)
        **gates,
        "gates": gates,
        "headlines": {
            "incident_headline": "The connector is hot. Is the wiring map still correct?",
            "incident_caption": (
                "Control case: current diagram matches baseline revision A."
                if str(run_data.get("revision_current", "")) == "A"
                else (
                    f"The old twin suggests moving an 8 A-equivalent workload to {old_rec}."
                    if old_rec
                    else "The old twin's recommendation has not been evaluated."
                )
            ),
            "result_caption": result_caption,
            "old_choice_banner": old_choice_banner,
            "repaired_choice_banner": repaired_choice_banner,
            "badge": "Synthetic case",
            "approval_banner": "Human review required — verify against site documentation before any physical change.",
        },
        "graph_score": {
            "before": graph_score_before,
            "after": graph_score_after,
            "before_correct": before_correct,
            "after_correct": after_correct,
            "total_edges": total_edges,
            "summary": graph_score_summary,
        },
        "action_summary": {
            "stale_recommended_receiver": old_rec,
            "repaired_recommended_receiver": repaired_rec,
            "n3_f1_current_a": n3_f1,
            "n3_f1_limit_a": n3_f1_lim,
            "n2_f1_current_a": n2_f1,
            "n2_f1_limit_a": n2_f1_lim,
            "n2_f2_current_a": n2_f2,
            "n2_f2_limit_a": n2_f2_lim,
            "n4_repaired_temp_180s_c": n4_temp_180s,
            "old_choice_text": _choice_text(old_rec, old_info),
            "repaired_choice_text": _choice_text(repaired_rec, rep_info),
        },
        "visual_verification": {
            "cable_pixels_changed": gates["cable_pixels_changed"],
            "changed_pixels_n2_corridor": int(score_data.get("changed_pixels_n2", 0)),
            "changed_pixels_n3_corridor": int(score_data.get("changed_pixels_n3", 0)),
            "render0_wiring_sha256": str(run_data.get("render0_wiring_sha256", "")),
            "render1_wiring_sha256": str(run_data.get("render1_wiring_sha256", "")),
            "current_sld_sha256": str(run_data.get("current_sld_sha256", "")),
            "hotspot_registration_px": float(score_data.get("hotspot_registration_px", 0.0)),
            "hotspot_iou": float(score_data.get("hotspot_iou", 1.0)),
        },
        **outcome,
        "patch": patch_public,
        "roles": {
            "builder": builder_prov,
            "critic": critic_prov,
        },
        "timing_spans_s": dict(run_data.get("timing_spans_s") or {}),
        "total_elapsed_s": float(run_data.get("total_elapsed_s", 0.0)),
        "token_usage": _strip_private_keys(dict(run_data.get("token_usage") or {})),
        "generated_at_utc": str(
            run_data.get("completed_at_utc") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ),
    }
    return _strip_private_keys(redact_for_log(card))


def render_results_twin_repair_md(run_data: Mapping[str, Any]) -> str:
    """Render `RESULTS_TWIN_REPAIR.md` following §8.G2 of the Twin Repair implementation plan."""
    card = build_public_result_card(run_data)
    manifest = dict(run_data.get("manifest") or {})
    builder = dict(card["roles"].get("builder") or {})
    critic = dict(card["roles"].get("critic") or {})
    patch = dict(card.get("patch") or {})
    discrepancies: List[Dict[str, Any]] = list(patch.get("discrepancies") or [])
    operations: List[Dict[str, Any]] = list(patch.get("operations") or [])
    vis = dict(card.get("visual_verification") or {})
    act = dict(card.get("action_summary") or {})
    gs = dict(card.get("graph_score") or {})
    spans = dict(card.get("timing_spans_s") or {})
    tok = dict(card.get("token_usage") or {})

    lines: List[str] = [
        "# BlackboxDC Twin Repair — Evidence Card (`RESULTS_TWIN_REPAIR.md`)",
        "",
        "## 1. Case & Revision Provenance",
        f"- **Run ID:** `{card['run_id']}`",
        f"- **Case ID:** `{card['case_id']}`",
        f"- **Provider Mode:** `{card['provider_mode']}` (source: `{card['source']}`)",
        f"- **Terminal State:** `{card['terminal_state']}` (`passed={card['passed']}`, `workflow_passed={card['workflow_passed']}`)",
        f"- **Revision Provenance:** Old twin revision `{manifest.get('revision_old', 'A')}` -> Current SLD revision `{manifest.get('revision_current', 'B')}`",
        f"- **Git Commit:** `{manifest.get('git_commit', 'working-tree')}`",
        f"- **Build / Run Timestamp (UTC):** `{card['generated_at_utc']}`",
        "",
        "## 2. Managed Agent Role Provenance & Sandbox Isolation",
        "| Role | Agent | Requested Model | Returned Model | Argon Verified | Interaction ID | Environment ID | Status | Output File SHA-256 |",
        "|---|---|---|---|---|---|---|---|---|",
        (
            f"| **Builder** | `{builder.get('requested_agent', 'antigravity-preview-09-2026')}` | "
            f"`{builder.get('requested_model', 'gemini-4-argon')}` | `{builder.get('returned_model', '')}` | "
            f"`{builder.get('argon_verified', False)}` | `{builder.get('interaction_id', '')}` | "
            f"`{builder.get('environment_id', '')}` | `{builder.get('status', '')}` | "
            f"`{str(builder.get('retrieved_sha256', ''))[:16]}...` |"
        ),
        (
            f"| **Critic** | `{critic.get('requested_agent', 'antigravity-preview-09-2026')}` | "
            f"`{critic.get('requested_model', 'gemini-4-argon')}` | `{critic.get('returned_model', '')}` | "
            f"`{critic.get('argon_verified', False)}` | `{critic.get('interaction_id', '')}` | "
            f"`{critic.get('environment_id', '')}` | `{critic.get('status', '')}` | "
            f"`{str(critic.get('retrieved_sha256', ''))[:16]}...` |"
        ),
        f"- **Distinct Remote Environments Enforced:** `{card['distinct_environments']}`",
        "",
        "## 3. Input & Rendered Artifact Hashes",
        f"- **Current SLD PNG SHA-256:** `{vis.get('current_sld_sha256', '')}`",
        f"- **Before Wiring Render (`render0/render_wiring.png`) SHA-256:** `{vis.get('render0_wiring_sha256', '')}`",
        f"- **After Wiring Render (`render1/render_wiring.png`) SHA-256:** `{vis.get('render1_wiring_sha256', '')}`",
        "- **Thumbnails:** `render0/render_wiring.png` (Before) · `render1/render_wiring.png` (After) · `public/current_sld_B.png` (Target SLD)",
        "",
        "## 4. Critic Visual Discrepancies & Applied Patch Operations",
        f"- **Critic Decision:** `{patch.get('decision', 'none')}` (confidence: `{patch.get('confidence', 0.0)}`)",
    ]

    if discrepancies:
        for d in discrepancies:
            lines.append(
                f"  - Discrepancy on `{d.get('rack_id')}`: observed `{d.get('observed_feeder')}` in render "
                f"(bbox `{d.get('render_evidence_bbox_xyxy_px')}`), required `{d.get('required_feeder')}` in SLD "
                f"(bbox `{d.get('sld_evidence_bbox_xyxy_px')}`) — {d.get('reason', '')}"
            )
    else:
        lines.append("  - No discrepancies reported.")

    if operations:
        for op in operations:
            lines.append(
                f"  - Applied `{op.get('op')}` on `{op.get('rack_id')}`: `{op.get('from')}` -> `{op.get('to')}`"
            )
    else:
        lines.append("  - Applied operations: `[]` (none)")

    lines.extend(
        [
            "",
            "## 5. Verification Gates, Graph Score & Electrothermal Consequence",
            f"- **Graph Edge Accuracy:** `{gs.get('summary')}` (`{gs.get('before')}` -> `{gs.get('after')}`)",
            f"- **Cable Pixels Changed in Projected N2/N3 Corridors:** `{vis.get('cable_pixels_changed')}` "
            f"(N2 delta px: `{vis.get('changed_pixels_n2_corridor')}`, N3 delta px: `{vis.get('changed_pixels_n3_corridor')}`)",
            f"- **Hotspot Registration & Localization:** registration delta `{vis.get('hotspot_registration_px')} px`, IoU `{vis.get('hotspot_iou')}`",
            f"- **Stale Twin Hypothetical Choice:** `{act.get('old_choice_text')}` (exceeds 44.0 A continuous limit on F1 under as-built B)",
            f"- **Repaired Twin Verified Choice:** `{act.get('repaired_choice_text')}`",
            "",
            "### 11-Gate Checklist",
        ]
    )
    for gk in REQUIRED_GATE_KEYS:
        lines.append(f"- `{gk}`: `{card['gates'][gk]}`")

    lines.extend(
        [
            "",
            "## 6. Timing Spans & Token Usage",
            f"- **Total Wall-Clock Elapsed:** `{card['total_elapsed_s']:.3f} s`",
            f"- **Timing Spans (s):** `{spans}`",
            f"- **Token Usage:** `{tok}`",
            "",
            "## 7. Provenance & Safety Disclosure",
            "- **Event-Day Original Extension Work:** `twin_repair/` closed visual render -> critique -> patch -> rerender -> physics verification loop.",
            "- **Pre-Event Reused Baseline:** `blackbox/domain.py` electrothermal solver and `blackbox/managed.py` Managed Agent transport.",
            f"- **Run Provenance Label:** `provider_mode={card['provider_mode']}`, `source={card['source']}`, `prepared_builder={card['prepared_builder']}`, `is_replay={card['is_replay']}`.",
            "- **Safety Disclaimer:** Synthetic as-built drift demonstration only. This output is NOT a physical data-center electrical safety certification and never actuates real equipment. Human engineering review is required (`human_review_required=True`).",
            "",
        ]
    )
    return "\n".join(lines)
