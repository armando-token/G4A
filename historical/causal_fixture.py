"""Predecessor causal identification fixture for cooling loop confounding vs. intervention."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class CausalFixture:
    """Structural causal model (SCM) fixture for rack cooling telemetry."""

    fixture_id: str
    nodes: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]
    confounder: str
    treatment: str
    outcome: str
    ground_truth_ate: float
    naive_observational_diff: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def intervene_fan_duty(self, delta_duty: float, workload_index: float = 0.5) -> dict[str, float]:
        """Compute counterfactual outcome under do(fan_duty = base + delta_duty).

        Under do-intervention, the incoming edge from workload_index -> fan_duty is severed.
        """
        base_temp = 31.0 + 9.0 * float(workload_index)
        causal_effect = self.ground_truth_ate * float(delta_duty)
        counterfactual_temp = round(base_temp + causal_effect, 4)
        return {
            "delta_duty": round(float(delta_duty), 4),
            "workload_index": round(float(workload_index), 4),
            "expected_delta_temp_c": round(causal_effect, 4),
            "counterfactual_hotspot_c": counterfactual_temp,
        }


def build_cooling_dag_fixture() -> CausalFixture:
    """Construct the canonical predecessor cooling loop SCM fixture.

    In observational logs, high `workload_index` triggers automated fan ramp-up (`fan_duty`),
    making higher `fan_duty` positively correlated with higher `hotspot_temp_c` (+4.2 C/unit)
    even though the true causal effect of `do(fan_duty)` on `hotspot_temp_c` is negative (-12.5 C/unit).
    """
    return CausalFixture(
        fixture_id="cooling-confounded-scm-v1",
        nodes=("workload_index", "fan_duty", "hotspot_temp_c"),
        edges=(
            ("workload_index", "fan_duty"),
            ("workload_index", "hotspot_temp_c"),
            ("fan_duty", "hotspot_temp_c"),
        ),
        confounder="workload_index",
        treatment="fan_duty",
        outcome="hotspot_temp_c",
        ground_truth_ate=-12.5,
        naive_observational_diff=4.2,
    )
