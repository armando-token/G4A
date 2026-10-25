"""Single-shot diagnostic and voltage-drop probe interfaces for BlackboxDC.

Provides:
1. Phase 1 `DiagnosticProbeSession` and `ProbeReceipt`:
   - Supports the 3-item diagnostic menu (`"voltage_drop"`,
     `"temperature_snapshot"`, `"controller_setting"`) on targets `"N1".."N4"`.
   - Requires a non-empty `i1_proposal_hash` (via `freeze_i1_proposal` or
     `issue_receipt`) prior to issuing the single receipt.
   - Enforces a strict 1-receipt budget per session; any second request,
     post-hoc request, missing/mismatched `i1_proposal_hash`, invalid `kind`, or
     invalid `target` is recorded in an immutable `rejected_attempts` audit log
     and raises `RuntimeError` or `ValueError` while preserving the issued
     receipt and `ledger` unchanged.
2. Backward-compatible `SingleShotProbeSession` for downstream protocol stages.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union

from blackbox.contracts import ProbeRequest, ProbeResponse
from blackbox.domain import (
    AMBIENT_TEMP_C,
    RackParams,
    World,
    compute_probe_voltage_drop,
    compute_steady_state_temperature_c,
    infer_resistance_from_probe,
)
from blackbox.fixture import SealedWorldTruth


VALID_DIAGNOSTIC_KINDS: Tuple[str, ...] = (
    "voltage_drop",
    "temperature_snapshot",
    "controller_setting",
)
VALID_RACK_TARGETS: Tuple[str, ...] = ("N1", "N2", "N3", "N4")


def _compute_hash(payload: Any) -> str:
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            raise ValueError("i1_proposal_hash must be a non-empty string")
        return text
    if isinstance(payload, Mapping):
        raw = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()
    raise ValueError("i1_proposal_or_hash must be a non-empty string or mapping")


@dataclass(frozen=True)
class ProbeReceipt:
    """Immutable receipt returned for the single permitted diagnostic probe."""

    receipt_id: str
    case_id: str
    kind: str
    target: str
    value: Union[float, str]
    exact_value: Union[float, str]
    units: str
    i1_proposal_hash: str
    timestamp_s: int
    receipt_sha256: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def model_dump(self) -> Dict[str, Any]:
        return asdict(self)

    def __getitem__(self, item: str) -> Any:
        return asdict(self)[item]


@dataclass(frozen=True)
class RejectedProbeAttempt:
    """Immutable audit log entry for any rejected or post-hoc probe request."""

    attempt_index: int
    kind: Any
    target: Any
    i1_proposal_hash: Any
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def __getitem__(self, item: str) -> Any:
        return asdict(self)[item]


class DiagnosticProbeSession:
    """Phase 1 single-shot diagnostic probe gatekeeper with immutable ledger."""

    def __init__(
        self,
        private_world: Union[Mapping[str, Any], World],
        i1_proposal_hash: Optional[str] = None,
    ) -> None:
        if isinstance(private_world, World):
            self._world = private_world
            self._case_id = "UNKNOWN"
            self._controller_settings: Dict[str, str] = {
                r.rack_id: "AUTO_BALANCED_480V" for r in private_world.racks
            }
        elif isinstance(private_world, Mapping) and "world" in private_world and isinstance(
            private_world["world"], World
        ):
            self._world = private_world["world"]
            self._case_id = str(private_world.get("case_id", "UNKNOWN"))
            self._controller_settings = dict(
                private_world.get(
                    "controller_settings",
                    {r.rack_id: "AUTO_BALANCED_480V" for r in self._world.racks},
                )
            )
        else:
            raise ValueError("private_world must be a World instance or build_private_world dict")

        self._frozen_i1_hash: Optional[str] = None
        if i1_proposal_hash is not None:
            self.freeze_i1_proposal(i1_proposal_hash)

        self._receipt: Optional[ProbeReceipt] = None
        self._ledger: List[ProbeReceipt] = []
        self._rejected_attempts: List[RejectedProbeAttempt] = []

    @property
    def i1_proposal_hash(self) -> Optional[str]:
        return self._frozen_i1_hash

    @property
    def executed(self) -> bool:
        return self._receipt is not None

    @property
    def receipt(self) -> Optional[ProbeReceipt]:
        return self._receipt

    @property
    def ledger(self) -> Tuple[ProbeReceipt, ...]:
        """Return an immutable snapshot of issued receipts (at most 1)."""
        return tuple(self._ledger)

    @property
    def rejected_attempts(self) -> Tuple[RejectedProbeAttempt, ...]:
        """Return an immutable snapshot of all rejected probe attempts."""
        return tuple(self._rejected_attempts)

    def _record_rejection(
        self,
        kind: Any,
        target: Any,
        i1_proposal_hash: Any,
        reason: str,
    ) -> None:
        entry = RejectedProbeAttempt(
            attempt_index=len(self._rejected_attempts) + 1,
            kind=kind,
            target=target,
            i1_proposal_hash=i1_proposal_hash,
            reason=reason,
        )
        self._rejected_attempts.append(entry)

    def freeze_i1_proposal(
        self,
        i1_payload_or_hash: Union[str, Mapping[str, Any]],
    ) -> str:
        """Freeze the Investigator I1 proposal hash prior to issuing a probe receipt."""
        try:
            computed = _compute_hash(i1_payload_or_hash)
        except ValueError as exc:
            self._record_rejection(None, None, i1_payload_or_hash, str(exc))
            raise

        if self._frozen_i1_hash is not None and self._frozen_i1_hash != computed:
            reason = "i1_proposal_hash is already frozen and cannot be mutated"
            self._record_rejection(None, None, computed, reason)
            raise RuntimeError(reason)

        self._frozen_i1_hash = computed
        return computed

    def issue_receipt(
        self,
        kind: str,
        target: str,
        i1_proposal_hash: Optional[str] = None,
    ) -> ProbeReceipt:
        """Issue the single allowed diagnostic probe receipt at t = 0."""
        # 1. Enforce single-shot budget first if already executed
        if self._receipt is not None:
            reason = "Probe budget exhausted: only one diagnostic probe receipt is permitted per session"
            self._record_rejection(kind, target, i1_proposal_hash or self._frozen_i1_hash, reason)
            raise RuntimeError(reason)

        # 2. Validate / freeze i1_proposal_hash
        effective_hash: Optional[str] = self._frozen_i1_hash
        if i1_proposal_hash is not None:
            if not isinstance(i1_proposal_hash, str) or not i1_proposal_hash.strip():
                reason = "i1_proposal_hash must be a non-empty string"
                self._record_rejection(kind, target, i1_proposal_hash, reason)
                raise ValueError(reason)
            candidate_hash = i1_proposal_hash.strip()
            if self._frozen_i1_hash is not None and candidate_hash != self._frozen_i1_hash:
                reason = "Provided i1_proposal_hash does not match frozen I1 proposal hash"
                self._record_rejection(kind, target, i1_proposal_hash, reason)
                raise ValueError(reason)
            effective_hash = candidate_hash

        if not effective_hash:
            reason = "Cannot issue probe receipt before freezing or providing a non-empty i1_proposal_hash"
            self._record_rejection(kind, target, i1_proposal_hash, reason)
            raise RuntimeError(reason)

        # 3. Validate diagnostic kind and target rack
        if not isinstance(kind, str) or kind not in VALID_DIAGNOSTIC_KINDS:
            reason = f"Invalid diagnostic kind {kind!r}; expected one of {VALID_DIAGNOSTIC_KINDS}"
            self._record_rejection(kind, target, effective_hash, reason)
            raise ValueError(reason)

        if not isinstance(target, str) or target not in VALID_RACK_TARGETS:
            reason = f"Invalid diagnostic target {target!r}; expected one of {VALID_RACK_TARGETS}"
            self._record_rejection(kind, target, effective_hash, reason)
            raise ValueError(reason)

        # Lock the I1 hash now that we are issuing the receipt
        self._frozen_i1_hash = effective_hash
        rack: RackParams = self._world.get_rack(target)

        if kind == "voltage_drop":
            r_t0 = rack.R0_ohm * (
                1.0 + rack.alpha_per_C * max(rack.initial_temp_C - self._world.ambient_C, 0.0)
            )
            exact_val: Union[float, str] = float(rack.current_A * r_t0)
            quant_val: Union[float, str] = round(float(exact_val), 6)
            units = "V"
        elif kind == "temperature_snapshot":
            exact_val = float(rack.initial_temp_C)
            quant_val = round(float(exact_val), 6)
            units = "C"
        else:  # controller_setting
            mode_str = str(self._controller_settings.get(target, "AUTO_BALANCED_480V"))
            exact_val = mode_str
            quant_val = mode_str
            units = "mode"

        core_payload = {
            "receipt_id": f"RCPT-{self._case_id}-001",
            "case_id": self._case_id,
            "kind": kind,
            "target": target,
            "value": quant_val,
            "exact_value": exact_val,
            "units": units,
            "i1_proposal_hash": effective_hash,
            "timestamp_s": 0,
        }
        digest = hashlib.sha256(
            json.dumps(core_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

        receipt = ProbeReceipt(**core_payload, receipt_sha256=digest)
        self._receipt = receipt
        self._ledger.append(receipt)
        return receipt

    def probe(
        self,
        kind: str,
        target: str,
        i1_proposal_hash: Optional[str] = None,
    ) -> ProbeReceipt:
        """Alias for issue_receipt."""
        return self.issue_receipt(kind=kind, target=target, i1_proposal_hash=i1_proposal_hash)


# ---------------------------------------------------------------------------
# Backward-Compatible Phase 00 / Downstream Probe Session
# ---------------------------------------------------------------------------


class SingleShotProbeSession:
    """Enforces that at most one voltage-drop probe measurement is taken per case run."""

    def __init__(self, truth: SealedWorldTruth, ambient_temp_c: float = AMBIENT_TEMP_C) -> None:
        self._truth = truth
        self._ambient_temp_c = ambient_temp_c
        self._executed: bool = False
        self._response: Optional[ProbeResponse] = None

    @property
    def executed(self) -> bool:
        return self._executed

    @property
    def last_response(self) -> Optional[ProbeResponse]:
        return self._response

    def execute_probe(
        self,
        request: ProbeRequest,
        timestamp_utc: Optional[str] = None,
    ) -> ProbeResponse:
        """Execute the single allowed voltage-drop probe on the target node."""
        if self._executed:
            raise RuntimeError(
                f"Probe budget exhausted for {self._truth.case_id}: only one probe is permitted."
            )

        node_id = request.target_node
        if node_id not in self._truth.node_states:
            raise ValueError(f"Unknown target_node: {node_id!r}")

        node_state = self._truth.node_states[node_id]
        base_i = float(node_state["load_current_a"])
        r_true = float(self._truth.node_resistances_ohm[node_id])
        delta_i = float(request.delta_current_a)

        v_base, v_probed, delta_v = compute_probe_voltage_drop(
            base_current_a=base_i,
            delta_current_a=delta_i,
            resistance_ohm=r_true,
        )
        r_inferred = infer_resistance_from_probe(delta_v=delta_v, delta_current_a=delta_i)
        temp_c = compute_steady_state_temperature_c(
            current_a=base_i,
            resistance_ohm=r_true,
            ambient_temp_c=self._ambient_temp_c,
        )
        ts = timestamp_utc or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        response = ProbeResponse(
            case_id=self._truth.case_id,
            target_node=node_id,
            connected_feeder_in_sld=self._truth.node_to_feeder[node_id],
            base_current_a=base_i,
            delta_current_a=delta_i,
            base_voltage_drop_v=v_base,
            probed_voltage_drop_v=v_probed,
            delta_voltage_drop_v=delta_v,
            inferred_resistance_ohm=r_inferred,
            terminal_temperature_c=temp_c,
            timestamp_utc=ts,
        )
        self._executed = True
        self._response = response
        return response
