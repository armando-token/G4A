"""Deterministic safety, geometry, and numerical oracle grading Investigator and Auditor outputs.

Provides:
1. Phase 4 (`04_AGENT_PROTOCOL_AND_CONTRACTS.md`) guarded truth loader and 4-layer V2 evaluator:
   - `GuardedOracleTruthLoader`: Enforces single-probe budget after `L1_I1_PROPOSAL`
     and refuses to unseal private evaluation truth until all 4 interaction locks
     (`L1_I1_PROPOSAL`, `L2_I2_PLAN`, `L3_A1_GRAPH`, `L4_A2_CHALLENGE`) are committed
     and verified.
   - Bounding box geometry evaluators (`compute_bbox_iou`, `is_center_inside_bbox`,
     `evaluate_connector_localization`) with rack-wide box rejection.
   - `CaseEvaluationReportV2` and `evaluate_case_v2(...)` computing the 4-layer
     grading flags (`structural_valid`, `scientific_correct`, `operational_complete`,
     `live_provider_evidence`, `strict_pass`, `offline_strict_pass`) and paired A/B
     campaign evaluation (`evaluate_ab_campaign_v2`).
2. Backward-compatible Phase 00 oracle functions (`CaseOracleGrade`, `grade_case_run`,
   `grade_ab_campaign`).
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Mapping, Optional, Sequence, Tuple

from blackbox.contracts import (
    CANONICAL_ACTION_IDS,
    CANONICAL_PDUS,
    CANONICAL_RACKS,
    LOCK_STAGE_ORDER,
    ArtifactLockRecordV2,
    AuditChallengeV2,
    IndependentGraphV2,
    PlanV2,
    ProposalV2,
    build_world_from_plan_v2,
    canonical_sha256,
    normalize_action_id,
    normalize_pdu_id,
    normalize_rack_id,
    normalize_sensor_id,
    validate_contract_hygiene,
    verify_lock_chain_v2,
    verify_plan_against_inferred_network,
)
from blackbox.domain import (
    DEFAULT_MIGRATION_AMPS,
    Migration,
    PDU,
    RackParams,
    World,
    evaluate_remediation_safety,
    simulate_world,
)
from blackbox.fixture import (
    CONNECTOR_BBOXES_CANONICAL,
    SealedWorldTruth,
    build_private_world,
)
from blackbox.probe import (
    DiagnosticProbeSession,
    ProbeReceipt,
)

if TYPE_CHECKING:
    from blackbox.protocol import CaseProtocolRunResult


DEFAULT_3D_N4_CONNECTOR_BBOX: Tuple[int, int, int, int] = (1079, 325, 1143, 393)
DEFAULT_FIXTURE_N4_CONNECTOR_BBOX: Tuple[int, int, int, int] = (
    int(CONNECTOR_BBOXES_CANONICAL["N4"][0]),
    int(CONNECTOR_BBOXES_CANONICAL["N4"][1]),
    int(CONNECTOR_BBOXES_CANONICAL["N4"][2]),
    int(CONNECTOR_BBOXES_CANONICAL["N4"][3]),
)


def verify_probe_receipt(receipt: ProbeReceipt | Mapping[str, Any]) -> bool:
    """Verify the deterministic SHA-256 signature of a `ProbeReceipt`."""
    d = receipt.to_dict() if isinstance(receipt, ProbeReceipt) else dict(receipt)
    core_payload = {
        "receipt_id": str(d["receipt_id"]),
        "case_id": str(d["case_id"]),
        "kind": str(d["kind"]),
        "target": str(d["target"]),
        "value": d["value"],
        "exact_value": d["exact_value"],
        "units": str(d["units"]),
        "i1_proposal_hash": str(d["i1_proposal_hash"]),
        "timestamp_s": int(d["timestamp_s"]),
    }
    expected_digest = hashlib.sha256(
        json.dumps(core_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return bool(expected_digest == str(d.get("receipt_sha256", "")))


# ---------------------------------------------------------------------------
# Phase 4: Bounding-Box Localization Geometry
# ---------------------------------------------------------------------------


def _normalize_bbox_tuple(box: Sequence[int | float]) -> Tuple[float, float, float, float]:
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        raise ValueError(f"Expected 4-element bbox [x1, y1, x2, y2], got {box!r}")
    x1, y1, x2, y2 = (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
    if x2 <= x1 or y2 <= y1:
        raise ValueError(f"Invalid bbox ordering in {box!r}: require x2 > x1 and y2 > y1")
    return x1, y1, x2, y2


def compute_bbox_iou(
    box_a: Sequence[int | float],
    box_b: Sequence[int | float],
) -> float:
    """Compute exact Intersection-over-Union (IoU) between two `[x1, y1, x2, y2]` boxes."""
    ax1, ay1, ax2, ay2 = _normalize_bbox_tuple(box_a)
    bx1, by1, bx2, by2 = _normalize_bbox_tuple(box_b)

    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)

    inter_w = max(0.0, inter_x2 - inter_x1)
    inter_h = max(0.0, inter_y2 - inter_y1)
    inter_area = inter_w * inter_h
    if inter_area <= 0.0:
        return 0.0

    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)
    union_area = area_a + area_b - inter_area
    if union_area <= 0.0:
        return 0.0
    return float(inter_area / union_area)


def is_center_inside_bbox(
    box_pred: Sequence[int | float],
    box_gt: Sequence[int | float],
) -> bool:
    """Return True if the geometric center of `box_pred` lies within `box_gt`."""
    px1, py1, px2, py2 = _normalize_bbox_tuple(box_pred)
    gx1, gy1, gx2, gy2 = _normalize_bbox_tuple(box_gt)
    cx = 0.5 * (px1 + px2)
    cy = 0.5 * (py1 + py2)
    return bool(gx1 <= cx <= gx2 and gy1 <= cy <= gy2)


def evaluate_connector_localization(
    bbox_xyxy_px: Sequence[int | float],
    gt_bboxes: Optional[Sequence[Any]] = None,
    *,
    gt_bbox: Optional[Sequence[int | float]] = None,
    min_iou: float = 0.30,
    max_box_width: int = 180,
    max_box_height: int = 180,
    max_area_ratio: float = 4.0,
) -> Dict[str, Any]:
    """Evaluate predicted N4 connector bounding box localization.

    Passes when:
    1. Either `IoU >= min_iou` (default 0.30) OR predicted center is inside the
       ground-truth N4 connector bounding box, AND
    2. The predicted box is NOT an overly broad rack-wide box
       (`width <= max_box_width`, `height <= max_box_height`, and
       `pred_area <= max_area_ratio * gt_area`).
    """
    px1, py1, px2, py2 = _normalize_bbox_tuple(bbox_xyxy_px)
    pred_w = px2 - px1
    pred_h = py2 - py1
    pred_area = pred_w * pred_h

    candidate_gts: List[Tuple[float, float, float, float]] = []
    if gt_bbox is not None:
        candidate_gts.append(_normalize_bbox_tuple(gt_bbox))
    elif gt_bboxes is not None:
        if (
            len(gt_bboxes) == 4
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in gt_bboxes)
        ):
            candidate_gts.append(_normalize_bbox_tuple(gt_bboxes))  # type: ignore[arg-type]
        else:
            for b in gt_bboxes:
                candidate_gts.append(_normalize_bbox_tuple(b))
    else:
        candidate_gts.append(_normalize_bbox_tuple(DEFAULT_3D_N4_CONNECTOR_BBOX))
        candidate_gts.append(_normalize_bbox_tuple(DEFAULT_FIXTURE_N4_CONNECTOR_BBOX))

    if not candidate_gts:
        candidate_gts.append(_normalize_bbox_tuple(DEFAULT_3D_N4_CONNECTOR_BBOX))

    best_entry: Optional[Dict[str, Any]] = None
    for gt in candidate_gts:
        gx1, gy1, gx2, gy2 = gt
        gt_area = (gx2 - gx1) * (gy2 - gy1)
        iou = compute_bbox_iou((px1, py1, px2, py2), gt)
        center_ok = is_center_inside_bbox((px1, py1, px2, py2), gt)
        area_ratio = pred_area / gt_area if gt_area > 0.0 else float("inf")
        is_rack_wide = bool(
            pred_w > max_box_width
            or pred_h > max_box_height
            or area_ratio > max_area_ratio
        )
        localized_pass = bool((iou >= min_iou or center_ok) and not is_rack_wide)
        entry = {
            "predicted_bbox": [int(round(px1)), int(round(py1)), int(round(px2)), int(round(py2))],
            "matched_gt_bbox": [int(round(gx1)), int(round(gy1)), int(round(gx2)), int(round(gy2))],
            "iou": round(float(iou), 6),
            "center_inside": bool(center_ok),
            "pred_width": round(float(pred_w), 2),
            "pred_height": round(float(pred_h), 2),
            "pred_area": round(float(pred_area), 2),
            "gt_area": round(float(gt_area), 2),
            "area_ratio": round(float(area_ratio), 4),
            "is_rack_wide": bool(is_rack_wide),
            "localized_pass": bool(localized_pass),
        }
        if best_entry is None:
            best_entry = entry
        else:
            cur_key = (
                1 if entry["localized_pass"] else 0,
                float(entry["iou"]),
                1 if entry["center_inside"] else 0,
            )
            best_key = (
                1 if best_entry["localized_pass"] else 0,
                float(best_entry["iou"]),
                1 if best_entry["center_inside"] else 0,
            )
            if cur_key > best_key:
                best_entry = entry

    assert best_entry is not None
    return best_entry


# ---------------------------------------------------------------------------
# Phase 4: Guarded Oracle Truth Loader
# ---------------------------------------------------------------------------


class GuardedOracleTruthLoader:
    """Information-barrier truth loader that gates probe access and private truth unsealing.

    Rules enforced:
    1. `issue_scoped_probe` is only allowed AFTER `L1_I1_PROPOSAL` is committed (with a
       matching `i1_lock_hash`) and BEFORE `L2_I2_PLAN` is committed, and returns ONLY a
       single `ProbeReceipt` via `DiagnosticProbeSession`.
    2. `open_private_truth` refuses to return private evaluation truth until all 4 locks
       (`L1_I1_PROPOSAL`, `L2_I2_PLAN`, `L3_A1_GRAPH`, `L4_A2_CHALLENGE`) are registered
       and verified via `verify_lock_chain_v2`.
    """

    def __init__(
        self,
        case_id: Literal["A", "B"] | str = "A",
        private_world: Optional[Mapping[str, Any]] = None,
    ) -> None:
        normalized_case = str(case_id).strip().upper()
        if normalized_case.startswith("CASE_"):
            normalized_case = normalized_case.split("CASE_", 1)[1]
        if normalized_case not in {"A", "B"}:
            raise ValueError(f"Unsupported case_id={case_id!r}; expected 'A' or 'B'")
        self._case_id: Literal["A", "B"] = "A" if normalized_case == "A" else "B"

        raw_world = (
            copy.deepcopy(dict(private_world))
            if private_world is not None
            else build_private_world(self._case_id)
        )
        raw_world.setdefault(
            "connector_bbox_3d_n4_xyxy_px", list(DEFAULT_3D_N4_CONNECTOR_BBOX)
        )
        raw_world.setdefault(
            "connector_bbox_fixture_n4_xyxy_px", list(DEFAULT_FIXTURE_N4_CONNECTOR_BBOX)
        )
        self.__private_world: Dict[str, Any] = raw_world
        self._probe_session = DiagnosticProbeSession(self.__private_world)
        self._locks: Dict[str, ArtifactLockRecordV2] = {}
        self._probe_requests_count: int = 0
        self._truth_access_count: int = 0
        self._premature_access_attempts: int = 0

    @property
    def case_id(self) -> Literal["A", "B"]:
        return self._case_id

    @property
    def probe_requests_count(self) -> int:
        return self._probe_requests_count

    @property
    def truth_access_count(self) -> int:
        return self._truth_access_count

    @property
    def premature_access_attempts(self) -> int:
        return self._premature_access_attempts

    @property
    def is_fully_locked(self) -> bool:
        if set(self._locks.keys()) != set(LOCK_STAGE_ORDER):
            return False
        ordered = [self._locks[stage] for stage in LOCK_STAGE_ORDER]
        verification = verify_lock_chain_v2(ordered)
        return bool(verification.get("valid", False))

    @property
    def locks(self) -> Dict[str, ArtifactLockRecordV2]:
        return dict(self._locks)

    @property
    def lock_list(self) -> List[ArtifactLockRecordV2]:
        return [
            self._locks[stage]
            for stage in LOCK_STAGE_ORDER
            if stage in self._locks
        ]

    def register_lock(
        self, lock_record: ArtifactLockRecordV2 | Mapping[str, Any]
    ) -> ArtifactLockRecordV2:
        """Register a lock record in canonical order `L1 -> L2 -> L3 -> L4`."""
        record = (
            lock_record
            if isinstance(lock_record, ArtifactLockRecordV2)
            else ArtifactLockRecordV2.model_validate(lock_record)
        )
        if record.stage in self._locks:
            existing = self._locks[record.stage]
            if existing.chain_hash != record.chain_hash:
                raise PermissionError(
                    f"Conflicting lock record re-registered for {record.stage}"
                )
            return existing

        expected_idx = len(self._locks)
        if expected_idx >= len(LOCK_STAGE_ORDER):
            raise PermissionError("All 4 locks are already registered")
        expected_stage = LOCK_STAGE_ORDER[expected_idx]
        if record.stage != expected_stage:
            raise PermissionError(
                f"Out-of-order lock registration: expected {expected_stage!r}, got {record.stage!r}"
            )
        if expected_idx == 0:
            if record.previous_lock_hash is not None:
                raise PermissionError(
                    f"First lock {record.stage} must have previous_lock_hash=None"
                )
            self._probe_session.freeze_i1_proposal(record.canonical_sha256)
        else:
            prev_stage = LOCK_STAGE_ORDER[expected_idx - 1]
            prev_record = self._locks[prev_stage]
            if record.previous_lock_hash != prev_record.chain_hash:
                raise PermissionError(
                    f"Broken lock chain at {record.stage}: previous_lock_hash={record.previous_lock_hash!r} "
                    f"!= {prev_record.chain_hash!r}"
                )
        self._locks[record.stage] = record
        return record

    commit_lock = register_lock

    def register_locks(
        self, locks: Sequence[ArtifactLockRecordV2 | Mapping[str, Any]]
    ) -> List[ArtifactLockRecordV2]:
        """Register a sequence of locks in canonical order."""
        registered: List[ArtifactLockRecordV2] = []
        for item in locks:
            registered.append(self.register_lock(item))
        return registered

    def issue_scoped_probe(
        self,
        kind: str,
        target: str,
        i1_lock_hash: Optional[str] = None,
        *,
        i1_proposal_hash: Optional[str] = None,
    ) -> ProbeReceipt:
        """Issue the single diagnostic probe allowed after `L1_I1_PROPOSAL` is locked."""
        if "L1_I1_PROPOSAL" not in self._locks:
            self._premature_access_attempts += 1
            raise PermissionError(
                "Cannot issue diagnostic probe before L1_I1_PROPOSAL is locked"
            )
        if "L2_I2_PLAN" in self._locks:
            self._premature_access_attempts += 1
            raise PermissionError(
                "Cannot issue diagnostic probe after L2_I2_PLAN is already locked"
            )
        l1 = self._locks["L1_I1_PROPOSAL"]
        candidate_hash = i1_lock_hash or i1_proposal_hash or l1.canonical_sha256
        if candidate_hash not in {l1.canonical_sha256, l1.chain_hash}:
            self._premature_access_attempts += 1
            raise PermissionError(
                f"Probe i1_lock_hash={candidate_hash!r} does not match committed L1 lock "
                f"({l1.canonical_sha256!r})"
            )
        norm_target = normalize_rack_id(target)
        receipt = self._probe_session.issue_receipt(
            kind=kind,
            target=norm_target,
            i1_proposal_hash=l1.canonical_sha256,
        )
        self._probe_requests_count += 1
        return receipt

    def open_private_truth(
        self,
        locks: Optional[Sequence[ArtifactLockRecordV2 | Mapping[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Unseal and return a deep copy of private world truth only after all 4 locks are valid."""
        if locks is not None:
            for item in locks:
                record = (
                    item
                    if isinstance(item, ArtifactLockRecordV2)
                    else ArtifactLockRecordV2.model_validate(item)
                )
                if record.stage not in self._locks:
                    self.register_lock(record)

        if set(self._locks.keys()) != set(LOCK_STAGE_ORDER):
            self._premature_access_attempts += 1
            missing = [s for s in LOCK_STAGE_ORDER if s not in self._locks]
            raise PermissionError(
                f"Evaluation truth accessed before all 4 locks (L1..L4) are committed; missing={missing}"
            )

        ordered = [self._locks[stage] for stage in LOCK_STAGE_ORDER]
        verification = verify_lock_chain_v2(ordered)
        if not verification.get("valid", False):
            self._premature_access_attempts += 1
            raise PermissionError(
                f"Evaluation truth access denied: invalid lock chain: {verification.get('reason')}"
            )

        self._truth_access_count += 1
        return copy.deepcopy(self.__private_world)


# ---------------------------------------------------------------------------
# Phase 4: 4-Layer Case Evaluation Report & Evaluator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseEvaluationReportV2:
    """Four-layer evaluation report for a `visual-v2` Case A or Case B run."""

    case_id: Literal["A", "B"]
    structural_valid: bool
    scientific_correct: bool
    operational_complete: bool
    live_provider_evidence: bool
    strict_pass: bool
    offline_strict_pass: bool
    physical_safety_certified: Literal[False]
    structural_checks: Dict[str, Any]
    scientific_checks: Dict[str, Any]
    operational_checks: Dict[str, Any]
    live_evidence_checks: Dict[str, Any]
    metrics: Dict[str, float]
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def model_dump(self) -> Dict[str, Any]:
        return asdict(self)


def _compute_trajectory_rmse(
    traj_a: Mapping[str, Sequence[float]],
    traj_b: Mapping[str, Sequence[float]],
) -> float:
    sq_errors: List[float] = []
    for rack_id in CANONICAL_RACKS:
        series_a = [float(x) for x in traj_a[rack_id]]
        series_b = [float(x) for x in traj_b[rack_id]]
        if len(series_a) != len(series_b) or not series_a:
            return float("inf")
        for va, vb in zip(series_a, series_b):
            sq_errors.append((va - vb) ** 2)
    if not sq_errors:
        return float("inf")
    return float(math.sqrt(sum(sq_errors) / len(sq_errors)))


def _evaluate_live_turn_evidences(
    turn_evidences: Optional[Mapping[str, Any]],
) -> Tuple[bool, Dict[str, Any], List[str]]:
    """Verify whether all 4 turns (`I1`, `I2`, `A1`, `A2`) have real provider evidence."""
    errors: List[str] = []
    if not turn_evidences:
        return (
            False,
            {
                "present": False,
                "turns_verified": {},
                "reason": "No live provider turn_evidences supplied (offline/deterministic mode)",
            },
            [],
        )

    required_turns = ("I1", "I2", "A1", "A2")
    turns_verified: Dict[str, bool] = {}
    for turn_id in required_turns:
        ev = turn_evidences.get(turn_id)
        if ev is None:
            turns_verified[turn_id] = False
            errors.append(f"Missing live provider TurnEvidence for {turn_id}")
            continue
        ev_dict = (
            ev.model_dump()
            if hasattr(ev, "model_dump")
            else (asdict(ev) if hasattr(ev, "__dataclass_fields__") else dict(ev))
        )
        has_response_id = bool(str(ev_dict.get("response_id", "")).strip())
        has_model = bool(str(ev_dict.get("model", "")).strip())
        has_req_sha = len(str(ev_dict.get("request_sha256", "")).strip()) == 64
        has_resp_sha = len(str(ev_dict.get("response_sha256", "")).strip()) == 64
        latency_ok = float(ev_dict.get("latency_ms", 0.0) or 0.0) > 0.0
        usage = ev_dict.get("usage") or {}
        tokens_ok = int(usage.get("total_tokens", 0) or 0) > 0
        ok = bool(
            has_response_id
            and has_model
            and has_req_sha
            and has_resp_sha
            and latency_ok
            and tokens_ok
        )
        turns_verified[turn_id] = ok
        if not ok:
            errors.append(f"Incomplete live provider TurnEvidence for {turn_id}")

    all_ok = all(turns_verified.get(t, False) for t in required_turns)
    return (
        all_ok,
        {
            "present": True,
            "turns_verified": turns_verified,
            "all_turns_live": all_ok,
        },
        errors,
    )


def evaluate_case_v2(
    *,
    i1_proposal: ProposalV2 | Mapping[str, Any],
    probe_receipt: ProbeReceipt | Mapping[str, Any],
    i2_plan: PlanV2 | Mapping[str, Any],
    a1_graph: IndependentGraphV2 | Mapping[str, Any],
    a2_challenge: AuditChallengeV2 | Mapping[str, Any],
    locks: Sequence[ArtifactLockRecordV2 | Mapping[str, Any]],
    truth_loader: GuardedOracleTruthLoader | Mapping[str, Any],
    turn_evidences: Optional[Mapping[str, Any]] = None,
    require_live_evidence: bool = False,
    trajectory_rmse_tol_c: float = 0.35,
    r0_abs_tol_ohm: float = 0.002,
    h_rel_tol: float = 0.05,
    c_rel_tol: float = 0.05,
) -> CaseEvaluationReportV2:
    """Evaluate a complete 4-turn `visual-v2` run across all 4 grading layers.

    Layers:
    1. `structural_valid`: Pydantic `extra='forbid'` schemas, contract hygiene,
       finite numbers, canonical IDs, and valid hash-chained `L1..L4` locks.
    2. `scientific_correct`: Visual hotspot & N4 connector bbox localization (`I1` and `A1`),
       exact 4 power edges & 4 sensor edges (`I1` and `A1`), empty `unknown_critical_edges`,
       probe target `(voltage_drop, N4)`, fitted `(R0, h, C)` accuracy, 19-point trajectory
       RMSE <= 0.35 C across all 3 actions, PDU current accuracy <= 1e-6 A, safe action
       selection (`MIGRATE_N4_TO_N3_8A` in Case A, `MIGRATE_N4_TO_N2_8A` in Case B),
       unsafe alternatives marked `safe=False`, `A2` independent trajectory recomputation
       and `+1 C` sensitivity canary, and coherent `A2` verdict (`"accept"` vs `"reject"`).
    3. `operational_complete`: All 4 locks committed prior to truth unsealing, single probe
       budget respected, `I` and `A` isolated (`environment_id` distinct), and
       `physical_safety_certified == False`.
    4. `live_provider_evidence`: Non-empty `TurnEvidence` across `I1`, `I2`, `A1`, `A2`.
    """
    errors: List[str] = []

    # 1. Structural validation & hygiene
    structural_checks: Dict[str, Any] = {}
    try:
        prop = (
            i1_proposal
            if isinstance(i1_proposal, ProposalV2)
            else ProposalV2.model_validate(i1_proposal)
        )
        validate_contract_hygiene(prop.model_dump())
        structural_checks["i1_proposal_valid"] = True
    except Exception as exc:
        prop = None  # type: ignore[assignment]
        structural_checks["i1_proposal_valid"] = False
        errors.append(f"I1 ProposalV2 structural error: {exc}")

    try:
        plan = (
            i2_plan
            if isinstance(i2_plan, PlanV2)
            else PlanV2.model_validate(i2_plan)
        )
        validate_contract_hygiene(plan.model_dump())
        structural_checks["i2_plan_valid"] = True
    except Exception as exc:
        plan = None  # type: ignore[assignment]
        structural_checks["i2_plan_valid"] = False
        errors.append(f"I2 PlanV2 structural error: {exc}")

    try:
        graph_a1 = (
            a1_graph
            if isinstance(a1_graph, IndependentGraphV2)
            else IndependentGraphV2.model_validate(a1_graph)
        )
        validate_contract_hygiene(graph_a1.model_dump())
        structural_checks["a1_graph_valid"] = True
    except Exception as exc:
        graph_a1 = None  # type: ignore[assignment]
        structural_checks["a1_graph_valid"] = False
        errors.append(f"A1 IndependentGraphV2 structural error: {exc}")

    try:
        challenge_a2 = (
            a2_challenge
            if isinstance(a2_challenge, AuditChallengeV2)
            else AuditChallengeV2.model_validate(a2_challenge)
        )
        validate_contract_hygiene(challenge_a2.model_dump())
        structural_checks["a2_challenge_valid"] = True
    except Exception as exc:
        challenge_a2 = None  # type: ignore[assignment]
        structural_checks["a2_challenge_valid"] = False
        errors.append(f"A2 AuditChallengeV2 structural error: {exc}")

    lock_records: List[ArtifactLockRecordV2] = []
    try:
        for item in locks:
            rec = (
                item
                if isinstance(item, ArtifactLockRecordV2)
                else ArtifactLockRecordV2.model_validate(item)
            )
            lock_records.append(rec)
    except Exception as exc:
        errors.append(f"ArtifactLockRecordV2 validation error: {exc}")

    lock_verification = verify_lock_chain_v2(lock_records)
    # Also verify each lock record's canonical_sha256 matches the corresponding contract
    artifact_hash_matches = False
    if (
        bool(lock_verification.get("valid", False))
        and prop is not None
        and plan is not None
        and graph_a1 is not None
        and challenge_a2 is not None
        and len(lock_records) == 4
    ):
        expected_shas = [
            canonical_sha256(prop),
            canonical_sha256(plan),
            canonical_sha256(graph_a1),
            canonical_sha256(challenge_a2),
        ]
        artifact_hash_matches = all(
            lock_records[i].canonical_sha256 == expected_shas[i] for i in range(4)
        )
        if not artifact_hash_matches:
            errors.append("Lock record canonical_sha256 mismatch against submitted contract artifacts")

    structural_checks["lock_chain_valid"] = bool(
        lock_verification.get("valid", False) and artifact_hash_matches
    )
    structural_checks["lock_verification"] = lock_verification
    structural_checks["artifact_hash_matches"] = artifact_hash_matches
    if not lock_verification.get("valid", False):
        errors.append(f"Lock chain error: {lock_verification.get('reason')}")

    def _norm_case(cid: str) -> str:
        s = str(cid).strip().upper()
        return s.split("CASE_", 1)[1] if s.startswith("CASE_") else s

    case_matches = bool(
        prop is not None
        and plan is not None
        and graph_a1 is not None
        and challenge_a2 is not None
        and len(
            {
                _norm_case(prop.case_id),
                _norm_case(plan.case_id),
                _norm_case(graph_a1.case_id),
                _norm_case(challenge_a2.case_id),
            }
        )
        == 1
    )
    structural_checks["case_id_consistent"] = case_matches
    if not case_matches:
        errors.append("Inconsistent case_id across I1, I2, A1, A2 contracts")

    structural_valid = bool(
        structural_checks.get("i1_proposal_valid")
        and structural_checks.get("i2_plan_valid")
        and structural_checks.get("a1_graph_valid")
        and structural_checks.get("a2_challenge_valid")
        and structural_checks.get("lock_chain_valid")
        and structural_checks.get("case_id_consistent")
    )

    # Unseal private truth ONLY via GuardedOracleTruthLoader after locks are verified
    if isinstance(truth_loader, GuardedOracleTruthLoader):
        private_truth = truth_loader.open_private_truth(lock_records)
        loader_premature_attempts = truth_loader.premature_access_attempts
        loader_probe_count = truth_loader.probe_requests_count
    else:
        if not bool(lock_verification.get("valid", False)):
            raise PermissionError(
                "Evaluation truth accessed before all 4 locks (L1..L4) are committed"
            )
        private_truth = copy.deepcopy(dict(truth_loader))
        loader_premature_attempts = 0
        loader_probe_count = 1

    case_id_val: Literal["A", "B"] = (
        "A" if _norm_case(str(private_truth.get("case_id", "A"))) == "A" else "B"
    )

    # Normalize probe receipt
    receipt_dict = (
        probe_receipt.to_dict()
        if isinstance(probe_receipt, ProbeReceipt)
        else dict(probe_receipt)
    )
    receipt_obj = ProbeReceipt(
        receipt_id=str(receipt_dict["receipt_id"]),
        case_id=str(receipt_dict["case_id"]),
        kind=str(receipt_dict["kind"]),
        target=normalize_rack_id(str(receipt_dict["target"])),
        value=receipt_dict["value"],
        exact_value=receipt_dict["exact_value"],
        units=str(receipt_dict["units"]),
        i1_proposal_hash=str(receipt_dict["i1_proposal_hash"]),
        timestamp_s=int(receipt_dict.get("timestamp_s", 0)),
        receipt_sha256=str(receipt_dict["receipt_sha256"]),
    )

    # 2. Scientific evaluation
    scientific_checks: Dict[str, Any] = {}
    metrics: Dict[str, float] = {}

    if prop is not None and plan is not None and graph_a1 is not None and challenge_a2 is not None:
        true_hot_rack = normalize_rack_id(str(private_truth.get("source_rack", "N4")))
        true_power_edges = {
            normalize_rack_id(k): normalize_pdu_id(v)
            for k, v in dict(private_truth["feeder_map"]).items()
        }
        true_sensor_edges = {
            normalize_sensor_id(k): normalize_rack_id(v)
            for k, v in dict(private_truth["sensor_to_rack"]).items()
        }
        true_n4_spec = dict(private_truth["racks"][true_hot_rack])
        true_r0 = float(true_n4_spec["R0_ohm"])
        true_h = float(true_n4_spec["h_W_per_K"])
        true_c = float(true_n4_spec["C_J_per_K"])

        gt_bboxes: List[Sequence[int]] = [
            list(private_truth.get("connector_bbox_3d_n4_xyxy_px", DEFAULT_3D_N4_CONNECTOR_BBOX)),
            list(
                private_truth.get(
                    "connector_bbox_fixture_n4_xyxy_px",
                    private_truth.get("connector_bboxes", {}).get("N4", DEFAULT_FIXTURE_N4_CONNECTOR_BBOX),
                )
            ),
        ]

        # I1 checks
        i1_hot_rack_ok = prop.hotspot.asset_id == true_hot_rack
        i1_bbox_eval = evaluate_connector_localization(
            prop.hotspot.bbox_xyxy_px, gt_bboxes=gt_bboxes
        )
        i1_power_map = prop.power_map()
        i1_sensor_map = prop.sensor_map()
        i1_power_ok = i1_power_map == true_power_edges
        i1_sensor_ok = i1_sensor_map == true_sensor_edges
        i1_unknown_empty = len(prop.unknown_critical_edges) == 0
        i1_measurement_ok = (
            prop.selected_measurement.type == "voltage_drop"
            and prop.selected_measurement.target_asset == true_hot_rack
        )

        scientific_checks["i1_hot_rack_correct"] = i1_hot_rack_ok
        scientific_checks["i1_bbox_localization"] = i1_bbox_eval
        scientific_checks["i1_bbox_localized_pass"] = bool(i1_bbox_eval["localized_pass"])
        scientific_checks["i1_power_edges_exact"] = i1_power_ok
        scientific_checks["i1_sensor_edges_exact"] = i1_sensor_ok
        scientific_checks["i1_unknown_critical_edges_empty"] = i1_unknown_empty
        scientific_checks["i1_selected_measurement_correct"] = i1_measurement_ok
        metrics["i1_bbox_iou"] = float(i1_bbox_eval["iou"])

        # Probe receipt checks
        l1_hash = lock_records[0].canonical_sha256 if lock_records else ""
        l1_chain_hash = lock_records[0].chain_hash if lock_records else ""
        probe_sig_ok = verify_probe_receipt(receipt_obj)
        probe_target_ok = (
            receipt_obj.kind == "voltage_drop"
            and receipt_obj.target == true_hot_rack
            and receipt_obj.i1_proposal_hash in {l1_hash, l1_chain_hash}
        )
        scientific_checks["probe_receipt_signature_valid"] = probe_sig_ok
        scientific_checks["probe_targeted_n4_voltage_drop"] = probe_target_ok

        # I2 parameter identification checks
        r0_err = abs(float(plan.post_measurement_parameters.r0_ohm) - true_r0)
        h_rel_err = abs(float(plan.post_measurement_parameters.h_w_per_k) - true_h) / true_h
        c_rel_err = abs(float(plan.post_measurement_parameters.c_j_per_k) - true_c) / true_c
        r0_ok = r0_err <= r0_abs_tol_ohm
        h_ok = h_rel_err <= h_rel_tol
        c_ok = c_rel_err <= c_rel_tol

        scientific_checks["i2_r0_accurate"] = r0_ok
        scientific_checks["i2_h_accurate"] = h_ok
        scientific_checks["i2_c_accurate"] = c_ok
        metrics["i2_r0_abs_error_ohm"] = round(r0_err, 6)
        metrics["i2_h_rel_error"] = round(h_rel_err, 6)
        metrics["i2_c_rel_error"] = round(c_rel_err, 6)

        # I2 internal consistency against its own inferred network
        plan_self_check = verify_plan_against_inferred_network(plan)
        scientific_checks["i2_self_consistency"] = plan_self_check
        scientific_checks["i2_self_consistent_pass"] = bool(plan_self_check["consistent"])

        # I2 trajectory & PDU current accuracy against private ground-truth physics
        true_world: World = private_truth["world"]
        action_rmse_by_id: Dict[str, float] = {}
        pdu_current_err_by_id: Dict[str, float] = {}
        safety_flag_matches: Dict[str, bool] = {}

        migrations_map = {
            "NO_ACTION": None,
            "MIGRATE_N4_TO_N2_8A": Migration("N4", "N2", DEFAULT_MIGRATION_AMPS),
            "MIGRATE_N4_TO_N3_8A": Migration("N4", "N3", DEFAULT_MIGRATION_AMPS),
        }

        for aid, mig in migrations_map.items():
            pred = plan.predictions_by_action[aid]
            true_sim = simulate_world(true_world, migration=mig)
            rmse = _compute_trajectory_rmse(pred.temps_c, true_sim.temps_C)
            action_rmse_by_id[aid] = round(rmse, 6)

            max_p_err = 0.0
            for pk in CANONICAL_PDUS:
                for t_idx in range(19):
                    err_i = abs(float(pred.pdu_currents_a[pk][t_idx]) - float(true_sim.pdu_currents_A[pk][t_idx]))
                    if err_i > max_p_err:
                        max_p_err = err_i
            pdu_current_err_by_id[aid] = round(max_p_err, 8)

            safety_flag_matches[aid] = bool(
                pred.safety.safe == bool(true_sim.safety_flags["safe"])
                and pred.safety.thermal_violation == bool(true_sim.safety_flags["thermal_violation"])
                and pred.safety.pdu_overload == bool(true_sim.safety_flags["pdu_overload"])
            )

        max_traj_rmse = max(action_rmse_by_id.values()) if action_rmse_by_id else float("inf")
        max_pdu_err = max(pdu_current_err_by_id.values()) if pdu_current_err_by_id else float("inf")
        trajectories_accurate = max_traj_rmse <= trajectory_rmse_tol_c
        pdu_currents_accurate = max_pdu_err <= 1e-6
        all_safety_flags_correct = all(safety_flag_matches.values())

        expected_safe_action = (
            "MIGRATE_N4_TO_N3_8A" if case_id_val == "A" else "MIGRATE_N4_TO_N2_8A"
        )
        selected_action_ok = (
            normalize_action_id(plan.selected_action_id) == expected_safe_action
            and plan.decision == "intervene"
            and bool(plan.predictions_by_action[expected_safe_action].safety.safe)
        )
        unsafe_actions_marked_unsafe = all(
            not plan.predictions_by_action[aid].safety.safe
            for aid in CANONICAL_ACTION_IDS
            if aid != expected_safe_action
        )

        scientific_checks["i2_action_rmse_by_id"] = action_rmse_by_id
        scientific_checks["i2_pdu_current_err_by_id"] = pdu_current_err_by_id
        scientific_checks["i2_trajectories_accurate"] = trajectories_accurate
        scientific_checks["i2_pdu_currents_accurate"] = pdu_currents_accurate
        scientific_checks["i2_safety_flags_correct"] = all_safety_flags_correct
        scientific_checks["i2_selected_action_correct"] = selected_action_ok
        scientific_checks["i2_unsafe_alternatives_rejected"] = unsafe_actions_marked_unsafe
        metrics["i2_max_trajectory_rmse_c"] = round(max_traj_rmse, 6)
        metrics["i2_max_pdu_current_error_a"] = round(max_pdu_err, 8)

        # A1 independent perception checks
        a1_hot_rack_ok = graph_a1.hotspot.asset_id == true_hot_rack
        a1_bbox_eval = evaluate_connector_localization(
            graph_a1.hotspot.bbox_xyxy_px, gt_bboxes=gt_bboxes
        )
        a1_power_map = graph_a1.power_map()
        a1_sensor_map = graph_a1.sensor_map()
        a1_power_ok = a1_power_map == true_power_edges
        a1_sensor_ok = a1_sensor_map == true_sensor_edges
        a1_unknown_empty = len(graph_a1.unknown_critical_edges) == 0

        scientific_checks["a1_hot_rack_correct"] = a1_hot_rack_ok
        scientific_checks["a1_bbox_localization"] = a1_bbox_eval
        scientific_checks["a1_bbox_localized_pass"] = bool(a1_bbox_eval["localized_pass"])
        scientific_checks["a1_power_edges_exact"] = a1_power_ok
        scientific_checks["a1_sensor_edges_exact"] = a1_sensor_ok
        scientific_checks["a1_unknown_critical_edges_empty"] = a1_unknown_empty
        metrics["a1_bbox_iou"] = float(a1_bbox_eval["iou"])

        # A2 independent recomputation & +1.0 C canary checks
        init = plan.initial_state
        post = plan.post_measurement_parameters
        net = plan.inferred_network
        a1_world = World(
            racks=tuple(
                RackParams(
                    rack_id=rk,
                    pdu_id=a1_power_map[rk],
                    current_A=init.currents_a[rk],
                    initial_temp_C=init.temperatures_c[rk],
                    R0_ohm=post.r0_ohm if rk == post.target_rack else net.racks[rk].r0_ohm,
                    alpha_per_C=net.racks[rk].alpha_per_c,
                    h_W_per_K=post.h_w_per_k if rk == post.target_rack else net.racks[rk].h_w_per_k,
                    C_J_per_K=post.c_j_per_k if rk == post.target_rack else net.racks[rk].c_j_per_k,
                    max_current_A=net.racks[rk].max_current_a,
                    max_temp_C=net.racks[rk].max_temp_c,
                )
                for rk in CANONICAL_RACKS
            ),
            pdus=(PDU("F1", net.pdus["F1"]), PDU("F2", net.pdus["F2"])),
            ambient_C=net.ambient_c,
            horizon_s=net.horizon_s,
            step_s=net.dt_s,
        )

        a2_recomp_rmses: List[float] = []
        a2_recomp_pdu_errs: List[float] = []
        for aid, mig in migrations_map.items():
            sim_a1 = simulate_world(a1_world, migration=mig)
            recomp_pred = challenge_a2.recomputed_predictions_by_action[aid]
            a2_recomp_rmses.append(_compute_trajectory_rmse(recomp_pred.temps_c, sim_a1.temps_C))
            for pk in CANONICAL_PDUS:
                for t_idx in range(19):
                    a2_recomp_pdu_errs.append(
                        abs(float(recomp_pred.pdu_currents_a[pk][t_idx]) - float(sim_a1.pdu_currents_A[pk][t_idx]))
                    )

        a2_recomp_rmse = max(a2_recomp_rmses) if a2_recomp_rmses else float("inf")
        a2_recomp_pdu_err = max(a2_recomp_pdu_errs) if a2_recomp_pdu_errs else float("inf")
        a2_recomp_ok = a2_recomp_rmse <= 1e-5 and a2_recomp_pdu_err <= 1e-5

        # Canary +1.0 C on N4 initial temperature under canary action_id (default NO_ACTION)
        canary_world = World(
            racks=tuple(
                RackParams(
                    rack_id=r.rack_id,
                    pdu_id=r.pdu_id,
                    current_A=r.current_A,
                    initial_temp_C=(r.initial_temp_C + 1.0) if r.rack_id == "N4" else r.initial_temp_C,
                    R0_ohm=r.R0_ohm,
                    alpha_per_C=r.alpha_per_C,
                    h_W_per_K=r.h_W_per_K,
                    C_J_per_K=r.C_J_per_K,
                    max_current_A=r.max_current_A,
                    max_temp_C=r.max_temp_c if hasattr(r, "max_temp_c") else r.max_temp_C,
                )
                for r in a1_world.racks
            ),
            pdus=a1_world.pdus,
            ambient_C=a1_world.ambient_C,
            horizon_s=a1_world.horizon_s,
            step_s=a1_world.step_s,
        )
        canary_act = normalize_action_id(challenge_a2.input_sensitivity_canary.action_id)
        expected_canary_sim = simulate_world(canary_world, migration=migrations_map[canary_act])
        canary_rmse = _compute_trajectory_rmse(
            challenge_a2.input_sensitivity_canary.trajectory_temps_c,
            expected_canary_sim.temps_C,
        )
        canary_ok = (
            canary_rmse <= 1e-4
            and float(challenge_a2.input_sensitivity_canary.max_abs_diff_c) > 0.1
        )

        # Verdict coherence check
        i2_power_map = plan.power_map()
        graphs_match = (
            a1_power_map == i2_power_map
            and challenge_a2.edge_comparison.graphs_identical
            and len(graph_a1.unknown_critical_edges) == 0
        )
        sel_recomp_safe = bool(
            challenge_a2.recomputed_predictions_by_action[
                normalize_action_id(plan.selected_action_id)
            ].safety.safe
        )
        if len(graph_a1.unknown_critical_edges) > 0 or plan.blocking_graph_uncertainty:
            verdict_coherent = challenge_a2.verdict in {"reject", "abstain"}
        elif graphs_match and sel_recomp_safe and plan_self_check["consistent"]:
            verdict_coherent = challenge_a2.verdict == "accept"
        else:
            verdict_coherent = challenge_a2.verdict in {"reject", "abstain"}

        scientific_checks["a2_recomputation_accurate"] = a2_recomp_ok
        scientific_checks["a2_canary_valid"] = canary_ok
        scientific_checks["a2_verdict_coherent"] = verdict_coherent
        scientific_checks["a2_verdict_is_accept"] = challenge_a2.verdict == "accept"
        metrics["a2_recomputation_rmse_c"] = round(a2_recomp_rmse, 8)
        metrics["a2_canary_rmse_c"] = round(canary_rmse, 8)

        scientific_correct = bool(
            i1_hot_rack_ok
            and i1_bbox_eval["localized_pass"]
            and i1_power_ok
            and i1_sensor_ok
            and i1_unknown_empty
            and i1_measurement_ok
            and probe_sig_ok
            and probe_target_ok
            and r0_ok
            and h_ok
            and c_ok
            and plan_self_check["consistent"]
            and trajectories_accurate
            and pdu_currents_accurate
            and all_safety_flags_correct
            and selected_action_ok
            and unsafe_actions_marked_unsafe
            and a1_hot_rack_ok
            and a1_bbox_eval["localized_pass"]
            and a1_power_ok
            and a1_sensor_ok
            and a1_unknown_empty
            and a2_recomp_ok
            and canary_ok
            and verdict_coherent
            and challenge_a2.verdict == "accept"
        )
        if not scientific_correct:
            for k, v in scientific_checks.items():
                if isinstance(v, bool) and not v:
                    errors.append(f"Scientific check failed: {k}")
    else:
        scientific_correct = False

    # 3. Operational completeness & isolation checks
    operational_checks: Dict[str, Any] = {}
    i_envs = {
        rec.environment_id
        for rec in lock_records
        if rec.stage in {"L1_I1_PROPOSAL", "L2_I2_PLAN"}
    }
    a_envs = {
        rec.environment_id
        for rec in lock_records
        if rec.stage in {"L3_A1_GRAPH", "L4_A2_CHALLENGE"}
    }
    env_isolated = bool(
        len(i_envs) >= 1
        and len(a_envs) >= 1
        and i_envs.isdisjoint(a_envs)
    )
    single_probe_budget_ok = loader_probe_count == 1
    zero_premature_truth_access = loader_premature_attempts == 0
    physical_safety_false = bool(
        plan is not None
        and plan.physical_safety_certified is False
        and challenge_a2 is not None
        and challenge_a2.constraints_verified.get("physical_safety_certified_false", True) is True
    )

    operational_checks["all_four_locks_present"] = len(lock_records) == 4
    operational_checks["lock_chain_verified"] = bool(
        structural_checks.get("lock_chain_valid", False)
    )
    operational_checks["investigator_auditor_env_isolated"] = env_isolated
    operational_checks["investigator_environment_ids"] = sorted(i_envs)
    operational_checks["auditor_environment_ids"] = sorted(a_envs)
    operational_checks["single_probe_budget_respected"] = single_probe_budget_ok
    operational_checks["probe_requests_count"] = loader_probe_count
    operational_checks["zero_premature_truth_access"] = zero_premature_truth_access
    operational_checks["premature_access_attempts"] = loader_premature_attempts
    operational_checks["physical_safety_certified_false"] = physical_safety_false

    operational_complete = bool(
        len(lock_records) == 4
        and structural_checks.get("lock_chain_valid", False)
        and env_isolated
        and single_probe_budget_ok
        and zero_premature_truth_access
        and physical_safety_false
    )
    if not operational_complete:
        for k, v in operational_checks.items():
            if isinstance(v, bool) and not v:
                errors.append(f"Operational check failed: {k}")

    # 4. Live provider evidence check
    live_ok, live_checks, live_errs = _evaluate_live_turn_evidences(turn_evidences)
    if require_live_evidence and not live_ok:
        errors.extend(live_errs)

    offline_strict_pass = bool(
        structural_valid and scientific_correct and operational_complete
    )
    strict_pass = bool(offline_strict_pass and live_ok)

    return CaseEvaluationReportV2(
        case_id=case_id_val,
        structural_valid=structural_valid,
        scientific_correct=scientific_correct,
        operational_complete=operational_complete,
        live_provider_evidence=live_ok,
        strict_pass=strict_pass,
        offline_strict_pass=offline_strict_pass,
        physical_safety_certified=False,
        structural_checks=structural_checks,
        scientific_checks=scientific_checks,
        operational_checks=operational_checks,
        live_evidence_checks=live_checks,
        metrics=metrics,
        errors=errors,
    )


def evaluate_ab_campaign_v2(
    report_a: CaseEvaluationReportV2,
    report_b: CaseEvaluationReportV2,
    plan_a: PlanV2 | Mapping[str, Any],
    plan_b: PlanV2 | Mapping[str, Any],
) -> Dict[str, Any]:
    """Evaluate paired Case A and Case B `visual-v2` reports for feeder disambiguation."""
    pa = plan_a if isinstance(plan_a, PlanV2) else PlanV2.model_validate(plan_a)
    pb = plan_b if isinstance(plan_b, PlanV2) else PlanV2.model_validate(plan_b)

    sel_a = normalize_action_id(pa.selected_action_id)
    sel_b = normalize_action_id(pb.selected_action_id)
    action_disambiguated = (
        sel_a == "MIGRATE_N4_TO_N3_8A"
        and sel_b == "MIGRATE_N4_TO_N2_8A"
        and sel_a != sel_b
    )
    offline_campaign_pass = bool(
        report_a.offline_strict_pass
        and report_b.offline_strict_pass
        and action_disambiguated
    )
    live_campaign_pass = bool(
        report_a.strict_pass
        and report_b.strict_pass
        and action_disambiguated
    )
    return {
        "campaign_pass": live_campaign_pass,
        "offline_campaign_pass": offline_campaign_pass,
        "action_disambiguation_pass": action_disambiguated,
        "selected_action_case_A": sel_a,
        "selected_action_case_B": sel_b,
        "physical_safety_certified": False,
        "case_A": report_a.to_dict(),
        "case_B": report_b.to_dict(),
    }


# ---------------------------------------------------------------------------
# Phase 00 Backward-Compatible Oracle Grading
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseOracleGrade:
    """Deterministic oracle grading result for a single case (Case A or Case B)."""

    case_id: str
    zero_leakage_verified: bool
    all_locks_present: bool
    i1_hotspot_correct: bool
    i1_sld_mapping_correct: bool
    probe_targeted_hotspot: bool
    i2_node_correct: bool
    i2_feeder_correct: bool
    i2_resistance_abs_error_ohm: float
    i2_resistance_accurate: bool
    i2_current_cap_safe: bool
    i2_current_cap_error_a: float
    a1_topology_correct: bool
    a2_verdict_correct: bool
    a2_feeder_correct: bool
    overall_case_pass: bool
    safety_details: Dict[str, Any]


def grade_case_run(
    run_result: CaseProtocolRunResult,
    sealed_truth: SealedWorldTruth,
    resistance_tol_ohm: float = 0.01,
    current_tol_a: float = 5.0,
) -> CaseOracleGrade:
    """Grade locked Investigator (I1, I2) and Auditor (A1, A2) outputs against sealed truth."""
    locks_present = set(run_result.locks.keys()) == {"I1", "I2", "A1", "A2"}
    zero_leak = bool(run_result.zero_leakage_verified and locks_present)

    true_node = sealed_truth.degraded_node_id
    true_feeder = sealed_truth.degraded_feeder_id
    true_r = float(sealed_truth.degraded_resistance_ohm)
    true_max_i = float(sealed_truth.true_max_safe_current_a)
    true_map = dict(sealed_truth.node_to_feeder)

    i1_hotspot_ok = run_result.i1.visual_hotspot_node == true_node
    i1_sld_ok = dict(run_result.i1.sld_feeder_mapping) == true_map
    probe_target_ok = run_result.probe_response.target_node == true_node

    r_err = round(abs(float(run_result.i2.estimated_resistance_ohm) - true_r), 4)
    r_acc = r_err <= resistance_tol_ohm

    safety = evaluate_remediation_safety(
        target_feeder_id=run_result.i2.diagnosed_fault_feeder,
        degraded_feeder_id=true_feeder,
        target_node_id=run_result.i2.diagnosed_fault_node,
        degraded_node_id=true_node,
        proposed_current_cap_a=run_result.i2.recommended_max_current_a,
        true_max_safe_current_a=true_max_i,
        tolerance_a=current_tol_a,
    )

    a1_ok = (
        dict(run_result.a1.verified_sld_mapping) == true_map
        and run_result.a1.verified_hotspot_node == true_node
        and bool(run_result.a1.topology_consistency_check)
    )

    expected_verdict = "APPROVE" if safety["is_safe"] else "REJECT"
    a2_verdict_ok = run_result.a2.audit_verdict == expected_verdict
    a2_feeder_ok = run_result.a2.verified_fault_feeder == true_feeder

    overall_pass = bool(
        zero_leak
        and i1_hotspot_ok
        and i1_sld_ok
        and probe_target_ok
        and safety["is_safe"]
        and r_acc
        and a1_ok
        and a2_verdict_ok
        and a2_feeder_ok
    )

    return CaseOracleGrade(
        case_id=run_result.case_id,
        zero_leakage_verified=zero_leak,
        all_locks_present=locks_present,
        i1_hotspot_correct=i1_hotspot_ok,
        i1_sld_mapping_correct=i1_sld_ok,
        probe_targeted_hotspot=probe_target_ok,
        i2_node_correct=bool(safety["node_match"]),
        i2_feeder_correct=bool(safety["feeder_match"]),
        i2_resistance_abs_error_ohm=r_err,
        i2_resistance_accurate=r_acc,
        i2_current_cap_safe=bool(safety["cap_safe"]),
        i2_current_cap_error_a=float(safety["cap_error_a"]),
        a1_topology_correct=a1_ok,
        a2_verdict_correct=a2_verdict_ok,
        a2_feeder_correct=a2_feeder_ok,
        overall_case_pass=overall_pass,
        safety_details=safety,
    )


def grade_ab_campaign(
    grade_a: CaseOracleGrade,
    grade_b: CaseOracleGrade,
    run_a: CaseProtocolRunResult,
    run_b: CaseProtocolRunResult,
) -> Dict[str, Any]:
    """Evaluate paired A/B campaign disambiguation across Case A and Case B."""
    feeder_shifted_correctly = (
        run_a.i2.diagnosed_fault_feeder != run_b.i2.diagnosed_fault_feeder
        and grade_a.i2_feeder_correct
        and grade_b.i2_feeder_correct
    )
    auditor_shifted_correctly = (
        run_a.a2.verified_fault_feeder != run_b.a2.verified_fault_feeder
        and grade_a.a2_feeder_correct
        and grade_b.a2_feeder_correct
    )
    campaign_pass = bool(
        grade_a.overall_case_pass
        and grade_b.overall_case_pass
        and feeder_shifted_correctly
        and auditor_shifted_correctly
    )
    return {
        "campaign_pass": campaign_pass,
        "feeder_disambiguation_pass": feeder_shifted_correctly,
        "auditor_disambiguation_pass": auditor_shifted_correctly,
        "total_interactions_locked": len(run_a.locks) + len(run_b.locks),
        "case_A": asdict(grade_a),
        "case_B": asdict(grade_b),
    }
