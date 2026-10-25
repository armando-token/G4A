"""Unit tests for all `historical/` predecessor modules and historical outcome labeling."""

from __future__ import annotations

import json

import pytest

from historical import (
    HISTORICAL_INSTALLATION_V1_OUTCOME,
    RackSpec,
    build_cooling_dag_fixture,
    compute_rack_hotspot,
    evaluate_strict_pair_gate,
    get_historical_installation_v1_record,
    run_causal_pilot,
    run_installation_pilot,
    run_legacy_pilot,
    simulate_installation_case,
    simulate_thermal_step,
)
from historical.cli_pilot import main as cli_pilot_main
from historical.installation_contracts import CaseDiagnostics


def test_legacy_thermal_hotspot_and_step() -> None:
    spec = RackSpec(rack_id="RACK-T1", base_power_kw=14.0, max_airflow_cfm=1400.0)
    state0 = compute_rack_hotspot(spec=spec, supply_temp_c=18.0, it_load_kw=14.0, fan_duty=0.7)
    assert state0.rack_id == "RACK-T1"
    assert state0.inlet_temp_c >= 18.0
    assert state0.exhaust_temp_c > state0.inlet_temp_c
    assert state0.hotspot_temp_c > state0.exhaust_temp_c

    # Higher load + lower fan duty increases hotspot temperature
    state1 = simulate_thermal_step(
        prev_state=state0,
        spec=spec,
        supply_temp_c=18.0,
        it_load_kw=22.0,
        fan_duty=0.35,
        dt_seconds=15.0,
    )
    assert state1.hotspot_temp_c > state0.hotspot_temp_c
    assert isinstance(state1.to_dict(), dict)


def test_legacy_pilot_and_cli(capsys: pytest.CaptureFixture[str]) -> None:
    pilot_res = run_legacy_pilot(steps=6, supply_temp_c=19.0, fan_duty=0.6)
    assert pilot_res["pilot_id"] == "legacy-thermal-pilot-v0"
    assert pilot_res["steps"] == 6
    assert len(pilot_res["history"]) == 6
    assert pilot_res["peak_rack_id"] == "RACK-A2"

    rc = cli_pilot_main(["--steps", "4", "--json"])
    assert rc == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["steps"] == 4


def test_causal_fixture_and_pilot() -> None:
    fixture = build_cooling_dag_fixture()
    assert fixture.confounder == "workload_index"
    assert fixture.treatment == "fan_duty"
    assert fixture.outcome == "hotspot_temp_c"
    assert fixture.naive_observational_diff > 0
    assert fixture.ground_truth_ate < 0

    pilot_out = run_causal_pilot(delta_duty=0.25)
    assert pilot_out["status"] == "PASS"
    assert pilot_out["simpsons_sign_reversal_detected"] is True
    assert pilot_out["intervention"]["expected_delta_temp_c"] < 0


def test_installation_lab_cases() -> None:
    case_a = simulate_installation_case("Case A")
    case_b = simulate_installation_case("Case B")
    assert case_a["fault_mode"] == "reversed_baffle_bypass"
    assert case_a["local_numerical_check_passed"] is True
    assert case_b["fault_mode"] == "obstructed_floor_damper"
    assert case_b["local_numerical_check_passed"] is True

    with pytest.raises(ValueError, match="Unknown installation-v1 case_id"):
        simulate_installation_case("Case C")


def test_historical_installation_v1_accurate_outcome_labeling() -> None:
    """Verify the historical installation-v1 record accurately reflects that:
    1. Local numerical checks succeeded for both Case A and Case B.
    2. Case A had an incomplete investigator turn (`investigator_turn_complete is False`).
    3. Therefore, the strict pair gate did NOT pass (`strict_pair_gate_passed is False`).
    """
    record = get_historical_installation_v1_record()
    assert record.benchmark_id == "installation-v1"
    assert record.local_numerical_checks_succeeded is True
    assert record.case_a.local_numerical_check_passed is True
    assert record.case_b.local_numerical_check_passed is True

    # Crucial historical accuracy checks:
    assert record.case_a.investigator_turn_complete is False
    assert record.case_a.case_passed is False
    assert record.case_b.investigator_turn_complete is True
    assert record.case_b.case_passed is True

    assert record.strict_pair_gate_passed is False
    assert record.outcome_label == "INCOMPLETE_INVESTIGATOR_TURN_PAIR_FAIL"
    assert "incomplete" in record.audit_summary.lower()

    # Check module-level constant and default pilot output match
    assert HISTORICAL_INSTALLATION_V1_OUTCOME["local_numerical_checks_succeeded"] is True
    assert HISTORICAL_INSTALLATION_V1_OUTCOME["case_a_investigator_turn_complete"] is False
    assert HISTORICAL_INSTALLATION_V1_OUTCOME["strict_pair_gate_passed"] is False

    pilot_default = run_installation_pilot()
    assert pilot_default["report"]["local_numerical_checks_succeeded"] is True
    assert pilot_default["report"]["strict_pair_gate_passed"] is False
    assert pilot_default["report"]["case_a"]["investigator_turn_complete"] is False


def test_strict_pair_gate_passes_only_when_both_cases_complete() -> None:
    case_a_ok = CaseDiagnostics(
        case_id="Case A",
        fault_mode="reversed_baffle_bypass",
        local_numerical_check_passed=True,
        investigator_turn_complete=True,
        root_cause_identified=True,
    )
    case_b_ok = CaseDiagnostics(
        case_id="Case B",
        fault_mode="obstructed_floor_damper",
        local_numerical_check_passed=True,
        investigator_turn_complete=True,
        root_cause_identified=True,
    )
    report = evaluate_strict_pair_gate(case_a_ok, case_b_ok)
    assert report.strict_pair_gate_passed is True
    assert report.outcome_label == "STRICT_PAIR_PASS"
