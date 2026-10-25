"""Unit tests for blackbox.oracle (safety and numerical grading)."""

from __future__ import annotations

from pathlib import Path

from blackbox.fixture import generate_case_bundle
from blackbox.managed import ManagedAgentTransport
from blackbox.oracle import grade_ab_campaign, grade_case_run
from blackbox.protocol import BlackboxProtocolStateMachine


def test_oracle_grades_passing_campaign(tmp_path: Path) -> None:
    transport = ManagedAgentTransport(mode="deterministic")

    pub_a, priv_a = generate_case_bundle("case_A", artifacts_root=tmp_path)
    sm_a = BlackboxProtocolStateMachine(pub_a, priv_a, transport)
    res_a = sm_a.run_case_protocol()
    grade_a = grade_case_run(res_a, sm_a.unseal_truth_for_oracle())
    assert grade_a.overall_case_pass is True
    assert grade_a.i2_feeder_correct is True

    pub_b, priv_b = generate_case_bundle("case_B", artifacts_root=tmp_path)
    sm_b = BlackboxProtocolStateMachine(pub_b, priv_b, transport)
    res_b = sm_b.run_case_protocol()
    grade_b = grade_case_run(res_b, sm_b.unseal_truth_for_oracle())
    assert grade_b.overall_case_pass is True
    assert grade_b.i2_feeder_correct is True

    ab = grade_ab_campaign(grade_a, grade_b, res_a, res_b)
    assert ab["campaign_pass"] is True
    assert ab["feeder_disambiguation_pass"] is True
    assert ab["total_interactions_locked"] == 8


def test_oracle_rejects_wrong_feeder_in_case_b(tmp_path: Path) -> None:
    transport = ManagedAgentTransport(mode="deterministic")
    pub_b, priv_b = generate_case_bundle("case_B", artifacts_root=tmp_path)
    sm_b = BlackboxProtocolStateMachine(pub_b, priv_b, transport)
    res_b = sm_b.run_case_protocol()

    # Corrupt I2 feeder diagnosis in Case B from F3 to F2 (ignoring SLD swap)
    res_b.i2.diagnosed_fault_feeder = "F2"
    grade_b = grade_case_run(res_b, sm_b.unseal_truth_for_oracle())
    assert grade_b.i2_feeder_correct is False
    assert grade_b.overall_case_pass is False
