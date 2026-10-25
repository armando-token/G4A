"""Predecessor installation-v1 pilot runner, 3-turn simulator, and historical outcome registry.

Provides:
1. Phase 7C (`07_HISTORICAL_RECONSTRUCTION_AND_FULL_PARITY.md`) 3-turn managed-file protocol:
   - `verify_v1_public_boundary`
   - `build_v1_state_transfer_packet`
   - `SimulatedInstallationV1Client` (supporting both `"historical_incomplete_case_a"`
     and `"simulated_all_completed"`)
   - `run_installation_v1_campaign`
   - `recover_interrupted_v1_run`
   - `build_v1_revision_dossier`
2. Backward-compatible `HISTORICAL_INSTALLATION_V1_OUTCOME`,
   `get_historical_installation_v1_record`, and `run_installation_pilot`.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from blackbox.domain import (
    DEFAULT_MIGRATION_AMPS,
    Migration,
    simulate_world,
    step_temperature,
)
from historical.installation_contracts import (
    CANONICAL_V1_ACTIONS,
    CaseDiagnostics,
    InstallationPairReport,
    build_verified_spanish_summary,
    classify_case_outcome,
    evaluate_strict_pair_gate,
    validate_v1_audit,
    validate_v1_plan,
    validate_v1_proposal,
)
from historical.installation_lab import (
    INSTALLATION_V1_LAYOUTS,
    INSTALLATION_V1_N4_HOTSPOT_BBOX,
    INSTALLATION_V1_SCHEMA,
    InstallationLab,
    _normalize_v1_layout,
    build_installation_v1_public_bundle,
    build_installation_v1_world,
)


FORBIDDEN_V1_PUBLIC_KEYS: frozenset[str] = frozenset(
    {
        "r0_ohm",
        "R0_ohm",
        "h_w_per_k",
        "h_W_per_K",
        "c_j_per_k",
        "C_J_per_K",
        "private_truth",
        "sealed_truth",
        "safe_receiver",
        "wrong_receiver",
    }
)


def verify_v1_public_boundary(
    public_bundle: Any,
    *,
    raise_on_leak: bool = False,
) -> bool:
    """Return True if `public_bundle` (mapping or path to public_bundle.json / bundle dir) contains zero private truth keys."""
    target: Any = public_bundle
    if isinstance(public_bundle, (str, Path)):
        p = Path(public_bundle)
        if p.is_dir():
            p = p / "public_bundle.json"
        if p.exists():
            target = json.loads(p.read_text(encoding="utf-8"))

    def _has_forbidden_key(obj: Any) -> Optional[str]:
        if isinstance(obj, Mapping):
            for k, v in obj.items():
                ks = str(k)
                if ks in FORBIDDEN_V1_PUBLIC_KEYS:
                    return ks
                sub = _has_forbidden_key(v)
                if sub is not None:
                    return sub
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                sub = _has_forbidden_key(item)
                if sub is not None:
                    return sub
        return None

    leaked = _has_forbidden_key(target)
    if leaked is not None:
        if raise_on_leak:
            raise PermissionError(f"Forbidden private key {leaked!r} leaked into installation-v1 public bundle")
        return False
    return True


def build_v1_state_transfer_packet(
    proposal: Mapping[str, Any],
    probe_receipt: Mapping[str, Any],
) -> Dict[str, Any]:
    """Build the compact state transfer packet passed from Turn 1 (`proposal` + `probe`) to Turn 2 (`plan`)."""
    prop_bytes = json.dumps(dict(proposal), sort_keys=True, separators=(",", ":")).encode("utf-8")
    prop_sha = hashlib.sha256(prop_bytes).hexdigest()
    return {
        "schema_version": INSTALLATION_V1_SCHEMA,
        "proposal_sha256": prop_sha,
        "proposal": copy.deepcopy(dict(proposal)),
        "probe_receipt": copy.deepcopy(dict(probe_receipt)),
    }


def _build_pure_step_temperature_source() -> str:
    return (
        "def step_temperature(t_c, i_a, ambient_c=25.0, r0_ohm=0.04, alpha=0.004, h_w_k=0.4, heat_capacity_j_k=8.0, dt_s=10.0):\n"
        "    delta_t = max(float(t_c) - float(ambient_c), 0.0)\n"
        "    r_eff = float(r0_ohm) * (1.0 + float(alpha) * delta_t)\n"
        "    teq = float(ambient_c) + (float(i_a) ** 2) * r_eff / float(h_w_k)\n"
        "    decay = 2.718281828459045 ** (-float(h_w_k) * float(dt_s) / float(heat_capacity_j_k))\n"
        "    return teq + (float(t_c) - teq) * decay\n"
    )


class SimulatedInstallationV1Client:
    """Deterministic 3-turn simulator for predecessor `installation-v1` (`proposal -> plan -> auditor`).

    Modes:
    - `"historical_incomplete_case_a"`: Reproduces the exact historical outcome where
      Case A's `plan` turn (`I2`) terminates with `status="incomplete"` while local
      numerical checks succeed (`strict_pair_gate_passed=False`), and Case B completes all 3 turns.
    - `"simulated_all_completed"`: Simulates all 3 turns completing in both Case A and Case B
      (`strict_pair_gate_passed=True`, `is_simulated_mock=True`, `counts_as_live_evidence=False`).
    """

    def __init__(self, mode: str = "historical_incomplete_case_a") -> None:
        if mode not in {"historical_incomplete_case_a", "simulated_all_completed"}:
            raise ValueError(
                f"Unsupported mode={mode!r}; expected 'historical_incomplete_case_a' or 'simulated_all_completed'"
            )
        self.mode = mode

    def run_case(self, layout: str = "A") -> Dict[str, Any]:
        norm_layout = _normalize_v1_layout(layout)
        layout_spec = INSTALLATION_V1_LAYOUTS[norm_layout]
        world = build_installation_v1_world(norm_layout, fault_type="electrical")

        # Turn 1: Proposal
        proposal_payload: Dict[str, Any] = {
            "schema_version": INSTALLATION_V1_SCHEMA,
            "layout": norm_layout,
            "hotspot_rack": "N4",
            "hotspot_bbox": list(INSTALLATION_V1_N4_HOTSPOT_BBOX),
            "power_edges": dict(layout_spec["power_edges"]),
            "sensor_edges": dict(layout_spec["sensor_edges"]),
            "unknown_critical_edges": [],
            "selected_measurement": {
                "type": "voltage_drop",
                "target": "N4",
            },
        }
        prop_val = validate_v1_proposal(proposal_payload, layout=norm_layout)

        # Single Probe Receipt after Proposal lock
        n4_rack = world.get_rack("N4")
        r_t0 = n4_rack.R0_ohm * (1.0 + n4_rack.alpha_per_C * (n4_rack.initial_temp_C - world.ambient_C))
        v_drop = round(float(n4_rack.current_A * r_t0), 6)
        probe_receipt = {
            "receipt_id": f"V1-RCPT-{norm_layout}-001",
            "kind": "voltage_drop",
            "target": "N4",
            "value": v_drop,
            "units": "V",
        }
        transfer_packet = build_v1_state_transfer_packet(proposal_payload, probe_receipt)

        # Turn 2: Plan (local numerical simulation of all 3 actions)
        preds_by_action: Dict[str, Dict[str, Any]] = {}
        for aid, (src, dst, amps) in CANONICAL_V1_ACTIONS.items():
            mig = None if src is None or dst is None else Migration(src, dst, amps)
            sim = simulate_world(world, migration=mig)
            preds_by_action[aid] = {
                "safe": bool(sim.safety_flags["safe"]),
                "thermal_violation": bool(sim.safety_flags["thermal_violation"]),
                "pdu_overload": bool(sim.safety_flags["pdu_overload"]),
                "n4_temp_t180_c": round(float(sim.temps_C["N4"][-1]), 4),
                "f1_current_a": round(float(sim.pdu_currents_A["F1"][-1]), 2),
                "f2_current_a": round(float(sim.pdu_currents_A["F2"][-1]), 2),
            }

        safe_act = layout_spec["safe_action_id"]
        plan_payload: Dict[str, Any] = {
            "schema_version": INSTALLATION_V1_SCHEMA,
            "layout": norm_layout,
            "forecast_source": _build_pure_step_temperature_source(),
            "unknown_critical_edges": [],
            "predictions_by_action": preds_by_action,
            "selected_action_id": safe_act,
            "decision": "intervene",
            "fitted_parameters": {"R0_ohm": 0.040, "h_W_per_K": 0.4, "C_J_per_K": 8.0},
        }
        plan_val = validate_v1_plan(plan_payload, layout=norm_layout)

        # Turn 3: Auditor
        audit_payload: Dict[str, Any] = {
            "schema_version": INSTALLATION_V1_SCHEMA,
            "layout": norm_layout,
            "verdict": "accept" if plan_val["valid"] else "reject",
            "verified_action_id": safe_act,
        }
        audit_val = validate_v1_audit(audit_payload, plan_val)

        # Determine operational completion status per mode
        if self.mode == "historical_incomplete_case_a" and norm_layout == "A":
            turn_statuses = {
                "proposal": "completed",
                "plan": "incomplete",
                "auditor": "not_started",
            }
            turns_completed = False
        else:
            turn_statuses = {
                "proposal": "completed",
                "plan": "completed",
                "auditor": "completed",
            }
            turns_completed = True

        numeric_visual_valid = bool(prop_val["valid"] and plan_val["valid"] and audit_val["valid"])
        classification = classify_case_outcome(
            numeric_visual_valid=numeric_visual_valid,
            turns_completed=turns_completed,
        )
        sel_metrics = preds_by_action[safe_act]
        spanish_summary = build_verified_spanish_summary(
            case_id=f"Case {norm_layout}",
            selected_action_id=safe_act,
            n4_temp_180s_c=float(sel_metrics["n4_temp_t180_c"]),
            f1_amps=float(sel_metrics["f1_current_a"]),
            f2_amps=float(sel_metrics["f2_current_a"]),
            classification_label=classification,
        )

        return {
            "schema_version": INSTALLATION_V1_SCHEMA,
            "case_id": f"Case {norm_layout}",
            "layout": norm_layout,
            "mode": self.mode,
            "is_simulated_mock": True,
            "counts_as_live_evidence": False,
            "turn_statuses": turn_statuses,
            "turns_completed": turns_completed,
            "numeric_visual_valid": numeric_visual_valid,
            "classification": classification,
            "strict_case_passed": classification == "strict case pass",
            "selected_action_id": safe_act,
            "proposal": proposal_payload,
            "probe_receipt": probe_receipt,
            "state_transfer_packet": transfer_packet,
            "plan": plan_payload,
            "audit": audit_payload,
            "validations": {
                "proposal": prop_val,
                "plan": plan_val,
                "audit": audit_val,
            },
            "spanish_summary": spanish_summary,
        }


def run_installation_v1_campaign(
    mode: str = "historical_incomplete_case_a",
    output_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run the 3-turn `installation-v1` campaign across Layout A and Layout B."""
    pub_a = build_installation_v1_public_bundle(
        "A", output_dir=(Path(output_dir) / "case_A" if output_dir else None)
    )
    pub_b = build_installation_v1_public_bundle(
        "B", output_dir=(Path(output_dir) / "case_B" if output_dir else None)
    )
    boundary_ok = verify_v1_public_boundary(pub_a, raise_on_leak=True) and verify_v1_public_boundary(
        pub_b, raise_on_leak=True
    )

    sim = SimulatedInstallationV1Client(mode=mode)
    res_a = sim.run_case("A")
    res_b = sim.run_case("B")

    strict_pair_passed = bool(res_a["strict_case_passed"] and res_b["strict_case_passed"])
    local_numeric_ok = bool(res_a["numeric_visual_valid"] and res_b["numeric_visual_valid"])

    result = {
        "schema_version": INSTALLATION_V1_SCHEMA,
        "mode": mode,
        "is_simulated_mock": True,
        "counts_as_live_evidence": False,
        "public_boundary_verified": boundary_ok,
        "local_numerical_checks_succeeded": local_numeric_ok,
        "strict_pair_gate_passed": strict_pair_passed,
        "case_A": res_a,
        "case_B": res_b,
        "cases": {
            "case_A": res_a,
            "case_B": res_b,
            "A": res_a,
            "B": res_b,
        },
    }
    if output_dir is not None:
        odir = Path(output_dir)
        odir.mkdir(parents=True, exist_ok=True)
        (odir / "installation_v1_campaign.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return result


def recover_interrupted_v1_run(
    journal_events: Sequence[Mapping[str, Any]],
    *,
    numeric_visual_valid: bool = True,
) -> Dict[str, Any]:
    """Reconcile an interrupted `installation-v1` JSONL event stream without hanging."""
    created_turns: Dict[str, Dict[str, Any]] = {}
    terminal_turns: Dict[str, str] = {}
    terminal_set = {"completed", "failed", "cancelled", "incomplete"}

    for ev in journal_events:
        tid = str(ev.get("turn_id") or ev.get("stage") or "")
        if not tid:
            continue
        ev_type = str(ev.get("event", "")).lower()
        status = str(ev.get("status", "")).lower()
        if ev_type == "created":
            created_turns[tid] = dict(ev)
        elif ev_type in {"terminal", "locked"} or status in terminal_set:
            terminal_turns[tid] = status if status in terminal_set else "completed"

    reconciled_statuses: Dict[str, str] = {}
    interrupted_turns: List[str] = []
    for tid in created_turns:
        if tid in terminal_turns:
            reconciled_statuses[tid] = terminal_turns[tid]
        else:
            reconciled_statuses[tid] = "incomplete"
            interrupted_turns.append(tid)

    all_completed = (
        len(reconciled_statuses) >= 3
        and all(st == "completed" for st in reconciled_statuses.values())
        and len(interrupted_turns) == 0
    )
    classification = classify_case_outcome(
        numeric_visual_valid=numeric_visual_valid,
        turns_completed=all_completed,
    )
    return {
        "reconciled_statuses": reconciled_statuses,
        "interrupted_turns": interrupted_turns,
        "turns_completed": all_completed,
        "numeric_visual_valid": bool(numeric_visual_valid),
        "classification": classification,
        "strict_case_passed": classification == "strict case pass",
    }


def build_v1_revision_dossier(
    campaign_result: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the Phase 7C revision dossier comparing `installation-v1` to `visual-v2`."""
    active_res = (
        dict(campaign_result)
        if campaign_result is not None
        else run_installation_v1_campaign(mode="historical_incomplete_case_a")
    )
    return {
        "schema_version": INSTALLATION_V1_SCHEMA,
        "predecessor_schema": INSTALLATION_V1_SCHEMA,
        "successor_schema": "visual-v2",
        "historical_v1_strict_pair_gate_passed": bool(active_res.get("strict_pair_gate_passed", False)),
        "historical_v1_local_numerical_checks_succeeded": bool(
            active_res.get("local_numerical_checks_succeeded", True)
        ),
        "case_A_classification": active_res["case_A"]["classification"],
        "case_B_classification": active_res["case_B"]["classification"],
        "architectural_upgrades_in_visual_v2": [
            "Upgraded from 800x500 2D schematic/thermal frames to 1280x800 3D Three.js rear-aisle RGB + false-color thermal renders and vector SLD with IEEE jump arches.",
            "Expanded from 3-turn shared flow to 4-turn sandbox-isolated Investigator (I1, I2) and Auditor (A1, A2) protocol with hash-chained locks (L1..L4).",
            "Added +1.0 C N4 initial temperature perturbation sensitivity canary in A2 and GuardedOracleTruthLoader blocking private truth access until L4.",
            "Added 3-ledger token accounting with Auditor/A2 reserves and direct environment file retrieval.",
        ],
        "campaign_snapshot": active_res,
    }


# ---------------------------------------------------------------------------
# Backward-Compatible Phase 00 Pilot Functions & Constants
# ---------------------------------------------------------------------------

HISTORICAL_INSTALLATION_V1_OUTCOME: dict[str, Any] = {
    "benchmark_id": "installation-v1",
    "local_numerical_checks_succeeded": True,
    "case_a_investigator_turn_complete": False,
    "case_b_investigator_turn_complete": True,
    "strict_pair_gate_passed": False,
    "outcome_label": "INCOMPLETE_INVESTIGATOR_TURN_PAIR_FAIL",
}


def get_historical_installation_v1_record() -> InstallationPairReport:
    """Return the exact historical installation-v1 audit record.

    In the historical installation-v1 run:
    - Local numerical checks succeeded on both Case A and Case B.
    - Case B completed its investigator turn and identified the damper obstruction.
    - Case A had an incomplete investigator turn and did not emit a final verdict.
    - Consequently, `strict_pair_gate_passed` is `False`.
    """
    lab = InstallationLab()
    sim_a = lab.simulate_case("Case A")
    sim_b = lab.simulate_case("Case B")

    case_a = CaseDiagnostics(
        case_id="Case A",
        fault_mode=sim_a["fault_mode"],
        local_numerical_check_passed=bool(sim_a["local_numerical_check_passed"]),
        investigator_turn_complete=False,
        root_cause_identified=False,
        notes="Historical run terminated with an incomplete Case A investigator turn.",
    )
    case_b = CaseDiagnostics(
        case_id="Case B",
        fault_mode=sim_b["fault_mode"],
        local_numerical_check_passed=bool(sim_b["local_numerical_check_passed"]),
        investigator_turn_complete=True,
        root_cause_identified=True,
        notes="Case B investigator turn completed and identified obstructed_floor_damper.",
    )
    return evaluate_strict_pair_gate(case_a=case_a, case_b=case_b, benchmark_id="installation-v1")


def run_installation_pilot(simulate_complete_case_a: bool = False) -> dict[str, Any]:
    """Run the installation-v1 pilot.

    By default (`simulate_complete_case_a=False`), reproduces the exact historical audit
    record where Case A had an incomplete investigator turn (`strict_pair_gate_passed=False`).
    """
    lab = InstallationLab()
    sim_a = lab.simulate_case("Case A")
    sim_b = lab.simulate_case("Case B")

    if not simulate_complete_case_a:
        report = get_historical_installation_v1_record()
    else:
        case_a = CaseDiagnostics(
            case_id="Case A",
            fault_mode=sim_a["fault_mode"],
            local_numerical_check_passed=bool(sim_a["local_numerical_check_passed"]),
            investigator_turn_complete=True,
            root_cause_identified=True,
            notes="Synthetic complete override for Case A.",
        )
        case_b = CaseDiagnostics(
            case_id="Case B",
            fault_mode=sim_b["fault_mode"],
            local_numerical_check_passed=bool(sim_b["local_numerical_check_passed"]),
            investigator_turn_complete=True,
            root_cause_identified=True,
            notes="Case B investigator turn completed.",
        )
        report = evaluate_strict_pair_gate(case_a=case_a, case_b=case_b, benchmark_id="installation-v1")

    return {
        "pilot_id": "installation-v1",
        "lab_telemetry": {
            "case_a": sim_a,
            "case_b": sim_b,
        },
        "report": report.to_dict(),
    }
