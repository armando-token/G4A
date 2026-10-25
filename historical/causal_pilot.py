"""Predecessor causal pilot evaluating observational vs. interventional identification."""

from __future__ import annotations

from typing import Any

from historical.causal_fixture import build_cooling_dag_fixture


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
