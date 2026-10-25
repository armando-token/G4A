"""Public electrical and thermal physics kernel for BlackboxDC.

Provides:
- Phase 1 typed domain contracts (`RackParams`, `PDU`, `Migration`, `World`, `Trajectory`),
- Exact discrete-time electro-thermal step kernel (`step_temperature`),
- Workload migration and 180s trajectory simulation (`apply_migration`, `simulate_world`),
- Probe resistance and ramp least-squares parameter identification
  (`infer_r0_from_voltage_drop`, `fit_thermal_parameters_from_ramp`),
- Legacy/auxiliary helper functions preserved for backward compatibility.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import exp, isfinite, log
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

# Public physical constants for BlackboxDC
NOMINAL_VOLTAGE_V: float = 480.0
AMBIENT_TEMP_C: float = 24.0
THERMAL_RESISTANCE_C_PER_W: float = 0.085
MAX_SAFE_JUNCTION_TEMP_C: float = 85.0
MAX_SAFE_FEEDER_CURRENT_A: float = 125.0
DEFAULT_PROBE_DELTA_I_A: float = 20.0

TOTAL_WORLD_CURRENT_A: float = 68.0
DEFAULT_HORIZON_S: int = 180
DEFAULT_STEP_S: int = 10
DEFAULT_MIGRATION_AMPS: float = 8.0
DEFAULT_MAX_TEMP_C: float = 90.0

# Canonical feeder nominal resistances (Ohms) and healthy baselines
NOMINAL_FEEDER_RESISTANCE_OHM: Dict[str, float] = {
    "F1": 0.015,
    "F2": 0.018,
    "F3": 0.018,
    "F4": 0.016,
}

# Degraded high-resistance joint anomaly resistance (Ohms)
DEGRADED_JOINT_RESISTANCE_OHM: float = 0.145


def _is_valid_number(value: Any) -> bool:
    """Return True if value is a finite int/float (excluding bool)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return isfinite(float(value))


def _validate_nonempty_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{field_name} must not contain leading/trailing whitespace")
    return value


@dataclass(frozen=True)
class RackParams:
    """Physical and electrical parameters for a single rack node."""

    rack_id: str
    pdu_id: str
    current_A: float
    initial_temp_C: float
    R0_ohm: float
    alpha_per_C: float
    h_W_per_K: float
    C_J_per_K: float
    max_current_A: float
    max_temp_C: float = DEFAULT_MAX_TEMP_C

    def __post_init__(self) -> None:
        _validate_nonempty_id(self.rack_id, "rack_id")
        _validate_nonempty_id(self.pdu_id, "pdu_id")

        numeric_fields = (
            ("current_A", self.current_A),
            ("initial_temp_C", self.initial_temp_C),
            ("R0_ohm", self.R0_ohm),
            ("alpha_per_C", self.alpha_per_C),
            ("h_W_per_K", self.h_W_per_K),
            ("C_J_per_K", self.C_J_per_K),
            ("max_current_A", self.max_current_A),
            ("max_temp_C", self.max_temp_C),
        )
        for name, val in numeric_fields:
            if not _is_valid_number(val):
                raise ValueError(f"nonfinite or invalid numeric field {name}")

        if self.current_A < 0:
            raise ValueError("current_A must be >= 0")
        if self.R0_ohm <= 0:
            raise ValueError("R0_ohm must be > 0")
        if self.alpha_per_C < 0:
            raise ValueError("alpha_per_C must be >= 0")
        if self.h_W_per_K <= 0:
            raise ValueError("h_W_per_K must be > 0")
        if self.C_J_per_K <= 0:
            raise ValueError("C_J_per_K must be > 0")
        if self.max_current_A <= 0:
            raise ValueError("max_current_A must be > 0")
        if self.max_temp_C <= AMBIENT_TEMP_C:
            raise ValueError("max_temp_C must be greater than ambient_C")


@dataclass(frozen=True)
class PDU:
    """Power distribution unit (feeder) specification."""

    pdu_id: str
    max_current_A: float

    def __post_init__(self) -> None:
        _validate_nonempty_id(self.pdu_id, "pdu_id")
        if not _is_valid_number(self.max_current_A):
            raise ValueError("nonfinite max_current_A")
        if self.max_current_A <= 0:
            raise ValueError("PDU max_current_A must be > 0")


@dataclass(frozen=True)
class Migration:
    """Workload current migration action between two racks."""

    source: str
    target: str
    amps: float = DEFAULT_MIGRATION_AMPS

    def __post_init__(self) -> None:
        _validate_nonempty_id(self.source, "source")
        _validate_nonempty_id(self.target, "target")
        if not _is_valid_number(self.amps):
            raise ValueError("nonfinite migration amps")
        if self.amps <= 0:
            raise ValueError("migration amps must be > 0")


@dataclass(frozen=True)
class World:
    """Complete electro-thermal world configuration for BlackboxDC."""

    racks: Tuple[RackParams, ...]
    pdus: Tuple[PDU, ...]
    ambient_C: float = AMBIENT_TEMP_C
    horizon_s: int = DEFAULT_HORIZON_S
    step_s: int = DEFAULT_STEP_S
    allowed_targets: Tuple[str, ...] = ("N2", "N3")

    def __post_init__(self) -> None:
        if not isinstance(self.racks, (list, tuple)) or len(self.racks) == 0:
            raise ValueError("racks must be a non-empty sequence of RackParams")
        if not isinstance(self.pdus, (list, tuple)) or len(self.pdus) == 0:
            raise ValueError("pdus must be a non-empty sequence of PDU")

        racks_tuple = tuple(self.racks)
        pdus_tuple = tuple(self.pdus)
        for r in racks_tuple:
            if not isinstance(r, RackParams):
                raise ValueError("all elements of racks must be RackParams instances")
        for p in pdus_tuple:
            if not isinstance(p, PDU):
                raise ValueError("all elements of pdus must be PDU instances")

        object.__setattr__(self, "racks", racks_tuple)
        object.__setattr__(self, "pdus", pdus_tuple)

        if not isinstance(self.allowed_targets, (list, tuple)) or len(self.allowed_targets) == 0:
            raise ValueError("allowed_targets must be a non-empty sequence of rack IDs")
        targets_tuple = tuple(_validate_nonempty_id(t, "allowed_target") for t in self.allowed_targets)
        object.__setattr__(self, "allowed_targets", targets_tuple)

        if not _is_valid_number(self.ambient_C):
            raise ValueError("nonfinite ambient_C")
        if (
            isinstance(self.horizon_s, bool)
            or not isinstance(self.horizon_s, (int, float))
            or not isfinite(float(self.horizon_s))
            or int(self.horizon_s) != self.horizon_s
        ):
            raise ValueError("invalid horizon_s")
        if (
            isinstance(self.step_s, bool)
            or not isinstance(self.step_s, (int, float))
            or not isfinite(float(self.step_s))
            or int(self.step_s) != self.step_s
        ):
            raise ValueError("invalid step_s")

        object.__setattr__(self, "horizon_s", int(self.horizon_s))
        object.__setattr__(self, "step_s", int(self.step_s))

        if self.step_s <= 0 or self.horizon_s <= 0:
            raise ValueError("horizon_s and step_s must be > 0")
        if self.horizon_s != DEFAULT_HORIZON_S or (self.horizon_s % self.step_s) != 0:
            raise ValueError("horizon_s must equal 180 and be divisible by step_s")

        rack_ids = [r.rack_id for r in racks_tuple]
        if len(rack_ids) != len(set(rack_ids)):
            raise ValueError("duplicate rack_id in World.racks")

        pdu_ids = [p.pdu_id for p in pdus_tuple]
        if len(pdu_ids) != len(set(pdu_ids)):
            raise ValueError("duplicate pdu_id in World.pdus")

        pdu_id_set = set(pdu_ids)
        rack_id_set = set(rack_ids)
        for r in racks_tuple:
            if r.pdu_id not in pdu_id_set:
                raise ValueError(f"rack {r.rack_id} references unknown pdu_id {r.pdu_id}")
            if r.max_temp_C <= self.ambient_C:
                raise ValueError(f"rack {r.rack_id} max_temp_C must exceed World.ambient_C")

        for t in targets_tuple:
            if t not in rack_id_set:
                raise ValueError(f"allowed_target {t} not found in World.racks")

        total_current = sum(r.current_A for r in racks_tuple)
        if abs(total_current - TOTAL_WORLD_CURRENT_A) >= 1e-9:
            raise ValueError(
                f"total current conservation violated: sum(current_A)={total_current}, expected {TOTAL_WORLD_CURRENT_A}"
            )

    def get_rack(self, rack_id: str) -> RackParams:
        for r in self.racks:
            if r.rack_id == rack_id:
                return r
        raise KeyError(f"Unknown rack_id: {rack_id}")

    def get_pdu(self, pdu_id: str) -> PDU:
        for p in self.pdus:
            if p.pdu_id == pdu_id:
                return p
        raise KeyError(f"Unknown pdu_id: {pdu_id}")


@dataclass(frozen=True)
class Trajectory:
    """Simulated multi-rack, multi-PDU electro-thermal trajectory over [0..horizon_s]."""

    time_s: List[int]
    temps_C: Dict[str, List[float]]
    currents_A: Dict[str, List[float]]
    pdu_currents_A: Dict[str, List[float]]
    safety_flags: Dict[str, Any]


def step_temperature(
    t_c: float,
    i_a: float,
    ambient_c: float,
    r0_ohm: float,
    alpha: float,
    h_w_k: float,
    heat_capacity_j_k: float,
    dt_s: float,
) -> float:
    """Advance rack temperature by one exact analytical step under piece-wise constant R(T_k)."""
    vals = (t_c, i_a, ambient_c, r0_ohm, alpha, h_w_k, heat_capacity_j_k, dt_s)
    if not all(_is_valid_number(x) for x in vals):
        raise ValueError("nonfinite physical parameter")
    if i_a < 0 or r0_ohm <= 0 or alpha < 0 or h_w_k <= 0 or heat_capacity_j_k <= 0 or dt_s <= 0:
        raise ValueError("invalid physical parameter")
    r = r0_ohm * (1 + alpha * max(t_c - ambient_c, 0))
    teq = ambient_c + i_a * i_a * r / h_w_k
    return teq + (t_c - teq) * exp(-h_w_k * dt_s / heat_capacity_j_k)


def apply_migration(world: World, migration: Migration) -> World:
    """Validate and apply an 8.0 A load migration from `migration.source` to `migration.target`."""
    if not isinstance(world, World):
        raise ValueError("world must be a World instance")
    if not isinstance(migration, Migration):
        raise ValueError("migration must be a Migration instance")

    if migration.source == migration.target:
        raise ValueError("migration source and target must be distinct")

    rack_map = {r.rack_id: r for r in world.racks}
    if migration.source not in rack_map:
        raise ValueError(f"migration source {migration.source!r} does not exist in world racks")
    if migration.target not in rack_map:
        raise ValueError(f"migration target {migration.target!r} does not exist in world racks")
    if migration.target not in world.allowed_targets:
        raise ValueError(
            f"migration target {migration.target!r} is not in allowed_targets {world.allowed_targets}"
        )

    if migration.amps <= 0 or abs(float(migration.amps) - DEFAULT_MIGRATION_AMPS) >= 1e-9:
        raise ValueError(f"migration amps must equal {DEFAULT_MIGRATION_AMPS} A")

    source_rack = rack_map[migration.source]
    target_rack = rack_map[migration.target]

    if source_rack.current_A + 1e-9 < migration.amps:
        raise ValueError(
            f"source rack {source_rack.rack_id} has insufficient current ({source_rack.current_A} A < {migration.amps} A)"
        )
    new_target_current = target_rack.current_A + migration.amps
    if new_target_current > target_rack.max_current_A + 1e-9:
        raise ValueError(
            f"target rack {target_rack.rack_id} current {new_target_current} A exceeds max_current_A {target_rack.max_current_A} A"
        )

    new_racks: List[RackParams] = []
    for r in world.racks:
        if r.rack_id == migration.source:
            new_racks.append(replace(r, current_A=float(r.current_A - migration.amps)))
        elif r.rack_id == migration.target:
            new_racks.append(replace(r, current_A=float(r.current_A + migration.amps)))
        else:
            new_racks.append(r)

    return replace(world, racks=tuple(new_racks))


def simulate_world(world: World, migration: Migration | None = None) -> Trajectory:
    """Simulate all racks and PDUs over `t = 0, step_s, ..., horizon_s` and evaluate safety."""
    if not isinstance(world, World):
        raise ValueError("world must be a World instance")

    active_world = apply_migration(world, migration) if migration is not None else world
    n_steps = active_world.horizon_s // active_world.step_s
    time_s: List[int] = [k * active_world.step_s for k in range(n_steps + 1)]

    temps_C: Dict[str, List[float]] = {}
    currents_A: Dict[str, List[float]] = {}

    for r in active_world.racks:
        rack_temps: List[float] = [float(r.initial_temp_C)]
        t_curr = float(r.initial_temp_C)
        for _ in range(n_steps):
            t_curr = step_temperature(
                t_c=t_curr,
                i_a=r.current_A,
                ambient_c=active_world.ambient_C,
                r0_ohm=r.R0_ohm,
                alpha=r.alpha_per_C,
                h_w_k=r.h_W_per_K,
                heat_capacity_j_k=r.C_J_per_K,
                dt_s=float(active_world.step_s),
            )
            rack_temps.append(float(t_curr))
        temps_C[r.rack_id] = rack_temps
        currents_A[r.rack_id] = [float(r.current_A)] * (n_steps + 1)

    pdu_currents_A: Dict[str, List[float]] = {}
    for p in active_world.pdus:
        total_pdu_current = float(
            sum(r.current_A for r in active_world.racks if r.pdu_id == p.pdu_id)
        )
        pdu_currents_A[p.pdu_id] = [total_pdu_current] * (n_steps + 1)

    thermal_violated_racks: List[str] = []
    current_violated_racks: List[str] = []
    violated_racks: List[str] = []

    for r in active_world.racks:
        has_thermal = any(t >= r.max_temp_C for t in temps_C[r.rack_id])
        has_rack_overcurrent = any(i > r.max_current_A + 1e-9 for i in currents_A[r.rack_id])
        if has_thermal:
            thermal_violated_racks.append(r.rack_id)
        if has_rack_overcurrent:
            current_violated_racks.append(r.rack_id)
        if has_thermal or has_rack_overcurrent:
            violated_racks.append(r.rack_id)

    violated_pdus: List[str] = []
    for p in active_world.pdus:
        if any(i > p.max_current_A + 1e-9 for i in pdu_currents_A[p.pdu_id]):
            violated_pdus.append(p.pdu_id)

    thermal_violation = len(thermal_violated_racks) > 0
    rack_overload = len(current_violated_racks) > 0
    pdu_overload = len(violated_pdus) > 0
    safe = not (thermal_violation or rack_overload or pdu_overload)

    if safe:
        verdict = "safe"
    else:
        reasons: List[str] = []
        if thermal_violation:
            reasons.append("thermal")
        for pdu_id in violated_pdus:
            reasons.append(f"{pdu_id} overload")
        for rack_id in current_violated_racks:
            reasons.append(f"{rack_id} overload")
        verdict = "unsafe, " + ", ".join(reasons)

    safety_flags: Dict[str, Any] = {
        "safe": safe,
        "thermal_violation": thermal_violation,
        "pdu_overload": pdu_overload,
        "rack_overload": rack_overload,
        "violated_racks": violated_racks,
        "thermal_violated_racks": thermal_violated_racks,
        "current_violated_racks": current_violated_racks,
        "violated_pdus": violated_pdus,
        "verdict": verdict,
        "max_temps_C": {rack_id: max(series) for rack_id, series in temps_C.items()},
        "max_pdu_currents_A": {pdu_id: max(series) for pdu_id, series in pdu_currents_A.items()},
    }

    return Trajectory(
        time_s=time_s,
        temps_C=temps_C,
        currents_A=currents_A,
        pdu_currents_A=pdu_currents_A,
        safety_flags=safety_flags,
    )


def infer_r0_from_voltage_drop(
    voltage_drop_v: float,
    current_a: float,
    temp_c: float,
    ambient_c: float = AMBIENT_TEMP_C,
    alpha_per_c: float = 0.004,
) -> float:
    """Infer baseline cold resistance R0 from voltage drop V = I * R0 * (1 + alpha * max(T - ambient, 0))."""
    vals = (voltage_drop_v, current_a, temp_c, ambient_c, alpha_per_c)
    if not all(_is_valid_number(x) for x in vals):
        raise ValueError("nonfinite parameter in infer_r0_from_voltage_drop")
    if voltage_drop_v <= 0 or current_a <= 0 or alpha_per_c < 0:
        raise ValueError("invalid parameter in infer_r0_from_voltage_drop")

    temp_factor = 1.0 + float(alpha_per_c) * max(float(temp_c) - float(ambient_c), 0.0)
    denom = float(current_a) * temp_factor
    if denom <= 0 or not isfinite(denom):
        raise ValueError("invalid denominator in infer_r0_from_voltage_drop")
    r0 = float(voltage_drop_v) / denom
    if r0 <= 0 or not isfinite(r0):
        raise ValueError("inferred R0 is non-positive or non-finite")
    return r0


def fit_thermal_parameters_from_ramp(
    times_s: Sequence[float],
    temps_c: Sequence[float],
    currents_a: Sequence[float],
    r0_ohm: float,
    ambient_c: float = AMBIENT_TEMP_C,
    alpha_per_c: float = 0.004,
    max_residual: float = 1e-2,
) -> Dict[str, float]:
    """Identify h_W_per_K and C_J_per_K from a temperature ramp via ordinary least squares.

    Fits:
        theta_{k+1} = a * theta_k + b * I_k^2 * (1 + alpha * max(theta_k, 0))
    where theta_k = T_k - ambient_c and dt = times_s[k+1] - times_s[k] == 10.
    Recovers:
        h_W_per_K = r0_ohm * (1 - a) / b
        C_J_per_K = -h_W_per_K * dt / log(a)
    """
    if not all(_is_valid_number(x) for x in (r0_ohm, ambient_c, alpha_per_c, max_residual)):
        raise ValueError("nonfinite scalar parameter in fit_thermal_parameters_from_ramp")
    if r0_ohm <= 0 or alpha_per_c < 0 or max_residual <= 0:
        raise ValueError("invalid scalar parameter in fit_thermal_parameters_from_ramp")

    if not (isinstance(times_s, (list, tuple)) and isinstance(temps_c, (list, tuple)) and isinstance(currents_a, (list, tuple))):
        times_s = list(times_s)
        temps_c = list(temps_c)
        currents_a = list(currents_a)

    n = len(times_s)
    if n < 3 or len(temps_c) != n or len(currents_a) != n:
        raise ValueError("times_s, temps_c, and currents_a must have matching length >= 3")

    for seq_name, seq in (("times_s", times_s), ("temps_c", temps_c), ("currents_a", currents_a)):
        if not all(_is_valid_number(v) for v in seq):
            raise ValueError(f"nonfinite value in {seq_name}")

    if any(float(i) <= 0 for i in currents_a):
        raise ValueError("all currents_a in ramp must be > 0")

    dts = [float(times_s[k + 1]) - float(times_s[k]) for k in range(n - 1)]
    if any(abs(dt - float(DEFAULT_STEP_S)) > 1e-9 for dt in dts):
        raise ValueError(f"all consecutive time steps must equal {DEFAULT_STEP_S}s")
    dt = float(DEFAULT_STEP_S)

    thetas = [float(t) - float(ambient_c) for t in temps_c]
    rows: List[List[float]] = []
    targets: List[float] = []
    for k in range(n - 1):
        theta_k = thetas[k]
        i_k = float(currents_a[k])
        heating_term = (i_k * i_k) * (1.0 + float(alpha_per_c) * max(theta_k, 0.0))
        rows.append([theta_k, heating_term])
        targets.append(thetas[k + 1])

    X = np.asarray(rows, dtype=np.float64)
    y = np.asarray(targets, dtype=np.float64)

    if np.linalg.matrix_rank(X) < 2:
        raise ValueError("singular design matrix in fit_thermal_parameters_from_ramp")
    cond = float(np.linalg.cond(X))
    if not isfinite(cond) or cond > 1e10:
        raise ValueError("ill-conditioned design matrix in fit_thermal_parameters_from_ramp")

    coeffs, _, rank, _ = np.linalg.lstsq(X, y, rcond=None)
    if rank < 2 or len(coeffs) != 2:
        raise ValueError("rank-deficient least-squares solution")

    a = float(coeffs[0])
    b = float(coeffs[1])
    if not (isfinite(a) and isfinite(b)):
        raise ValueError("nonfinite fitted coefficients")
    if a <= 0.0 or a >= 1.0 or b <= 0.0:
        raise ValueError(f"unphysical fitted coefficients a={a}, b={b}")

    residuals = np.abs(y - (X @ coeffs))
    max_res = float(np.max(residuals))
    if not isfinite(max_res) or max_res > float(max_residual):
        raise ValueError(
            f"fitted ramp residual {max_res:.6g} exceeds threshold {max_residual:.6g}"
        )

    h_w_per_k = float(r0_ohm) * (1.0 - a) / b
    c_j_per_k = -h_w_per_k * dt / log(a)
    if not (isfinite(h_w_per_k) and isfinite(c_j_per_k)) or h_w_per_k <= 0.0 or c_j_per_k <= 0.0:
        raise ValueError("recovered physical parameters are non-positive or non-finite")

    return {
        "a": a,
        "b": b,
        "h_W_per_K": h_w_per_k,
        "C_J_per_K": c_j_per_k,
        "max_residual": max_res,
    }


# ---------------------------------------------------------------------------
# Legacy / Auxiliary Helper Functions (retained for backward compatibility)
# ---------------------------------------------------------------------------


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


NodeElectricalState = PhysicsNodeState


def compute_voltage_drop(current_a: float, resistance_ohm: float) -> float:
    """Compute steady-state DC/RMS single-line equivalent voltage drop V = I * R."""
    return round(float(current_a) * float(resistance_ohm), 4)


def compute_probe_voltage_drop(
    base_current_a: float,
    delta_current_a: float,
    resistance_ohm: float,
) -> Tuple[float, float, float]:
    """Compute voltage drops (v_base, v_probed, delta_v) for a probe injection."""
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
