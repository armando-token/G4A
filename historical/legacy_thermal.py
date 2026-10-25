"""Predecessor datacenter rack thermal model, connector thermodynamics, and hotspot calculations.

Provides:
1. Predecessor rack thermal model (`RackSpec`, `ThermalState`, `compute_rack_hotspot`,
   `simulate_thermal_step`).
2. Phase 7A (`legacy_thermal.py`) compatibility adapter around `blackbox.domain` supporting
   both scalar `float` and `numpy.ndarray` inputs:
   - `ConnectorThermalParams`
   - `ThermalTrajectory`
   - `compute_joule_heating_w`
   - `compute_equilibrium_temp_c`
   - `compute_time_constant_s`
   - `step_temperature_c`
   - `simulate_connector_series`
   - `simulate_installation_thermodynamics`
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np

from blackbox.domain import step_temperature as _domain_step_temperature


def _validate_finite_scalar(val: Any, name: str) -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float, np.number)):
        raise ValueError(f"{name} must be a finite real number, got {val!r}")
    f = float(val)
    if not math.isfinite(f):
        raise ValueError(f"{name} must be finite, got {val!r}")
    return f


def _validate_finite_scalar_or_array(
    val: Union[float, int, np.ndarray],
    name: str,
) -> Tuple[bool, Union[float, np.ndarray]]:
    if isinstance(val, np.ndarray):
        if val.size == 0:
            raise ValueError(f"{name} array must not be empty")
        arr = np.asarray(val, dtype=np.float64)
        if not np.all(np.isfinite(arr)):
            raise ValueError(f"{name} array contains non-finite values")
        return True, arr
    return False, _validate_finite_scalar(val, name)


@dataclass(frozen=True)
class ConnectorThermalParams:
    """Lumped electro-thermal parameters for a connector or busbar termination."""

    r_ohm: float
    h_w_per_k: float
    c_j_per_k: float
    ambient_c: float = 25.0
    alpha_per_c: float = 0.0

    def __post_init__(self) -> None:
        r = _validate_finite_scalar(self.r_ohm, "r_ohm")
        h = _validate_finite_scalar(self.h_w_per_k, "h_w_per_k")
        c = _validate_finite_scalar(self.c_j_per_k, "c_j_per_k")
        amb = _validate_finite_scalar(self.ambient_c, "ambient_c")
        alpha = _validate_finite_scalar(self.alpha_per_c, "alpha_per_c")
        if r <= 0.0:
            raise ValueError(f"r_ohm must be > 0, got {r}")
        if h <= 0.0:
            raise ValueError(f"h_w_per_k must be > 0, got {h}")
        if c <= 0.0:
            raise ValueError(f"c_j_per_k must be > 0, got {c}")
        if alpha < 0.0:
            raise ValueError(f"alpha_per_c must be >= 0, got {alpha}")
        object.__setattr__(self, "r_ohm", r)
        object.__setattr__(self, "h_w_per_k", h)
        object.__setattr__(self, "c_j_per_k", c)
        object.__setattr__(self, "ambient_c", amb)
        object.__setattr__(self, "alpha_per_c", alpha)

    @property
    def time_constant_s(self) -> float:
        return compute_time_constant_s(self.c_j_per_k, self.h_w_per_k)

    def to_dict(self) -> Dict[str, float]:
        return {
            "r_ohm": self.r_ohm,
            "h_w_per_k": self.h_w_per_k,
            "c_j_per_k": self.c_j_per_k,
            "ambient_c": self.ambient_c,
            "alpha_per_c": self.alpha_per_c,
            "time_constant_s": self.time_constant_s,
        }


@dataclass(frozen=True)
class ThermalTrajectory:
    """Simulated connector electro-thermal trajectory across a multi-step load profile."""

    time_s: Tuple[float, ...]
    temps_c: Tuple[float, ...]
    currents_a: Tuple[float, ...]
    joule_heating_w: Tuple[float, ...]
    equilibrium_temp_c: Tuple[float, ...]
    time_constant_s: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "time_s": list(self.time_s),
            "temps_c": list(self.temps_c),
            "currents_a": list(self.currents_a),
            "joule_heating_w": list(self.joule_heating_w),
            "equilibrium_temp_c": list(self.equilibrium_temp_c),
            "time_constant_s": float(self.time_constant_s),
        }


def compute_joule_heating_w(
    current_a: Union[float, np.ndarray],
    r_ohm: Union[float, np.ndarray],
) -> Union[float, np.ndarray]:
    """Compute Joule heating power $P = I^2 R$ (W) for scalar or `np.ndarray` inputs."""
    is_arr_i, i_val = _validate_finite_scalar_or_array(current_a, "current_a")
    is_arr_r, r_val = _validate_finite_scalar_or_array(r_ohm, "r_ohm")

    if is_arr_r:
        if np.any(r_val < 0.0):  # type: ignore[operator]
            raise ValueError("r_ohm must be >= 0")
    else:
        if float(r_val) < 0.0:
            raise ValueError(f"r_ohm must be >= 0, got {r_val}")

    if is_arr_i:
        if np.any(i_val < 0.0):  # type: ignore[operator]
            raise ValueError("current_a must be >= 0")
    else:
        if float(i_val) < 0.0:
            raise ValueError(f"current_a must be >= 0, got {i_val}")

    if is_arr_i or is_arr_r:
        i_arr = np.asarray(i_val, dtype=np.float64)
        r_arr = np.asarray(r_val, dtype=np.float64)
        return np.asarray((i_arr * i_arr) * r_arr, dtype=np.float64)
    return float((float(i_val) ** 2) * float(r_val))


def compute_equilibrium_temp_c(
    current_a: Union[float, np.ndarray],
    r_ohm: Union[float, np.ndarray],
    h_w_per_k: Union[float, np.ndarray],
    ambient_c: float = 25.0,
) -> Union[float, np.ndarray]:
    """Compute steady-state equilibrium temperature $T_{\\text{eq}} = T_{\\text{amb}} + I^2 R / h$."""
    amb = _validate_finite_scalar(ambient_c, "ambient_c")
    is_arr_h, h_val = _validate_finite_scalar_or_array(h_w_per_k, "h_w_per_k")
    if is_arr_h:
        if np.any(h_val <= 0.0):  # type: ignore[operator]
            raise ValueError("h_w_per_k must be > 0")
    else:
        if float(h_val) <= 0.0:
            raise ValueError(f"h_w_per_k must be > 0, got {h_val}")

    q_w = compute_joule_heating_w(current_a, r_ohm)
    if isinstance(q_w, np.ndarray) or is_arr_h:
        q_arr = np.asarray(q_w, dtype=np.float64)
        h_arr = np.asarray(h_val, dtype=np.float64)
        return np.asarray(amb + q_arr / h_arr, dtype=np.float64)
    return float(amb + float(q_w) / float(h_val))


def compute_time_constant_s(c_j_per_k: float, h_w_per_k: float) -> float:
    """Compute thermal relaxation time constant $\\tau = C / h$ (s)."""
    c = _validate_finite_scalar(c_j_per_k, "c_j_per_k")
    h = _validate_finite_scalar(h_w_per_k, "h_w_per_k")
    if c <= 0.0:
        raise ValueError(f"c_j_per_k must be > 0, got {c}")
    if h <= 0.0:
        raise ValueError(f"h_w_per_k must be > 0, got {h}")
    return float(c / h)


def step_temperature_c(
    temp_c: Union[float, np.ndarray],
    current_a: Union[float, np.ndarray],
    params: ConnectorThermalParams,
    dt_s: float = 10.0,
) -> Union[float, np.ndarray]:
    """Advance connector temperature by `dt_s` seconds using `blackbox.domain.step_temperature`."""
    if not isinstance(params, ConnectorThermalParams):
        raise ValueError("params must be a ConnectorThermalParams instance")
    dt = _validate_finite_scalar(dt_s, "dt_s")
    if dt <= 0.0:
        raise ValueError(f"dt_s must be > 0, got {dt}")

    is_arr_t, t_val = _validate_finite_scalar_or_array(temp_c, "temp_c")
    is_arr_i, i_val = _validate_finite_scalar_or_array(current_a, "current_a")

    if is_arr_i:
        if np.any(i_val < 0.0):  # type: ignore[operator]
            raise ValueError("current_a must be >= 0")
    else:
        if float(i_val) < 0.0:
            raise ValueError(f"current_a must be >= 0, got {i_val}")

    if not is_arr_t and not is_arr_i:
        return float(
            _domain_step_temperature(
                t_c=float(t_val),
                i_a=float(i_val),
                ambient_c=params.ambient_c,
                r0_ohm=params.r_ohm,
                alpha=params.alpha_per_c,
                h_w_k=params.h_w_per_k,
                heat_capacity_j_k=params.c_j_per_k,
                dt_s=dt,
            )
        )

    t_arr, i_arr = np.broadcast_arrays(
        np.asarray(t_val, dtype=np.float64),
        np.asarray(i_val, dtype=np.float64),
    )
    flat_t = t_arr.ravel()
    flat_i = i_arr.ravel()
    out_flat = np.empty_like(flat_t, dtype=np.float64)
    for idx in range(flat_t.size):
        out_flat[idx] = _domain_step_temperature(
            t_c=float(flat_t[idx]),
            i_a=float(flat_i[idx]),
            ambient_c=params.ambient_c,
            r0_ohm=params.r_ohm,
            alpha=params.alpha_per_c,
            h_w_k=params.h_w_per_k,
            heat_capacity_j_k=params.c_j_per_k,
            dt_s=dt,
        )
    return out_flat.reshape(t_arr.shape)


def simulate_connector_series(
    initial_temp_c: float,
    currents_a: Union[Sequence[float], np.ndarray],
    params: ConnectorThermalParams,
    dt_s: float = 10.0,
) -> ThermalTrajectory:
    """Simulate a connector temperature trajectory across a multi-step load current series."""
    if not isinstance(params, ConnectorThermalParams):
        raise ValueError("params must be a ConnectorThermalParams instance")
    t0 = _validate_finite_scalar(initial_temp_c, "initial_temp_c")
    dt = _validate_finite_scalar(dt_s, "dt_s")
    if dt <= 0.0:
        raise ValueError(f"dt_s must be > 0, got {dt}")

    if isinstance(currents_a, np.ndarray):
        curr_seq = [float(x) for x in currents_a.tolist()]
    elif isinstance(currents_a, (list, tuple)):
        curr_seq = [_validate_finite_scalar(x, "currents_a[k]") for x in currents_a]
    else:
        raise ValueError("currents_a must be a sequence or numpy.ndarray of currents")

    if len(curr_seq) == 0:
        raise ValueError("currents_a must be non-empty")

    times: List[float] = [0.0]
    temps: List[float] = [t0]
    joule_w: List[float] = []
    eq_temps: List[float] = []
    curr_list: List[float] = []

    t_curr = t0
    for idx, i_a in enumerate(curr_seq):
        if i_a < 0.0:
            raise ValueError(f"currents_a[{idx}] must be >= 0, got {i_a}")
        r_eff = params.r_ohm * (1.0 + params.alpha_per_c * max(t_curr - params.ambient_c, 0.0))
        q_k = float(compute_joule_heating_w(i_a, r_eff))
        teq_k = float(compute_equilibrium_temp_c(i_a, r_eff, params.h_w_per_k, params.ambient_c))
        t_curr = float(step_temperature_c(t_curr, i_a, params, dt_s=dt))

        curr_list.append(float(i_a))
        joule_w.append(q_k)
        eq_temps.append(teq_k)
        times.append(round((idx + 1) * dt, 6))
        temps.append(t_curr)

    return ThermalTrajectory(
        time_s=tuple(times),
        temps_c=tuple(temps),
        currents_a=tuple(curr_list),
        joule_heating_w=tuple(joule_w),
        equilibrium_temp_c=tuple(eq_temps),
        time_constant_s=params.time_constant_s,
    )


def simulate_installation_thermodynamics(
    components: Mapping[str, ConnectorThermalParams],
    load_profiles_a: Mapping[str, Sequence[float]],
    initial_temps_c: Optional[Mapping[str, float]] = None,
    dt_s: float = 10.0,
) -> Dict[str, ThermalTrajectory]:
    """Simulate a dictionary of installation components across their load profiles."""
    if not isinstance(components, Mapping) or len(components) == 0:
        raise ValueError("components must be a non-empty mapping")
    if not isinstance(load_profiles_a, Mapping):
        raise ValueError("load_profiles_a must be a mapping")

    out: Dict[str, ThermalTrajectory] = {}
    for comp_id, params in components.items():
        if comp_id not in load_profiles_a:
            raise ValueError(f"Missing load profile for component {comp_id!r}")
        t0 = (
            float(initial_temps_c[comp_id])
            if initial_temps_c is not None and comp_id in initial_temps_c
            else params.ambient_c
        )
        out[comp_id] = simulate_connector_series(
            initial_temp_c=t0,
            currents_a=load_profiles_a[comp_id],
            params=params,
            dt_s=dt_s,
        )
    return out


# ---------------------------------------------------------------------------
# Predecessor Rack Thermal Model (Retained for Backward Compatibility)
# ---------------------------------------------------------------------------


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
