"""Single measured voltage-drop probe interface for BlackboxDC.

Executes a single bounded test current pulse against a requested node, returning
only the physically observable terminal voltage drop without exposing the full
sealed world truth dictionary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from blackbox.contracts import ProbeRequest, ProbeResponse
from blackbox.domain import (
    compute_probe_voltage_drop,
     compute_steady_state_temperature_c,
    infer_resistance_from_probe,
)
from blackbox.fixture import SealedWorldTruth


class SingleShotProbeSession:
    """Enforces that at most one voltage-drop probe measurement is taken per case run."""

    def __init__(self, truth: SealedWorldTruth, ambient_temp_c: float = 24.0) -> None:
        self._truth = truth
        self._ambient_temp_c = ambient_temp_c
        self._executed: bool = False
        self._response: Optional[ProbeResponse] = None

    @property
    def executed(self) -> bool:
        return self._executed

    @property
    def last_response(self) -> Optional[ProbeResponse]:
        return self._response

    def execute_probe(
        self,
        request: ProbeRequest,
        timestamp_utc: Optional[str] = None,
    ) -> ProbeResponse:
        """Execute the single allowed voltage-drop probe on the target node."""
        if self._executed:
            raise RuntimeError(
                f"Probe budget exhausted for {self._truth.case_id}: only one probe is permitted."
            )

        node_id = request.target_node
        if node_id not in self._truth.node_states:
            raise ValueError(f"Unknown target_node: {node_id!r}")

        node_state = self._truth.node_states[node_id]
        base_i = float(node_state["load_current_a"])
        r_true = float(self._truth.node_resistances_ohm[node_id])
        delta_i = float(request.delta_current_a)

        v_base, v_probed, delta_v = compute_probe_voltage_drop(
            base_current_a=base_i,
            delta_current_a=delta_i,
            resistance_ohm=r_true,
        )
        r_inferred = infer_resistance_from_probe(delta_v=delta_v, delta_current_a=delta_i)
        temp_c = compute_steady_state_temperature_c(
            current_a=base_i,
            resistance_ohm=r_true,
            ambient_temp_c=self._ambient_temp_c,
        )
        ts = timestamp_utc or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        response = ProbeResponse(
            case_id=self._truth.case_id,
            target_node=node_id,
            connected_feeder_in_sld=self._truth.node_to_feeder[node_id],
            base_current_a=base_i,
            delta_current_a=delta_i,
            base_voltage_drop_v=v_base,
            probed_voltage_drop_v=v_probed,
            delta_voltage_drop_v=delta_v,
            inferred_resistance_ohm=r_inferred,
            terminal_temperature_c=temp_c,
            timestamp_utc=ts,
        )
        self._executed = True
        self._response = response
        return response
