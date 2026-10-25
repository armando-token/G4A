"""Public electrical and thermal physics kernel for BlackboxDC.

Provides deterministic equations for feeder resistance, voltage drop under probe
injection, thermal rise at steady state, feeder ampacity / current limits, and
safety evaluation of remediation actions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple


# Public physical constants for BlackboxDC
NOMINAL_VOLTAGE_V: float = 480.0
AMBIENT_TEMP_C: float = 24.0
THERMAL_RESISTANCE_C_PER_W: float = 0.085
MAX_SAFE_JUNCTION_TEMP_C: float = 85.0
MAX_SAFE_FEEDER_CURRENT_A: float = 125.0
DEFAULT_PROBE_DELTA_I_A: float = 20.0

# Canonical feeder nominal resistances (Ohms) and healthy baselines
NOMINAL_FEEDER_RESISTANCE_OHM: Dict[str, float] = {
    "F1": 0.015,
    "F2": 0.018,
    "F3": 0.018,
    "F4": 0.016,
}

# Degraded high-resistance joint anomaly resistance (Ohms)
DEGRADED_JOINT_RESISTANCE_OHM: float = 0.145


@dataclass(frozen=True)
class PhysicsNodeState:
    """Electrical and thermal operating state for a single node/feeder pair."""

    node_id: str
    feeder_id: str
    load_current_a: float
    resistance_ohm: float
    voltage_drop_v: float
    terminal_voltage_v: float
    joule_heating_w: float
    temperature_c: float
    max_safe_current_a: float


def compute_voltage_drop(current_a: float, resistance_ohm: float) -> float:
    """Compute steady-state DC/RMS single-line equivalent voltage drop V = I * R."""
    return round(float(current_a) * float(resistance_ohm), 4)


def compute_probe_voltage_drop(
    base_current_a: float,
    delta_current_a: float,
    resistance_ohm: float,
) -> Tuple[float, float, float]:
    """Compute voltage drops (v_base, v_probed, delta_v) for a probe injection.

    Returns:
        Tuple of (base_voltage_drop_v, probed_voltage_drop_v, delta_voltage_drop_v)
    """
    v_base = compute_voltage_drop(base_current_a, resistance_ohm)
    v_probed = compute_voltage_drop(base_current_a + delta_current_a, resistance_ohm)
    delta_v = round(v_probed - v_base, 4)
    return v_base, v_probed, delta_v


def infer_resistance_from_probe(delta_v: float, delta_current_a: float) -> float:
    """Infer effective feeder resistance R = dV / dI from probe measurement."""
    if abs(delta_current_a) < 1e-9:
        raise ValueError("delta_current_a must be non-zero")
    return round(float(delta_v) / float(delta_current_a), 4)


def compute_joule_heating_w(current_a: float, resistance_ohm: float) -> float:
    """Compute resistive dissipation P = I^2 * R in Watts."""
    return round((float(current_a) ** 2) * float(resistance_ohm), 4)


def compute_steady_state_temperature_c(
    current_a: float,
    resistance_ohm: float,
    ambient_temp_c: float = AMBIENT_TEMP_C,
    thermal_resistance_c_per_w: float = THERMAL_RESISTANCE_C_PER_W,
) -> float:
    """Compute steady-state terminal temperature T = T_amb + (I^2 * R) * R_th."""
    power_w = compute_joule_heating_w(current_a, resistance_ohm)
    return round(float(ambient_temp_c) + power_w * float(thermal_resistance_c_per_w), 2)


def compute_max_safe_current_a(
    resistance_ohm: float,
    ambient_temp_c: float = AMBIENT_TEMP_C,
    max_temp_c: float = MAX_SAFE_JUNCTION_TEMP_C,
    thermal_resistance_c_per_w: float = THERMAL_RESISTANCE_C_PER_W,
    ampacity_limit_a: float = MAX_SAFE_FEEDER_CURRENT_A,
) -> float:
    """Compute the maximum safe continuous current bounded by thermal and ampacity limits."""
    if resistance_ohm <= 0 or thermal_resistance_c_per_w <= 0:
        return round(float(ampacity_limit_a), 2)
    max_power_w = max(0.0, (max_temp_c - ambient_temp_c) / thermal_resistance_c_per_w)
    thermal_current_limit_a = (max_power_w / resistance_ohm) ** 0.5
    return round(min(float(ampacity_limit_a), thermal_current_limit_a), 2)


def evaluate_node_state(
    node_id: str,
    feeder_id: str,
    load_current_a: float,
    resistance_ohm: float,
    ambient_temp_c: float = AMBIENT_TEMP_C,
) -> PhysicsNodeState:
    """Evaluate complete physics state for a node connected to a feeder."""
    v_drop = compute_voltage_drop(load_current_a, resistance_ohm)
    v_term = round(NOMINAL_VOLTAGE_V - v_drop, 4)
    p_joule = compute_joule_heating_w(load_current_a, resistance_ohm)
    temp_c = compute_steady_state_temperature_c(
        load_current_a, resistance_ohm, ambient_temp_c=ambient_temp_c
    )
    max_i = compute_max_safe_current_a(resistance_ohm, ambient_temp_c=ambient_temp_c)
    return PhysicsNodeState(
        node_id=node_id,
        feeder_id=feeder_id,
        load_current_a=round(float(load_current_a), 2),
        resistance_ohm=round(float(resistance_ohm), 4),
        voltage_drop_v=v_drop,
        terminal_voltage_v=v_term,
        joule_heating_w=p_joule,
        temperature_c=temp_c,
        max_safe_current_a=max_i,
    )


def evaluate_remediation_safety(
    target_feeder_id: str,
    degraded_feeder_id: str,
    target_node_id: str,
    degraded_node_id: str,
    proposed_current_cap_a: float,
    true_max_safe_current_a: float,
    tolerance_a: float = 5.0,
) -> Dict[str, object]:
    """Deterministic safety check comparing proposed remediation against true physical limits."""
    feeder_match = target_feeder_id.strip().upper() == degraded_feeder_id.strip().upper()
    node_match = target_node_id.strip().upper() == degraded_node_id.strip().upper()
    cap_safe = 0.0 < proposed_current_cap_a <= (true_max_safe_current_a + tolerance_a)
    cap_error_a = round(abs(proposed_current_cap_a - true_max_safe_current_a), 2)
    is_safe = bool(feeder_match and node_match and cap_safe)
    return {
        "is_safe": is_safe,
        "feeder_match": feeder_match,
        "node_match": node_match,
        "cap_safe": cap_safe,
        "proposed_current_cap_a": round(float(proposed_current_cap_a), 2),
        "true_max_safe_current_a": round(float(true_max_safe_current_a), 2),
        "cap_error_a": cap_error_a,
    }
