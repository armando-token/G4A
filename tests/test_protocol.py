"""Unit tests for blackbox.protocol (I1 -> Probe -> I2 and A1 -> A2 state machine)."""

from __future__ import annotations

from pathlib import Path
import pytest

from blackbox.fixture import generate_case_bundle
from blackbox.managed import ManagedAgentTransport
from blackbox.protocol import BlackboxProtocolStateMachine


def test_cannot_unseal_truth_before_locks(tmp_path: Path) -> None:
    pub, priv = generate_case_bundle("case_A", artifacts_root=tmp_path)
    transport = ManagedAgentTransport(mode="deterministic")
    sm = BlackboxProtocolStateMachine(public_evidence=pub, sealed_truth=priv, transport=transport)

    with pytest.raises(PermissionError, match="Cannot unseal ground truth"):
        sm.unseal_truth_for_oracle()


def test_full_protocol_execution_case_a_and_b(tmp_path: Path) -> None:
    transport = ManagedAgentTransport(mode="deterministic")

    pub_a, priv_a = generate_case_bundle("case_A", artifacts_root=tmp_path)
    sm_a = BlackboxProtocolStateMachine(pub_a, priv_a, transport)
    res_a = sm_a.run_case_protocol()
    assert sm_a.all_locks_committed
    assert res_a.zero_leakage_verified
    assert res_a.i2.diagnosed_fault_node == "N2"
    assert res_a.i2.diagnosed_fault_feeder == "F2"
    assert res_a.a2.verified_fault_feeder == "F2"

    pub_b, priv_b = generate_case_bundle("case_B", artifacts_root=tmp_path)
    sm_b = BlackboxProtocolStateMachine(pub_b, priv_b, transport)
    res_b = sm_b.run_case_protocol()
    assert sm_b.all_locks_committed
    assert res_b.zero_leakage_verified
    assert res_b.i2.diagnosed_fault_node == "N2"
    assert res_b.i2.diagnosed_fault_feeder == "F3"
    assert res_b.a2.verified_fault_feeder == "F3"
