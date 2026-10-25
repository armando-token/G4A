"""Unit tests for blackbox.managed (Managed Agent transport & sandbox handoff)."""

from __future__ import annotations

from pathlib import Path
import pytest

from blackbox.managed import (
    DEFAULT_FOUNDATION_MODEL,
    DEFAULT_MANAGED_AGENT_MODEL,
    ManagedAgentTransport,
    validate_sandbox_handoff_files,
)


def test_managed_agent_defaults() -> None:
    transport = ManagedAgentTransport(mode="deterministic")
    assert transport.managed_model == DEFAULT_MANAGED_AGENT_MODEL
    assert transport.foundation_model == DEFAULT_FOUNDATION_MODEL
    assert transport.managed_model == "antigravity-preview-09-2026"
    assert transport.foundation_model == "gemini-4-argon"


def test_sandbox_handoff_blocks_private_files(tmp_path: Path) -> None:
    priv_dir = tmp_path / "artifacts" / "private"
    priv_dir.mkdir(parents=True)
    secret_file = priv_dir / "sealed_truth_case_A.json"
    secret_file.write_text("{}", encoding="utf-8")

    with pytest.raises(PermissionError, match="Sandbox isolation violation"):
        validate_sandbox_handoff_files([secret_file])


def test_invoke_interaction_blocks_leaked_context(tmp_path: Path) -> None:
    pub_file = tmp_path / "public_info.json"
    pub_file.write_text('{"ok": true}', encoding="utf-8")
    transport = ManagedAgentTransport(mode="deterministic")

    with pytest.raises(PermissionError, match="Context leakage violation"):
        transport.invoke_interaction(
            interaction_id="I1",
            case_id="case_A",
            agent_role="Investigator",
            prompt_context={"node_resistances_ohm": {"N2": 0.145}},
            handoff_files=[pub_file],
        )


def test_invoke_interaction_accounting(tmp_path: Path) -> None:
    pub_file = tmp_path / "sld_case_A.png"
    pub_file.write_bytes(b"PNG_DATA")
    transport = ManagedAgentTransport(mode="deterministic")

    payload, acc = transport.invoke_interaction(
        interaction_id="I1",
        case_id="case_A",
        agent_role="Investigator",
        prompt_context={"public_evidence": {"case_id": "case_A"}},
        handoff_files=[pub_file],
    )
    assert payload["interaction_id"] == "I1"
    assert payload["probe_request"]["target_node"] == "N2"
    assert acc.managed_model == "antigravity-preview-09-2026"
    assert acc.foundation_model == "gemini-4-argon"
    assert len(acc.handoff_files) == 1
    assert acc.total_tokens > 0
