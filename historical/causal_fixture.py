"""Predecessor causal identification fixture and Phase 7B three-world electro-thermal ambiguity fixture.

Provides:
1. Predecessor cooling loop SCM fixture (`CausalFixture`, `build_cooling_dag_fixture`).
2. Phase 7B (`causal_fixture.py`) three-world electro-thermal causal disambiguation fixture:
   - `DEFAULT_CAUSAL_SEED = 20261024`
   - `VOLTAGE_NOISE_BOUND_V = 0.002`
   - `CAUSAL_HIDDEN_WORLDS` (`"electrical"`, `"cooling"`, `"outside_candidate"`)
   - `simulate_ramp_12_18_24a(world_key)`
   - `verify_thermal_trajectory_ambiguity()`
   - `build_causal_public_packet(seed)`
   - `build_causal_private_manifest(world_key, seed, noise_bound_v)`
   - `compute_candidate_voltage_intervals(...)`
   - `precommit_voltage_probe(proposal)`
   - `filter_supported_candidates(measured_voltage_v, intervals)`
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from blackbox.domain import step_temperature

DEFAULT_CAUSAL_SEED: int = 20261024
VOLTAGE_NOISE_BOUND_V: float = 0.002

CAUSAL_HIDDEN_WORLDS: Dict[str, Dict[str, float]] = {
    "electrical": {
        "r0_ohm": 0.040,
        "h_w_per_k": 0.40,
        "c_j_per_k": 8.0,
        "alpha_per_c": 0.004,
        "ambient_c": 25.0,
    },
    "cooling": {
        "r0_ohm": 0.020,
        "h_w_per_k": 0.20,
        "c_j_per_k": 4.0,
        "alpha_per_c": 0.004,
        "ambient_c": 25.0,
    },
    "outside_candidate": {
        "r0_ohm": 0.030,
        "h_w_per_k": 0.30,
        "c_j_per_k": 6.0,
        "alpha_per_c": 0.004,
        "ambient_c": 25.0,
    },
}

RAMP_CURRENTS_12_18_24A: Tuple[float, ...] = (
    12.0,
    12.0,
    12.0,
    12.0,
    18.0,
    18.0,
    18.0,
    18.0,
    24.0,
    24.0,
    24.0,
    24.0,
)


def _canonical_sha256(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def simulate_ramp_12_18_24a(world_key: str = "electrical") -> Dict[str, Any]:
    """Simulate the 12-step (`-120..0s`, `dt=10s`) `12A/18A/24A` ramp for a hidden world."""
    if world_key not in CAUSAL_HIDDEN_WORLDS:
        raise ValueError(
            f"Unsupported world_key {world_key!r}; expected one of {sorted(CAUSAL_HIDDEN_WORLDS)}"
        )
    spec = CAUSAL_HIDDEN_WORLDS[world_key]
    r0 = float(spec["r0_ohm"])
    h = float(spec["h_w_per_k"])
    c = float(spec["c_j_per_k"])
    alpha = float(spec["alpha_per_c"])
    amb = float(spec["ambient_c"])
    dt_s = 10.0

    times_s: List[float] = [-120.0]
    temps_c: List[float] = [amb]
    t_curr = amb
    for idx, i_a in enumerate(RAMP_CURRENTS_12_18_24A):
        t_curr = float(
            step_temperature(
                t_c=t_curr,
                i_a=float(i_a),
                ambient_c=amb,
                r0_ohm=r0,
                alpha=alpha,
                h_w_k=h,
                heat_capacity_j_k=c,
                dt_s=dt_s,
            )
        )
        times_s.append(-120.0 + (idx + 1) * dt_s)
        temps_c.append(t_curr)

    t0_temp = temps_c[-1]
    t0_current = float(RAMP_CURRENTS_12_18_24A[-1])
    r_at_t0 = r0 * (1.0 + alpha * max(t0_temp - amb, 0.0))
    exact_v_drop = t0_current * r_at_t0

    return {
        "world_key": world_key,
        "time_s": times_s,
        "currents_a": list(RAMP_CURRENTS_12_18_24A),
        "temps_c": temps_c,
        "t0_temp_c": t0_temp,
        "t0_current_a": t0_current,
        "exact_voltage_drop_v": exact_v_drop,
        "time_constant_s": c / h,
    }


def verify_thermal_trajectory_ambiguity() -> Dict[str, Any]:
    """Verify that all 3 worlds produce identical temperature ramps but distinct voltage drops."""
    sims = {wk: simulate_ramp_12_18_24a(wk) for wk in ("electrical", "cooling", "outside_candidate")}
    ref_temps = sims["electrical"]["temps_c"]
    max_diff = 0.0
    for wk in ("cooling", "outside_candidate"):
        for a, b in zip(ref_temps, sims[wk]["temps_c"]):
            d = abs(float(a) - float(b))
            if d > max_diff:
                max_diff = d

    v_drops = {wk: round(float(sims[wk]["exact_voltage_drop_v"]), 6) for wk in sims}
    return {
        "worlds": list(sims.keys()),
        "max_temp_diff_across_worlds_c": max_diff,
        "trajectories_identical": bool(max_diff < 1e-12),
        "voltage_drops_v": v_drops,
        "voltage_drops_distinct": len(set(v_drops.values())) == 3,
    }


def compute_candidate_voltage_intervals(
    current_a: float = 24.0,
    temp_c: Optional[float] = None,
    alpha_per_c: float = 0.004,
    ambient_c: float = 25.0,
    noise_bound_v: float = VOLTAGE_NOISE_BOUND_V,
    candidates: Sequence[str] = ("electrical", "cooling"),
) -> Dict[str, Tuple[float, float]]:
    """Compute bounded `[v_min, v_max]` voltage-drop intervals for candidate hypotheses."""
    if noise_bound_v < 0.0:
        raise ValueError(f"noise_bound_v must be >= 0, got {noise_bound_v}")
    active_temp = (
        float(temp_c)
        if temp_c is not None
        else float(simulate_ramp_12_18_24a("electrical")["t0_temp_c"])
    )
    factor = 1.0 + float(alpha_per_c) * max(active_temp - float(ambient_c), 0.0)
    intervals: Dict[str, Tuple[float, float]] = {}
    for cand in candidates:
        if cand not in CAUSAL_HIDDEN_WORLDS:
            raise ValueError(f"Unknown candidate hypothesis {cand!r}")
        r0 = float(CAUSAL_HIDDEN_WORLDS[cand]["r0_ohm"])
        exact_v = float(current_a) * r0 * factor
        intervals[cand] = (
            round(exact_v - float(noise_bound_v), 6),
            round(exact_v + float(noise_bound_v), 6),
        )
    return intervals


def build_causal_public_packet(seed: int = DEFAULT_CAUSAL_SEED) -> Dict[str, Any]:
    """Build the public evidence packet for the 3-world causal disambiguation experiment."""
    ramp = simulate_ramp_12_18_24a("electrical")
    intervals = compute_candidate_voltage_intervals(
        current_a=ramp["t0_current_a"],
        temp_c=ramp["t0_temp_c"],
        noise_bound_v=VOLTAGE_NOISE_BOUND_V,
        candidates=("electrical", "cooling"),
    )
    return {
        "schema_version": "causal-v1",
        "seed": int(seed),
        "ambient_c": 25.0,
        "alpha_per_c": 0.004,
        "dt_s": 10.0,
        "voltage_noise_bound_v": VOLTAGE_NOISE_BOUND_V,
        "ramp": {
            "time_s": ramp["time_s"],
            "currents_a": ramp["currents_a"],
            "temps_c": ramp["temps_c"],
            "t0_temp_c": ramp["t0_temp_c"],
            "t0_current_a": ramp["t0_current_a"],
        },
        "candidate_hypotheses": {
            "electrical": {
                "r0_ohm": 0.040,
                "h_w_per_k": 0.40,
                "c_j_per_k": 8.0,
                "voltage_interval_v": list(intervals["electrical"]),
            },
            "cooling": {
                "r0_ohm": 0.020,
                "h_w_per_k": 0.20,
                "c_j_per_k": 4.0,
                "voltage_interval_v": list(intervals["cooling"]),
            },
        },
        "probe_menu": ["voltage_drop", "thermal_recheck"],
    }


def _deterministic_noise_v(world_key: str, seed: int, noise_bound_v: float) -> float:
    if noise_bound_v == 0.0:
        return 0.0
    digest = hashlib.sha256(f"{world_key}:{seed}".encode("utf-8")).digest()
    u = int.from_bytes(digest[:8], "big") / float(1 << 64)  # in [0, 1)
    # Map to [-0.5 * noise_bound_v, +0.5 * noise_bound_v] strictly inside [-noise_bound_v, +noise_bound_v]
    return round((u - 0.5) * float(noise_bound_v), 6)


def build_causal_private_manifest(
    world_key: str = "electrical",
    seed: int = DEFAULT_CAUSAL_SEED,
    noise_bound_v: float = VOLTAGE_NOISE_BOUND_V,
) -> Dict[str, Any]:
    """Build the sealed private truth manifest for one of the three hidden worlds."""
    ramp = simulate_ramp_12_18_24a(world_key)
    noise_v = _deterministic_noise_v(world_key, int(seed), float(noise_bound_v))
    exact_v = float(ramp["exact_voltage_drop_v"])
    measured_v = round(exact_v + noise_v, 6)
    return {
        "schema_version": "causal-private-v1",
        "world_key": world_key,
        "seed": int(seed),
        "true_parameters": copy.deepcopy(CAUSAL_HIDDEN_WORLDS[world_key]),
        "exact_voltage_drop_v": round(exact_v, 6),
        "noise_v": noise_v,
        "noise_bound_v": float(noise_bound_v),
        "measured_voltage_drop_v": measured_v,
        "t0_temp_c": ramp["t0_temp_c"],
        "t0_current_a": ramp["t0_current_a"],
    }


def precommit_voltage_probe(proposal: Mapping[str, Any]) -> str:
    """Validate a researcher probe proposal and return its canonical SHA-256 commitment hash."""
    if not isinstance(proposal, Mapping):
        raise ValueError("proposal must be a mapping")
    if "probe_type" not in proposal:
        raise ValueError("proposal must specify 'probe_type'")
    if "candidate_intervals" not in proposal and "hypotheses" not in proposal:
        raise ValueError("proposal must include 'candidate_intervals' or 'hypotheses'")
    return _canonical_sha256(dict(proposal))


def filter_supported_candidates(
    measured_voltage_v: float,
    intervals: Mapping[str, Sequence[float]],
) -> List[str]:
    """Return sorted candidate IDs whose `[low_v, high_v]` interval contains `measured_voltage_v`."""
    v = float(measured_voltage_v)
    supported: List[str] = []
    for cand, bounds in intervals.items():
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
            raise ValueError(f"Invalid interval for {cand!r}: {bounds!r}")
        lo, hi = float(bounds[0]), float(bounds[1])
        if lo - 1e-9 <= v <= hi + 1e-9:
            supported.append(str(cand))
    return sorted(supported)


# ---------------------------------------------------------------------------
# Predecessor Cooling Loop SCM Fixture (Retained for Backward Compatibility)
# ---------------------------------------------------------------------------


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
