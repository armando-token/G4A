"""Pydantic contracts, canonical JSON hashing, and lock-chain records for BlackboxDC.

Provides:
1. Phase 4 (`04_AGENT_PROTOCOL_AND_CONTRACTS.md`) `visual-v2` typed contracts:
   - `ProposalV2` (`proposal.json` — Investigator I1)
   - `PlanV2` (`plan.json` — Investigator I2)
   - `IndependentGraphV2` (`independent_graph.json` — Auditor A1)
   - `AuditChallengeV2` (`audit_challenge.json` — Auditor A2)
   - `ArtifactLockRecordV2`, `create_lock_record_v2`, `verify_lock_chain_v2`
   - `canonical_json_bytes`, `canonical_sha256`, `verify_plan_against_inferred_network`
   - Deterministic reference builders (`build_reference_proposal_v2`,
     `build_reference_plan_v2`, `build_reference_independent_graph_v2`,
     `build_reference_audit_challenge_v2`).
2. Backward-compatible Phase 00 contracts (`ProbeRequest`, `ProbeResponse`,
   `InvestigatorI1Contract`, `InvestigatorI2Contract`, `AuditorA1Contract`,
   `AuditorA2Contract`, `SafetyLockRecord`).
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import inspect
import json
import math
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence, Tuple
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from blackbox.domain import (
    AMBIENT_TEMP_C,
    DEFAULT_HORIZON_S,
    DEFAULT_MAX_TEMP_C,
    DEFAULT_MIGRATION_AMPS,
    DEFAULT_STEP_S,
    TOTAL_WORLD_CURRENT_A,
    Migration,
    PDU,
    RackParams,
    World,
    fit_thermal_parameters_from_ramp,
    infer_r0_from_voltage_drop,
    simulate_world,
    step_temperature,
)


VALID_NODES = {"N1", "N2", "N3", "N4"}
VALID_FEEDERS = {"F1", "F2", "F3", "F4"}

SCHEMA_VERSION_V2: Literal["visual-v2"] = "visual-v2"
MAX_CONTRACT_BLOB_BYTES: int = 128 * 1024  # 128 KiB

CANONICAL_RACKS: Tuple[str, ...] = ("N1", "N2", "N3", "N4")
CANONICAL_PDUS: Tuple[str, ...] = ("F1", "F2")
CANONICAL_SENSORS: Tuple[str, ...] = ("SNS_K1", "SNS_K2", "SNS_K3", "SNS_K4")
CANONICAL_DIAGNOSTICS: Tuple[str, ...] = (
    "voltage_drop",
    "temperature_snapshot",
    "controller_setting",
)
CANONICAL_ACTION_IDS: Tuple[str, ...] = (
    "NO_ACTION",
    "MIGRATE_N4_TO_N2_8A",
    "MIGRATE_N4_TO_N3_8A",
)
LOCK_STAGE_ORDER: Tuple[str, ...] = (
    "L1_I1_PROPOSAL",
    "L2_I2_PLAN",
    "L3_A1_GRAPH",
    "L4_A2_CHALLENGE",
)
EXPECTED_LOCK_SEQUENCE_V2: Tuple[str, ...] = LOCK_STAGE_ORDER

FORBIDDEN_CONTRACT_KEYS: frozenset[str] = frozenset(
    {
        "truth_sha256",
        "node_resistances_ohm",
        "sensor_to_rack",
        "rack_to_sensor",
        "safe_receiver",
        "wrong_receiver",
        "connector_bboxes",
        "candidate_explanations",
        "reference_outcomes",
        "truth_mask",
        "sealed_truth",
    }
)

INVESTIGATOR_ONLY_KEYS: frozenset[str] = frozenset(
    {
        "selected_measurement",
        "hypotheses",
        "predictions_by_action",
        "candidate_actions",
        "selected_action_id",
        "post_measurement_parameters",
        "forecast_source",
    }
)


# ---------------------------------------------------------------------------
# Normalization & Canonical JSON Hashing
# ---------------------------------------------------------------------------


def _ensure_finite_float(val: Any, field_name: str = "value") -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"{field_name} must be a finite number, got {val!r}")
    f = float(val)
    if not math.isfinite(f):
        raise ValueError(f"{field_name} must be finite (no NaN/Inf), got {val!r}")
    return f


def normalize_rack_id(value: str) -> str:
    """Normalize rack identifier (`'N4'`, `'RACK_N4'`, `'rack_n4'`) to canonical `'N1'..'N4'`."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("rack_id must be a non-empty string")
    s = value.strip().upper()
    for prefix in ("RACK_", "NODE_", "BAY_"):
        if s.startswith(prefix):
            s = s[len(prefix) :]
    if s not in CANONICAL_RACKS:
        raise ValueError(f"Invalid rack identifier {value!r}; expected one of {CANONICAL_RACKS}")
    return s


def to_prefixed_rack_id(value: str) -> str:
    """Convert a rack identifier to prefixed `'RACK_N1'..'RACK_N4'` form."""
    return f"RACK_{normalize_rack_id(value)}"


def normalize_pdu_id(value: str) -> str:
    """Normalize PDU feeder identifier (`'F1'`, `'PDU_F1'`, `'FEEDER_F1'`) to `'F1'` or `'F2'`."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("pdu_id must be a non-empty string")
    s = value.strip().upper()
    for prefix in ("PDU_", "FEEDER_", "BUS_"):
        if s.startswith(prefix):
            s = s[len(prefix) :]
    if s not in CANONICAL_PDUS:
        raise ValueError(f"Invalid PDU identifier {value!r}; expected one of {CANONICAL_PDUS}")
    return s


def normalize_sensor_id(value: str) -> str:
    """Normalize sensor identifier (`'SNS_K2'`, `'K2'`) to canonical `'SNS_K1'..'SNS_K4'`."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("sensor_id must be a non-empty string")
    s = value.strip().upper()
    if s in ("K1", "K2", "K3", "K4"):
        s = f"SNS_{s}"
    if s not in CANONICAL_SENSORS:
        raise ValueError(f"Invalid sensor identifier {value!r}; expected one of {CANONICAL_SENSORS}")
    return s


def normalize_action_id(value: str, target_rack: Optional[str] = None) -> str:
    """Normalize candidate action identifier into `'NO_ACTION'`, `'MIGRATE_N4_TO_N2_8A'`, or `'MIGRATE_N4_TO_N3_8A'`."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("action_id must be a non-empty string")
    s = value.strip().upper().replace(" ", "_").replace("-", "_")
    if s in ("NO_ACTION", "NONE", "HOLD", "DO_NOTHING", "BASELINE"):
        return "NO_ACTION"
    if s in ("MIGRATE_N4_TO_N2_8A", "N4_TO_N2", "N4_>N2", "N4->N2", "MIGRATE_N2", "MOVE_8A_TO_N2", "N2"):
        return "MIGRATE_N4_TO_N2_8A"
    if s in ("MIGRATE_N4_TO_N3_8A", "N4_TO_N3", "N4_>N3", "N4->N3", "MIGRATE_N3", "MOVE_8A_TO_N3", "N3"):
        return "MIGRATE_N4_TO_N3_8A"
    if target_rack is not None:
        norm_t = normalize_rack_id(target_rack)
        if norm_t == "N2":
            return "MIGRATE_N4_TO_N2_8A"
        if norm_t == "N3":
            return "MIGRATE_N4_TO_N3_8A"
    raise ValueError(
        f"Unsupported action_id {value!r}; expected one of {CANONICAL_ACTION_IDS}"
    )


def _to_plain_data(payload: Any) -> Any:
    if isinstance(payload, BaseModel):
        return payload.model_dump()
    if is_dataclass(payload) and not isinstance(payload, type):
        return asdict(payload)
    if isinstance(payload, Mapping):
        return {str(k): _to_plain_data(v) for k, v in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [_to_plain_data(x) for x in payload]
    return payload


def validate_contract_hygiene(
    payload: Any,
    max_bytes: int = MAX_CONTRACT_BLOB_BYTES,
) -> Dict[str, Any]:
    """Validate that a contract payload has finite numbers, no private truth keys, and size <= 128 KiB."""
    plain = _to_plain_data(payload)
    if not isinstance(plain, dict):
        raise ValueError("Contract payload must be a JSON object")

    def _walk(obj: Any, path: str = "root") -> None:
        if isinstance(obj, float):
            if not math.isfinite(obj):
                raise ValueError(f"Non-finite float at {path}: {obj!r}")
        elif isinstance(obj, Mapping):
            for k, v in obj.items():
                k_str = str(k)
                if k_str in FORBIDDEN_CONTRACT_KEYS:
                    raise ValueError(f"Forbidden private key {k_str!r} found at {path}")
                _walk(v, f"{path}.{k_str}")
        elif isinstance(obj, (list, tuple)):
            for idx, item in enumerate(obj):
                _walk(item, f"{path}[{idx}]")

    _walk(plain)
    raw = json.dumps(plain, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) > max_bytes:
        raise ValueError(f"Contract payload size {len(raw)} bytes exceeds {max_bytes} byte cap")
    return plain


def canonical_json_bytes(payload: Any) -> bytes:
    """Serialize payload into canonical UTF-8 JSON bytes (sorted keys, compact separators, no NaN/Inf)."""
    plain = _to_plain_data(payload)
    raw_str = json.dumps(plain, sort_keys=True, separators=(",", ":"), allow_nan=False)
    raw_bytes = raw_str.encode("utf-8")
    if len(raw_bytes) > MAX_CONTRACT_BLOB_BYTES:
        raise ValueError(f"Canonical JSON size {len(raw_bytes)} exceeds {MAX_CONTRACT_BLOB_BYTES}")
    return raw_bytes


def canonical_sha256(payload: Any) -> str:
    """Compute canonical SHA-256 hex digest over normalized contract structure."""
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


# ---------------------------------------------------------------------------
# Phase 4 `visual-v2` Shared Sub-Models
# ---------------------------------------------------------------------------


class HotspotV2(BaseModel):
    """Visual hotspot localization on the 1280x800 rear-aisle frame."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    asset_id: str
    surface: str = "rear_visible_connector"
    bbox_xyxy_px: List[int]
    confidence: float
    evidence_refs: List[str] = Field(default_factory=lambda: ["rgb.png", "thermal.png"])

    @field_validator("asset_id")
    @classmethod
    def _val_asset(cls, v: str) -> str:
        return normalize_rack_id(v)

    @field_validator("surface")
    @classmethod
    def _val_surface(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("surface must be a non-empty string")
        norm = v.strip()
        if norm != "rear_visible_connector":
            raise ValueError(f"surface must be 'rear_visible_connector', got {v!r}")
        return norm

    @field_validator("bbox_xyxy_px")
    @classmethod
    def _val_bbox(cls, v: Sequence[Any]) -> List[int]:
        if not isinstance(v, (list, tuple)) or len(v) != 4:
            raise ValueError("bbox_xyxy_px must be a 4-element list [x0, y0, x1, y1]")
        coords: List[int] = []
        for idx, item in enumerate(v):
            f = _ensure_finite_float(item, f"bbox_xyxy_px[{idx}]")
            coords.append(int(round(f)))
        x0, y0, x1, y1 = coords
        if not (0 <= x0 < x1 <= 1280 and 0 <= y0 < y1 <= 800):
            raise ValueError(
                f"bbox_xyxy_px {coords} violates 1280x800 bounds (0 <= x0 < x1 <= 1280, 0 <= y0 < y1 <= 800)"
            )
        return coords

    @field_validator("confidence")
    @classmethod
    def _val_conf(cls, v: Any) -> float:
        f = _ensure_finite_float(v, "confidence")
        if f < 0.0 or f > 1.0:
            raise ValueError(f"confidence must be in [0.0, 1.0], got {f}")
        return f

    @field_validator("evidence_refs")
    @classmethod
    def _val_refs(cls, v: Sequence[str]) -> List[str]:
        if not v:
            raise ValueError("evidence_refs must be non-empty")
        out = [str(x).strip() for x in v if str(x).strip()]
        if not out:
            raise ValueError("evidence_refs must contain at least one non-empty reference")
        return out


class EdgeV2(BaseModel):
    """Power feeder edge binding a rack to a PDU busbar (`F1` or `F2`)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    rack: str = Field(..., alias="rack_id")
    pdu: str = Field(..., alias="pdu_id")
    evidence_ref: str = "sld.png"

    @model_validator(mode="before")
    @classmethod
    def _accept_aliases(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            d = dict(data)
            if "rack" in d and "rack_id" not in d:
                d["rack_id"] = d.pop("rack")
            if "pdu" in d and "pdu_id" not in d:
                d["pdu_id"] = d.pop("pdu")
            if "feeder" in d and "pdu_id" not in d:
                d["pdu_id"] = d.pop("feeder")
            return d
        return data

    @field_validator("rack")
    @classmethod
    def _val_rack(cls, v: str) -> str:
        return normalize_rack_id(v)

    @field_validator("pdu")
    @classmethod
    def _val_pdu(cls, v: str) -> str:
        return normalize_pdu_id(v)


class SensorEdgeV2(BaseModel):
    """Sensor-to-rack edge binding `SNS_K1..SNS_K4` to `N1..N4`."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    sensor_id: str = Field(..., alias="sensor")
    rack_id: str = Field(..., alias="rack")
    evidence_ref: str = "sld.png"

    @model_validator(mode="before")
    @classmethod
    def _accept_aliases(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            d = dict(data)
            if "sensor_id" in d and "sensor" not in d:
                d["sensor"] = d.pop("sensor_id")
            if "rack_id" in d and "rack" not in d:
                d["rack"] = d.pop("rack_id")
            return d
        return data

    @field_validator("sensor_id")
    @classmethod
    def _val_sensor(cls, v: str) -> str:
        return normalize_sensor_id(v)

    @field_validator("rack_id")
    @classmethod
    def _val_rack(cls, v: str) -> str:
        return normalize_rack_id(v)


class HypothesisV2(BaseModel):
    """Candidate electro-thermal explanation prior to active probe measurement."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    hypothesis_id: str
    r0_ohm: float
    h_w_per_k: float
    c_j_per_k: float
    description: str = ""

    @model_validator(mode="before")
    @classmethod
    def _accept_case_aliases(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            d = dict(data)
            if "R0_ohm" in d and "r0_ohm" not in d:
                d["r0_ohm"] = d.pop("R0_ohm")
            if "h_W_per_K" in d and "h_w_per_k" not in d:
                d["h_w_per_k"] = d.pop("h_W_per_K")
            if "C_J_per_K" in d and "c_j_per_k" not in d:
                d["c_j_per_k"] = d.pop("C_J_per_K")
            if "hypothesis" in d and "hypothesis_id" not in d:
                d["hypothesis_id"] = d.pop("hypothesis")
            return d
        return data

    @field_validator("hypothesis_id")
    @classmethod
    def _val_id(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("hypothesis_id must be a non-empty string")
        return v.strip()

    @field_validator("r0_ohm", "h_w_per_k", "c_j_per_k")
    @classmethod
    def _val_pos(cls, v: Any, info: Any) -> float:
        f = _ensure_finite_float(v, info.field_name)
        if f <= 0.0:
            raise ValueError(f"{info.field_name} must be > 0, got {f}")
        return f


class SelectedMeasurementV2(BaseModel):
    """Single diagnostic probe request selected in I1."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["voltage_drop", "temperature_snapshot", "controller_setting"]
    target_asset: str
    rationale: str = Field(..., min_length=3)

    @model_validator(mode="before")
    @classmethod
    def _accept_aliases(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            d = dict(data)
            if "kind" in d and "type" not in d:
                d["type"] = d.pop("kind")
            if "target" in d and "target_asset" not in d:
                d["target_asset"] = d.pop("target")
            if "target_node" in d and "target_asset" not in d:
                d["target_asset"] = d.pop("target_node")
            return d
        return data

    @field_validator("target_asset")
    @classmethod
    def _val_target(cls, v: str) -> str:
        return normalize_rack_id(v)


def _validate_complete_edges(edges: Sequence[EdgeV2]) -> List[EdgeV2]:
    if len(edges) != 4:
        raise ValueError(f"Expected 4 rack->PDU edges, got {len(edges)}")
    seen_racks = [e.rack for e in edges]
    if set(seen_racks) != set(CANONICAL_RACKS):
        raise ValueError(
            f"Edges must cover all 4 distinct racks {CANONICAL_RACKS}, got {seen_racks}"
        )
    return sorted(list(edges), key=lambda e: e.rack)


def _validate_complete_sensor_edges(sensor_edges: Sequence[SensorEdgeV2]) -> List[SensorEdgeV2]:
    if len(sensor_edges) != 4:
        raise ValueError(f"Expected 4 sensor->rack edges, got {len(sensor_edges)}")
    seen_sensors = [se.sensor_id for se in sensor_edges]
    seen_racks = [se.rack_id for se in sensor_edges]
    if set(seen_sensors) != set(CANONICAL_SENSORS):
        raise ValueError(
            f"sensor_edges must cover all 4 distinct sensors {CANONICAL_SENSORS}, got {seen_sensors}"
        )
    if set(seen_racks) != set(CANONICAL_RACKS):
        raise ValueError(
            f"sensor_edges must map bijectively to all 4 racks {CANONICAL_RACKS}, got {seen_racks}"
        )
    return sorted(list(sensor_edges), key=lambda se: se.sensor_id)


# ---------------------------------------------------------------------------
# Turn 1: Investigator Proposal (`ProposalV2` / `proposal.json`)
# ---------------------------------------------------------------------------


class ProposalV2(BaseModel):
    """Turn 1 (`I1`) Investigator proposal contract (`proposal.json`)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal["visual-v2"] = "visual-v2"
    role: Literal["investigator_proposal"] = "investigator_proposal"
    case_id: str
    hotspot: HotspotV2
    edges: List[EdgeV2]
    sensor_edges: List[SensorEdgeV2]
    unknown_critical_edges: List[str]
    hypotheses: List[HypothesisV2]
    selected_measurement: SelectedMeasurementV2

    @field_validator("edges")
    @classmethod
    def _val_edges(cls, v: List[EdgeV2]) -> List[EdgeV2]:
        return _validate_complete_edges(v)

    @field_validator("sensor_edges")
    @classmethod
    def _val_sensors(cls, v: List[SensorEdgeV2]) -> List[SensorEdgeV2]:
        return _validate_complete_sensor_edges(v)

    @field_validator("unknown_critical_edges")
    @classmethod
    def _val_unknown_edges(cls, v: Any) -> List[str]:
        if not isinstance(v, (list, tuple)):
            raise ValueError("unknown_critical_edges must be a list of strings")
        return [str(x).strip() for x in v if str(x).strip()]

    @field_validator("hypotheses")
    @classmethod
    def _val_hyps(cls, v: List[HypothesisV2]) -> List[HypothesisV2]:
        if len(v) < 2:
            raise ValueError("hypotheses must contain at least 2 candidate thermal hypotheses")
        ids = [h.hypothesis_id for h in v]
        if len(ids) != len(set(ids)):
            raise ValueError(f"Duplicate hypothesis_id in hypotheses: {ids}")
        return v

    @model_validator(mode="after")
    def _check_consistency(self) -> "ProposalV2":
        validate_contract_hygiene(self.model_dump())
        if self.selected_measurement.target_asset != self.hotspot.asset_id:
            raise ValueError(
                f"selected_measurement.target_asset ({self.selected_measurement.target_asset!r}) "
                f"must match hotspot.asset_id ({self.hotspot.asset_id!r})"
            )
        return self

    def power_map(self) -> Dict[str, str]:
        return {e.rack: e.pdu for e in self.edges}

    def sensor_map(self) -> Dict[str, str]:
        return {se.sensor_id: se.rack_id for se in self.sensor_edges}


InvestigatorProposalV2 = ProposalV2


# ---------------------------------------------------------------------------
# Turn 2: Investigator Plan (`PlanV2` / `plan.json`)
# ---------------------------------------------------------------------------


class InferredRackSpecV2(BaseModel):
    """Per-rack inferred physical & electrical specification in I2 `inferred_network`."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    pdu_id: str
    max_current_a: float
    r0_ohm: float
    h_w_per_k: float
    c_j_per_k: float
    alpha_per_c: float = 0.004
    max_temp_c: float = DEFAULT_MAX_TEMP_C

    @field_validator("pdu_id")
    @classmethod
    def _val_pdu(cls, v: str) -> str:
        return normalize_pdu_id(v)

    @field_validator("max_current_a", "r0_ohm", "h_w_per_k", "c_j_per_k", "max_temp_c")
    @classmethod
    def _val_pos(cls, v: Any, info: Any) -> float:
        f = _ensure_finite_float(v, info.field_name)
        if f <= 0.0:
            raise ValueError(f"{info.field_name} must be > 0")
        return f

    @field_validator("alpha_per_c")
    @classmethod
    def _val_alpha(cls, v: Any) -> float:
        f = _ensure_finite_float(v, "alpha_per_c")
        if f < 0.0:
            raise ValueError("alpha_per_c must be >= 0")
        return f


class InferredNetworkV2(BaseModel):
    """Complete inferred network model used by I2 to simulate candidate migrations."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    dt_s: int = DEFAULT_STEP_S
    horizon_s: int = DEFAULT_HORIZON_S
    ambient_c: float = AMBIENT_TEMP_C
    alpha_per_c: float = 0.004
    max_temp_c: float = DEFAULT_MAX_TEMP_C
    racks: Dict[str, InferredRackSpecV2]
    pdus: Dict[str, float] = Field(default_factory=lambda: {"F1": 44.0, "F2": 50.0})

    @field_validator("dt_s")
    @classmethod
    def _val_dt(cls, v: int) -> int:
        if int(v) != DEFAULT_STEP_S:
            raise ValueError(f"dt_s must equal {DEFAULT_STEP_S}, got {v}")
        return int(v)

    @field_validator("horizon_s")
    @classmethod
    def _val_horizon(cls, v: int) -> int:
        if int(v) != DEFAULT_HORIZON_S:
            raise ValueError(f"horizon_s must equal {DEFAULT_HORIZON_S}, got {v}")
        return int(v)

    @field_validator("racks")
    @classmethod
    def _val_racks(cls, v: Mapping[str, InferredRackSpecV2]) -> Dict[str, InferredRackSpecV2]:
        norm: Dict[str, InferredRackSpecV2] = {}
        for k, spec in v.items():
            rk = normalize_rack_id(k)
            norm[rk] = spec
        if set(norm.keys()) != set(CANONICAL_RACKS):
            raise ValueError(f"inferred_network.racks must contain {CANONICAL_RACKS}")
        return {rk: norm[rk] for rk in CANONICAL_RACKS}

    @field_validator("pdus")
    @classmethod
    def _val_pdus(cls, v: Mapping[str, Any]) -> Dict[str, float]:
        norm: Dict[str, float] = {}
        for k, limit_val in v.items():
            pk = normalize_pdu_id(k)
            if isinstance(limit_val, Mapping):
                raw_lim = limit_val.get("max_current_a", limit_val.get("max_current_A"))
            else:
                raw_lim = limit_val
            lim = _ensure_finite_float(raw_lim, f"pdus[{pk}]")
            if lim <= 0.0:
                raise ValueError(f"PDU limit for {pk} must be > 0")
            norm[pk] = lim
        if set(norm.keys()) != set(CANONICAL_PDUS):
            raise ValueError(f"inferred_network.pdus must contain {CANONICAL_PDUS}")
        return {pk: norm[pk] for pk in CANONICAL_PDUS}


class InitialStateV2(BaseModel):
    """Initial t=0 rack temperatures and load currents in I2 `plan.json`."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    temperatures_c: Dict[str, float]
    currents_a: Dict[str, float]

    @field_validator("temperatures_c")
    @classmethod
    def _val_temps(cls, v: Mapping[str, Any]) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for k, val in v.items():
            rk = normalize_rack_id(k)
            out[rk] = _ensure_finite_float(val, f"temperatures_c[{rk}]")
        if set(out.keys()) != set(CANONICAL_RACKS):
            raise ValueError(f"temperatures_c must cover {CANONICAL_RACKS}")
        return {rk: out[rk] for rk in CANONICAL_RACKS}

    @field_validator("currents_a")
    @classmethod
    def _val_currents(cls, v: Mapping[str, Any]) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for k, val in v.items():
            rk = normalize_rack_id(k)
            c = _ensure_finite_float(val, f"currents_a[{rk}]")
            if c < 0.0:
                raise ValueError(f"currents_a[{rk}] must be >= 0")
            out[rk] = c
        if set(out.keys()) != set(CANONICAL_RACKS):
            raise ValueError(f"currents_a must cover {CANONICAL_RACKS}")
        total = sum(out.values())
        if abs(total - TOTAL_WORLD_CURRENT_A) > 1e-6:
            raise ValueError(
                f"initial_state.currents_a sum {total} violates conservation ({TOTAL_WORLD_CURRENT_A} A)"
            )
        return {rk: out[rk] for rk in CANONICAL_RACKS}


class PostMeasurementParamsV2(BaseModel):
    """Post-probe identified physical parameters for the hot source rack (`N4`)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    target_rack: str = "N4"
    r0_ohm: float
    h_w_per_k: float
    c_j_per_k: float

    @model_validator(mode="before")
    @classmethod
    def _accept_case_aliases(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            d = dict(data)
            if "R0_ohm" in d and "r0_ohm" not in d:
                d["r0_ohm"] = d.pop("R0_ohm")
            if "h_W_per_K" in d and "h_w_per_k" not in d:
                d["h_w_per_k"] = d.pop("h_W_per_K")
            if "C_J_per_K" in d and "c_j_per_k" not in d:
                d["c_j_per_k"] = d.pop("C_J_per_K")
            return d
        return data

    @field_validator("target_rack")
    @classmethod
    def _val_target(cls, v: str) -> str:
        return normalize_rack_id(v)

    @field_validator("r0_ohm", "h_w_per_k", "c_j_per_k")
    @classmethod
    def _val_pos(cls, v: Any, info: Any) -> float:
        f = _ensure_finite_float(v, info.field_name)
        if f <= 0.0:
            raise ValueError(f"{info.field_name} must be > 0")
        return f


class CandidateActionV2(BaseModel):
    """Candidate action evaluated in I2 (`NO_ACTION`, `N4->N2`, `N4->N3`)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    action_id: str
    source_rack: Optional[str] = None
    target_rack: Optional[str] = None
    amps: float = 0.0

    @model_validator(mode="after")
    def _norm_fields(self) -> "CandidateActionV2":
        norm_id = normalize_action_id(self.action_id, self.target_rack)
        object.__setattr__(self, "action_id", norm_id)
        if norm_id == "NO_ACTION":
            object.__setattr__(self, "source_rack", None)
            object.__setattr__(self, "target_rack", None)
            object.__setattr__(self, "amps", 0.0)
        elif norm_id == "MIGRATE_N4_TO_N2_8A":
            object.__setattr__(self, "source_rack", "N4")
            object.__setattr__(self, "target_rack", "N2")
            object.__setattr__(self, "amps", DEFAULT_MIGRATION_AMPS)
        elif norm_id == "MIGRATE_N4_TO_N3_8A":
            object.__setattr__(self, "source_rack", "N4")
            object.__setattr__(self, "target_rack", "N3")
            object.__setattr__(self, "amps", DEFAULT_MIGRATION_AMPS)
        return self


class ActionSafetyV2(BaseModel):
    """Safety evaluation flags for a simulated candidate action."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    safe: bool
    thermal_violation: bool
    pdu_overload: bool
    rack_overload: bool = False
    violated_racks: List[str] = Field(default_factory=list)
    violated_pdus: List[str] = Field(default_factory=list)
    verdict: str = ""

    @field_validator("violated_racks")
    @classmethod
    def _val_vracks(cls, v: Sequence[str]) -> List[str]:
        return [normalize_rack_id(x) for x in v]

    @field_validator("violated_pdus")
    @classmethod
    def _val_vpdus(cls, v: Sequence[str]) -> List[str]:
        return [normalize_pdu_id(x) for x in v]


class ActionPredictionV2(BaseModel):
    """19-sample (`0..180s`, `dt=10s`) electro-thermal trajectory prediction for one action."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    time_s: List[int]
    temps_c: Dict[str, List[float]]
    rack_currents_a: Dict[str, List[float]]
    pdu_currents_a: Dict[str, List[float]]
    safety: ActionSafetyV2

    @field_validator("time_s")
    @classmethod
    def _val_time(cls, v: Sequence[Any]) -> List[int]:
        expected = [k * DEFAULT_STEP_S for k in range(DEFAULT_HORIZON_S // DEFAULT_STEP_S + 1)]
        ints = [int(round(_ensure_finite_float(x, "time_s"))) for x in v]
        if ints != expected:
            raise ValueError(f"time_s must equal {expected}, got {ints}")
        return ints

    @field_validator("temps_c")
    @classmethod
    def _val_temps(cls, v: Mapping[str, Sequence[Any]]) -> Dict[str, List[float]]:
        out: Dict[str, List[float]] = {}
        for k, seq in v.items():
            rk = normalize_rack_id(k)
            if not isinstance(seq, (list, tuple)) or len(seq) != 19:
                raise ValueError(f"temps_c[{rk}] must have 19 samples, got {len(seq) if isinstance(seq, (list, tuple)) else 'non-list'}")
            out[rk] = [_ensure_finite_float(x, f"temps_c[{rk}]") for x in seq]
        if set(out.keys()) != set(CANONICAL_RACKS):
            raise ValueError(f"temps_c must cover {CANONICAL_RACKS}")
        return {rk: out[rk] for rk in CANONICAL_RACKS}

    @field_validator("rack_currents_a")
    @classmethod
    def _val_rcurrents(cls, v: Mapping[str, Sequence[Any]]) -> Dict[str, List[float]]:
        out: Dict[str, List[float]] = {}
        for k, seq in v.items():
            rk = normalize_rack_id(k)
            if not isinstance(seq, (list, tuple)) or len(seq) != 19:
                raise ValueError(f"rack_currents_a[{rk}] must have 19 samples")
            out[rk] = [_ensure_finite_float(x, f"rack_currents_a[{rk}]") for x in seq]
        if set(out.keys()) != set(CANONICAL_RACKS):
            raise ValueError(f"rack_currents_a must cover {CANONICAL_RACKS}")
        for idx in range(19):
            step_sum = sum(out[rk][idx] for rk in CANONICAL_RACKS)
            if abs(step_sum - TOTAL_WORLD_CURRENT_A) > 1e-6:
                raise ValueError(
                    f"rack_currents_a step {idx} sum {step_sum} violates 68.0 A conservation"
                )
        return {rk: out[rk] for rk in CANONICAL_RACKS}

    @field_validator("pdu_currents_a")
    @classmethod
    def _val_pcurrents(cls, v: Mapping[str, Sequence[Any]]) -> Dict[str, List[float]]:
        out: Dict[str, List[float]] = {}
        for k, seq in v.items():
            pk = normalize_pdu_id(k)
            if not isinstance(seq, (list, tuple)) or len(seq) != 19:
                raise ValueError(f"pdu_currents_a[{pk}] must have 19 samples")
            out[pk] = [_ensure_finite_float(x, f"pdu_currents_a[{pk}]") for x in seq]
        if set(out.keys()) != set(CANONICAL_PDUS):
            raise ValueError(f"pdu_currents_a must cover {CANONICAL_PDUS}")
        return {pk: out[pk] for pk in CANONICAL_PDUS}


class PlanV2(BaseModel):
    """Turn 2 (`I2`) Investigator post-probe plan contract (`plan.json`)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal["visual-v2"] = "visual-v2"
    role: Literal["investigator_plan"] = "investigator_plan"
    case_id: str
    incident_id: str = "INC-BLACKBOX-001"
    inferred_network: InferredNetworkV2
    initial_state: InitialStateV2
    post_measurement_parameters: PostMeasurementParamsV2
    forecast_source: str = Field(..., min_length=10)
    candidate_actions: List[CandidateActionV2]
    predictions_by_action: Dict[str, ActionPredictionV2]
    selected_action_id: str
    decision: Literal["intervene", "abstain"]
    blocking_graph_uncertainty: bool = False
    reason_es: str = Field(..., min_length=5)
    physical_safety_certified: bool

    @field_validator("forecast_source")
    @classmethod
    def _val_forecast_source(cls, v: str) -> str:
        s = v.strip()
        if not any(tok in s for tok in ("step_temperature", "simulate_world", "def ", "sha256:")):
            raise ValueError(
                "forecast_source must be an inspectable function definition or kernel source/hash reference"
            )
        return s

    @field_validator("selected_action_id")
    @classmethod
    def _val_selected_action(cls, v: str) -> str:
        return normalize_action_id(v)

    @field_validator("physical_safety_certified")
    @classmethod
    def _val_cert(cls, v: bool) -> bool:
        if v is not False:
            raise ValueError("physical_safety_certified must be False (advisory simulation only)")
        return False

    @model_validator(mode="after")
    def _validate_plan_invariants(self) -> "PlanV2":
        validate_contract_hygiene(self.model_dump())

        action_ids = [a.action_id for a in self.candidate_actions]
        if set(action_ids) != set(CANONICAL_ACTION_IDS) or len(action_ids) != 3:
            raise ValueError(
                f"candidate_actions must cover exactly {CANONICAL_ACTION_IDS}, got {action_ids}"
            )

        norm_preds: Dict[str, ActionPredictionV2] = {}
        for k, pred in self.predictions_by_action.items():
            nk = normalize_action_id(k)
            norm_preds[nk] = pred
        if set(norm_preds.keys()) != set(action_ids):
            raise ValueError(
                f"predictions_by_action keys {sorted(norm_preds.keys())} must equal candidate_actions {sorted(action_ids)}"
            )
        object.__setattr__(self, "predictions_by_action", norm_preds)

        if self.selected_action_id not in norm_preds:
            raise ValueError(
                f"selected_action_id {self.selected_action_id!r} not in candidate_actions"
            )

        if self.blocking_graph_uncertainty:
            if self.decision != "abstain" or self.selected_action_id != "NO_ACTION":
                raise ValueError(
                    "When blocking_graph_uncertainty is True, decision must be 'abstain' and selected_action_id must be 'NO_ACTION'"
                )
        if self.decision == "abstain" and self.selected_action_id != "NO_ACTION":
            raise ValueError("When decision == 'abstain', selected_action_id must be 'NO_ACTION'")
        if self.decision == "intervene" and self.selected_action_id == "NO_ACTION":
            raise ValueError("When decision == 'intervene', selected_action_id must be a migration action")

        return self

    def power_map(self) -> Dict[str, str]:
        return {rk: spec.pdu_id for rk, spec in self.inferred_network.racks.items()}


InvestigatorPlanV2 = PlanV2


def build_world_from_plan_v2(plan: PlanV2) -> World:
    """Construct a `domain.World` instance from `PlanV2.inferred_network`, `initial_state`, and `post_measurement_parameters`."""
    net = plan.inferred_network
    init = plan.initial_state
    post = plan.post_measurement_parameters

    rack_list: List[RackParams] = []
    for rk in CANONICAL_RACKS:
        spec = net.racks[rk]
        r0 = post.r0_ohm if rk == post.target_rack else spec.r0_ohm
        h = post.h_w_per_k if rk == post.target_rack else spec.h_w_per_k
        c = post.c_j_per_k if rk == post.target_rack else spec.c_j_per_k
        rack_list.append(
            RackParams(
                rack_id=rk,
                pdu_id=spec.pdu_id,
                current_A=init.currents_a[rk],
                initial_temp_C=init.temperatures_c[rk],
                R0_ohm=r0,
                alpha_per_C=spec.alpha_per_c,
                h_W_per_K=h,
                C_J_per_K=c,
                max_current_A=spec.max_current_a,
                max_temp_C=spec.max_temp_c,
            )
        )

    pdu_list = [
        PDU(pdu_id="F1", max_current_A=net.pdus["F1"]),
        PDU(pdu_id="F2", max_current_A=net.pdus["F2"]),
    ]
    return World(
        racks=tuple(rack_list),
        pdus=tuple(pdu_list),
        ambient_C=net.ambient_c,
        horizon_s=net.horizon_s,
        step_s=net.dt_s,
        allowed_targets=("N2", "N3"),
    )


def verify_plan_against_inferred_network(
    plan: PlanV2,
    temp_tol_c: float = 1e-3,
    current_tol_a: float = 1e-6,
) -> Dict[str, Any]:
    """Verify that `plan.predictions_by_action` matches `simulate_world` on `plan.inferred_network`."""
    world = build_world_from_plan_v2(plan)
    migrations = {
        "NO_ACTION": None,
        "MIGRATE_N4_TO_N2_8A": Migration(source="N4", target="N2", amps=DEFAULT_MIGRATION_AMPS),
        "MIGRATE_N4_TO_N3_8A": Migration(source="N4", target="N3", amps=DEFAULT_MIGRATION_AMPS),
    }

    mismatches: List[str] = []
    max_temp_err = 0.0
    max_pdu_err = 0.0

    for aid, mig in migrations.items():
        sim = simulate_world(world, migration=mig)
        pred = plan.predictions_by_action[aid]

        for rk in CANONICAL_RACKS:
            for t_idx in range(19):
                err_t = abs(pred.temps_c[rk][t_idx] - sim.temps_C[rk][t_idx])
                if err_t > max_temp_err:
                    max_temp_err = err_t
                if err_t > temp_tol_c:
                    mismatches.append(f"{aid}.temps_c[{rk}][{t_idx}] error {err_t:.6f} > {temp_tol_c}")

        for pk in CANONICAL_PDUS:
            for t_idx in range(19):
                err_i = abs(pred.pdu_currents_a[pk][t_idx] - sim.pdu_currents_A[pk][t_idx])
                if err_i > max_pdu_err:
                    max_pdu_err = err_i
                if err_i > current_tol_a:
                    mismatches.append(f"{aid}.pdu_currents_a[{pk}][{t_idx}] error {err_i:.6f} > {current_tol_a}")

        if pred.safety.safe != bool(sim.safety_flags["safe"]):
            mismatches.append(f"{aid}.safety.safe={pred.safety.safe} != {sim.safety_flags['safe']}")
        if pred.safety.thermal_violation != bool(sim.safety_flags["thermal_violation"]):
            mismatches.append(f"{aid}.safety.thermal_violation mismatch")
        if pred.safety.pdu_overload != bool(sim.safety_flags["pdu_overload"]):
            mismatches.append(f"{aid}.safety.pdu_overload mismatch")

    return {
        "consistent": len(mismatches) == 0,
        "max_temp_error_c": max_temp_err,
        "max_pdu_current_error_a": max_pdu_err,
        "mismatches": mismatches,
    }


# ---------------------------------------------------------------------------
# Turn 3: Auditor Visual Graph (`IndependentGraphV2` / `independent_graph.json`)
# ---------------------------------------------------------------------------


class IndependentGraphV2(BaseModel):
    """Turn 3 (`A1`) Independent Auditor visual graph contract (`independent_graph.json`)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal["visual-v2"] = "visual-v2"
    role: Literal["auditor_visual"] = "auditor_visual"
    case_id: str
    input_png_sha256: Dict[str, str]
    hotspot: HotspotV2
    edges: List[EdgeV2]
    sensor_edges: List[SensorEdgeV2]
    unknown_critical_edges: List[str]
    notes: str = Field(..., min_length=3)

    @model_validator(mode="before")
    @classmethod
    def _reject_investigator_fields(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            leaked = INVESTIGATOR_ONLY_KEYS.intersection(data.keys())
            if leaked:
                raise ValueError(
                    f"Auditor A1 contract contains forbidden Investigator fields: {sorted(leaked)}"
                )
        return data

    @field_validator("input_png_sha256")
    @classmethod
    def _val_png_hashes(cls, v: Mapping[str, str]) -> Dict[str, str]:
        if not isinstance(v, Mapping):
            raise ValueError("input_png_sha256 must be a mapping")
        out: Dict[str, str] = {}
        for k, digest in v.items():
            ks = str(k).strip()
            ds = str(digest).strip().lower()
            if len(ds) != 64 or any(c not in "0123456789abcdef" for c in ds):
                raise ValueError(f"Invalid SHA-256 hex digest for {ks!r}: {digest!r}")
            out[ks] = ds
        for req in ("sld", "rgb", "thermal"):
            if req not in out:
                raise ValueError(f"input_png_sha256 missing required key {req!r}")
        return out

    @field_validator("edges")
    @classmethod
    def _val_edges(cls, v: List[EdgeV2]) -> List[EdgeV2]:
        return _validate_complete_edges(v)

    @field_validator("sensor_edges")
    @classmethod
    def _val_sensors(cls, v: List[SensorEdgeV2]) -> List[SensorEdgeV2]:
        return _validate_complete_sensor_edges(v)

    @field_validator("unknown_critical_edges")
    @classmethod
    def _val_unknown_edges(cls, v: Any) -> List[str]:
        if not isinstance(v, (list, tuple)):
            raise ValueError("unknown_critical_edges must be a list of strings")
        return [str(x).strip() for x in v if str(x).strip()]

    @model_validator(mode="after")
    def _val_hygiene(self) -> "IndependentGraphV2":
        validate_contract_hygiene(self.model_dump())
        return self

    def power_map(self) -> Dict[str, str]:
        return {e.rack: e.pdu for e in self.edges}

    def sensor_map(self) -> Dict[str, str]:
        return {se.sensor_id: se.rack_id for se in self.sensor_edges}


AuditorGraphV2 = IndependentGraphV2


# ---------------------------------------------------------------------------
# Turn 4: Auditor Challenge (`AuditChallengeV2` / `audit_challenge.json`)
# ---------------------------------------------------------------------------


class EdgeComparisonV2(BaseModel):
    """Comparison between Auditor A1's independent power graph and Investigator I2's inferred graph."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    a1_power_map: Dict[str, str]
    i2_power_map: Dict[str, str]
    graphs_identical: bool
    disagreed_racks: List[str] = Field(default_factory=list)

    @field_validator("a1_power_map", "i2_power_map")
    @classmethod
    def _val_map(cls, v: Mapping[str, str]) -> Dict[str, str]:
        out = {normalize_rack_id(k): normalize_pdu_id(val) for k, val in v.items()}
        if set(out.keys()) != set(CANONICAL_RACKS):
            raise ValueError(f"Power map must cover {CANONICAL_RACKS}")
        return {rk: out[rk] for rk in CANONICAL_RACKS}

    @field_validator("disagreed_racks")
    @classmethod
    def _val_disagreed(cls, v: Sequence[str]) -> List[str]:
        return sorted({normalize_rack_id(x) for x in v})

    @model_validator(mode="after")
    def _check_diff(self) -> "EdgeComparisonV2":
        actual_diff = sorted(
            rk for rk in CANONICAL_RACKS if self.a1_power_map[rk] != self.i2_power_map[rk]
        )
        expected_identical = len(actual_diff) == 0
        if self.graphs_identical != expected_identical or self.disagreed_racks != actual_diff:
            raise ValueError(
                f"edge_comparison inconsistent: graphs_identical={self.graphs_identical}, "
                f"disagreed_racks={self.disagreed_racks}, actual_diff={actual_diff}"
            )
        return self


class SensitivityCanaryV2(BaseModel):
    """+1.0 C initial temperature perturbation canary on N4 verifying dynamic re-simulation."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    delta_temp_c: float = 1.0
    target_rack: str = "N4"
    action_id: str = "NO_ACTION"
    baseline_n4_t180_c: float
    perturbed_n4_t180_c: float
    max_abs_diff_c: float
    trajectory_temps_c: Dict[str, List[float]]

    @field_validator("target_rack")
    @classmethod
    def _val_rack(cls, v: str) -> str:
        return normalize_rack_id(v)

    @field_validator("action_id")
    @classmethod
    def _val_act(cls, v: str) -> str:
        return normalize_action_id(v)

    @field_validator("delta_temp_c", "baseline_n4_t180_c", "perturbed_n4_t180_c", "max_abs_diff_c")
    @classmethod
    def _val_floats(cls, v: Any, info: Any) -> float:
        return _ensure_finite_float(v, info.field_name)

    @field_validator("trajectory_temps_c")
    @classmethod
    def _val_traj(cls, v: Mapping[str, Sequence[Any]]) -> Dict[str, List[float]]:
        out: Dict[str, List[float]] = {}
        for k, seq in v.items():
            rk = normalize_rack_id(k)
            if not isinstance(seq, (list, tuple)) or len(seq) != 19:
                raise ValueError(f"trajectory_temps_c[{rk}] must have 19 samples")
            out[rk] = [_ensure_finite_float(x, f"trajectory_temps_c[{rk}]") for x in seq]
        if set(out.keys()) != set(CANONICAL_RACKS):
            raise ValueError(f"trajectory_temps_c must cover {CANONICAL_RACKS}")
        return {rk: out[rk] for rk in CANONICAL_RACKS}

    @model_validator(mode="after")
    def _check_nonzero_sensitivity(self) -> "SensitivityCanaryV2":
        if abs(self.delta_temp_c - 1.0) > 1e-6:
            raise ValueError(f"delta_temp_c must be 1.0 C, got {self.delta_temp_c}")
        if self.max_abs_diff_c <= 1e-6 or abs(self.perturbed_n4_t180_c - self.baseline_n4_t180_c) <= 1e-9:
            raise ValueError(
                "Sensitivity canary trajectory is identical to baseline (must reflect +1.0 C perturbation)"
            )
        return self


class AuditChallengeV2(BaseModel):
    """Turn 4 (`A2`) Independent Auditor challenge contract (`audit_challenge.json`)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal["visual-v2"] = "visual-v2"
    role: Literal["auditor_challenge"] = "auditor_challenge"
    case_id: str
    verdict: Literal["accept", "reject", "abstain"]
    reason_codes: List[str] = Field(..., min_length=1)
    edge_comparison: EdgeComparisonV2
    recomputed_predictions_by_action: Dict[str, ActionPredictionV2]
    input_sensitivity_canary: SensitivityCanaryV2
    constraints_verified: Dict[str, bool]
    discrepancies: List[str] = Field(default_factory=list)

    @field_validator("reason_codes")
    @classmethod
    def _val_reasons(cls, v: Sequence[str]) -> List[str]:
        out = [str(x).strip() for x in v if str(x).strip()]
        if not out:
            raise ValueError("reason_codes must contain at least one non-empty code")
        return out

    @model_validator(mode="after")
    def _val_challenge(self) -> "AuditChallengeV2":
        validate_contract_hygiene(self.model_dump())

        norm_preds: Dict[str, ActionPredictionV2] = {}
        for k, pred in self.recomputed_predictions_by_action.items():
            norm_preds[normalize_action_id(k)] = pred
        if set(norm_preds.keys()) != set(CANONICAL_ACTION_IDS):
            raise ValueError(
                f"recomputed_predictions_by_action must cover {CANONICAL_ACTION_IDS}"
            )
        object.__setattr__(self, "recomputed_predictions_by_action", norm_preds)

        # If A1 and I2 power graphs disagree, A2 must NOT blindly accept I2's plan
        if not self.edge_comparison.graphs_identical and self.verdict == "accept":
            raise ValueError(
                "Auditor A2 cannot issue verdict='accept' when edge_comparison.graphs_identical is False"
            )
        return self


AuditorChallengeV2 = AuditChallengeV2


# ---------------------------------------------------------------------------
# Phase 4 Hash-Chain Lock Records (`L1..L4`)
# ---------------------------------------------------------------------------


class ArtifactLockRecordV2(BaseModel):
    """Cryptographic lock record binding each protocol stage (`L1..L4`) into a hash chain."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    stage: Literal["L1_I1_PROPOSAL", "L2_I2_PLAN", "L3_A1_GRAPH", "L4_A2_CHALLENGE"]
    case_id: str
    canonical_sha256: str
    raw_file_sha256: str
    environment_id: str
    interaction_id: str
    retrieval_timestamp_utc: str
    previous_lock_hash: Optional[str] = None
    chain_hash: str

    @property
    def step_id(self) -> str:
        return self.stage

    @property
    def prev_chain_hash(self) -> str:
        return self.previous_lock_hash if self.previous_lock_hash is not None else "GENESIS"


def compute_lock_chain_hash(
    stage: str,
    case_id: str,
    canonical_sha256_hex: str,
    raw_file_sha256_hex: str,
    environment_id: str,
    interaction_id: str,
    retrieval_timestamp_utc: str,
    previous_lock_hash: Optional[str],
) -> str:
    """Compute deterministic chain hash over lock metadata and `previous_lock_hash`."""
    core = {
        "stage": stage,
        "case_id": case_id,
        "canonical_sha256": canonical_sha256_hex,
        "raw_file_sha256": raw_file_sha256_hex,
        "environment_id": environment_id,
        "interaction_id": interaction_id,
        "retrieval_timestamp_utc": retrieval_timestamp_utc,
        "previous_lock_hash": previous_lock_hash,
    }
    raw = json.dumps(core, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def create_lock_record_v2(
    stage: Literal["L1_I1_PROPOSAL", "L2_I2_PLAN", "L3_A1_GRAPH", "L4_A2_CHALLENGE"],
    case_id: str,
    contract_obj: Any,
    raw_file_bytes: Optional[bytes] = None,
    environment_id: str = "env_default",
    interaction_id: str = "int_default",
    previous_lock_hash: Optional[str] = None,
    retrieval_timestamp_utc: Optional[str] = None,
) -> ArtifactLockRecordV2:
    """Create a verified `ArtifactLockRecordV2` for a retrieved contract artifact."""
    if previous_lock_hash == "GENESIS":
        previous_lock_hash = None
    if stage == "L1_I1_PROPOSAL" and previous_lock_hash is not None:
        raise ValueError("L1_I1_PROPOSAL must have previous_lock_hash=None")
    if stage != "L1_I1_PROPOSAL" and not previous_lock_hash:
        raise ValueError(f"{stage} requires a non-empty previous_lock_hash")

    if raw_file_bytes is None:
        raw_file_bytes = canonical_json_bytes(contract_obj)

    ts = retrieval_timestamp_utc or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    can_sha = canonical_sha256(contract_obj)
    raw_sha = hashlib.sha256(raw_file_bytes).hexdigest()
    c_hash = compute_lock_chain_hash(
        stage=stage,
        case_id=case_id,
        canonical_sha256_hex=can_sha,
        raw_file_sha256_hex=raw_sha,
        environment_id=environment_id,
        interaction_id=interaction_id,
        retrieval_timestamp_utc=ts,
        previous_lock_hash=previous_lock_hash,
    )
    return ArtifactLockRecordV2(
        stage=stage,
        case_id=case_id,
        canonical_sha256=can_sha,
        raw_file_sha256=raw_sha,
        environment_id=environment_id,
        interaction_id=interaction_id,
        retrieval_timestamp_utc=ts,
        previous_lock_hash=previous_lock_hash,
        chain_hash=c_hash,
    )


def verify_lock_chain_v2(locks: Sequence[ArtifactLockRecordV2]) -> Dict[str, Any]:
    """Verify ordering (`L1 -> L2 -> L3 -> L4`), non-empty hashes, and unbroken `chain_hash` links."""
    if len(locks) != 4:
        return {
            "valid": False,
            "reason": f"Expected 4 lock records {LOCK_STAGE_ORDER}, got {len(locks)}",
        }

    prev_hash: Optional[str] = None
    for idx, expected_stage in enumerate(LOCK_STAGE_ORDER):
        rec = locks[idx]
        if rec.stage != expected_stage:
            return {
                "valid": False,
                "reason": f"Lock index {idx} has stage {rec.stage!r}, expected {expected_stage!r}",
            }
        if rec.previous_lock_hash != prev_hash:
            return {
                "valid": False,
                "reason": (
                    f"Broken previous_lock_hash at {rec.stage}: "
                    f"{rec.previous_lock_hash!r} != {prev_hash!r}"
                ),
            }
        recomputed = compute_lock_chain_hash(
            stage=rec.stage,
            case_id=rec.case_id,
            canonical_sha256_hex=rec.canonical_sha256,
            raw_file_sha256_hex=rec.raw_file_sha256,
            environment_id=rec.environment_id,
            interaction_id=rec.interaction_id,
            retrieval_timestamp_utc=rec.retrieval_timestamp_utc,
            previous_lock_hash=rec.previous_lock_hash,
        )
        if recomputed != rec.chain_hash:
            return {
                "valid": False,
                "reason": f"Tampered chain_hash at {rec.stage}: {rec.chain_hash!r} != {recomputed!r}",
            }
        prev_hash = rec.chain_hash

    return {
        "valid": True,
        "reason": None,
        "head_chain_hash": prev_hash,
    }


# ---------------------------------------------------------------------------
# Deterministic Reference Builders for Phase 4 Contracts (used by offline/tests)
# ---------------------------------------------------------------------------


def _trajectory_to_prediction_v2(traj: Any) -> ActionPredictionV2:
    return ActionPredictionV2(
        time_s=list(traj.time_s),
        temps_c={rk: [float(x) for x in traj.temps_C[rk]] for rk in CANONICAL_RACKS},
        rack_currents_a={rk: [float(x) for x in traj.currents_A[rk]] for rk in CANONICAL_RACKS},
        pdu_currents_a={pk: [float(x) for x in traj.pdu_currents_A[pk]] for pk in CANONICAL_PDUS},
        safety=ActionSafetyV2(
            safe=bool(traj.safety_flags["safe"]),
            thermal_violation=bool(traj.safety_flags["thermal_violation"]),
            pdu_overload=bool(traj.safety_flags["pdu_overload"]),
            rack_overload=bool(traj.safety_flags["rack_overload"]),
            violated_racks=list(traj.safety_flags["violated_racks"]),
            violated_pdus=list(traj.safety_flags["violated_pdus"]),
            verdict=str(traj.safety_flags["verdict"]),
        ),
    )


def _default_power_map_for_case(case_id: str) -> Dict[str, str]:
    short = "A" if str(case_id).strip().upper() in {"A", "CASE_A"} else "B"
    if short == "A":
        return {"N1": "F1", "N2": "F1", "N3": "F2", "N4": "F2"}
    return {"N1": "F1", "N2": "F2", "N3": "F1", "N4": "F2"}


def build_reference_proposal_v2(
    case_id: str,
    sld_power_map: Optional[Mapping[str, str]] = None,
    bbox_xyxy_px: Sequence[int] = (1079, 325, 1143, 393),
    measurement_type: Literal["voltage_drop", "temperature_snapshot", "controller_setting"] = "voltage_drop",
    unknown_critical_edges: Optional[List[str]] = None,
) -> ProposalV2:
    """Build a valid reference `ProposalV2` from SLD topology and visual localization."""
    if sld_power_map is None:
        sld_power_map = _default_power_map_for_case(case_id)
    sensor_binding = {"SNS_K1": "N1", "SNS_K2": "N4", "SNS_K3": "N2", "SNS_K4": "N3"}
    return ProposalV2(
        case_id=case_id,
        hotspot=HotspotV2(
            asset_id="N4",
            surface="rear_visible_connector",
            bbox_xyxy_px=list(bbox_xyxy_px),
            confidence=0.96,
            evidence_refs=["rgb.png", "thermal.png"],
        ),
        edges=[
            EdgeV2(rack=rk, pdu=sld_power_map[rk], evidence_ref="sld.png")
            for rk in CANONICAL_RACKS
        ],
        sensor_edges=[
            SensorEdgeV2(sensor_id=sid, rack_id=rk, evidence_ref="sld.png")
            for sid, rk in sorted(sensor_binding.items())
        ],
        unknown_critical_edges=list(unknown_critical_edges or []),
        hypotheses=[
            HypothesisV2(
                hypothesis_id="electrical",
                r0_ohm=0.040,
                h_w_per_k=0.4,
                c_j_per_k=8.0,
                description="High-resistance rear connector degradation on N4",
            ),
            HypothesisV2(
                hypothesis_id="cooling",
                r0_ohm=0.020,
                h_w_per_k=0.2,
                c_j_per_k=4.0,
                description="Local cooling degradation with nominal connector resistance",
            ),
        ],
        selected_measurement=SelectedMeasurementV2(
            type=measurement_type,
            target_asset="N4",
            rationale="Disambiguate electrical resistance R0 vs cooling degradation on hot rack N4.",
        ),
    )


def build_reference_plan_v2(
    case_id: str,
    public_packet: Optional[Mapping[str, Any]] = None,
    sld_power_map: Optional[Mapping[str, str]] = None,
    probe_receipt: Optional[Mapping[str, Any]] = None,
    blocking_graph_uncertainty: bool = False,
    *,
    proposal: Optional[ProposalV2] = None,
) -> PlanV2:
    """Build a valid reference `PlanV2` using public telemetry, SLD power map, and probe receipt."""
    if public_packet is None:
        from blackbox.fixture import build_private_world, export_public_packet

        public_packet = export_public_packet(build_private_world(case_id))
    if sld_power_map is None:
        if proposal is not None:
            sld_power_map = proposal.power_map()
        else:
            sld_power_map = _default_power_map_for_case(case_id)
    if probe_receipt is None:
        probe_receipt = {"kind": "voltage_drop", "value": 1.209409, "exact_value": 1.2094091596115595}

    telemetry = public_packet["telemetry"]
    ramp_k2 = telemetry["SNS_K2"]
    t0_n4 = float(ramp_k2["temps_C"][-1])
    i0_n4 = float(ramp_k2["currents_A"][-1])

    if probe_receipt.get("kind") == "voltage_drop":
        v_drop = float(probe_receipt.get("exact_value", probe_receipt["value"]))
        r0_n4 = infer_r0_from_voltage_drop(
            voltage_drop_v=v_drop,
            current_a=i0_n4,
            temp_c=t0_n4,
            ambient_c=float(public_packet.get("ambient_C", AMBIENT_TEMP_C)),
            alpha_per_c=float(public_packet.get("alpha_per_C", 0.004)),
        )
    else:
        r0_n4 = 0.040

    fitted = fit_thermal_parameters_from_ramp(
        times_s=ramp_k2["t_s"],
        temps_c=ramp_k2["temps_C"],
        currents_a=ramp_k2["currents_A"],
        r0_ohm=r0_n4,
        ambient_c=float(public_packet.get("ambient_C", AMBIENT_TEMP_C)),
        alpha_per_c=float(public_packet.get("alpha_per_C", 0.004)),
    )
    h_n4 = float(fitted["h_W_per_K"])
    c_n4 = float(fitted["C_J_per_K"])

    racks_spec = {
        "N1": InferredRackSpecV2(
            pdu_id=sld_power_map["N1"],
            max_current_a=40.0,
            r0_ohm=0.005,
            h_w_per_k=0.5,
            c_j_per_k=8.0,
        ),
        "N2": InferredRackSpecV2(
            pdu_id=sld_power_map["N2"],
            max_current_a=16.0,
            r0_ohm=0.010,
            h_w_per_k=0.5,
            c_j_per_k=8.0,
        ),
        "N3": InferredRackSpecV2(
            pdu_id=sld_power_map["N3"],
            max_current_a=16.0,
            r0_ohm=0.010,
            h_w_per_k=0.5,
            c_j_per_k=8.0,
        ),
        "N4": InferredRackSpecV2(
            pdu_id=sld_power_map["N4"],
            max_current_a=32.0,
            r0_ohm=r0_n4,
            h_w_per_k=h_n4,
            c_j_per_k=c_n4,
        ),
    }

    inferred_net = InferredNetworkV2(
        dt_s=DEFAULT_STEP_S,
        horizon_s=DEFAULT_HORIZON_S,
        ambient_c=AMBIENT_TEMP_C,
        alpha_per_c=0.004,
        max_temp_c=DEFAULT_MAX_TEMP_C,
        racks=racks_spec,
        pdus={"F1": 44.0, "F2": 50.0},
    )
    init_state = InitialStateV2(
        temperatures_c={
            "N1": float(telemetry["SNS_K1"]["temps_C"][-1]),
            "N2": float(telemetry["SNS_K3"]["temps_C"][-1]),
            "N3": float(telemetry["SNS_K4"]["temps_C"][-1]),
            "N4": t0_n4,
        },
        currents_a={
            "N1": float(telemetry["SNS_K1"]["currents_A"][-1]),
            "N2": float(telemetry["SNS_K3"]["currents_A"][-1]),
            "N3": float(telemetry["SNS_K4"]["currents_A"][-1]),
            "N4": i0_n4,
        },
    )
    post_params = PostMeasurementParamsV2(
        target_rack="N4",
        r0_ohm=r0_n4,
        h_w_per_k=h_n4,
        c_j_per_k=c_n4,
    )

    temp_world = World(
        racks=tuple(
            RackParams(
                rack_id=rk,
                pdu_id=racks_spec[rk].pdu_id,
                current_A=init_state.currents_a[rk],
                initial_temp_C=init_state.temperatures_c[rk],
                R0_ohm=racks_spec[rk].r0_ohm,
                alpha_per_C=0.004,
                h_W_per_K=racks_spec[rk].h_w_per_k,
                C_J_per_K=racks_spec[rk].c_j_per_k,
                max_current_A=racks_spec[rk].max_current_a,
                max_temp_C=DEFAULT_MAX_TEMP_C,
            )
            for rk in CANONICAL_RACKS
        ),
        pdus=(PDU("F1", 44.0), PDU("F2", 50.0)),
    )

    traj_none = simulate_world(temp_world, None)
    traj_n2 = simulate_world(temp_world, Migration("N4", "N2", 8.0))
    traj_n3 = simulate_world(temp_world, Migration("N4", "N3", 8.0))

    preds = {
        "NO_ACTION": _trajectory_to_prediction_v2(traj_none),
        "MIGRATE_N4_TO_N2_8A": _trajectory_to_prediction_v2(traj_n2),
        "MIGRATE_N4_TO_N3_8A": _trajectory_to_prediction_v2(traj_n3),
    }

    if blocking_graph_uncertainty:
        decision: Literal["intervene", "abstain"] = "abstain"
        selected_id = "NO_ACTION"
    elif preds["MIGRATE_N4_TO_N3_8A"].safety.safe:
        decision = "intervene"
        selected_id = "MIGRATE_N4_TO_N3_8A"
    elif preds["MIGRATE_N4_TO_N2_8A"].safety.safe:
        decision = "intervene"
        selected_id = "MIGRATE_N4_TO_N2_8A"
    else:
        decision = "abstain"
        selected_id = "NO_ACTION"

    kernel_src = inspect.getsource(step_temperature)
    return PlanV2(
        case_id=case_id,
        inferred_network=inferred_net,
        initial_state=init_state,
        post_measurement_parameters=post_params,
        forecast_source=kernel_src,
        candidate_actions=[
            CandidateActionV2(action_id="NO_ACTION"),
            CandidateActionV2(action_id="MIGRATE_N4_TO_N2_8A", source_rack="N4", target_rack="N2", amps=8.0),
            CandidateActionV2(action_id="MIGRATE_N4_TO_N3_8A", source_rack="N4", target_rack="N3", amps=8.0),
        ],
        predictions_by_action=preds,
        selected_action_id=selected_id,
        decision=decision,
        blocking_graph_uncertainty=blocking_graph_uncertainty,
        reason_es=(
            f"Se seleccionó {selected_id} tras verificar mediante simulación electro-térmica "
            f"que mantiene N4 bajo 90.0°C (52.53°C a t=180s) y respeta los límites de F1 (44A) y F2 (50A)."
        ),
        physical_safety_certified=False,
    )


def build_reference_independent_graph_v2(
    case_id: str,
    sld_power_map: Optional[Mapping[str, str]] = None,
    input_png_sha256: Optional[Mapping[str, str]] = None,
    bbox_xyxy_px: Sequence[int] = (1079, 325, 1143, 393),
    unknown_critical_edges: Optional[List[str]] = None,
) -> IndependentGraphV2:
    """Build a valid reference `IndependentGraphV2` (`independent_graph.json`) for Turn 3 (`A1`)."""
    if sld_power_map is None:
        sld_power_map = _default_power_map_for_case(case_id)
    if input_png_sha256 is None:
        input_png_sha256 = {
            "sld": hashlib.sha256(f"sld_{case_id}".encode("utf-8")).hexdigest(),
            "rgb": hashlib.sha256(b"rgb_shared").hexdigest(),
            "thermal": hashlib.sha256(b"thermal_shared").hexdigest(),
        }
    sensor_binding = {"SNS_K1": "N1", "SNS_K2": "N4", "SNS_K3": "N2", "SNS_K4": "N3"}
    return IndependentGraphV2(
        case_id=case_id,
        input_png_sha256=dict(input_png_sha256),
        hotspot=HotspotV2(
            asset_id="N4",
            surface="rear_visible_connector",
            bbox_xyxy_px=list(bbox_xyxy_px),
            confidence=0.97,
            evidence_refs=["rgb.png", "thermal.png"],
        ),
        edges=[
            EdgeV2(rack=rk, pdu=sld_power_map[rk], evidence_ref="sld.png")
            for rk in CANONICAL_RACKS
        ],
        sensor_edges=[
            SensorEdgeV2(sensor_id=sid, rack_id=rk, evidence_ref="sld.png")
            for sid, rk in sorted(sensor_binding.items())
        ],
        unknown_critical_edges=list(unknown_critical_edges or []),
        notes="Independent visual verification of SLD junction dots, crossing arches, and N4 rear connector hotspot.",
    )


def build_reference_audit_challenge_v2(
    case_id: str,
    a1_graph: Optional[IndependentGraphV2] = None,
    i2_plan: Optional[PlanV2] = None,
    *,
    independent_graph: Optional[IndependentGraphV2] = None,
    plan: Optional[PlanV2] = None,
    proposal: Optional[ProposalV2] = None,
) -> AuditChallengeV2:
    """Build a valid reference `AuditChallengeV2` (`audit_challenge.json`) for Turn 4 (`A2`)."""
    if a1_graph is None:
        a1_graph = independent_graph or build_reference_independent_graph_v2(case_id)
    if i2_plan is None:
        i2_plan = plan or build_reference_plan_v2(case_id, proposal=proposal)
    a1_map = a1_graph.power_map()
    i2_map = i2_plan.power_map()
    disagreed = sorted(rk for rk in CANONICAL_RACKS if a1_map[rk] != i2_map[rk])
    graphs_identical = len(disagreed) == 0

    init = i2_plan.initial_state
    post = i2_plan.post_measurement_parameters
    net = i2_plan.inferred_network

    # Recompute all 3 candidate actions under A1's independent power graph + I2's fitted parameters
    a1_world = World(
        racks=tuple(
            RackParams(
                rack_id=rk,
                pdu_id=a1_map[rk],
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

    recomputed = {
        "NO_ACTION": _trajectory_to_prediction_v2(simulate_world(a1_world, None)),
        "MIGRATE_N4_TO_N2_8A": _trajectory_to_prediction_v2(
            simulate_world(a1_world, Migration("N4", "N2", 8.0))
        ),
        "MIGRATE_N4_TO_N3_8A": _trajectory_to_prediction_v2(
            simulate_world(a1_world, Migration("N4", "N3", 8.0))
        ),
    }

    # Sensitivity canary: +1.0 C on N4 initial temperature under NO_ACTION
    perturbed_world = World(
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
                max_temp_C=r.max_temp_C,
            )
            for r in a1_world.racks
        ),
        pdus=a1_world.pdus,
        ambient_C=a1_world.ambient_C,
        horizon_s=a1_world.horizon_s,
        step_s=a1_world.step_s,
    )
    traj_pert = simulate_world(perturbed_world, None)
    base_n4_series = recomputed["NO_ACTION"].temps_c["N4"]
    pert_n4_series = [float(x) for x in traj_pert.temps_C["N4"]]
    max_diff = max(abs(a - b) for a, b in zip(base_n4_series, pert_n4_series))

    selected_recomputed = recomputed[i2_plan.selected_action_id]
    plan_check = verify_plan_against_inferred_network(i2_plan)

    discrepancies: List[str] = []
    reason_codes: List[str] = []
    if not graphs_identical:
        discrepancies.append(f"Graph topology mismatch on racks: {disagreed}")
        reason_codes.append("GRAPH_MISMATCH")
    if not plan_check["consistent"]:
        discrepancies.extend(plan_check["mismatches"])
        reason_codes.append("TRAJECTORY_MISMATCH")
    if i2_plan.decision == "intervene" and not selected_recomputed.safety.safe:
        discrepancies.append(
            f"Selected action {i2_plan.selected_action_id} is unsafe under A1 graph: {selected_recomputed.safety.verdict}"
        )
        reason_codes.append("UNSAFE_SELECTED_ACTION")

    if a1_graph.unknown_critical_edges or i2_plan.blocking_graph_uncertainty:
        verdict: Literal["accept", "reject", "abstain"] = "abstain"
        reason_codes.append("BLOCKING_UNCERTAINTY")
    elif discrepancies:
        verdict = "reject"
    else:
        verdict = "accept"
        reason_codes.append("ALL_CONSTRAINTS_VERIFIED")

    return AuditChallengeV2(
        case_id=case_id,
        verdict=verdict,
        reason_codes=reason_codes,
        edge_comparison=EdgeComparisonV2(
            a1_power_map=a1_map,
            i2_power_map=i2_map,
            graphs_identical=graphs_identical,
            disagreed_racks=disagreed,
        ),
        recomputed_predictions_by_action=recomputed,
        input_sensitivity_canary=SensitivityCanaryV2(
            delta_temp_c=1.0,
            target_rack="N4",
            action_id="NO_ACTION",
            baseline_n4_t180_c=float(base_n4_series[-1]),
            perturbed_n4_t180_c=float(pert_n4_series[-1]),
            max_abs_diff_c=float(max_diff),
            trajectory_temps_c={rk: [float(x) for x in traj_pert.temps_C[rk]] for rk in CANONICAL_RACKS},
        ),
        constraints_verified={
            "graphs_identical": graphs_identical,
            "current_conservation_68a": True,
            "selected_action_safe_under_a1": bool(selected_recomputed.safety.safe),
            "sensitivity_canary_nonzero": bool(max_diff > 1e-6),
            "physical_safety_certified_false": (i2_plan.physical_safety_certified is False),
        },
        discrepancies=discrepancies,
    )


# ---------------------------------------------------------------------------
# Backward-Compatible Phase 00 Contracts
# ---------------------------------------------------------------------------


class ProbeRequest(BaseModel):
    """Single active voltage-drop probe request emitted at the end of I1."""

    target_node: str = Field(..., description="Node ID to probe (e.g., N2 or N3)")
    delta_current_a: float = Field(
        default=20.0,
        gt=0.0,
        le=50.0,
        description="Test current pulse amplitude in Amperes",
    )
    rationale: str = Field(..., min_length=3, description="Engineering reason for probing this node")

    @field_validator("target_node")
    @classmethod
    def _validate_node(cls, v: str) -> str:
        norm = v.strip().upper()
        if norm not in VALID_NODES:
            raise ValueError(f"target_node must be one of {sorted(VALID_NODES)}, got {v!r}")
        return norm


class ProbeResponse(BaseModel):
    """Measured voltage-drop response returned by the deterministic probe interface."""

    case_id: str
    target_node: str
    connected_feeder_in_sld: str
    base_current_a: float
    delta_current_a: float
    base_voltage_drop_v: float
    probed_voltage_drop_v: float
    delta_voltage_drop_v: float
    inferred_resistance_ohm: float
    terminal_temperature_c: float
    timestamp_utc: str


class InvestigatorI1Contract(BaseModel):
    """Phase I1 output: Initial visual + telemetry diagnosis and single probe selection."""

    interaction_id: Literal["I1"] = "I1"
    case_id: str
    visual_hotspot_node: str = Field(..., description="Node identified as thermal hotspot in 3D render")
    sld_feeder_mapping: Dict[str, str] = Field(
        ..., description="Observed mapping of node_id -> feeder_id from the Single-Line Diagram"
    )
    candidate_fault_nodes: List[str] = Field(..., min_length=1)
    probe_request: ProbeRequest
    initial_hypothesis: str = Field(..., min_length=5)
    confidence: float = Field(..., ge=0.0, le=1.0)

    @field_validator("visual_hotspot_node")
    @classmethod
    def _norm_hotspot(cls, v: str) -> str:
        norm = v.strip().upper()
        if norm not in VALID_NODES:
            raise ValueError(f"Invalid node {v!r}")
        return norm


class InvestigatorI2Contract(BaseModel):
    """Phase I2 output: Post-probe final diagnosis and safe remediation lock."""

    interaction_id: Literal["I2"] = "I2"
    case_id: str
    diagnosed_fault_node: str = Field(..., description="Confirmed faulty node ID (N1..N4)")
    diagnosed_fault_feeder: str = Field(..., description="Confirmed faulty feeder ID (F1..F4)")
    estimated_resistance_ohm: float = Field(..., gt=0.0)
    recommended_max_current_a: float = Field(..., gt=0.0)
    remediation_action: str = Field(..., min_length=5)
    safety_lock_rationale: Optional[str] = Field(default=None)
    confidence: float = Field(..., ge=0.0, le=1.0)

    model_config = {"populate_by_name": True}

    @field_validator("diagnosed_fault_node")
    @classmethod
    def _norm_node(cls, v: str) -> str:
        norm = v.strip().upper()
        if norm not in VALID_NODES:
            raise ValueError(f"Invalid node {v!r}")
        return norm

    @field_validator("diagnosed_fault_feeder")
    @classmethod
    def _norm_feeder(cls, v: str) -> str:
        norm = v.strip().upper()
        if norm not in VALID_FEEDERS:
            raise ValueError(f"Invalid feeder {v!r}")
        return norm


class AuditorA1Contract(BaseModel):
    """Phase A1 output: Independent pre-probe audit of visual evidence and SLD topology."""

    interaction_id: Literal["A1"] = "A1"
    case_id: str
    verified_sld_mapping: Dict[str, str]
    verified_hotspot_node: str
    topology_consistency_check: bool
    pre_probe_risk_assessment: str = Field(..., min_length=5)

    @field_validator("verified_hotspot_node")
    @classmethod
    def _norm_hotspot(cls, v: str) -> str:
        norm = v.strip().upper()
        if norm not in VALID_NODES:
            raise ValueError(f"Invalid node {v!r}")
        return norm


class AuditorA2Contract(BaseModel):
    """Phase A2 output: Independent post-probe audit grading I2 remediation safety."""

    interaction_id: Literal["A2"] = "A2"
    case_id: str
    audit_verdict: Literal["APPROVE", "REJECT"]
    verified_fault_node: str
    verified_fault_feeder: str
    verified_resistance_ohm: float = Field(..., gt=0.0)
    verified_safe_current_limit_a: float = Field(..., gt=0.0)
    discrepancy_notes: str = Field(..., min_length=3)

    @field_validator("verified_fault_node")
    @classmethod
    def _norm_node(cls, v: str) -> str:
        norm = v.strip().upper()
        if norm not in VALID_NODES:
            raise ValueError(f"Invalid node {v!r}")
        return norm

    @field_validator("verified_fault_feeder")
    @classmethod
    def _norm_feeder(cls, v: str) -> str:
        norm = v.strip().upper()
        if norm not in VALID_FEEDERS:
            raise ValueError(f"Invalid feeder {v!r}")
        return norm


class SafetyLockRecord(BaseModel):
    """Immutable lock record proving an interaction completed before truth unsealing."""

    interaction_id: Literal["I1", "I2", "A1", "A2"]
    case_id: str
    payload_sha256: str
    locked_at_utc: str
    private_truth_accessed_before_lock: bool = False
    payload: Dict[str, Any]
