"""Protocol state machine for BlackboxDC (I1 -> Probe -> I2 and A1 -> A2).

Enforces strict ordering and zero private-truth leakage before all four safety
locks (I1, I2, A1, A2) are cryptographically committed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from blackbox.contracts import (
    AuditorA1Contract,
    AuditorA2Contract,
    InvestigatorI1Contract,
    InvestigatorI2Contract,
    ProbeResponse,
    SafetyLockRecord,
)
from blackbox.fixture import PublicCaseEvidence, SealedWorldTruth
from blackbox.managed import InteractionAccounting, ManagedAgentTransport
from blackbox.probe import SingleShotProbeSession


def _hash_payload(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass
class CaseProtocolRunResult:
    """Complete record of the 4 locked interactions (I1, I2, A1, A2) and 1 probe for a case."""

    case_id: str
    i1: InvestigatorI1Contract
    probe_response: ProbeResponse
    i2: InvestigatorI2Contract
    a1: AuditorA1Contract
    a2: AuditorA2Contract
    locks: Dict[str, SafetyLockRecord]
    accounting: List[InteractionAccounting]
    zero_leakage_verified: bool


class BlackboxProtocolStateMachine:
    """Executes the two-stage Investigator and independent two-stage Auditor protocol."""

    def __init__(
        self,
        public_evidence: PublicCaseEvidence,
        sealed_truth: SealedWorldTruth,
        transport: ManagedAgentTransport,
    ) -> None:
        self.public_evidence = public_evidence
        self._sealed_truth = sealed_truth
        self.transport = transport
        self.probe_session = SingleShotProbeSession(
            truth=sealed_truth,
            ambient_temp_c=public_evidence.ambient_temp_c,
        )
        self.locks: Dict[str, SafetyLockRecord] = {}
        self._truth_unsealed: bool = False

    @property
    def all_locks_committed(self) -> bool:
        return set(self.locks.keys()) == {"I1", "I2", "A1", "A2"}

    def unseal_truth_for_oracle(self) -> SealedWorldTruth:
        """Unseal world truth only after all four interaction locks (I1, I2, A1, A2) are recorded."""
        if not self.all_locks_committed:
            missing = sorted({"I1", "I2", "A1", "A2"} - set(self.locks.keys()))
            raise PermissionError(
                f"Cannot unseal ground truth before all interaction locks are committed. Missing: {missing}"
            )
        self._truth_unsealed = True
        return self._sealed_truth

    def _commit_lock(
        self,
        interaction_id: str,
        payload: Dict[str, Any],
    ) -> SafetyLockRecord:
        if self._truth_unsealed:
            raise PermissionError(
                f"Leakage violation: truth was already unsealed prior to locking {interaction_id}"
            )
        if interaction_id in self.locks:
            raise RuntimeError(f"Lock {interaction_id} already committed for {self.public_evidence.case_id}")
        rec = SafetyLockRecord(
            interaction_id=interaction_id,  # type: ignore[arg-type]
            case_id=self.public_evidence.case_id,
            payload_sha256=_hash_payload(payload),
            locked_at_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            private_truth_accessed_before_lock=self._truth_unsealed,
            payload=payload,
        )
        self.locks[interaction_id] = rec
        return rec

    def run_case_protocol(self) -> CaseProtocolRunResult:
        """Execute I1 -> Probe -> I2 and independent A1 -> A2 with strict lock ordering."""
        cid = self.public_evidence.case_id
        pub_dict = asdict(self.public_evidence)
        handoff_paths: List[Path] = []
        for img_p in (self.public_evidence.sld_image_path, self.public_evidence.scene_3d_image_path):
            if Path(img_p).exists():
                handoff_paths.append(Path(img_p))

        accounting_records: List[InteractionAccounting] = []

        # Step 1: Investigator I1 (Pre-probe hypothesis + single probe request)
        i1_raw, acc_i1 = self.transport.invoke_interaction(
            interaction_id="I1",
            case_id=cid,
            agent_role="Investigator",
            prompt_context={"public_evidence": pub_dict},
            handoff_files=handoff_paths,
        )
        i1_contract = InvestigatorI1Contract.model_validate(i1_raw)
        self._commit_lock("I1", i1_contract.model_dump())
        accounting_records.append(acc_i1)

        # Step 2: Independent Auditor A1 (Pre-probe visual & topology verification)
        a1_raw, acc_a1 = self.transport.invoke_interaction(
            interaction_id="A1",
            case_id=cid,
            agent_role="Auditor",
            prompt_context={"public_evidence": pub_dict},
            handoff_files=handoff_paths,
        )
        a1_contract = AuditorA1Contract.model_validate(a1_raw)
        self._commit_lock("A1", a1_contract.model_dump())
        accounting_records.append(acc_a1)

        # Step 3: Execute single active voltage-drop probe requested by I1
        probe_resp = self.probe_session.execute_probe(i1_contract.probe_request)

        # Step 4: Investigator I2 (Post-probe diagnosis and remediation lock)
        i2_raw, acc_i2 = self.transport.invoke_interaction(
            interaction_id="I2",
            case_id=cid,
            agent_role="Investigator",
            prompt_context={
                "public_evidence": pub_dict,
                "i1_contract": i1_contract.model_dump(),
                "probe_response": probe_resp.model_dump(),
            },
            handoff_files=handoff_paths,
        )
        i2_contract = InvestigatorI2Contract.model_validate(i2_raw)
        self._commit_lock("I2", i2_contract.model_dump())
        accounting_records.append(acc_i2)

        # Step 5: Independent Auditor A2 (Post-probe verification of I2 remediation)
        a2_raw, acc_a2 = self.transport.invoke_interaction(
            interaction_id="A2",
            case_id=cid,
            agent_role="Auditor",
            prompt_context={
                "public_evidence": pub_dict,
                "a1_contract": a1_contract.model_dump(),
                "i2_contract": i2_contract.model_dump(),
                "probe_response": probe_resp.model_dump(),
            },
            handoff_files=handoff_paths,
        )
        a2_contract = AuditorA2Contract.model_validate(a2_raw)
        self._commit_lock("A2", a2_contract.model_dump())
        accounting_records.append(acc_a2)

        zero_leakage = all(
            not lk.private_truth_accessed_before_lock for lk in self.locks.values()
        )
        return CaseProtocolRunResult(
            case_id=cid,
            i1=i1_contract,
            probe_response=probe_resp,
            i2=i2_contract,
            a1=a1_contract,
            a2=a2_contract,
            locks=dict(self.locks),
            accounting=accounting_records,
            zero_leakage_verified=zero_leakage,
        )
