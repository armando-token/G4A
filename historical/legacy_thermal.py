"""Predecessor datacenter rack thermal model and hotspot calculations."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class RackSpec:
    """Static physical specification for a legacy server rack."""

    rack_id: str
    base_power_kw: float = 12.0
    max_airflow_cfm: float = 1400.0
    recirculation_factor: float = 0.12
    thermal_mass_kj_per_c: float = 45.0


@dataclass(frozen=True)
class ThermalState:
    """Instantaneous thermal telemetry for a single rack."""

    rack_id: str
    inlet_temp_c: float
    exhaust_temp_c: float
    hotspot_temp_c: float
    airflow_cfm: float
    it_load_kw: float
    throttled: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_rack_hotspot(
    spec: RackSpec,
    supply_temp_c: float,
    it_load_kw: float,
    fan_duty: float,
    neighbor_exhaust_temp_c: float | None = None,
) -> ThermalState:
    """Compute steady-state inlet, exhaust, and peak hotspot temperatures for a rack.

    Uses the predecessor lumped-parameter energy balance with hot-aisle recirculation.
    """
    clamped_duty = max(0.10, min(1.0, float(fan_duty)))
    airflow_cfm = spec.max_airflow_cfm * clamped_duty
    # Volumetric air heat capacity approximation: ~0.00057 kW / (CFM * degC)
    effective_conductance_kw_per_c = max(0.15, airflow_cfm * 0.00057)
    delta_t_core = max(0.0, float(it_load_kw)) / effective_conductance_kw_per_c

    neighbor_temp = supply_temp_c + delta_t_core if neighbor_exhaust_temp_c is None else neighbor_exhaust_temp_c
    recirc_shift = spec.recirculation_factor * max(0.0, neighbor_temp - supply_temp_c)
    inlet_temp_c = round(supply_temp_c + recirc_shift, 3)
    exhaust_temp_c = round(inlet_temp_c + delta_t_core, 3)
    # Non-uniform slot stratification adds a quadratic hotspot penalty at low fan duty
    stratification_penalty = 2.5 * ((1.0 - clamped_duty) ** 2) + 0.15 * delta_t_core
    hotspot_temp_c = round(exhaust_temp_c + stratification_penalty, 3)
    throttled = hotspot_temp_c >= 42.0

    return ThermalState(
        rack_id=spec.rack_id,
        inlet_temp_c=inlet_temp_c,
        exhaust_temp_c=exhaust_temp_c,
        hotspot_temp_c=hotspot_temp_c,
        airflow_cfm=round(airflow_cfm, 2),
        it_load_kw=round(float(it_load_kw), 3),
        throttled=throttled,
    )


def simulate_thermal_step(
    prev_state: ThermalState,
    spec: RackSpec,
    supply_temp_c: float,
    it_load_kw: float,
    fan_duty: float,
    dt_seconds: float = 10.0,
) -> ThermalState:
    """Advance rack thermal state by one discrete time step with first-order relaxation."""
    target = compute_rack_hotspot(
        spec=spec,
        supply_temp_c=supply_temp_c,
        it_load_kw=it_load_kw,
        fan_duty=fan_duty,
        neighbor_exhaust_temp_c=prev_state.exhaust_temp_c,
    )
    tau_seconds = max(5.0, spec.thermal_mass_kj_per_c / max(1.0, it_load_kw * 0.25))
    alpha = min(1.0, max(0.05, dt_seconds / (tau_seconds + dt_seconds)))

    exhaust = round(prev_state.exhaust_temp_c + alpha * (target.exhaust_temp_c - prev_state.exhaust_temp_c), 3)
    hotspot = round(prev_state.hotspot_temp_c + alpha * (target.hotspot_temp_c - prev_state.hotspot_temp_c), 3)
    inlet = round(prev_state.inlet_temp_c + alpha * (target.inlet_temp_c - prev_state.inlet_temp_c), 3)

    return ThermalState(
        rack_id=spec.rack_id,
        inlet_temp_c=inlet,
        exhaust_temp_c=exhaust,
        hotspot_temp_c=hotspot,
        airflow_cfm=target.airflow_cfm,
        it_load_kw=target.it_load_kw,
        throttled=hotspot >= 42.0,
    )
