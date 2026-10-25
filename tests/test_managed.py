"""Unit tests for blackbox.managed (Phase 3 Managed Agent runtime, accounting, retrieval & isolation)."""

from __future__ import annotations

import io
import json
from pathlib import Path
import tarfile
from typing import Any, Dict, List, Optional
import zipfile

from PIL import Image
import pytest

from blackbox.managed import (
    A2_NONCACHED_RESERVE,
    AUDITOR_NONCACHED_RESERVE,
    CASE_CACHED_INCLUSIVE_CEILING,
    CASE_NONCACHED_CAP,
    DEFAULT_FOUNDATION_MODEL,
    DEFAULT_MANAGED_AGENT_MODEL,
    MAX_IMAGE_BYTES,
    NATIVE_INTERACTION_NONCACHED_CAP,
    PAIR_CACHED_INCLUSIVE_CEILING,
    PAIR_NONCACHED_CAP,
    UNKNOWN_USAGE_RESERVE,
    ManagedAgentTransport,
    PairBudgetTracker,
    append_journal_event,
    normalize_usage_snapshot,
    preflight_managed_agent,
    reconcile_journal,
    redact_for_log,
    reset_environment_role_registry,
    resolve_auth_config,
    retrieve_environment_file,
    run_turn,
    validate_sandbox_handoff_files,
)


def _make_valid_png_bytes(width: int = 64, height: int = 64) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(18, 28, 45)).save(buf, format="PNG")
    return buf.getvalue()


class FakeFilesAPI:
    """Controllable mock for `client.environments.files`."""

    def __init__(self, files: Optional[Dict[str, bytes]] = None) -> None:
        self.files: Dict[str, bytes] = dict(files or {})
        self.list_calls: int = 0
        self.download_calls: int = 0

    def list(self, **kwargs: Any) -> Dict[str, Any]:
        self.list_calls += 1
        return {"files": [{"path": p} for p in self.files.keys()]}

    def download(self, **kwargs: Any) -> bytes:
        self.download_calls += 1
        path = kwargs.get("path")
        if path not in self.files:
            raise FileNotFoundError(f"Mock file not found: {path}")
        return self.files[path]


class FakeEnvironmentsAPI:
    def __init__(self, files_api: FakeFilesAPI) -> None:
        self.files = files_api


class FakeInteractionsAPI:
    """Controllable mock for `client.interactions`."""

    def __init__(
        self,
        create_response: Dict[str, Any],
        poll_responses: Optional[List[Dict[str, Any]]] = None,
        cancel_side_effect: Optional[Exception] = None,
    ) -> None:
        self.create_response = dict(create_response)
        self.poll_responses = list(poll_responses or [])
        self.cancel_side_effect = cancel_side_effect
        self.create_calls: List[Dict[str, Any]] = []
        self.get_calls: List[str] = []
        self.cancel_calls: List[str] = []

    def create(self, **kwargs: Any) -> Dict[str, Any]:
        self.create_calls.append(kwargs)
        return dict(self.create_response)

    def get(self, id: str, **kwargs: Any) -> Dict[str, Any]:
        self.get_calls.append(id)
        if self.poll_responses:
            nxt = self.poll_responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return dict(nxt)
        return dict(self.create_response)

    def cancel(self, id: str, **kwargs: Any) -> Dict[str, Any]:
        self.cancel_calls.append(id)
        if self.cancel_side_effect is not None:
            raise self.cancel_side_effect
        return {"id": id, "status": "cancelled"}


class FakeGenAIClient:
    """Composite mock client exposing `.interactions`, `.environments.files`, and `.models`."""

    def __init__(
        self,
        create_response: Optional[Dict[str, Any]] = None,
        poll_responses: Optional[List[Any]] = None,
        files: Optional[Dict[str, bytes]] = None,
    ) -> None:
        self.files_api = FakeFilesAPI(files=files)
        self.environments = FakeEnvironmentsAPI(self.files_api)
        self.interactions = FakeInteractionsAPI(
            create_response=create_response
            or {
                "id": "inter-001",
                "environment_id": "env-001",
                "status": "queued",
                "usage": {"total_tokens": 500, "total_cached_tokens": 100},
            },
            poll_responses=poll_responses,
        )


@pytest.fixture(autouse=True)
def _clear_env_registry() -> None:
    reset_environment_role_registry()


# ---------------------------------------------------------------------------
# Existing Backward-Compatibility Tests
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Phase 3 Section 5 Mock Scenarios (1 through 16)
# ---------------------------------------------------------------------------


def test_scenario_1_create_queued_in_progress_completed_direct_retrieval(tmp_path: Path) -> None:
    """Scenario 1: create -> queued -> in_progress -> completed -> direct file retrieval."""
    proposal_bytes = json.dumps({"target_rack": "N4", "probe": "voltage_drop"}).encode("utf-8")
    client = FakeGenAIClient(
        create_response={
            "id": "inter-s1",
            "environment_id": "env-inv-1",
            "status": "queued",
            "usage": {"total_tokens": 1200, "total_cached_tokens": 200},
        },
        poll_responses=[
            {
                "id": "inter-s1",
                "environment_id": "env-inv-1",
                "status": "in_progress",
                "usage": {"total_tokens": 3400, "total_cached_tokens": 400},
            },
            {
                "id": "inter-s1",
                "environment_id": "env-inv-1",
                "status": "completed",
                "steps": [{"type": "code_execution", "name": "python"}],
                "usage": {"total_tokens": 6500, "total_cached_tokens": 1500},
            },
        ],
        files={"/workspace/proposal.json": proposal_bytes},
    )
    tracker = PairBudgetTracker()
    journal_path = tmp_path / "journal.jsonl"

    ev = run_turn(
        role="investigator",
        prompt="Analyze packet and write proposal.json",
        packet={"case_id": "case_A"},
        images=[_make_valid_png_bytes()],
        expected_filename="proposal.json",
        case_id="case_A",
        turn_id="I1",
        client=client,
        journal_path=journal_path,
        budget_tracker=tracker,
        sleep_fn=lambda _: None,
    )

    assert ev.status == "completed"
    assert ev.scientific_complete is True
    assert ev.diagnostic_only is False
    assert ev.has_code_execution_step is True
    assert ev.retrieved_json == {"target_rack": "N4", "probe": "voltage_drop"}
    assert ev.retrieved_path == "/workspace/proposal.json"
    assert ev.poll_count == 2
    # Latest cumulative usage only (not sum of polls): 6500 total, 1500 cached -> 5000 noncached
    assert ev.usage.noncached_tokens == 5000
    assert ev.usage.cached_tokens == 1500
    assert ev.usage.total_tokens == 6500
    assert tracker.get_case_ledger("case_A").noncached_tokens == 5000


def test_scenario_2_completed_without_file_in_environment() -> None:
    """Scenario 2: completed status with no file in environments.files -> scientific_complete is False."""
    client = FakeGenAIClient(
        create_response={
            "id": "inter-s2",
            "environment_id": "env-inv-2",
            "status": "completed",
            "steps": [{"type": "code_execution"}],
            "usage": {"total_tokens": 4000, "total_cached_tokens": 0},
        },
        files={},
    )
    ev = run_turn(
        role="investigator",
        prompt="Run I1",
        packet={"case_id": "case_A"},
        images=[_make_valid_png_bytes()],
        expected_filename="proposal.json",
        client=client,
        sleep_fn=lambda _: None,
    )
    assert ev.status == "completed"
    assert ev.scientific_complete is False
    assert ev.retrieved_json is None
    assert ev.error is not None and "FileNotFoundError" in ev.error


def test_scenario_3_completed_with_text_echo_only_but_no_workspace_file() -> None:
    """Scenario 3: JSON echoed in output_text but missing from environments.files -> scientific_complete is False."""
    client = FakeGenAIClient(
        create_response={
            "id": "inter-s3",
            "environment_id": "env-inv-3",
            "status": "completed",
            "output_text": '{"target_rack": "N4", "probe": "voltage_drop"}',
            "steps": [{"type": "code_execution"}],
            "usage": {"total_tokens": 4200, "total_cached_tokens": 200},
        },
        files={},
    )
    ev = run_turn(
        role="investigator",
        prompt="Run I1",
        packet={"case_id": "case_A"},
        images=[_make_valid_png_bytes()],
        expected_filename="proposal.json",
        client=client,
        sleep_fn=lambda _: None,
    )
    assert ev.status == "completed"
    assert ev.scientific_complete is False
    assert ev.retrieved_json is None


def test_scenario_4_completed_missing_code_execution_step() -> None:
    """Scenario 4: completed status with valid file but missing code_execution step -> scientific_complete is False."""
    client = FakeGenAIClient(
        create_response={
            "id": "inter-s4",
            "environment_id": "env-inv-4",
            "status": "completed",
            "steps": [{"type": "message_output"}],
            "usage": {"total_tokens": 3000, "total_cached_tokens": 0},
        },
        files={"proposal.json": b'{"target_rack": "N4"}'},
    )
    ev = run_turn(
        role="investigator",
        prompt="Run I1",
        packet={"case_id": "case_A"},
        images=[_make_valid_png_bytes()],
        expected_filename="proposal.json",
        client=client,
        sleep_fn=lambda _: None,
    )
    assert ev.status == "completed"
    assert ev.has_code_execution_step is False
    assert ev.scientific_complete is False
    assert ev.retrieved_json == {"target_rack": "N4"}
    assert ev.error is not None and "code_execution" in ev.error


def test_scenario_5_failed_and_incomplete_statuses() -> None:
    """Scenario 5: failed status and incomplete status (diagnostic_only=True when file exists)."""
    client_failed = FakeGenAIClient(
        create_response={
            "id": "inter-fail",
            "environment_id": "env-fail",
            "status": "failed",
            "steps": [{"type": "code_execution"}],
            "usage": {"total_tokens": 2500, "total_cached_tokens": 0},
        },
        files={"proposal.json": b'{"target_rack": "N4"}'},
    )
    ev_fail = run_turn(
        role="investigator",
        prompt="Run I1",
        packet={"case_id": "case_A"},
        images=[_make_valid_png_bytes()],
        client=client_failed,
        sleep_fn=lambda _: None,
    )
    assert ev_fail.status == "failed"
    assert ev_fail.scientific_complete is False
    assert ev_fail.diagnostic_only is False
    assert ev_fail.retrieved_json is None

    client_incomplete = FakeGenAIClient(
        create_response={
            "id": "inter-inc",
            "environment_id": "env-inc",
            "status": "incomplete",
            "steps": [{"type": "code_execution"}],
            "usage": {"total_tokens": 29000, "total_cached_tokens": 1000},
        },
        files={"proposal.json": b'{"partial": true}'},
    )
    ev_inc = run_turn(
        role="investigator",
        prompt="Run I1",
        packet={"case_id": "case_A"},
        images=[_make_valid_png_bytes()],
        client=client_incomplete,
        sleep_fn=lambda _: None,
    )
    assert ev_inc.status == "incomplete"
    assert ev_inc.scientific_complete is False
    assert ev_inc.diagnostic_only is True
    assert ev_inc.retrieved_json == {"partial": True}


def test_scenario_6_timeout_cancel_confirmed_cancelled() -> None:
    """Scenario 6: Timeout -> cancel -> follow-up get confirms status == 'cancelled'."""
    clock = [0.0]

    def fake_time() -> float:
        return clock[0]

    def fake_sleep(dt: float) -> None:
        clock[0] += dt

    client = FakeGenAIClient(
        create_response={
            "id": "inter-timeout-ok",
            "environment_id": "env-to-1",
            "status": "in_progress",
            "usage": {"total_tokens": 5000, "total_cached_tokens": 0},
        },
        poll_responses=[
            # First poll at t=5s is still in_progress
            {
                "id": "inter-timeout-ok",
                "environment_id": "env-to-1",
                "status": "in_progress",
                "usage": {"total_tokens": 8000, "total_cached_tokens": 0},
            },
            # Second get happens after cancel() at timeout t=10s -> returns 'canceled'
            {
                "id": "inter-timeout-ok",
                "environment_id": "env-to-1",
                "status": "canceled",
                "usage": {"total_tokens": 9500, "total_cached_tokens": 500},
            },
        ],
    )
    tracker = PairBudgetTracker()
    ev = run_turn(
        role="investigator",
        prompt="Run I1",
        packet={"case_id": "case_A"},
        images=[_make_valid_png_bytes()],
        client=client,
        budget_tracker=tracker,
        poll_timeout_s=10.0,
        poll_interval_s=10.0,
        time_fn=fake_time,
        sleep_fn=fake_sleep,
    )
    assert client.interactions.cancel_calls == ["inter-timeout-ok"]
    assert ev.status == "cancelled"
    assert ev.scientific_complete is False
    assert ev.usage.noncached_tokens == 9000
    assert tracker.unknown_hold_active is False


def test_scenario_7_timeout_cancel_uncertain_sets_unknown_and_holds_reserve() -> None:
    """Scenario 7: Timeout -> cancel -> follow-up get still non-terminal -> status='unknown' & 30k hold blocks next turn."""
    clock = [0.0]

    def fake_time() -> float:
        val = clock[0]
        clock[0] += 15.0
        return val

    client = FakeGenAIClient(
        create_response={
            "id": "inter-timeout-unk",
            "environment_id": "env-to-2",
            "status": "in_progress",
            "usage": {"total_tokens": 4000, "total_cached_tokens": 0},
        },
        poll_responses=[
            # Post-cancel get still reports in_progress (uncertain state)
            {
                "id": "inter-timeout-unk",
                "environment_id": "env-to-2",
                "status": "in_progress",
                "usage": {"total_tokens": 4500, "total_cached_tokens": 0},
            }
        ],
    )
    tracker = PairBudgetTracker()
    ev = run_turn(
        role="investigator",
        prompt="Run I1",
        packet={"case_id": "case_A"},
        images=[_make_valid_png_bytes()],
        client=client,
        budget_tracker=tracker,
        poll_timeout_s=10.0,
        time_fn=fake_time,
        sleep_fn=lambda _: None,
    )
    assert ev.status == "unknown"
    assert ev.usage.unknown_usage is True
    assert ev.usage.noncached_tokens == UNKNOWN_USAGE_RESERVE
    assert tracker.unknown_hold_active is True

    # Any subsequent turn must be blocked by the active unknown hold
    with pytest.raises(RuntimeError, match="Unknown interaction status hold active"):
        run_turn(
            role="investigator",
            prompt="Run I2",
            packet={"case_id": "case_A"},
            images=[_make_valid_png_bytes()],
            environment_id="env-to-2",
            turn_id="I2",
            client=client,
            budget_tracker=tracker,
        )


def test_scenario_8_process_restart_journal_reconciliation(tmp_path: Path) -> None:
    """Scenario 8: Reconcile interrupted JSONL journal on restart without automatic retry."""
    journal_path = tmp_path / "interrupted_journal.jsonl"
    append_journal_event(
        journal_path,
        {
            "event": "created",
            "run_id": "run-crash-1",
            "case_id": "case_A",
            "turn_id": "I1",
            "role": "investigator",
            "interaction_id": "inter-dangling-99",
            "environment_id": "env-99",
        },
    )

    client = FakeGenAIClient(
        create_response={
            "id": "inter-dangling-99",
            "status": "completed",
            "usage": {"total_tokens": 11000, "total_cached_tokens": 1000},
        }
    )
    tracker = PairBudgetTracker()
    res = reconcile_journal(journal_path, client=client, budget_tracker=tracker)

    assert res["pending_before"] == 1
    assert len(res["reconciled"]) == 1
    assert res["reconciled"][0]["interaction_id"] == "inter-dangling-99"
    assert res["reconciled"][0]["status"] == "completed"
    # No create() call was made during reconciliation
    assert len(client.interactions.create_calls) == 0
    assert tracker.get_case_ledger("case_A").noncached_tokens == 10000

    # Re-running reconcile_journal sees 0 pending interactions
    res_again = reconcile_journal(journal_path, client=client, budget_tracker=tracker)
    assert res_again["pending_before"] == 0


def test_scenario_9_duplicate_polls_never_double_count() -> None:
    """Scenario 9: Multiple polls of the same interaction_id replace snapshot rather than summing."""
    tracker = PairBudgetTracker()
    tracker.update_interaction(
        case_id="case_A",
        interaction_id="inter-dup",
        raw_usage={"total_tokens": 5000, "total_cached_tokens": 1000},
        role="investigator",
        turn_id="I1",
        status="in_progress",
    )
    assert tracker.get_case_ledger("case_A").noncached_tokens == 4000

    tracker.update_interaction(
        case_id="case_A",
        interaction_id="inter-dup",
        raw_usage={"total_tokens": 12000, "total_cached_tokens": 2000},
        role="investigator",
        turn_id="I1",
        status="in_progress",
    )
    assert tracker.get_case_ledger("case_A").noncached_tokens == 10000

    tracker.update_interaction(
        case_id="case_A",
        interaction_id="inter-dup",
        raw_usage={"total_tokens": 18000, "total_cached_tokens": 3000},
        role="investigator",
        turn_id="I1",
        status="completed",
    )
    ledger = tracker.get_case_ledger("case_A")
    assert ledger.noncached_tokens == 15000
    assert ledger.cached_tokens == 3000
    assert ledger.total_tokens == 18000


def test_scenario_10_malformed_or_missing_usage_uses_30k_reserve() -> None:
    """Scenario 10: Missing or malformed usage metadata assigns 30,000 reserve and never infers 0."""
    for bad_usage in (None, {}, {"total_tokens": -5}, {"total_tokens": "bad"}, {"total_tokens": True}):
        snap = normalize_usage_snapshot(bad_usage)
        assert snap.unknown_usage is True
        assert snap.noncached_tokens == UNKNOWN_USAGE_RESERVE
        assert snap.total_tokens == UNKNOWN_USAGE_RESERVE
        assert snap.noncached_tokens == 30_000


def test_scenario_11_three_ledger_budget_reserves_and_ceilings() -> None:
    """Scenario 11: Verify 40k Auditor reserve, 20k A2 reserve, 140k case cap, and 280k/560k ceilings."""
    tracker = PairBudgetTracker()

    # 1. Investigator consumes 85,000 noncached tokens across prior turns -> only 55,000 left in 140,000 case cap.
    # Requesting another 30,000 for I2 would leave 25,000 < 40,000 Auditor reserve!
    tracker.update_interaction(
        case_id="case_A",
        interaction_id="inter-i1",
        raw_usage={"total_tokens": 85_000, "total_cached_tokens": 0},
        role="investigator",
        turn_id="I1",
        status="completed",
    )
    chk_inv = tracker.check_budget_before_turn("case_A", "I2", requested_noncached_cap=30_000, role="investigator")
    assert chk_inv["allowed"] is False
    assert "Auditor reserve" in chk_inv["reason"]

    # 2. Auditor A1 is allowed to use part of the Auditor reserve, EXCEPT it cannot encroach on the 20,000 A2 reserve!
    # At 85,000 used, if I2 used 15,000 -> 100,000 used (40,000 left for A1+A2).
    tracker.update_interaction(
        case_id="case_A",
        interaction_id="inter-i2",
        raw_usage={"total_tokens": 15_000, "total_cached_tokens": 0},
        role="investigator",
        turn_id="I2",
        status="completed",
    )
    # Now 100,000 noncached used. Requesting 25,000 for A1 would leave 15,000 < 20,000 A2 reserve!
    chk_a1_excess = tracker.check_budget_before_turn("case_A", "A1", requested_noncached_cap=25_000, role="auditor")
    assert chk_a1_excess["allowed"] is False
    assert "A2 reserve" in chk_a1_excess["reason"]

    # Requesting 20,000 for A1 leaves exactly 20,000 for A2 -> allowed!
    chk_a1_ok = tracker.check_budget_before_turn("case_A", "A1", requested_noncached_cap=20_000, role="auditor")
    assert chk_a1_ok["allowed"] is True

    # 3. Cached-inclusive case ceiling (280,000) and pair ceiling (560,000)
    tracker.update_interaction(
        case_id="case_A",
        interaction_id="inter-a1",
        raw_usage={"total_tokens": 170_000, "total_cached_tokens": 155_000},
        role="auditor",
        turn_id="A1",
        status="completed",
    )
    # Total tokens in case_A is now 85k + 15k + 170k = 270k. Requesting 20k for A2 -> 290k > 280k case ceiling!
    chk_ceil = tracker.check_budget_before_turn("case_A", "A2", requested_noncached_cap=20_000, role="auditor")
    assert chk_ceil["allowed"] is False
    assert "cached-inclusive ceiling" in chk_ceil["reason"]


def test_scenario_12_oversized_or_corrupt_images_rejected_before_sdk_call() -> None:
    """Scenario 12: Reject >512 KiB, >2048x2048, or non-PNG bytes before any client.interactions.create call."""
    client = FakeGenAIClient()

    # 1. Corrupt non-PNG bytes
    with pytest.raises(ValueError, match="PNG"):
        run_turn(
            role="investigator",
            prompt="Run I1",
            packet={"case_id": "case_A"},
            images=[b"NOT_A_PNG_FILE_AT_ALL"],
            client=client,
        )
    assert len(client.interactions.create_calls) == 0

    # 2. Image dimensions > 2048x2048
    big_dim_png = _make_valid_png_bytes(width=2050, height=64)
    with pytest.raises(ValueError, match="exceed 2048x2048"):
        run_turn(
            role="investigator",
            prompt="Run I1",
            packet={"case_id": "case_A"},
            images=[big_dim_png],
            client=client,
        )
    assert len(client.interactions.create_calls) == 0

    # 3. Image size > 512 KiB
    oversized_bytes = b"\x89PNG\r\n\x1a\n" + (b"A" * (MAX_IMAGE_BYTES + 10))
    with pytest.raises(ValueError, match="exceeds 512 KiB"):
        run_turn(
            role="investigator",
            prompt="Run I1",
            packet={"case_id": "case_A"},
            images=[oversized_bytes],
            client=client,
        )
    assert len(client.interactions.create_calls) == 0


def test_scenario_13_dangerous_archive_paths_and_bombs_rejected() -> None:
    """Scenario 13: Reject ../evil.json, /etc/passwd, symlinks, and >512 KiB bombs in archives."""
    # 1. Path traversal ../proposal.json in zip
    zip_traversal = io.BytesIO()
    with zipfile.ZipFile(zip_traversal, "w") as zf:
        zf.writestr("../proposal.json", '{"evil": true}')
    client_trav = FakeGenAIClient(files={"proposal.zip": zip_traversal.getvalue()})
    with pytest.raises(ValueError, match="traversal"):
        retrieve_environment_file(client_trav, "env-1", "proposal.json")

    # 2. Absolute path /etc/passwd in zip and tar
    zip_abs = io.BytesIO()
    with zipfile.ZipFile(zip_abs, "w") as zf:
        zf.writestr("/etc/passwd", '{"evil": true}')
    client_abs = FakeGenAIClient(files={"proposal.zip": zip_abs.getvalue()})
    with pytest.raises(ValueError, match="absolute"):
        retrieve_environment_file(client_abs, "env-1", "proposal.json")

    # 3. Symlink in zip (unix mode 0o120000) and tar archive
    zip_sym = io.BytesIO()
    with zipfile.ZipFile(zip_sym, "w") as zf:
        zinfo = zipfile.ZipInfo("proposal.json")
        zinfo.external_attr = (0o120777 << 16)
        zf.writestr(zinfo, "/etc/shadow")
    client_sym = FakeGenAIClient(files={"proposal.zip": zip_sym.getvalue()})
    with pytest.raises(ValueError, match="symlink"):
        retrieve_environment_file(client_sym, "env-1", "proposal.json")

    tar_sym = io.BytesIO()
    with tarfile.open(fileobj=tar_sym, mode="w:gz") as tf:
        sym = tarfile.TarInfo(name="proposal.tar.gz")
        sym.type = tarfile.SYMTYPE
        sym.linkname = "/etc/shadow"
        tf.addfile(sym)
    client_tar_sym = FakeGenAIClient(files={"proposal.tar.gz": tar_sym.getvalue()})
    with pytest.raises(ValueError, match="symlink"):
        retrieve_environment_file(client_tar_sym, "env-1", "proposal.tar.gz")

    # 4. Decompression bomb (> 512 KiB uncompressed inside small compressed zip)
    zip_bomb = io.BytesIO()
    with zipfile.ZipFile(zip_bomb, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("proposal.json", "{" + (" " * (600 * 1024)) + "}")
    client_bomb = FakeGenAIClient(files={"proposal.zip": zip_bomb.getvalue()})
    with pytest.raises(ValueError, match="exceeds cap"):
        retrieve_environment_file(client_bomb, "env-1", "proposal.json")


def test_scenario_14_duplicate_basename_ambiguity_rejected() -> None:
    """Scenario 14: Reject duplicate basename matches across directories."""
    client = FakeGenAIClient(
        files={
            "dir1/proposal.json": b'{"v": 1}',
            "dir2/proposal.json": b'{"v": 2}',
        }
    )
    with pytest.raises(ValueError, match="Duplicate basename ambiguity"):
        retrieve_environment_file(client, "env-dup", "proposal.json")


def test_scenario_15_invalid_utf8_and_invalid_json_fail_cleanly() -> None:
    """Scenario 15: Invalid UTF-8 or malformed JSON sets scientific_complete=False cleanly."""
    client_bad_utf8 = FakeGenAIClient(
        create_response={
            "id": "inter-utf8",
            "environment_id": "env-utf8",
            "status": "completed",
            "steps": [{"type": "code_execution"}],
            "usage": {"total_tokens": 2000, "total_cached_tokens": 0},
        },
        files={"proposal.json": b"\xff\xfe\xfd"},
    )
    ev_utf8 = run_turn(
        role="investigator",
        prompt="Run I1",
        packet={"case_id": "case_A"},
        images=[_make_valid_png_bytes()],
        client=client_bad_utf8,
        sleep_fn=lambda _: None,
    )
    assert ev_utf8.scientific_complete is False
    assert ev_utf8.retrieved_json is None
    assert ev_utf8.error is not None and "UnicodeDecodeError" in ev_utf8.error

    client_bad_json = FakeGenAIClient(
        create_response={
            "id": "inter-json",
            "environment_id": "env-json",
            "status": "completed",
            "steps": [{"type": "code_execution"}],
            "usage": {"total_tokens": 2000, "total_cached_tokens": 0},
        },
        files={"proposal.json": b"{not valid json"},
    )
    ev_json = run_turn(
        role="investigator",
        prompt="Run I1",
        packet={"case_id": "case_A"},
        images=[_make_valid_png_bytes()],
        client=client_bad_json,
        sleep_fn=lambda _: None,
    )
    assert ev_json.scientific_complete is False
    assert ev_json.retrieved_json is None
    assert ev_json.error is not None and "JSONDecodeError" in ev_json.error


def test_scenario_16_environment_isolation_followup_contract_and_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario 16: Investigator/Auditor environment isolation, previous_interaction_id=None, and secret/base64 redaction."""
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSySecretKeyForUnitTesting123456")
    journal_path = tmp_path / "iso_journal.jsonl"

    client = FakeGenAIClient(
        create_response={
            "id": "inter-i1",
            "environment_id": "env-shared-inv",
            "status": "completed",
            "steps": [{"type": "code_execution"}],
            "usage": {"total_tokens": 3000, "total_cached_tokens": 500},
        },
        files={"proposal.json": b'{"target_rack": "N4"}', "decision.json": b'{"target": "N2"}'},
    )
    tracker = PairBudgetTracker()

    # 1. Fresh Investigator turn I1 creates environment_id 'env-shared-inv'
    ev_i1 = run_turn(
        role="investigator",
        prompt="Run I1 with key AIzaSySecretKeyForUnitTesting123456",
        packet={"case_id": "case_A"},
        images=[_make_valid_png_bytes()],
        expected_filename="proposal.json",
        turn_id="I1",
        client=client,
        journal_path=journal_path,
        budget_tracker=tracker,
    )
    assert ev_i1.scientific_complete is True
    create_i1 = client.interactions.create_calls[0]
    assert create_i1["previous_interaction_id"] is None
    assert create_i1["environment"]["type"] == "remote"
    assert "sources" in create_i1["environment"]

    # 2. Follow-up Investigator turn I2 reuses 'env-shared-inv' and keeps previous_interaction_id=None
    ev_i2 = run_turn(
        role="investigator",
        prompt="Run I2 with probe reading",
        packet={"case_id": "case_A"},
        images=[_make_valid_png_bytes()],
        environment_id="env-shared-inv",
        expected_filename="decision.json",
        turn_id="I2",
        client=client,
        journal_path=journal_path,
        budget_tracker=tracker,
    )
    assert ev_i2.scientific_complete is True
    create_i2 = client.interactions.create_calls[1]
    assert create_i2["previous_interaction_id"] is None
    assert create_i2["environment"] == {"type": "remote", "environment_id": "env-shared-inv"}

    # 3. Auditor A1 attempting to reuse Investigator's 'env-shared-inv' must raise PermissionError
    with pytest.raises(PermissionError, match="Cross-role environment reuse forbidden"):
        run_turn(
            role="auditor",
            prompt="Run A1",
            packet={"case_id": "case_A"},
            images=[_make_valid_png_bytes()],
            environment_id="env-shared-inv",
            expected_filename="audit_a1.json",
            turn_id="A1",
            client=client,
            budget_tracker=tracker,
        )

    # 4. Leak scanner blocks private truth keys in outgoing packet or prompt
    with pytest.raises(PermissionError, match="Leakage barrier triggered"):
        run_turn(
            role="auditor",
            prompt="Run A1",
            packet={"case_id": "case_A", "sensor_to_rack": {"T1": "N1"}},
            images=[_make_valid_png_bytes()],
            turn_id="A1",
            client=client,
        )

    # 5. Journal and redact_for_log strip API keys and base64 image blobs
    journal_text = journal_path.read_text(encoding="utf-8")
    assert "AIzaSySecretKeyForUnitTesting123456" not in journal_text
    assert "[REDACTED_API_KEY]" in journal_text
    assert "[REDACTED_BASE64_PNG:" in journal_text

    auth_cfg = resolve_auth_config(env={"GEMINI_API_KEY": "AIzaSySecretKeyForUnitTesting123456"})
    redacted_auth = redact_for_log(auth_cfg)
    assert redacted_auth["api_key"] == "[REDACTED]"
