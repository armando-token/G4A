"""Predecessor thermal pilot simulation."""

from __future__ import annotations

from typing import Any

from historical.legacy_thermal import (
    RackSpec,
    ThermalState,
    compute_rack_hotspot,
    simulate_thermal_step,
)


def run_legacy_pilot(
    steps: int = 8,
    supply_temp_c: float = 18.5,
    fan_duty: float = 0.65,
) -> dict[str, Any]:
    """Run the deterministic predecessor 3-rack thermal pilot trajectory."""
    racks = [
        RackSpec(rack_id="RACK-A1", base_power_kw=11.0, recirculation_factor=0.08),
        RackSpec(rack_id="RACK-A2", base_power_kw=15.5, recirculation_factor=0.16),
        RackSpec(rack_id="RACK-A3", base_power_kw=13.0, recirculation_factor=0.11),
    ]

    states: dict[str, ThermalState] = {
        spec.rack_id: compute_rack_hotspot(
            spec=spec,
            supply_temp_c=supply_temp_c,
            it_load_kw=spec.base_power_kw,
            fan_duty=fan_duty,
        )
        for spec in racks
    }

    history: list[dict[str, Any]] = []
    for step_idx in range(steps):
        # Deterministic workload surge on RACK-A2 mid-trajectory
        step_snapshot: dict[str, Any] = {"step": step_idx, "racks": {}}
        for spec in racks:
            surge = 3.5 if (spec.rack_id == "RACK-A2" and step_idx >= steps // 2) else 0.0
            next_state = simulate_thermal_step(
                prev_state=states[spec.rack_id],
                spec=spec,
                supply_temp_c=supply_temp_c,
                it_load_kw=spec.base_power_kw + surge,
                fan_duty=fan_duty,
            )
            states[spec.rack_id] = next_state
            step_snapshot["racks"][spec.rack_id] = next_state.to_dict()
        history.append(step_snapshot)

    peak_rack = max(states.values(), key=lambda s: s.hotspot_temp_c)
    return {
        "pilot_id": "legacy-thermal-pilot-v0",
        "steps": steps,
        "peak_rack_id": peak_rack.rack_id,
        "peak_hotspot_temp_c": peak_rack.hotspot_temp_c,
        "any_throttled": any(s.throttled for s in states.values()),
        "final_states": {k: v.to_dict() for k, v in states.items()},
        "history": history,
    }
