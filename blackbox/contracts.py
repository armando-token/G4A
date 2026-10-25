"""Pydantic contracts for BlackboxDC Investigator (I1, I2), Probe, and Auditor (A1, A2).

Enforces strict schema validation, canonical identifier normalization, and
zero-leakage lock verification.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, field_validator


VALID_NODES = {"N1", "N2", "N3", "N4"}
VALID_FEEDERS = {"F1", "F2", "F3", "F4"}


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
