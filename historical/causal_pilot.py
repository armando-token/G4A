"""Predecessor causal pilot and Phase 7B three-world causal disambiguation experiment runner.

Provides:
1. Predecessor cooling loop SCM pilot (`run_causal_pilot`).
2. Phase 7B (`causal_pilot.py`) three-world electro-thermal causal experiment runner:
   - `run_three_world_causal_experiment(...)`
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from historical.causal_fixture import (
    DEFAULT_CAUSAL_SEED,
    VOLTAGE_NOISE_BOUND_V,
    build_causal_private_manifest,
    build_causal_public_packet,
     build_cooling_dag_fixture,
    compute_candidate_voltage_intervals,
    filter_supported_candidates,
    precommit_voltage_probe,
)


def run_three_world_causal_experiment(
    world_key: str = "electrical",
    *,
    seed: int = DEFAULT_CAUSAL_SEED,
    probe_type: str = "voltage_drop",
    noise_bound_v: float = VOLTAGE_NOISE_BOUND_V,
    mutate_commitment: bool = False,
    echo_auditor: bool = False,
    missing_evidence: bool = False,
    budget_exhausted: bool = False,
    interrupted_turn: bool = False,
) -> Dict[str, Any]:
    """Run the 3-world causal disambiguation protocol with precommitment and blind auditor checks."""
    pub = build_causal_public_packet(seed=seed)
    t0_curr = float(pub["ramp"]["t0_current_a"])
    t0_temp = float(pub["ramp"]["t0_temp_c"])

    # 1. Researcher precommitment prior to probe
    intervals = compute_candidate_voltage_intervals(
        current_a=t0_curr,
        temp_c=t0_temp,
        noise_bound_v=noise_bound_v,
        candidates=("electrical", "cooling"),
    )
    proposal: Dict[str, Any] = {
        "probe_type": str(probe_type),
        "candidate_intervals": {k: list(v) for k, v in intervals.items()},
        "hypotheses": ["electrical", "cooling"],
    }
    locked_commit_hash = precommit_voltage_probe(proposal)

    # 2. Execute single diagnostic probe against sealed world
    priv = build_causal_private_manifest(
        world_key=world_key,
        seed=seed,
        noise_bound_v=noise_bound_v,
    )

    if missing_evidence:
        probe_result: Dict[str, Any] = {
            "probe_type": str(probe_type),
            "measured_voltage_v": None,
            "missing_evidence": True,
        }
        supported_candidates: List[str] = []
        identifiable = False
        conclusion = "abstain_missing_evidence"
        reason = "missing_probe_evidence"
    elif probe_type != "voltage_drop":
        probe_result = {
            "probe_type": str(probe_type),
            "measured_temp_c": t0_temp,
            "measured_voltage_v": None,
            "missing_evidence": False,
        }
        supported_candidates = ["electrical", "cooling"]
        identifiable = False
        conclusion = "ambiguous_uninformative_probe"
        reason = "uninformative_probe_type"
    else:
        measured_v = float(priv["measured_voltage_drop_v"])
        probe_result = {
            "probe_type": "voltage_drop",
            "measured_voltage_v": measured_v,
            "noise_bound_v": float(noise_bound_v),
            "missing_evidence": False,
        }
        supported_candidates = filter_supported_candidates(measured_v, intervals)
        if len(supported_candidates) == 1:
            identifiable = True
            conclusion = supported_candidates[0]
            reason = "unique_candidate_identified"
        elif len(supported_candidates) > 1:
            identifiable = False
            conclusion = "ambiguous_interval_overlap"
            reason = "interval_overlap"
        else:
            identifiable = False
            conclusion = "outside_supplied_hypotheses"
            reason = "outside_candidate_intervals"

    # 3. Verify precommitment integrity
    claimed_proposal = copy.deepcopy(proposal)
    if mutate_commitment:
        claimed_proposal["probe_type"] = "mutated_post_hoc_probe"
    claimed_hash = precommit_voltage_probe(claimed_proposal)
    commitment_mutated = claimed_hash != locked_commit_hash
    commitment_valid = not commitment_mutated

    # 4. Blind Auditor verification (zero private truth access)
    auditor_packet: Dict[str, Any] = {
        "commit_hash": locked_commit_hash,
        "claimed_proposal": claimed_proposal,
        "probe_result": copy.deepcopy(probe_result),
        "researcher_conclusion": conclusion,
        "supported_candidates": list(supported_candidates),
    }

    if echo_auditor:
        auditor_independent = False
        auditor_recomputed_intervals: Optional[Dict[str, Any]] = None
        auditor_supported: Optional[List[str]] = None
        auditor_passed = False
        auditor_reason = "echo_auditor_rejected_no_independent_recomputation"
    else:
        auditor_independent = True
        auditor_recomputed_intervals = {
            k: list(v)
            for k, v in compute_candidate_voltage_intervals(
                current_a=t0_curr,
                temp_c=t0_temp,
                noise_bound_v=noise_bound_v,
                candidates=("electrical", "cooling"),
            ).items()
        }
        if not commitment_valid:
            auditor_supported = []
            auditor_passed = False
            auditor_reason = "commitment_hash_mismatch"
        elif missing_evidence:
            auditor_supported = []
            auditor_passed = False
            auditor_reason = "missing_probe_evidence"
        elif probe_type != "voltage_drop":
            auditor_supported = ["electrical", "cooling"]
            auditor_passed = False
            auditor_reason = "uninformative_probe_rejected"
        else:
            auditor_supported = filter_supported_candidates(
                float(probe_result["measured_voltage_v"]),
                auditor_recomputed_intervals,
            )
            if len(auditor_supported) == 1 and conclusion == auditor_supported[0]:
                auditor_passed = True
                auditor_reason = "verified_unique_candidate"
            elif len(auditor_supported) == 0 and conclusion == "outside_supplied_hypotheses":
                auditor_passed = True
                auditor_reason = "verified_outside_supplied_hypotheses"
            else:
                auditor_passed = False
                auditor_reason = "ambiguous_or_unsupported_conclusion"

    # 5. Operational vs scientific separation & derived recovery dossier
    outside_world_acknowledged = bool(
        world_key == "outside_candidate" and conclusion == "outside_supplied_hypotheses"
    )
    model_correctness = bool(
        commitment_valid
        and auditor_independent
        and not missing_evidence
        and (
            (world_key in {"electrical", "cooling"} and conclusion == world_key and identifiable)
            or outside_world_acknowledged
        )
    )

    if interrupted_turn or budget_exhausted:
        event_status = "incomplete_interrupted" if interrupted_turn else "incomplete_budget_exhausted"
        operational_complete = False
        token_ledger = {
            "noncached_tokens": 280_000 if budget_exhausted else 30_000,
            "reserve_held_tokens": 30_000 if interrupted_turn else 0,
            "budget_exhausted": bool(budget_exhausted),
            "interrupted_turn": bool(interrupted_turn),
        }
        recovery_dossier: Optional[Dict[str, Any]] = {
            "derived_from_partial_artifacts": True,
            "fabricated_completed_turn": False,
            "event_status": event_status,
            "preserved_commit_hash": locked_commit_hash,
            "preserved_conclusion": conclusion,
        }
    else:
        event_status = "completed"
        operational_complete = True
        token_ledger = {
            "noncached_tokens": 4200,
            "reserve_held_tokens": 0,
            "budget_exhausted": False,
            "interrupted_turn": False,
        }
        recovery_dossier = None

    strict_pass = bool(model_correctness and operational_complete and auditor_passed)
    return {
        "world_key": world_key,
        "seed": int(seed),
        "probe_type": str(probe_type),
        "noise_bound_v": float(noise_bound_v),
        "commit_hash": locked_commit_hash,
        "commitment_valid": commitment_valid,
        "commitment_mutated": commitment_mutated,
        "candidate_intervals": {k: list(v) for k, v in intervals.items()},
        "probe_result": probe_result,
        "supported_candidates": supported_candidates,
        "identifiable": identifiable,
        "conclusion": conclusion,
        "outside_world_acknowledged": outside_world_acknowledged,
        "reason": reason,
        "auditor_packet": auditor_packet,
        "auditor": {
            "independent": auditor_independent,
            "recomputed_intervals": auditor_recomputed_intervals,
            "supported_candidates": auditor_supported,
            "passed": auditor_passed,
            "reason": auditor_reason,
        },
        "event_status": event_status,
        "operational_complete": operational_complete,
        "model_correctness": model_correctness,
        "token_ledger": token_ledger,
        "recovery_dossier": recovery_dossier,
        "strict_pass": strict_pass,
    }


# ---------------------------------------------------------------------------
# Predecessor Cooling Loop SCM Pilot (Retained for Backward Compatibility)
# ---------------------------------------------------------------------------


def run_causal_pilot(delta_duty: float = 0.20) -> dict[str, Any]:
    """Run the predecessor causal identification pilot on the cooling SCM fixture."""
    fixture = build_cooling_dag_fixture()
    intervention = fixture.intervene_fan_duty(delta_duty=delta_duty, workload_index=0.6)
    sign_reversal_detected = (fixture.naive_observational_diff > 0) and (fixture.ground_truth_ate < 0)

    return {
        "pilot_id": "causal-identification-pilot-v1",
        "fixture": fixture.to_dict(),
        "intervention": intervention,
        "simpsons_sign_reversal_detected": sign_reversal_detected,
        "backdoor_adjustment_set": [fixture.confounder],
        "status": "PASS" if sign_reversal_detected else "FAIL",
    }
