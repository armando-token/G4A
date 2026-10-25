"""Unit and regression tests for `historical/` predecessor modules (Phase 00 & Phase 7A–7D)."""

from __future__ import annotations

import copy
import json
import socket
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from blackbox.dashboard import build_research_history_tabs_model
from blackbox.domain import Migration, simulate_world
from blackbox.evidence import verify_and_replay_bundle
from blackbox.fixture import build_private_world
from historical import (
    HISTORICAL_INSTALLATION_V1_OUTCOME,
    RackSpec,
    build_cooling_dag_fixture,
    build_installation_v1_public_bundle,
    build_verified_spanish_summary,
    check_critical_dependency_gate,
    classify_case_outcome,
    compute_rack_hotspot,
    evaluate_strict_pair_gate,
    execute_safe_forecast_function,
    get_historical_installation_v1_record,
    permute_installation_labels,
    render_installation_v1_layout_png,
    render_installation_v1_thermal_png,
    run_causal_pilot,
    run_installation_pilot,
    run_installation_v1_campaign,
    run_legacy_pilot,
    simulate_installation_case,
    simulate_thermal_step,
    validate_forecast_source_ast,
    validate_v1_plan,
    verify_v1_public_boundary,
)
from historical.causal_fixture import (
    CAUSAL_HIDDEN_WORLDS,
    compute_candidate_voltage_intervals,
    simulate_ramp_12_18_24a,
    verify_thermal_trajectory_ambiguity,
)
from historical.causal_pilot import run_three_world_causal_experiment
from historical.cli_pilot import (
    ScenarioLab,
    discover_coding_cli,
    invoke_cli_bounded,
    main as cli_pilot_main,
    sanitize_cli_env,
)
from historical.installation_contracts import CaseDiagnostics
from historical.installation_pilot import SimulatedInstallationV1Client
from historical.legacy_thermal import (
    ConnectorThermalParams,
    compute_equilibrium_temp_c,
    compute_joule_heating_w,
    compute_time_constant_s,
    simulate_connector_series,
    simulate_installation_thermodynamics,
    step_temperature_c,
)
from historical.pilot import Lab, ManagedRunner, Store, validate_load_profile


REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_DOSSIER_DIR = REPO_ROOT / "artifacts" / "reports" / "phase5_live_pair_dossier"
COVERAGE_MANIFEST_PATH = REPO_ROOT / "historical" / "coverage_manifest.json"


# ---------------------------------------------------------------------------
# Phase 00 Baseline Tests (Preserved)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Suite 1 — checks.py equivalent (7A Thermal POC, Reference Check & Managed Lab)
# ---------------------------------------------------------------------------


def test_7a_private_reference_receiver_sanity_check() -> None:
    world_a = build_private_world("A")["world"]
    world_b = build_private_world("B")["world"]

    # Case A: N3 is safe (F1=40A<=44A, F2=28A<=50A, N4<90C); N2 overloads F1 (48A>44A)
    sim_a_n3 = simulate_world(world_a, migration=Migration("N4", "N3", 8.0))
    sim_a_n2 = simulate_world(world_a, migration=Migration("N4", "N2", 8.0))
    assert sim_a_n3.safety_flags["safe"] is True
    assert sim_a_n3.pdu_currents_A["F1"][-1] == pytest.approx(40.0)
    assert sim_a_n3.pdu_currents_A["F2"][-1] == pytest.approx(28.0)
    assert sim_a_n3.temps_C["N4"][-1] < 90.0

    assert sim_a_n2.safety_flags["safe"] is False
    assert sim_a_n2.safety_flags["pdu_overload"] is True
    assert sim_a_n2.pdu_currents_A["F1"][-1] == pytest.approx(48.0)

    # Case B: N2 is safe (F1=40A<=44A, F2=28A<=50A, N4<90C); N3 overloads F1 (48A>44A)
    sim_b_n2 = simulate_world(world_b, migration=Migration("N4", "N2", 8.0))
    sim_b_n3 = simulate_world(world_b, migration=Migration("N4", "N3", 8.0))
    assert sim_b_n2.safety_flags["safe"] is True
    assert sim_b_n2.pdu_currents_A["F1"][-1] == pytest.approx(40.0)
    assert sim_b_n2.pdu_currents_A["F2"][-1] == pytest.approx(28.0)
    assert sim_b_n2.temps_C["N4"][-1] < 90.0

    assert sim_b_n3.safety_flags["safe"] is False
    assert sim_b_n3.safety_flags["pdu_overload"] is True
    assert sim_b_n3.pdu_currents_A["F1"][-1] == pytest.approx(48.0)


def test_7a_connector_thermal_kernel_scalars_and_vectors() -> None:
    params = ConnectorThermalParams(r_ohm=0.04, h_w_per_k=0.4, c_j_per_k=8.0, ambient_c=25.0)
    assert params.time_constant_s == pytest.approx(20.0)
    assert compute_time_constant_s(8.0, 0.4) == pytest.approx(20.0)

    # Joule heating: zero current -> 0.0, positive -> I^2 * R, vector shape preserved
    assert compute_joule_heating_w(0.0, 0.04) == pytest.approx(0.0)
    assert compute_joule_heating_w(10.0, 0.04) == pytest.approx(4.0)
    vec_i = np.array([[0.0, 10.0], [20.0, 30.0]], dtype=np.float64)
    vec_q = compute_joule_heating_w(vec_i, 0.04)
    assert isinstance(vec_q, np.ndarray)
    assert vec_q.shape == (2, 2)
    assert np.allclose(vec_q, np.array([[0.0, 4.0], [16.0, 36.0]]))

    with pytest.raises(ValueError, match="r_ohm"):
        compute_joule_heating_w(10.0, -0.01)
    with pytest.raises(ValueError, match="r_ohm"):
        ConnectorThermalParams(r_ohm=-0.04, h_w_per_k=0.4, c_j_per_k=8.0)

    # Equilibrium temperature and step_temperature_c
    teq = compute_equilibrium_temp_c(10.0, 0.04, 0.4, ambient_c=25.0)
    assert teq == pytest.approx(35.0)
    t1 = step_temperature_c(25.0, 10.0, params, dt_s=10.0)
    assert 25.0 < float(t1) < 35.0
    vec_t1 = step_temperature_c(np.array([25.0, 30.0]), np.array([10.0, 10.0]), params, dt_s=10.0)
    assert isinstance(vec_t1, np.ndarray)
    assert vec_t1.shape == (2,)
    assert np.all(np.isfinite(vec_t1))

    # Multi-segment continuity in simulate_connector_series & simulate_installation_thermodynamics
    seg1 = simulate_connector_series(25.0, [12.0, 12.0], params, dt_s=10.0)
    seg2 = simulate_connector_series(seg1.temps_c[-1], [18.0, 18.0], params, dt_s=10.0)
    combined = simulate_connector_series(25.0, [12.0, 12.0, 18.0, 18.0], params, dt_s=10.0)
    assert combined.temps_c[2] == pytest.approx(seg1.temps_c[-1])
    assert combined.temps_c[-1] == pytest.approx(seg2.temps_c[-1])

    inst = simulate_installation_thermodynamics(
        components={"N4": params},
        load_profiles_a={"N4": [12.0, 12.0, 18.0, 18.0]},
        initial_temps_c={"N4": 25.0},
        dt_s=10.0,
    )
    assert inst["N4"].temps_c[-1] == pytest.approx(combined.temps_c[-1])


def test_7a_managed_lab_precommitment_and_audit_gates(tmp_path: Path) -> None:
    store = Store(path=tmp_path / "lab_events.jsonl")
    params = ConnectorThermalParams(r_ohm=0.04, h_w_per_k=0.4, c_j_per_k=8.0, ambient_c=25.0)
    lab = Lab(private_params=params, store=store, dt_s=10.0)

    pub = lab.publish_public_packet()
    assert "r_ohm" not in pub
    assert "h_w_per_k" not in pub
    assert "c_j_per_k" not in pub

    # Malformed load profile raises ValueError
    with pytest.raises(ValueError):
        validate_load_profile([{"duration_s": -10.0, "current_a": 12.0}], dt_s=10.0)
    with pytest.raises(ValueError):
        validate_load_profile([{"duration_s": 15.0, "current_a": 12.0}], dt_s=10.0)

    # Truthful model precommits and passes blind numerical audit
    profile_1 = [{"duration_s": 20.0, "current_a": 12.0}]
    profile_2 = [{"duration_s": 20.0, "current_a": 18.0}]
    expected_1 = simulate_connector_series(25.0, [12.0, 12.0], params, dt_s=10.0)
    expected_2 = simulate_connector_series(25.0, [18.0, 18.0], params, dt_s=10.0)

    lab.register_model("truthful-model")
    lab.commit_predictions(
        "truthful-model",
        {
            "experiments": [
                {"temps_c": list(expected_1.temps_c)},
                {"temps_c": list(expected_2.temps_c)},
            ]
        },
    )
    lab.run_experiment("truthful-model", profile_1)
    lab.run_experiment("truthful-model", profile_2)
    assert store.verify_chain() is True

    audit_ok = lab.check_blind_numerical_audit("truthful-model")
    assert audit_ok["passed"] is True
    assert audit_ok["commitment_valid"] is True
    assert audit_ok["max_abs_error_c"] == pytest.approx(0.0)

    # Mutating predictions post-hoc fails blind audit and commit_predictions raises RuntimeError
    with pytest.raises(RuntimeError):
        lab.commit_predictions("truthful-model", {"experiments": []})
    audit_tampered = lab.check_blind_numerical_audit(
        "truthful-model",
        claimed_predictions={"experiments": [{"temps_c": [25.0, 25.0, 25.0]}, {"temps_c": [25.0, 25.0, 25.0]}]},
    )
    assert audit_tampered["passed"] is False
    assert audit_tampered["commitment_mutated"] is True

    # Requesting > 2 experiments raises RuntimeError
    with pytest.raises(RuntimeError, match="Experiment limit"):
        lab.run_experiment("truthful-model", profile_1)

    # ManagedRunner mock mode & cancelled usage reconciliation
    import historical.pilot as _pilot_mod
    from blackbox.managed import PairBudgetTracker as _BaseTracker

    class _CompatPairBudgetTracker(_BaseTracker):
        def __init__(self, pair_ceiling: int = 280_000, **kwargs: Any) -> None:
            super().__init__()
            self.pair_ceiling = pair_ceiling

        @property
        def pair_cached_inclusive_tokens(self) -> int:
            return self.pair_total_tokens

        def to_dict(self) -> dict[str, Any]:
            return {
                "pair_ceiling": self.pair_ceiling,
                "pair_noncached_tokens": self.pair_noncached_tokens,
                "pair_cached_inclusive_tokens": self.pair_cached_inclusive_tokens,
            }

    _pilot_mod.PairBudgetTracker = _CompatPairBudgetTracker  # type: ignore[misc,assignment]
    runner = ManagedRunner(mode="mock")
    assert runner.mock_counts_as_real is False
    assert runner.live_direct_function_calling_supported is False
    reconciled = runner.reconcile_prior_cancelled_usage(
        [
            {"interaction_id": "int-cancel-1", "event": "created", "case_id": "case_A", "turn_id": "I1"},
        ]
    )
    assert reconciled["reconciled_count"] == 1
    assert reconciled["pair_noncached_tokens"] == reconciled["unknown_turn_reserve_tokens"]


# ---------------------------------------------------------------------------
# Suite 2 — checks_causal.py equivalent (7B CLI Harness & Three-World Causal)
# ---------------------------------------------------------------------------


def test_7b_cli_pilot_sanitization_and_scenario_separation() -> None:
    cli_info = discover_coding_cli()
    assert cli_info["is_managed_agent_evidence"] is False

    fake_key_prefix = "AIza" + "Sy"
    dirty_env = {
        "PATH": "/usr/bin:/bin",
        "GOOGLE_API_KEY": f"{fake_key_prefix}Secret123",
        "GEMINI_API_KEY": f"{fake_key_prefix}Gemini456",
        "GOOGLE_APPLICATION_CREDENTIALS": "/tmp/creds.json",
        "AUTHORIZATION": "Bearer secret-token",
        "CUSTOM_SECRET_KEY": "top-secret",
    }
    clean_env = sanitize_cli_env(dirty_env)
    for k in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "AUTHORIZATION", "CUSTOM_SECRET_KEY"):
        assert k not in clean_env
    assert clean_env["PATH"] == "/usr/bin:/bin"

    res = invoke_cli_bounded("Evaluate thermal step response", timeout_s=5.0, env=dirty_env)
    assert res["returncode"] == 0
    assert res["sanitized_env_verified"] is True
    assert res["is_managed_agent_evidence"] is False
    assert len(res["prompt_sha256"]) == 64

    with pytest.raises(PermissionError, match="Secret leak blocked"):
        invoke_cli_bounded(f"Prompt with {fake_key_prefix}LeakedKey", timeout_s=2.0)

    scen = ScenarioLab().evaluate_scenarios()
    assert scen["separation"]["separated"] is True
    assert scen["separation"]["max_abs_diff_c"] > 1.0
    assert scen["is_managed_agent_evidence"] is False


def test_7b_three_world_thermal_ambiguity_and_voltage_probe() -> None:
    assert set(CAUSAL_HIDDEN_WORLDS.keys()) == {"electrical", "cooling", "outside_candidate"}
    amb = verify_thermal_trajectory_ambiguity()
    assert amb["trajectories_identical"] is True
    assert amb["max_temp_diff_across_worlds_c"] < 1e-9
    assert amb["voltage_drops_distinct"] is True

    ramp_e = simulate_ramp_12_18_24a("electrical")
    ramp_c = simulate_ramp_12_18_24a("cooling")
    ramp_o = simulate_ramp_12_18_24a("outside_candidate")
    assert ramp_e["t0_temp_c"] == pytest.approx(ramp_c["t0_temp_c"], abs=1e-9)
    assert ramp_e["t0_temp_c"] == pytest.approx(ramp_o["t0_temp_c"], abs=1e-9)

    exp_e = run_three_world_causal_experiment("electrical")
    assert exp_e["strict_pass"] is True
    assert exp_e["identifiable"] is True
    assert exp_e["conclusion"] == "electrical"
    assert exp_e["auditor"]["passed"] is True

    exp_c = run_three_world_causal_experiment("cooling")
    assert exp_c["strict_pass"] is True
    assert exp_c["identifiable"] is True
    assert exp_c["conclusion"] == "cooling"
    assert exp_c["auditor"]["passed"] is True


def test_7b_outside_world_and_negative_audit_gates() -> None:
    # Outside candidate world acknowledged
    exp_out = run_three_world_causal_experiment("outside_candidate")
    assert exp_out["outside_world_acknowledged"] is True
    assert exp_out["conclusion"] == "outside_supplied_hypotheses"
    assert exp_out["strict_pass"] is True

    # Echo auditor rejected
    exp_echo = run_three_world_causal_experiment("electrical", echo_auditor=True)
    assert exp_echo["auditor"]["independent"] is False
    assert exp_echo["auditor"]["passed"] is False
    assert exp_echo["strict_pass"] is False

    # Uninformative probe rejected
    exp_uninf = run_three_world_causal_experiment("electrical", probe_type="thermal_recheck")
    assert exp_uninf["identifiable"] is False
    assert exp_uninf["strict_pass"] is False

    # Large noise bound causes interval overlap
    exp_overlap = run_three_world_causal_experiment("electrical", noise_bound_v=1.0)
    assert exp_overlap["identifiable"] is False
    assert exp_overlap["conclusion"] == "ambiguous_interval_overlap"
    assert exp_overlap["strict_pass"] is False

    # Post-hoc commitment mutation rejected
    exp_mut = run_three_world_causal_experiment("electrical", mutate_commitment=True)
    assert exp_mut["commitment_valid"] is False
    assert exp_mut["commitment_mutated"] is True
    assert exp_mut["strict_pass"] is False

    # Exhausted budget & interrupted trace recovery
    exp_budget = run_three_world_causal_experiment("electrical", budget_exhausted=True)
    assert exp_budget["operational_complete"] is False
    assert exp_budget["strict_pass"] is False
    assert exp_budget["recovery_dossier"]["fabricated_completed_turn"] is False

    exp_intr = run_three_world_causal_experiment("electrical", interrupted_turn=True)
    assert exp_intr["operational_complete"] is False
    assert exp_intr["strict_pass"] is False
    assert exp_intr["recovery_dossier"]["derived_from_partial_artifacts"] is True
    assert exp_intr["recovery_dossier"]["fabricated_completed_turn"] is False


# ---------------------------------------------------------------------------
# Suite 3 — checks_installation.py equivalent (7C Lab, AST, Contracts & 4-Cell)
# ---------------------------------------------------------------------------


def test_7c_installation_v1_2d_lab_and_public_boundary(tmp_path: Path) -> None:
    perm = permute_installation_labels(seed=20261024)
    assert set(perm["A"] for _ in range(1)) <= {"layout_alpha_v1", "layout_beta_v1"}
    assert perm[perm["A"]] == "A"
    assert perm[perm["B"]] == "B"

    layout_png = render_installation_v1_layout_png("A", tmp_path / "layout_a.png")
    thermal_png = render_installation_v1_thermal_png(tmp_path / "thermal.png")
    assert ( layout_png["width"], layout_png["height"]) == (800, 500)
    assert (thermal_png["width"], thermal_png["height"]) == (800, 500)

    bundle_a = build_installation_v1_public_bundle("A", output_dir=tmp_path / "case_A")
    assert verify_v1_public_boundary(bundle_a, raise_on_leak=True) is True

    leaked_bundle = copy.deepcopy(bundle_a)
    leaked_bundle["r0_ohm"] = 0.04
    assert verify_v1_public_boundary(leaked_bundle, raise_on_leak=False) is False
    with pytest.raises(PermissionError):
        verify_v1_public_boundary(leaked_bundle, raise_on_leak=True)


def test_7c_forecast_ast_allowlist_and_forged_safety_detection() -> None:
    valid_src = (
        "def step_temperature(t_c, i_a, ambient_c=25.0, r0_ohm=0.04, alpha=0.004, h_w_k=0.4, heat_capacity_j_k=8.0, dt_s=10.0):\n"
        "    delta_t = max(float(t_c) - float(ambient_c), 0.0)\n"
        "    r_eff = float(r0_ohm) * (1.0 + float(alpha) * delta_t)\n"
        "    teq = float(ambient_c) + (float(i_a) ** 2) * r_eff / float(h_w_k)\n"
        "    decay = 2.718281828459045 ** (-float(h_w_k) * float(dt_s) / float(heat_capacity_j_k))\n"
        "    return teq + (float(t_c) - teq) * decay\n"
    )
    fn_node = validate_forecast_source_ast(valid_src)
    assert fn_node.name == "step_temperature"
    out_temp = execute_safe_forecast_function(valid_src)
    assert out_temp > 88.95

    forbidden_snippets = [
        "import os\ndef step_temperature(t_c, i_a, ambient_c=25.0, r0_ohm=0.04, alpha=0.004, h_w_k=0.4, heat_capacity_j_k=8.0, dt_s=10.0):\n    return t_c\n",
        "def step_temperature(t_c, i_a, ambient_c=25.0, r0_ohm=0.04, alpha=0.004, h_w_k=0.4, heat_capacity_j_k=8.0, dt_s=10.0):\n    return __import__('os')\n",
        "def step_temperature(t_c, i_a, ambient_c=25.0, r0_ohm=0.04, alpha=0.004, h_w_k=0.4, heat_capacity_j_k=8.0, dt_s=10.0):\n    return open('/etc/passwd')\n",
        "def step_temperature(t_c, i_a, ambient_c=25.0, r0_ohm=0.04, alpha=0.004, h_w_k=0.4, heat_capacity_j_k=8.0, dt_s=10.0):\n    return eval('1')\n",
        "def step_temperature(t_c, i_a, ambient_c=25.0, r0_ohm=0.04, alpha=0.004, h_w_k=0.4, heat_capacity_j_k=8.0, dt_s=10.0):\n    return t_c.__class__\n",
    ]
    for bad_src in forbidden_snippets:
        with pytest.raises(ValueError, match="Disallowed AST construct"):
            validate_forecast_source_ast(bad_src)

    # Forged safety flag detection on overloaded feeder (F1=48A > 44A in Layout A for MIGRATE_N4_TO_N2_8A)
    sim_client = SimulatedInstallationV1Client(mode="simulated_all_completed")
    case_a_res = sim_client.run_case("A")
    forged_plan = copy.deepcopy(case_a_res["plan"])
    forged_plan["predictions_by_action"]["MIGRATE_N4_TO_N2_8A"]["safe"] = True
    val_forged = validate_v1_plan(forged_plan, layout="A")
    assert val_forged["valid"] is False
    assert val_forged["safety_flag_forged"] is True

    # Critical dependency gate when N2/N3 edges are unknown
    dep_fail = check_critical_dependency_gate(["N2", "N3"], "MIGRATE_N4_TO_N3_8A", "intervene")
    assert dep_fail["passed"] is False
    assert dep_fail["blocking_uncertainty"] is True

    dep_ok = check_critical_dependency_gate(["N2", "N3"], "NO_ACTION", "abstain")
    assert dep_ok["passed"] is True
    assert dep_ok["blocking_uncertainty"] is True

    es = build_verified_spanish_summary(
        case_id="Case A",
        selected_action_id="MIGRATE_N4_TO_N3_8A",
        n4_temp_180s_c=52.5348,
        f1_amps=40.0,
        f2_amps=28.0,
        classification_label="strict case pass",
    )
    assert "MIGRATE_N4_TO_N3_8A" in es
    assert "52.53" in es
    assert "40.0" in es
    assert "28.0" in es


def test_7c_four_cell_classification_matrix_and_v1_campaigns() -> None:
    assert classify_case_outcome(True, True) == "strict case pass"
    assert classify_case_outcome(True, False) == "numerical evidence only; operational incomplete"
    assert classify_case_outcome(False, True) == "completed but scientific fail"
    assert classify_case_outcome(False, False) == "incomplete and invalid"

    hist_camp = run_installation_v1_campaign("historical_incomplete_case_a")
    assert hist_camp["strict_pair_gate_passed"] is False
    assert hist_camp["local_numerical_checks_succeeded"] is True
    assert hist_camp["case_A"]["classification"] == "numerical evidence only; operational incomplete"
    assert hist_camp["case_B"]["classification"] == "strict case pass"

    sim_camp = run_installation_v1_campaign("simulated_all_completed")
    assert sim_camp["strict_pair_gate_passed"] is True
    assert sim_camp["counts_as_live_evidence"] is False
    assert sim_camp["case_A"]["classification"] == "strict case pass"
    assert sim_camp["case_B"]["classification"] == "strict case pass"


# ---------------------------------------------------------------------------
# Suite 4 & 5 — checks_installation_ui.py & checks_visual_v2.py (7D)
# ---------------------------------------------------------------------------


def test_7d_research_history_ui_tabs_offline_no_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for env_var in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"):
        monkeypatch.delenv(env_var, raising=False)

    def _block_socket_connect(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Outbound network socket connection forbidden in offline 7D UI test")

    monkeypatch.setattr(socket.socket, "connect", _block_socket_connect)

    tabs_model = build_research_history_tabs_model()
    assert tabs_model["offline_only"] is True
    assert tabs_model["zero_provider_calls"] is True

    assert tabs_model["tab_7a_thermal_poc"]["blind_numerical_audit"]["passed"] is True
    assert tabs_model["tab_7b_causal_identifiability"]["thermal_ambiguity"]["trajectories_identical"] is True
    assert tabs_model["tab_7b_causal_identifiability"]["outside_world_acknowledged"] is True

    tab_7c = tabs_model["tab_7c_installation_v1"]
    assert tab_7c["strict_pair_gate_passed"] is False
    assert tab_7c["local_numerical_checks_succeeded"] is True
    assert tab_7c["case_a_classification"] == "numerical evidence only; operational incomplete"
    assert "numerical evidence only; operational incomplete" in tab_7c["caveat_banner"]

    tab_7d = tabs_model["tab_7d_coverage_manifest"]
    assert len(tab_7d["suite_mapping"]) == 5


def test_7d_coverage_manifest_and_visual_v2_non_contamination() -> None:
    assert COVERAGE_MANIFEST_PATH.is_file()
    manifest = json.loads(COVERAGE_MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["phase"] == "07_HISTORICAL_RECONSTRUCTION_AND_FULL_PARITY"
    assert set(manifest["subphases"].keys()) == {"7A", "7B", "7C", "7D"}
    assert set(manifest["predecessor_suites"].keys()) == {
        "checks.py",
        "checks_causal.py",
        "checks_installation.py",
        "checks_installation_ui.py",
        "checks_visual_v2.py",
    }
    invariants = manifest["non_conflation_invariants"]
    assert invariants["historical_installation_v1_strict_pair_passed"] is False
    assert invariants["historical_counts_as_visual_v2_live_evidence"] is False
    assert invariants["physical_safety_certified"] is False

    # Verify visual-v2 live dossier remains completely uncontaminated and passes full replay
    replay_res = verify_and_replay_bundle(LIVE_DOSSIER_DIR, require_live=True)
    assert replay_res["verified_from_archived_run"] is True
    assert replay_res["strict_pair_passed"] is True
    assert replay_res["integrity_passed"] is True
