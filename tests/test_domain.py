"""Unit tests for the BlackboxDC public electrical and thermal physics kernel."""

from __future__ import annotations

from dataclasses import replace
import math
from typing import List, Tuple

import pytest

from blackbox.domain import (
    AMBIENT_TEMP_C,
    DEFAULT_HORIZON_S,
    DEFAULT_MIGRATION_AMPS,
    DEFAULT_PROBE_DELTA_I_A,
    DEFAULT_STEP_S,
    DEGRADED_JOINT_RESISTANCE_OHM,
    MAX_SAFE_FEEDER_CURRENT_A,
    MAX_SAFE_JUNCTION_TEMP_C,
    NOMINAL_FEEDER_RESISTANCE_OHM,
    NOMINAL_VOLTAGE_V,
    THERMAL_RESISTANCE_C_PER_W,
    TOTAL_WORLD_CURRENT_A,
    Migration,
    PDU,
    PhysicsNodeState,
    RackParams,
    Trajectory,
    World,
    apply_migration,
    compute_joule_heating_w,
    compute_max_safe_current_a,
    compute_probe_voltage_drop,
    compute_steady_state_temperature_c,
    compute_voltage_drop,
    evaluate_node_state,
    evaluate_remediation_safety,
    fit_thermal_parameters_from_ramp,
    infer_r0_from_voltage_drop,
    infer_resistance_from_probe,
    simulate_world,
    step_temperature,
)


def _generate_n4_ramp() -> Tuple[List[int], List[float], List[float]]:
    """Generate the canonical 13-sample N4 pre-incident ramp from t=-120s to t=0s."""
    r0 = 0.04
    alpha = 0.004
    h = 0.4
    c = 8.0
    ambient = 24.0
    dt = 10.0

    # 12 steps: 4 steps at 12A, 4 steps at 18A, 4 steps at 24A
    step_currents = [12.0] * 4 + [18.0] * 4 + [24.0] * 4
    times_s: List[int] = [-120 + 10 * k for k in range(13)]
    temps_c: List[float] = [34.0]
    for i_a in step_currents:
        t_next = step_temperature(
            t_c=temps_c[-1],
            i_a=i_a,
            ambient_c=ambient,
            r0_ohm=r0,
            alpha=alpha,
            h_w_k=h,
            heat_capacity_j_k=c,
            dt_s=dt,
        )
        temps_c.append(t_next)
    currents_a: List[float] = step_currents + [24.0]
    return times_s, temps_c, currents_a


def _build_reference_world(case_id: str = "A") -> World:
    """Construct canonical Phase 1 World A or World B."""
    _, n4_ramp_temps, _ = _generate_n4_ramp()
    n4_t0 = n4_ramp_temps[-1]

    # Case A: F1 -> N1, N2; F2 -> N3, N4
    # Case B: F1 -> N1, N3; F2 -> N2, N4
    n2_pdu = "F1" if case_id == "A" else "F2"
    n3_pdu = "F2" if case_id == "A" else "F1"

    racks = (
        RackParams(
            rack_id="N1",
            pdu_id="F1",
            current_A=36.0,
            initial_temp_C=36.0,
            R0_ohm=0.005,
            alpha_per_C=0.004,
            h_W_per_K=0.5,
            C_J_per_K=8.0,
            max_current_A=40.0,
            max_temp_C=90.0,
        ),
        RackParams(
            rack_id="N2",
            pdu_id=n2_pdu,
            current_A=4.0,
            initial_temp_C=25.0,
            R0_ohm=0.010,
            alpha_per_C=0.004,
            h_W_per_K=0.5,
            C_J_per_K=8.0,
            max_current_A=16.0,
            max_temp_C=90.0,
        ),
        RackParams(
            rack_id="N3",
            pdu_id=n3_pdu,
            current_A=4.0,
            initial_temp_C=25.0,
            R0_ohm=0.010,
            alpha_per_C=0.004,
            h_W_per_K=0.5,
            C_J_per_K=8.0,
            max_current_A=16.0,
            max_temp_C=90.0,
        ),
        RackParams(
            rack_id="N4",
            pdu_id="F2",
            current_A=24.0,
            initial_temp_C=n4_t0,
            R0_ohm=0.040,
            alpha_per_C=0.004,
            h_W_per_K=0.40,
            C_J_per_K=8.0,
            max_current_A=32.0,
            max_temp_C=90.0,
        ),
    )
    pdus = (
        PDU(pdu_id="F1", max_current_A=44.0),
        PDU(pdu_id="F2", max_current_A=50.0),
    )
    return World(
        racks=racks,
        pdus=pdus,
        ambient_C=24.0,
        horizon_s=180,
        step_s=10,
        allowed_targets=("N2", "N3"),
    )


# ---------------------------------------------------------------------------
# Phase 1 Tests: Step Kernel, World Simulation, Parameter ID & Validation
# ---------------------------------------------------------------------------


def test_step_temperature_exact_reference_values() -> None:
    """Verify N4 ramp to t=0 and 180s trajectories at 24A and 16A match exact reference values."""
    times_s, temps_c, _ = _generate_n4_ramp()
    assert len(times_s) == 13
    assert times_s[0] == -120
    assert times_s[-1] == 0

    t0_n4 = temps_c[-1]
    assert t0_n4 == pytest.approx(88.95030198217694, abs=1e-11)

    # 18 steps (180s) at 24A without migration -> 98.82909050044 C
    t_unmitigated = t0_n4
    for _ in range(18):
        t_unmitigated = step_temperature(
            t_c=t_unmitigated,
            i_a=24.0,
            ambient_c=24.0,
            r0_ohm=0.04,
            alpha=0.004,
            h_w_k=0.4,
            heat_capacity_j_k=8.0,
            dt_s=10.0,
        )
    assert t_unmitigated == pytest.approx(98.82909050044, abs=1e-9)

    # 18 steps (180s) at 16A after 8A migration -> 52.53480720813 C
    t_mitigated = t0_n4
    for _ in range(18):
        t_mitigated = step_temperature(
            t_c=t_mitigated,
            i_a=16.0,
            ambient_c=24.0,
            r0_ohm=0.04,
            alpha=0.004,
            h_w_k=0.4,
            heat_capacity_j_k=8.0,
            dt_s=10.0,
        )
    assert t_mitigated == pytest.approx(52.53480720813, abs=1e-9)


def test_simulate_world_trajectories_conservation_and_verdicts() -> None:
    """Verify 19-sample trajectory, 68A current conservation, and exact verdicts across Worlds A & B."""
    world_a = _build_reference_world("A")
    world_b = _build_reference_world("B")

    # 1. No migration in World A -> "unsafe, thermal"
    traj_none = simulate_world(world_a, migration=None)
    assert isinstance(traj_none, Trajectory)
    assert len(traj_none.time_s) == 19
    assert traj_none.time_s[0] == 0
    assert traj_none.time_s[-1] == 180
    assert traj_none.temps_C["N4"][-1] == pytest.approx(98.82909050044, abs=1e-9)
    assert traj_none.safety_flags["safe"] is False
    assert traj_none.safety_flags["thermal_violation"] is True
    assert traj_none.safety_flags["pdu_overload"] is False
    assert traj_none.safety_flags["verdict"] == "unsafe, thermal"
    assert sum(traj_none.currents_A[r][0] for r in traj_none.currents_A) == pytest.approx(
        TOTAL_WORLD_CURRENT_A, abs=1e-9
    )

    # 2. World A safe migration: N4 -> N3 (F1=40A, F2=28A) -> "safe"
    mig_n3 = Migration(source="N4", target="N3", amps=8.0)
    migrated_a_n3 = apply_migration(world_a, mig_n3)
    assert sum(r.current_A for r in migrated_a_n3.racks) == pytest.approx(68.0, abs=1e-9)

    traj_a_safe = simulate_world(world_a, migration=mig_n3)
    assert len(traj_a_safe.time_s) == 19
    assert traj_a_safe.temps_C["N4"][-1] == pytest.approx(52.53480720813, abs=1e-9)
    assert traj_a_safe.pdu_currents_A["F1"][0] == pytest.approx(40.0, abs=1e-9)
    assert traj_a_safe.pdu_currents_A["F2"][0] == pytest.approx(28.0, abs=1e-9)
    assert traj_a_safe.safety_flags["safe"] is True
    assert traj_a_safe.safety_flags["verdict"] == "safe"

    # 3. World A wrong receiver: N4 -> N2 (F1=48A > 44A, F2=20A) -> "unsafe, F1 overload"
    mig_n2 = Migration(source="N4", target="N2", amps=8.0)
    traj_a_overload = simulate_world(world_a, migration=mig_n2)
    assert traj_a_overload.pdu_currents_A["F1"][0] == pytest.approx(48.0, abs=1e-9)
    assert traj_a_overload.pdu_currents_A["F2"][0] == pytest.approx(20.0, abs=1e-9)
    assert traj_a_overload.safety_flags["safe"] is False
    assert traj_a_overload.safety_flags["thermal_violation"] is False
    assert traj_a_overload.safety_flags["pdu_overload"] is True
    assert traj_a_overload.safety_flags["violated_pdus"] == ["F1"]
    assert traj_a_overload.safety_flags["verdict"] == "unsafe, F1 overload"

    # 4. World B mirror symmetry: N4 -> N2 is "safe" (F1=40A, F2=28A), N4 -> N3 is "unsafe, F1 overload"
    traj_b_safe = simulate_world(world_b, migration=mig_n2)
    assert traj_b_safe.pdu_currents_A["F1"][0] == pytest.approx(40.0, abs=1e-9)
    assert traj_b_safe.pdu_currents_A["F2"][0] == pytest.approx(28.0, abs=1e-9)
    assert traj_b_safe.safety_flags["verdict"] == "safe"

    traj_b_overload = simulate_world(world_b, migration=mig_n3)
    assert traj_b_overload.pdu_currents_A["F1"][0] == pytest.approx(48.0, abs=1e-9)
    assert traj_b_overload.pdu_currents_A["F2"][0] == pytest.approx(20.0, abs=1e-9)
    assert traj_b_overload.safety_flags["verdict"] == "unsafe, F1 overload"


def test_infer_r0_and_fit_thermal_parameters_from_ramp() -> None:
    """Verify probe R0 inference and least-squares recovery of h=0.4 W/K and C=8.0 J/K."""
    times_s, temps_c, currents_a = _generate_n4_ramp()
    t0_n4 = temps_c[-1]

    # Voltage drop at t=0: V = 24 * 0.04 * (1 + 0.004 * (88.95030198217694 - 24)) = 1.2094091596115595 V
    v_drop = 1.2094091596115595
    r0_est = infer_r0_from_voltage_drop(
        voltage_drop_v=v_drop,
        current_a=24.0,
        temp_c=t0_n4,
        ambient_c=24.0,
        alpha_per_c=0.004,
    )
    assert r0_est == pytest.approx(0.040, abs=1e-12)

    fit = fit_thermal_parameters_from_ramp(
        times_s=times_s,
        temps_c=temps_c,
        currents_a=currents_a,
        r0_ohm=r0_est,
        ambient_c=24.0,
        alpha_per_c=0.004,
    )
    assert fit["h_W_per_K"] == pytest.approx(0.4, abs=1e-9)
    assert fit["C_J_per_K"] == pytest.approx(8.0, abs=1e-9)
    assert fit["max_residual"] < 1e-11


def test_fit_thermal_parameters_rejects_ill_conditioned_and_invalid_data() -> None:
    """Verify fit_thermal_parameters_from_ramp rejects constant, ill-conditioned, or nonfinite data."""
    times_s, temps_c, currents_a = _generate_n4_ramp()

    # Constant temperature & current -> singular/rank-deficient matrix
    with pytest.raises(ValueError):
        fit_thermal_parameters_from_ramp(
            times_s=times_s,
            temps_c=[40.0] * 13,
            currents_a=[15.0] * 13,
            r0_ohm=0.04,
        )

    # Non-10s time step
    bad_times = list(times_s)
    bad_times[-1] = 5
    with pytest.raises(ValueError, match="10s"):
        fit_thermal_parameters_from_ramp(
            times_s=bad_times,
            temps_c=temps_c,
            currents_a=currents_a,
            r0_ohm=0.04,
        )

    # Nonfinite value in sequence
    nan_temps = list(temps_c)
    nan_temps[3] = float("nan")
    with pytest.raises(ValueError, match="nonfinite"):
        fit_thermal_parameters_from_ramp(
            times_s=times_s,
            temps_c=nan_temps,
            currents_a=currents_a,
            r0_ohm=0.04,
        )

    # Corrupt noisy temperatures exceeding max_residual
    corrupt_temps = list(temps_c)
    corrupt_temps[5] += 15.0
    with pytest.raises(ValueError):
        fit_thermal_parameters_from_ramp(
            times_s=times_s,
            temps_c=corrupt_temps,
            currents_a=currents_a,
            r0_ohm=0.04,
        )


def test_strict_validation_rejects_invalid_inputs() -> None:
    """Verify strict domain validation rejects NaN, inf, bad IDs, non-68A totals, and bad migrations."""
    world = _build_reference_world("A")

    # 1. NaN and inf in step_temperature and infer_r0_from_voltage_drop
    with pytest.raises(ValueError, match="nonfinite"):
        step_temperature(float("nan"), 24.0, 24.0, 0.04, 0.004, 0.4, 8.0, 10.0)
    with pytest.raises(ValueError, match="nonfinite"):
        step_temperature(50.0, float("inf"), 24.0, 0.04, 0.004, 0.4, 8.0, 10.0)
    with pytest.raises(ValueError, match="nonfinite"):
        infer_r0_from_voltage_drop(float("nan"), 24.0, 88.95)

    # 2. Negative current or zero/negative R0, h, C, dt_s in step_temperature
    with pytest.raises(ValueError, match="invalid physical parameter"):
        step_temperature(50.0, -1.0, 24.0, 0.04, 0.004, 0.4, 8.0, 10.0)
    with pytest.raises(ValueError, match="invalid physical parameter"):
        step_temperature(50.0, 24.0, 24.0, 0.0, 0.004, 0.4, 8.0, 10.0)
    with pytest.raises(ValueError, match="invalid physical parameter"):
        step_temperature(50.0, 24.0, 24.0, 0.04, 0.004, -0.4, 8.0, 10.0)
    with pytest.raises(ValueError, match="invalid physical parameter"):
        step_temperature(50.0, 24.0, 24.0, 0.04, 0.004, 0.4, 0.0, 10.0)
    with pytest.raises(ValueError, match="invalid physical parameter"):
        step_temperature(50.0, 24.0, 24.0, 0.04, 0.004, 0.4, 8.0, 0.0)

    # 3. RackParams & PDU validation (bad ID, negative current, zero R0/h/C, NaN)
    with pytest.raises(ValueError, match="rack_id"):
        replace(world.racks[0], rack_id="")
    with pytest.raises(ValueError, match="rack_id"):
        replace(world.racks[0], rack_id=" N1 ")
    with pytest.raises(ValueError, match="current_A"):
        replace(world.racks[0], current_A=-5.0)
    with pytest.raises(ValueError, match="R0_ohm"):
        replace(world.racks[0], R0_ohm=0.0)
    with pytest.raises(ValueError, match="h_W_per_K"):
        replace(world.racks[0], h_W_per_K=-0.1)
    with pytest.raises(ValueError, match="C_J_per_K"):
        replace(world.racks[0], C_J_per_K=0.0)
    with pytest.raises(ValueError, match="nonfinite"):
        replace(world.racks[0], initial_temp_C=float("nan"))
    with pytest.raises(ValueError, match="PDU max_current_A"):
        PDU(pdu_id="F1", max_current_A=0.0)

    # 4. World validation: missing PDU, duplicate rack, duplicate PDU, nonconserved current (!= 68A), wrong horizon
    with pytest.raises(ValueError, match="unknown pdu_id"):
        bad_rack = replace(world.racks[0], pdu_id="F99")
        replace(world, racks=(bad_rack,) + world.racks[1:])

    with pytest.raises(ValueError, match="duplicate rack_id"):
        dup_rack = replace(world.racks[1], rack_id="N1")
        replace(world, racks=(world.racks[0], dup_rack) + world.racks[2:])

    with pytest.raises(ValueError, match="duplicate pdu_id"):
        replace(world, pdus=(PDU("F1", 42.0), PDU("F1", 42.0)))

    with pytest.raises(ValueError, match="total current conservation violated"):
        bad_current_rack = replace(world.racks[0], current_A=25.0)
        replace(world, racks=(bad_current_rack,) + world.racks[1:])

    with pytest.raises(ValueError, match="horizon_s"):
        replace(world, horizon_s=120)

    with pytest.raises(ValueError, match="horizon_s"):
        replace(world, step_s=7)

    # 5. Migration validation: source == target, negative/non-8A migration, disallowed target, unknown rack
    with pytest.raises(ValueError, match="distinct"):
        apply_migration(world, Migration(source="N4", target="N4", amps=8.0))

    with pytest.raises(ValueError, match="migration amps must be > 0"):
        Migration(source="N4", target="N2", amps=-8.0)

    with pytest.raises(ValueError, match="8.0 A"):
        apply_migration(world, Migration(source="N4", target="N2", amps=5.0))

    with pytest.raises(ValueError, match="allowed_targets"):
        apply_migration(world, Migration(source="N4", target="N1", amps=8.0))

    with pytest.raises(ValueError, match="does not exist"):
        apply_migration(world, Migration(source="N99", target="N2", amps=8.0))


# ---------------------------------------------------------------------------
# Legacy / Auxiliary Domain Tests (retained for backward compatibility)
# ---------------------------------------------------------------------------


def test_voltage_drop_and_probe_equations() -> None:
    """Verify V = I * R and active probe delta-V / inferred R consistency."""
    r_degraded = DEGRADED_JOINT_RESISTANCE_OHM  # 0.145 Ohm
    base_i = 95.0
    delta_i = DEFAULT_PROBE_DELTA_I_A  # 20.0 A

    v_drop = compute_voltage_drop(base_i, r_degraded)
    assert v_drop == pytest.approx(13.775, abs=1e-4)

    v_base, v_probed, delta_v = compute_probe_voltage_drop(base_i, delta_i, r_degraded)
    assert v_base == pytest.approx(13.775, abs=1e-4)
    assert v_probed == pytest.approx(16.675, abs=1e-4)
    assert delta_v == pytest.approx(2.90, abs=1e-4)

    r_inferred = infer_resistance_from_probe(delta_v, delta_i)
    assert r_inferred == pytest.approx(r_degraded, abs=1e-4)


def test_infer_resistance_zero_current_raises() -> None:
    """Verify zero probe current pulse raises ValueError."""
    with pytest.raises(ValueError, match="delta_current_a must be non-zero"):
        infer_resistance_from_probe(1.5, 0.0)


def test_joule_heating_and_steady_state_temperature() -> None:
    """Verify P = I^2 * R and steady-state junction temperature rise."""
    # Healthy node N3 at 95 A on 0.018 Ohm feeder
    p_healthy = compute_joule_heating_w(95.0, NOMINAL_FEEDER_RESISTANCE_OHM["F3"])
    assert p_healthy == pytest.approx(162.45, abs=1e-2)

    t_healthy = compute_steady_state_temperature_c(95.0, NOMINAL_FEEDER_RESISTANCE_OHM["F3"])
    assert t_healthy == pytest.approx(37.81, abs=0.05)
    assert t_healthy < MAX_SAFE_JUNCTION_TEMP_C

    # Degraded node N2 at 95 A on 0.145 Ohm high-resistance lug
    p_degraded = compute_joule_heating_w(95.0, DEGRADED_JOINT_RESISTANCE_OHM)
    assert p_degraded == pytest.approx(1308.625, abs=1e-2)

    t_degraded = compute_steady_state_temperature_c(95.0, DEGRADED_JOINT_RESISTANCE_OHM)
    assert t_degraded == pytest.approx(135.23, abs=0.05)
    assert t_degraded > MAX_SAFE_JUNCTION_TEMP_C


def test_compute_max_safe_current_limits() -> None:
    """Verify thermal derating cap for degraded joint vs ampacity limit for healthy feeder."""
    # Healthy feeder (0.018 Ohm) is bounded by 125 A breaker ampacity limit
    max_i_healthy = compute_max_safe_current_a(NOMINAL_FEEDER_RESISTANCE_OHM["F2"])
    assert max_i_healthy == pytest.approx(MAX_SAFE_FEEDER_CURRENT_A, abs=1e-2)

    # Degraded joint (0.145 Ohm) must derate to ~70.35 A so T <= 85 C
    max_i_degraded = compute_max_safe_current_a(DEGRADED_JOINT_RESISTANCE_OHM)
    assert max_i_degraded == pytest.approx(70.35, abs=0.05)

    # At max_i_degraded, steady state temperature should not exceed MAX_SAFE_JUNCTION_TEMP_C
    temp_at_limit = compute_steady_state_temperature_c(max_i_degraded, DEGRADED_JOINT_RESISTANCE_OHM)
    assert temp_at_limit <= MAX_SAFE_JUNCTION_TEMP_C + 0.1

    # Non-positive resistance fallback
    assert compute_max_safe_current_a(0.0) == MAX_SAFE_FEEDER_CURRENT_A


def test_evaluate_node_state_dataclass() -> None:
    """Verify evaluate_node_state populates PhysicsNodeState consistently."""
    state = evaluate_node_state(
        node_id="N2",
        feeder_id="F2",
        load_current_a=95.0,
        resistance_ohm=DEGRADED_JOINT_RESISTANCE_OHM,
        ambient_temp_c=AMBIENT_TEMP_C,
    )
    assert isinstance(state, PhysicsNodeState)
    assert state.node_id == "N2"
    assert state.feeder_id == "F2"
    assert state.terminal_voltage_v == pytest.approx(NOMINAL_VOLTAGE_V - state.voltage_drop_v, abs=1e-4)
    assert state.temperature_c == pytest.approx(135.23, abs=0.05)
    assert state.max_safe_current_a == pytest.approx(70.35, abs=0.05)


def test_evaluate_remediation_safety_oracle() -> None:
    """Verify safety oracle checks feeder match, node match, and current cap bounds."""
    true_cap = compute_max_safe_current_a(DEGRADED_JOINT_RESISTANCE_OHM)  # 70.35 A

    # Safe action matching Case A (N2 on F2) with 70 A cap
    res_safe = evaluate_remediation_safety(
        target_feeder_id="f2",
        degraded_feeder_id="F2",
        target_node_id="n2",
        degraded_node_id="N2",
        proposed_current_cap_a=70.0,
        true_max_safe_current_a=true_cap,
    )
    assert res_safe["is_safe"] is True
    assert res_safe["feeder_match"] is True
    assert res_safe["node_match"] is True
    assert res_safe["cap_safe"] is True

    # Unsafe action: wrong feeder (e.g. F2 in Case B where N2 is on F3)
    res_wrong_feeder = evaluate_remediation_safety(
        target_feeder_id="F2",
        degraded_feeder_id="F3",
        target_node_id="N2",
        degraded_node_id="N2",
        proposed_current_cap_a=70.0,
        true_max_safe_current_a=true_cap,
    )
    assert res_wrong_feeder["is_safe"] is False
    assert res_wrong_feeder["feeder_match"] is False

    # Unsafe action: current cap too high (90 A > 70.35 + 5.0)
    res_excess_cap = evaluate_remediation_safety(
        target_feeder_id="F3",
        degraded_feeder_id="F3",
        target_node_id="N2",
        degraded_node_id="N2",
        proposed_current_cap_a=90.0,
        true_max_safe_current_a=true_cap,
    )
    assert res_excess_cap["is_safe"] is False
    assert res_excess_cap["cap_safe"] is False
