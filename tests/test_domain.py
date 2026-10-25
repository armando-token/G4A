"""Unit tests for the BlackboxDC public electrical and thermal physics kernel."""

from __future__ import annotations

import pytest

from blackbox.domain import (
    AMBIENT_TEMP_C,
    DEFAULT_PROBE_DELTA_I_A,
    DEGRADED_JOINT_RESISTANCE_OHM,
    MAX_SAFE_FEEDER_CURRENT_A,
    MAX_SAFE_JUNCTION_TEMP_C,
    NOMINAL_FEEDER_RESISTANCE_OHM,
    NOMINAL_VOLTAGE_V,
    THERMAL_RESISTANCE_C_PER_W,
    PhysicsNodeState,
    compute_joule_heating_w,
    compute_max_safe_current_a,
    compute_probe_voltage_drop,
    compute_steady_state_temperature_c,
    compute_voltage_drop,
    evaluate_node_state,
    evaluate_remediation_safety,
    infer_resistance_from_probe,
)


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
