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
    old_rec = str(action_comp.get("stale_recommended_receiver") or action_comp.get("old_receiver") or "N3")
    repaired_rec = str(
        action_comp.get("repaired_recommended_receiver")
        or action_comp.get("recommended_receiver")
        or ("N2" if decision in ("repair", "no_change") else "none")
    )

    # Extract per-candidate currents/temperatures under the post-repair (or current) graph
    post_candidates = action_comp.get("post_repair_candidates") or action_comp.get("candidates") or {}
    n3_info = post_candidates.get("N3") or post_candidates.get("MIGRATE_N4_TO_N3_8A") or {}
    n2_info = post_candidates.get("N2") or post_candidates.get("MIGRATE_N4_TO_N2_8A") or {}

    n3_f1 = float(n3_info.get("f1_current_a", n3_info.get("F1_A", 48.0)))
    n3_f1_lim = float(n3_info.get("f1_limit_a", 44.0))
    n2_f1 = float(n2_info.get("f1_current_a", n2_info.get("F1_A", 40.0)))
    n2_f1_lim = float(n2_info.get("f1_limit_a", 44.0))
    n2_f2 = float(n2_info.get("f2_current_a", n2_info.get("F2_A", 28.0)))
    n2_f2_lim = float(n2_info.get("f2_limit_a", 50.0))
    n4_temp_180s = float(
        n2_info.get("n4_temp_180s_c", n2_info.get("n4_final_temp_c", action_comp.get("n4_repaired_temp_180s_c", 52.53)))
    )

    if terminal_state == "PASS":
        result_caption = "Argon corrected the twin's wiring before the operator relied on its old recommendation."
        old_choice_banner = (
            f"Old choice {old_rec}: F1 = {n3_f1:.0f}/{n3_f1_lim:.0f} A (over limit)"
        )
        repaired_choice_banner = (
            f"Repaired choice {repaired_rec}: F1 = {n2_f1:.0f}/{n2_f1_lim:.0f} A, "
            f"F2 = {n2_f2:.0f}/{n2_f2_lim:.0f} A (N4 180s = {n4_temp_180s:.2f} °C)"
        )
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
                "The old twin suggests moving an 8 A-equivalent workload to N3."
                if str(run_data.get("revision_current", "B")) == "B"
                else "Control case: current diagram matches baseline revision A."
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
            "n3_f1_current_a": round(n3_f1, 2),
            "n3_f1_limit_a": round(n3_f1_lim, 2),
            "n2_f1_current_a": round(n2_f1, 2),
            "n2_f1_limit_a": round(n2_f1_lim, 2),
            "n2_f2_current_a": round(n2_f2, 2),
            "n2_f2_limit_a": round(n2_f2_lim, 2),
            "n4_repaired_temp_180s_c": round(n4_temp_180s, 2),
            "old_choice_text": f"N3: F1 = {n3_f1:.1f}/{n3_f1_lim:.1f} A",
            "repaired_choice_text": (
                f"N2: F1 = {n2_f1:.1f}/{n2_f1_lim:.1f} A, F2 = {n2_f2:.1f}/{n2_f2_lim:.1f} A, "
                f"N4(180s) = {n4_temp_180s:.2f}°C"
            ),
        },
        "visual_verification": {
            "cable_pixels_changed": gates["cable_pixels_changed"],
            "changed_Pixels_n2_corridor": int(score_data.get("changed_pixels_n2", 0)),
            "changed_pixels_n3_corridor": int(score_data.get("changed_pixels_n3", 0)),
            "render0_wiring_sha256": str(run_data.get("render0_wiring_sha256", "")),
            "render1_wiring_sha256": str(run_data.get("render1_wiring_sha256", "")),
            "current_sld_sha256": str(run_data.get("current_sld_sha256", "")),
            "hotspot_registration_px": float(score_data.get("hotspot_registration_px", 0.0)),
            "hotspot_iou": float(score_data.get("hotspot_iou", 1.0)),
        },
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
            f"(N2 delta px: `{vis.get('changed_Pixels_n2_corridor')}`, N3 delta px: `{vis.get('changed_pixels_n3_corridor')}`)",
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
