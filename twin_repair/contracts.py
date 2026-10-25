"""Strict data contracts, JSON Schema validators, and patch application for BlackboxDC Twin Repair.

Implements §3.B1, §4.C1, and §4.C2 of `11_TWIN_REPAIR_ADDON_IMPLEMENTATION_PLAN_EN.md`:
- `SceneSpec`, `RepairPatch`, `ImageManifest`, `GraphScore`, `ActionOutcome`
- `canonical_json_bytes`, `sha256_bytes`
- `validate_scene_spec`, `validate_repair_patch`, `apply_patch`, `extract_feeder_graph`
- `build_canonical_scene_spec`, `build_golden_repair_patch`
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Union


SCENE_SPEC_SCHEMA_VERSION: str = "scene_spec.v1"
REPAIR_PATCH_SCHEMA_VERSION: str = "repair_patch.v1"
CANONICAL_COORDINATE_SYSTEM: str = "meters; floor=y0; rear=positive_z"

CANONICAL_RACK_IDS: Tuple[str, ...] = ("N1", "N2", "N3", "N4")
CANONICAL_RACK_CENTERS_X_M: Dict[str, float] = {
    "N1": 0.0,
    "N2": 0.82,
    "N3": 1.64,
    "N4": 2.46,
}
CANONICAL_RACK_BODY_TEMPS_C: Dict[str, float] = {
    "N1": 29.5,
    "N2": 26.5,
    "N3": 26.5,
    "N4": 38.0,
}
CANONICAL_RACK_CONNECTOR_TEMPS_C: Dict[str, float] = {
    "N1": 36.0,
    "N2": 25.0,
    "N3": 25.0,
    "N4": 88.95030198217694,
}

CANONICAL_FEEDER_IDS: Tuple[str, ...] = ("F1", "F2")
CANONICAL_FEEDER_LIMITS_A: Dict[str, float] = {
    "F1": 44.0,
    "F2": 50.0,
}
CANONICAL_FEEDER_STYLES: Dict[str, str] = {
    "F1": "blue",
    "F2": "orange",
}
CANONICAL_FEEDER_ANCHORS_M: Dict[str, Tuple[float, float]] = {
    "F1": (-0.45, 2.24),
    "F2": (-0.45, 2.02),
}

CANONICAL_EDGES_A: Dict[str, str] = {
    "N1": "F1",
    "N2": "F1",
    "N3": "F2",
    "N4": "F2",
}
CANONICAL_EDGES_B: Dict[str, str] = {
    "N1": "F1",
    "N2": "F2",
    "N3": "F1",
    "N4": "F2",
}

VIEWPORT_WIDTH_PX: int = 1280
VIEWPORT_HEIGHT_PX: int = 800
ALLOWED_CAMERA_POSES: Tuple[str, ...] = ("rear_inspection", "wiring_oblique")
ALLOWED_FEEDER_STYLES: Tuple[str, ...] = ("blue", "orange", "amber", "cyan", "gold")
ALLOWED_PATCH_DECISIONS: Tuple[str, ...] = ("repair", "no_change", "abstain")

CANONICAL_HOTSPOT_TEMP_C: float = 88.95030198217694
CANONICAL_HOTSPOT_SIGMA_M: float = 0.062

# Default 2D bounding boxes (xyxy pixels in 1280x800) for SLD columns and 3D wiring corridors
DEFAULT_SLD_BRANCH_BBOXES_PX: Dict[str, Tuple[int, int, int, int]] = {
    "N1": (112, 195, 288, 632),
    "N2": (372, 195, 548, 632),
    "N3": (632, 195, 808, 632),
    "N4": (892, 195, 1068, 632),
}

DEFAULT_RENDER_CABLE_CORRIDORS_PX: Dict[str, Tuple[int, int, int, int]] = {
    "N1": (120, 110, 420, 470),
    "N2": (360, 110, 670, 470),
    "N3": (610, 110, 930, 470),
    "N4": (860, 110, 1185, 470),
}

DEFAULT_ALLOWED_CRITIC_IMAGES: Tuple[str, ...] = (
    "render0_wiring.png",
    "render_wiring.png",
    "current_sld_B.png",
    "current_sld_A.png",
    "current_sld.png",
    "render0_thermal_rear.png",
    "render_thermal_rear.png",
    "render0_rgb_rear.png",
    "render_rgb_rear.png",
)

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _reject_nonfinite_constant(token: str) -> Any:
    raise ValueError(f"Non-finite numeric constant forbidden in JSON contract: {token!r}")


def _is_finite_number(val: Any) -> bool:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return False
    return math.isfinite(float(val))


def _require_finite_float(val: Any, field_name: str, *, min_val: Optional[float] = None, max_val: Optional[float] = None) -> float:
    if not _is_finite_number(val):
        raise ValueError(f"{field_name} must be a finite number, got {val!r}")
    fval = float(val)
    if min_val is not None and fval < min_val - 1e-9:
        raise ValueError(f"{field_name} ({fval}) is below minimum {min_val}")
    if max_val is not None and fval > max_val + 1e-9:
        raise ValueError(f"{field_name} ({fval}) exceeds maximum {max_val}")
    return fval


def _require_exact_int(val: Any, field_name: str, *, min_val: Optional[int] = None, max_val: Optional[int] = None) -> int:
    if isinstance(val, bool) or not isinstance(val, int):
        raise ValueError(f"{field_name} must be an integer, got {val!r}")
    if min_val is not None and val < min_val:
        raise ValueError(f"{field_name} ({val}) is below minimum {min_val}")
    if max_val is not None and val > max_val:
        raise ValueError(f"{field_name} ({val}) exceeds maximum {max_val}")
    return int(val)


def _require_nonempty_str(val: Any, field_name: str, *, max_len: int = 240) -> str:
    if not isinstance(val, str) or not val.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if len(val) > max_len:
        raise ValueError(f"{field_name} exceeds max length {max_len} ({len(val)} chars)")
    return val


def _require_safe_filename(val: Any, field_name: str) -> str:
    s = _require_nonempty_str(val, field_name, max_len=128)
    if "/" in s or "\\" in s or ".." in s or s.startswith("."):
        raise ValueError(f"{field_name} contains illegal path traversal or directory separator: {s!r}")
    return s


def _check_exact_keys(
    mapping: Mapping[str, Any],
    *,
    required: Sequence[str],
    optional: Sequence[str] = (),
    context: str,
) -> None:
    if not isinstance(mapping, Mapping):
        raise ValueError(f"{context} must be a JSON object/mapping")
    req_set = set(required)
    allowed_set = req_set | set(optional)
    actual_set = set(mapping.keys())
    missing = sorted(req_set - actual_set)
    if missing:
        raise ValueError(f"{context} missing required keys: {missing}")
    extra = sorted(actual_set - allowed_set)
    if extra:
        raise ValueError(f"{context} contains unknown/disallowed keys: {extra}")


def _validate_bbox_xyxy(
    raw_bbox: Any,
    field_name: str,
    *,
    width_px: int = VIEWPORT_WIDTH_PX,
    height_px: int = VIEWPORT_HEIGHT_PX,
) -> Tuple[int, int, int, int]:
    if not isinstance(raw_bbox, (list, tuple)) or len(raw_bbox) != 4:
        raise ValueError(f"{field_name} must be a 4-element [x0, y0, x1, y1] integer list")
    x0 = _require_exact_int(raw_bbox[0], f"{field_name}[0]", min_val=0, max_val=width_px)
    y0 = _require_exact_int(raw_bbox[1], f"{field_name}[1]", min_val=0, max_val=height_px)
    x1 = _require_exact_int(raw_bbox[2], f"{field_name}[2]", min_val=0, max_val=width_px)
    y1 = _require_exact_int(raw_bbox[3], f"{field_name}[3]", min_val=0, max_val=height_px)
    if x0 >= x1 or y0 >= y1:
        raise ValueError(f"{field_name} must have strictly increasing coordinates (x0 < x1, y0 < y1): {raw_bbox!r}")
    if (x1 - x0) < 6 or (y1 - y0) < 6 or ((x1 - x0) * (y1 - y0)) < 36:
        raise ValueError(f"{field_name} bounding box is degenerate or placeholder-sized: {raw_bbox!r}")
    return (x0, y0, x1, y1)


def bboxes_intersect(box_a: Sequence[int], box_b: Sequence[int]) -> bool:
    """Return True if two `[x0, y0, x1, y1]` rectangles have positive overlap area."""
    ix0 = max(int(box_a[0]), int(box_b[0]))
    iy0 = max(int(box_a[1]), int(box_b[1]))
    ix1 = min(int(box_a[2]), int(box_b[2]))
    iy1 = min(int(box_a[3]), int(box_b[3]))
    return (ix1 > ix0) and (iy1 > iy0)


def canonical_json_bytes(payload: Any) -> bytes:
    """Serialize a contract or JSON-compatible structure into canonical UTF-8 bytes."""
    if hasattr(payload, "to_dict") and callable(payload.to_dict):
        payload = payload.to_dict()
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(data: Union[bytes, str, Any]) -> str:
    """Return lowercase 64-character hex SHA-256 of bytes or canonicalized payload."""
    if isinstance(data, bytes):
        raw = data
    elif isinstance(data, str):
        raw = data.encode("utf-8")
    else:
        raw = canonical_json_bytes(data)
    return hashlib.sha256(raw).hexdigest()


class _DictAccessibleMixin:
    """Mixin allowing dataclasses to support `.get()`, `[]`, and `in` lookups."""

    def to_dict(self) -> Dict[str, Any]:
        raise NotImplementedError

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def __contains__(self, key: object) -> bool:
        return key in self.to_dict()

    def keys(self) -> Any:
        return self.to_dict().keys()

    def items(self) -> Any:
        return self.to_dict().items()

    def values(self) -> Any:
        return self.to_dict().values()

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())


@dataclass(frozen=True)
class ViewportSpec(_DictAccessibleMixin):
    width_px: int = VIEWPORT_WIDTH_PX
    height_px: int = VIEWPORT_HEIGHT_PX
    camera_pose: str = "rear_inspection"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "width_px": int(self.width_px),
            "height_px": int(self.height_px),
            "camera_pose": str(self.camera_pose),
        }


@dataclass(frozen=True)
class SceneRackSpec(_DictAccessibleMixin):
    rack_id: str
    center_x_m: float
    width_m: float = 0.6
    height_m: float = 2.0
    depth_m: float = 1.0
    connector_side: str = "rear"
    body_temp_c: float = 26.5
    connector_temp_c: float = 25.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rack_id": str(self.rack_id),
            "center_x_m": float(self.center_x_m),
            "width_m": float(self.width_m),
            "height_m": float(self.height_m),
            "depth_m": float(self.depth_m),
            "connector_side": str(self.connector_side),
            "body_temp_c": float(self.body_temp_c),
            "connector_temp_c": float(self.connector_temp_c),
        }


@dataclass(frozen=True)
class SceneFeederSpec(_DictAccessibleMixin):
    feeder_id: str
    anchor_x_m: float
    anchor_y_m: float
    limit_a: float
    style: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feeder_id": str(self.feeder_id),
            "anchor_x_m": float(self.anchor_x_m),
            "anchor_y_m": float(self.anchor_y_m),
            "limit_a": float(self.limit_a),
            "style": str(self.style),
        }


@dataclass(frozen=True)
class SceneEdgeSpec(_DictAccessibleMixin):
    rack_id: str
    feeder_id: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "rack_id": str(self.rack_id),
            "feeder_id": str(self.feeder_id),
        }


@dataclass(frozen=True)
class HotspotSpec(_DictAccessibleMixin):
    rack_id: str = "N4"
    surface: str = "rear_connector"
    temperature_c: float = CANONICAL_HOTSPOT_TEMP_C
    sigma_m: float = CANONICAL_HOTSPOT_SIGMA_M

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rack_id": str(self.rack_id),
            "surface": str(self.surface),
            "temperature_c": float(self.temperature_c),
            "sigma_m": float(self.sigma_m),
        }


@dataclass(frozen=True)
class SceneSpec(_DictAccessibleMixin):
    schema_version: str
    case_id: str
    source_revision: str
    coordinate_system: str
    viewport: ViewportSpec
    racks: Tuple[SceneRackSpec, ...]
    feeders: Tuple[SceneFeederSpec, ...]
    edges: Tuple[SceneEdgeSpec, ...]
    hotspot: HotspotSpec
    notes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": str(self.schema_version),
            "case_id": str(self.case_id),
            "source_revision": str(self.source_revision),
            "coordinate_system": str(self.coordinate_system),
            "viewport": self.viewport.to_dict(),
            "racks": [r.to_dict() for r in self.racks],
            "feeders": [f.to_dict() for f in self.feeders],
            "edges": [e.to_dict() for e in self.edges],
            "hotspot": self.hotspot.to_dict(),
            "notes": [str(n) for n in self.notes],
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    def sha256(self) -> str:
        return sha256_bytes(self.canonical_bytes())


@dataclass(frozen=True)
class ObservedEdgeEvidence(_DictAccessibleMixin):
    rack_id: str
    feeder_id: str
    image_ref: str
    bbox_xyxy_px: Tuple[int, int, int, int]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rack_id": str(self.rack_id),
            "feeder_id": str(self.feeder_id),
            "image_ref": str(self.image_ref),
            "bbox_xyxy_px": [int(x) for x in self.bbox_xyxy_px],
        }


@dataclass(frozen=True)
class EdgeDiscrepancy(_DictAccessibleMixin):
    rack_id: str
    observed_feeder: str
    required_feeder: str
    render_evidence_bbox_xyxy_px: Tuple[int, int, int, int]
    sld_evidence_bbox_xyxy_px: Tuple[int, int, int, int]
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rack_id": str(self.rack_id),
            "observed_feeder": str(self.observed_feeder),
            "required_feeder": str(self.required_feeder),
            "render_evidence_bbox_xyxy_px": [int(x) for x in self.render_evidence_bbox_xyxy_px],
            "sld_evidence_bbox_xyxy_px": [int(x) for x in self.sld_evidence_bbox_xyxy_px],
            "reason": str(self.reason),
        }


@dataclass(frozen=True)
class PatchOperation(_DictAccessibleMixin):
    op: str
    rack_id: str
    from_feeder: str
    to_feeder: str

    @property
    def from_(self) -> str:
        return self.from_feeder

    @property
    def to(self) -> str:
        return self.to_feeder

    def to_dict(self) -> Dict[str, str]:
        return {
            "op": str(self.op),
            "rack_id": str(self.rack_id),
            "from": str(self.from_feeder),
            "to": str(self.to_feeder),
        }


@dataclass(frozen=True)
class RepairPatch(_DictAccessibleMixin):
    schema_version: str
    case_id: str
    basis_scene_sha256: str
    target_revision_seen: str
    observed_render_edges: Tuple[ObservedEdgeEvidence, ...]
    target_sld_edges: Tuple[ObservedEdgeEvidence, ...]
    discrepancies: Tuple[EdgeDiscrepancy, ...]
    operations: Tuple[PatchOperation, ...]
    confidence: float
    decision: str
    reason: Optional[str] = None
    abstain_reason: Optional[str] = None
    notes: Optional[Tuple[str, ...]] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "schema_version": str(self.schema_version),
            "case_id": str(self.case_id),
            "basis_scene_sha256": str(self.basis_scene_sha256),
            "target_revision_seen": str(self.target_revision_seen),
            "observed_render_edges": [e.to_dict() for e in self.observed_render_edges],
            "target_sld_edges": [e.to_dict() for e in self.target_sld_edges],
            "discrepancies": [d.to_dict() for d in self.discrepancies],
            "operations": [o.to_dict() for o in self.operations],
            "confidence": float(self.confidence),
            "decision": str(self.decision),
        }
        if self.reason is not None:
            out["reason"] = str(self.reason)
        if self.abstain_reason is not None:
            out["abstain_reason"] = str(self.abstain_reason)
        if self.notes is not None:
            out["notes"] = [str(n) for n in self.notes]
        return out

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    def sha256(self) -> str:
        return sha256_bytes(self.canonical_bytes())


@dataclass(frozen=True)
class ImageManifest(_DictAccessibleMixin):
    """Whitelisted images and optional corridor/branch ROI metadata for Critic patch validation."""

    allowed_images: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    render_corridors_px: Optional[Dict[str, Tuple[int, int, int, int]]] = None
    sld_branch_bboxes_px: Optional[Dict[str, Tuple[int, int, int, int]]] = None
    expected_target_revision: Optional[str] = None

    @classmethod
    def default(
        cls,
        *,
        include_corridors: bool = True,
        expected_target_revision: Optional[str] = None,
    ) -> "ImageManifest":
        allowed = {
            name: {"width_px": VIEWPORT_WIDTH_PX, "height_px": VIEWPORT_HEIGHT_PX}
            for name in DEFAULT_ALLOWED_CRITIC_IMAGES
        }
        return cls(
            allowed_images=allowed,
            render_corridors_px=dict(DEFAULT_RENDER_CABLE_CORRIDORS_PX) if include_corridors else None,
            sld_branch_bboxes_px=dict(DEFAULT_SLD_BRANCH_BBOXES_PX) if include_corridors else None,
            expected_target_revision=expected_target_revision,
        )

    @classmethod
    def from_mapping(cls, raw: Optional[Union["ImageManifest", Mapping[str, Any], Sequence[str]]]) -> "ImageManifest":
        if raw is None:
            return cls.default(include_corridors=True)
        if isinstance(raw, ImageManifest):
            return raw
        if isinstance(raw, (list, tuple, set, frozenset)):
            allowed = {
                str(name): {"width_px": VIEWPORT_WIDTH_PX, "height_px": VIEWPORT_HEIGHT_PX}
                for name in raw
            }
            return cls(
                allowed_images=allowed,
                render_corridors_px=dict(DEFAULT_RENDER_CABLE_CORRIDORS_PX),
                sld_branch_bboxes_px=dict(DEFAULT_SLD_BRANCH_BBOXES_PX),
            )
        if not isinstance(raw, Mapping):
            raise ValueError(f"images manifest must be an ImageManifest, Mapping, or Sequence, got {type(raw)!r}")

        raw_allowed = raw.get("allowed_images", raw.get("images"))
        if raw_allowed is None:
            # Check if raw itself is a mapping of filename -> metadata
            known_meta_keys = {
                "allowed_images",
                "images",
                "render_corridors_px",
                "projected_cable_corridors",
                "sld_branch_bboxes_px",
                "expected_target_revision",
            }
            if any(str(k).endswith(".png") for k in raw.keys()):
                allowed = {}
                for k, v in raw.items():
                    if k in known_meta_keys:
                        continue
                    if isinstance(v, Mapping):
                        allowed[str(k)] = dict(v)
                    else:
                        allowed[str(k)] = {"width_px": VIEWPORT_WIDTH_PX, "height_px": VIEWPORT_HEIGHT_PX, "sha256": str(v)}
            else:
                allowed = {
                    name: {"width_px": VIEWPORT_WIDTH_PX, "height_px": VIEWPORT_HEIGHT_PX}
                    for name in DEFAULT_ALLOWED_CRITIC_IMAGES
                }
        elif isinstance(raw_allowed, Mapping):
            allowed = {
                str(k): (dict(v) if isinstance(v, Mapping) else {"width_px": VIEWPORT_WIDTH_PX, "height_px": VIEWPORT_HEIGHT_PX})
                for k, v in raw_allowed.items()
            }
        elif isinstance(raw_allowed, (list, tuple, set, frozenset)):
            allowed = {
                str(k): {"width_px": VIEWPORT_WIDTH_PX, "height_px": VIEWPORT_HEIGHT_PX}
                for k in raw_allowed
            }
        else:
            raise ValueError("Invalid allowed_images in ImageManifest")

        raw_corridors = raw.get("render_corridors_px", raw.get("projected_cable_corridors"))
        corridors: Optional[Dict[str, Tuple[int, int, int, int]]] = None
        if isinstance(raw_corridors, Mapping):
            corridors = {}
            for rk, box in raw_corridors.items():
                if isinstance(box, Mapping) and "bbox_xyxy_px" in box:
                    b = box["bbox_xyxy_px"]
                else:
                    b = box
                if isinstance(b, (list, tuple)) and len(b) == 4:
                    corridors[str(rk)] = (int(b[0]), int(b[1]), int(b[2]), int(b[3]))
        elif raw.get("verify_corridors", True):
            corridors = dict(DEFAULT_RENDER_CABLE_CORRIDORS_PX)

        raw_sld_boxes = raw.get("sld_branch_bboxes_px")
        sld_boxes: Optional[Dict[str, Tuple[int, int, int, int]]] = None
        if isinstance(raw_sld_boxes, Mapping):
            sld_boxes = {
                str(rk): (int(b[0]), int(b[1]), int(b[2]), int(b[3]))
                for rk, b in raw_sld_boxes.items()
                if isinstance(b, (list, tuple)) and len(b) == 4
            }
        elif raw.get("verify_corridors", True):
            sld_boxes = dict(DEFAULT_SLD_BRANCH_BBOXES_PX)

        expected_rev = raw.get("expected_target_revision")
        return cls(
            allowed_images=allowed,
            render_corridors_px=corridors,
            sld_branch_bboxes_px=sld_boxes,
            expected_target_revision=str(expected_rev) if expected_rev is not None else None,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed_images": copy.deepcopy(self.allowed_images),
            "render_corridors_px": (
                {k: list(v) for k, v in self.render_corridors_px.items()}
                if self.render_corridors_px is not None
                else None
            ),
            "sld_branch_bboxes_px": (
                {k: list(v) for k, v in self.sld_branch_bboxes_px.items()}
                if self.sld_branch_bboxes_px is not None
                else None
            ),
            "expected_target_revision": self.expected_target_revision,
        }


@dataclass(frozen=True)
class GraphScore(_DictAccessibleMixin):
    correct_edges: int
    total_edges: int
    accuracy: float
    edge_matches: Dict[str, bool]
    candidate_edges: Dict[str, str]
    target_edges: Dict[str, str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "correct_edges": int(self.correct_edges),
            "total_edges": int(self.total_edges),
            "accuracy": float(self.accuracy),
            "score_str": f"{self.correct_edges}/{self.total_edges}",
            "edge_matches": dict(self.edge_matches),
            "candidate_edges": dict(self.candidate_edges),
            "target_edges": dict(self.target_edges),
        }


@dataclass(frozen=True)
class ActionOutcome(_DictAccessibleMixin):
    action_id: str
    target_rack: Optional[str]
    amps: float
    f1_current_a: float
    f2_current_a: float
    f1_limit_a: float
    f2_limit_a: float
    n4_temp_180s_c: float
    max_temp_limit_c: float
    feeder_safe: bool
    thermal_safe: bool
    safe: bool
    reason: str
    rack_currents_a: Optional[Dict[str, float]] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "action_id": str(self.action_id),
            "target_rack": self.target_rack,
            "amps": float(self.amps),
            "f1_current_a": float(self.f1_current_a),
            "f2_current_a": float(self.f2_current_a),
            "f1_limit_a": float(self.f1_limit_a),
            "f2_limit_a": float(self.f2_limit_a),
            "n4_temp_180s_c": float(self.n4_temp_180s_c),
            "max_temp_limit_c": float(self.max_temp_limit_c),
            "feeder_safe": bool(self.feeder_safe),
            "thermal_safe": bool(self.thermal_safe),
            "safe": bool(self.safe),
            "reason": str(self.reason),
        }
        if self.rack_currents_a is not None:
            out["rack_currents_a"] = {k: float(v) for k, v in self.rack_currents_a.items()}
        return out


def _parse_raw_json_if_needed(raw: Union[Mapping[str, Any], str, bytes, Any], context: str) -> Mapping[str, Any]:
    if isinstance(raw, (str, bytes)):
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        try:
            parsed = json.loads(text, parse_constant=_reject_nonfinite_constant)
        except Exception as exc:
            raise ValueError(f"Malformed JSON in {context}: {exc}") from exc
        if not isinstance(parsed, Mapping):
            raise ValueError(f"{context} top-level JSON must be an object")
        return parsed
    if hasattr(raw, "to_dict") and callable(raw.to_dict):
        return raw.to_dict()
    if isinstance(raw, Mapping):
        return raw
    raise ValueError(f"{context} must be a dict, JSON string, or contract instance")


def validate_scene_spec(
    raw: Union[Mapping[str, Any], SceneSpec, str, bytes],
    public_state: Optional[Mapping[str, Any]] = None,
) -> SceneSpec:
    """Strictly validate a `scene_spec.v1` payload against schema and immutable `public_state`."""
    data = _parse_raw_json_if_needed(raw, "SceneSpec")

    _check_exact_keys(
        data,
        required=(
            "schema_version",
            "case_id",
            "source_revision",
            "coordinate_system",
            "viewport",
            "racks",
            "feeders",
            "edges",
            "hotspot",
            "notes",
        ),
        context="SceneSpec",
    )

    if data["schema_version"] != SCENE_SPEC_SCHEMA_VERSION:
        raise ValueError(f"Invalid SceneSpec schema_version: {data['schema_version']!r}")

    case_id = _require_nonempty_str(data["case_id"], "SceneSpec.case_id", max_len=64)
    if not _SAFE_ID_RE.match(case_id) or ".." in case_id:
        raise ValueError(f"Invalid SceneSpec.case_id: {case_id!r}")

    if public_state is not None and "case_id" in public_state:
        expected_case_id = str(public_state["case_id"])
        if case_id != expected_case_id:
            raise ValueError(f"SceneSpec.case_id {case_id!r} does not match public_state.case_id {expected_case_id!r}")

    source_revision = _require_nonempty_str(data["source_revision"], "SceneSpec.source_revision", max_len=8)
    if source_revision not in ("A", "B"):
        raise ValueError(f"SceneSpec.source_revision must be 'A' or 'B', got {source_revision!r}")
    if public_state is not None and "revision_old" in public_state:
        expected_rev = str(public_state["revision_old"])
        if source_revision != expected_rev:
            raise ValueError(
                f"SceneSpec.source_revision {source_revision!r} does not match public_state.revision_old {expected_rev!r}"
            )

    coord_sys = _require_nonempty_str(data["coordinate_system"], "SceneSpec.coordinate_system", max_len=64)
    if coord_sys != CANONICAL_COORDINATE_SYSTEM:
        raise ValueError(f"SceneSpec.coordinate_system must be {CANONICAL_COORDINATE_SYSTEM!r}, got {coord_sys!r}")

    # Viewport validation
    vp_raw = data["viewport"]
    _check_exact_keys(
        vp_raw,
        required=("width_px", "height_px", "camera_pose"),
        context="SceneSpec.viewport",
    )
    width_px = _require_exact_int(vp_raw["width_px"], "viewport.width_px", min_val=VIEWPORT_WIDTH_PX, max_val=VIEWPORT_WIDTH_PX)
    height_px = _require_exact_int(vp_raw["height_px"], "viewport.height_px", min_val=VIEWPORT_HEIGHT_PX, max_val=VIEWPORT_HEIGHT_PX)
    camera_pose = _require_nonempty_str(vp_raw["camera_pose"], "viewport.camera_pose", max_len=32)
    if camera_pose not in ALLOWED_CAMERA_POSES:
        raise ValueError(f"viewport.camera_pose must be in {ALLOWED_CAMERA_POSES}, got {camera_pose!r}")
    viewport = ViewportSpec(width_px=width_px, height_px=height_px, camera_pose=camera_pose)

    # Racks validation
    racks_raw = data["racks"]
    if not isinstance(racks_raw, (list, tuple)) or len(racks_raw) != 4:
        raise ValueError("SceneSpec.racks must be a list of exactly 4 rack objects")

    seen_racks: set[str] = set()
    parsed_racks: List[SceneRackSpec] = []
    for idx, r_raw in enumerate(racks_raw):
        _check_exact_keys(
            r_raw,
            required=(
                "rack_id",
                "center_x_m",
                "width_m",
                "height_m",
                "depth_m",
                "connector_side",
                "body_temp_c",
                "connector_temp_c",
            ),
            context=f"SceneSpec.racks[{idx}]",
        )
        rack_id = _require_nonempty_str(r_raw["rack_id"], f"racks[{idx}].rack_id", max_len=8)
        if rack_id not in CANONICAL_RACK_IDS:
            raise ValueError(f"Unknown rack_id {rack_id!r} in SceneSpec.racks[{idx}]")
        if rack_id in seen_racks:
            raise ValueError(f"Duplicate rack_id {rack_id!r} in SceneSpec.racks")
        seen_racks.add(rack_id)

        expected_x = CANONICAL_RACK_CENTERS_X_M[rack_id]
        center_x_m = _require_finite_float(r_raw["center_x_m"], f"racks[{idx}].center_x_m", min_val=-0.5, max_val=3.5)
        if abs(center_x_m - expected_x) > 0.15:
            raise ValueError(
                f"Rack {rack_id} center_x_m={center_x_m} deviates from canonical position {expected_x} by > 0.15 m"
            )
        width_m = _require_finite_float(r_raw["width_m"], f"racks[{idx}].width_m", min_val=0.4, max_val=1.0)
        height_m = _require_finite_float(r_raw["height_m"], f"racks[{idx}].height_m", min_val=1.5, max_val=2.5)
        depth_m = _require_finite_float(r_raw["depth_m"], f"racks[{idx}].depth_m", min_val=0.6, max_val=1.5)
        connector_side = _require_nonempty_str(r_raw["connector_side"], f"racks[{idx}].connector_side", max_len=16)
        if connector_side != "rear":
            raise ValueError(f"Rack {rack_id} connector_side must be 'rear', got {connector_side!r}")
        body_temp_c = _require_finite_float(r_raw["body_temp_c"], f"racks[{idx}].body_temp_c", min_val=15.0, max_val=60.0)
        connector_temp_c = _require_finite_float(
            r_raw["connector_temp_c"], f"racks[{idx}].connector_temp_c", min_val=15.0, max_val=120.0
        )
        parsed_racks.append(
            SceneRackSpec(
                rack_id=rack_id,
                center_x_m=center_x_m,
                width_m=width_m,
                height_m=height_m,
                depth_m=depth_m,
                connector_side=connector_side,
                body_temp_c=body_temp_c,
                connector_temp_c=connector_temp_c,
            )
        )

    if tuple(r.rack_id for r in parsed_racks) != CANONICAL_RACK_IDS:
        raise ValueError(
            f"SceneSpec.racks must be ordered {CANONICAL_RACK_IDS}, got {tuple(r.rack_id for r in parsed_racks)}"
        )

    # Feeders validation
    feeders_raw = data["feeders"]
    if not isinstance(feeders_raw, (list, tuple)) or len(feeders_raw) != 2:
        raise ValueError("SceneSpec.feeders must be a list of exactly 2 feeder objects")

    expected_limits = dict(CANONICAL_FEEDER_LIMITS_A)
    if public_state is not None and isinstance(public_state.get("feeders"), Mapping):
        for fid, fmeta in public_state["feeders"].items():
            if isinstance(fmeta, Mapping) and "limit_a" in fmeta:
                expected_limits[str(fid)] = float(fmeta["limit_a"])
            elif _is_finite_number(fmeta):
                expected_limits[str(fid)] = float(fmeta)

    seen_feeders: set[str] = set()
    parsed_feeders: List[SceneFeederSpec] = []
    for idx, f_raw in enumerate(feeders_raw):
        _check_exact_keys(
            f_raw,
            required=("feeder_id", "anchor_x_m", "anchor_y_m", "limit_a", "style"),
            context=f"SceneSpec.feeders[{idx}]",
        )
        feeder_id = _require_nonempty_str(f_raw["feeder_id"], f"feeders[{idx}].feeder_id", max_len=8)
        if feeder_id not in CANONICAL_FEEDER_IDS:
            raise ValueError(f"Unknown feeder_id {feeder_id!r} in SceneSpec.feeders[{idx}]")
        if feeder_id in seen_feeders:
            raise ValueError(f"Duplicate feeder_id {feeder_id!r} in SceneSpec.feeders")
        seen_feeders.add(feeder_id)

        anchor_x_m = _require_finite_float(f_raw["anchor_x_m"], f"feeders[{idx}].anchor_x_m", min_val=-2.0, max_val=4.0)
        anchor_y_m = _require_finite_float(f_raw["anchor_y_m"], f"feeders[{idx}].anchor_y_m", min_val=1.8, max_val=3.0)
        limit_a = _require_finite_float(f_raw["limit_a"], f"feeders[{idx}].limit_a", min_val=1.0, max_val=200.0)
        if abs(limit_a - expected_limits[feeder_id]) > 1e-6:
            raise ValueError(
                f"Feeder {feeder_id} limit_a={limit_a} does not match trusted limit {expected_limits[feeder_id]} A"
            )
        style = _require_nonempty_str(f_raw["style"], f"feeders[{idx}].style", max_len=16)
        if style not in ALLOWED_FEEDER_STYLES:
            raise ValueError(f"Feeder {feeder_id} style must be in {ALLOWED_FEEDER_STYLES}, got {style!r}")
        parsed_feeders.append(
            SceneFeederSpec(
                feeder_id=feeder_id,
                anchor_x_m=anchor_x_m,
                anchor_y_m=anchor_y_m,
                limit_a=limit_a,
                style=style,
            )
        )

    if tuple(f.feeder_id for f in parsed_feeders) != CANONICAL_FEEDER_IDS:
        raise ValueError(
            f"SceneSpec.feeders must be ordered {CANONICAL_FEEDER_IDS}, got {tuple(f.feeder_id for f in parsed_feeders)}"
        )
    if parsed_feeders[0].style == parsed_feeders[1].style:
        raise ValueError("F1 and F2 feeder styles must be visually distinct")

    # Edges validation
    edges_raw = data["edges"]
    if not isinstance(edges_raw, (list, tuple)) or len(edges_raw) != 4:
        raise ValueError("SceneSpec.edges must be a list of exactly 4 rack->feeder edges")

    edge_by_rack: Dict[str, SceneEdgeSpec] = {}
    for idx, e_raw in enumerate(edges_raw):
        _check_exact_keys(
            e_raw,
            required=("rack_id", "feeder_id"),
            context=f"SceneSpec.edges[{idx}]",
        )
        rk = _require_nonempty_str(e_raw["rack_id"], f"edges[{idx}].rack_id", max_len=8)
        fd = _require_nonempty_str(e_raw["feeder_id"], f"edges[{idx}].feeder_id", max_len=8)
        if rk not in CANONICAL_RACK_IDS:
            raise ValueError(f"Unknown rack_id {rk!r} in SceneSpec.edges[{idx}]")
        if fd not in CANONICAL_FEEDER_IDS:
            raise ValueError(f"Unknown feeder_id {fd!r} in SceneSpec.edges[{idx}]")
        if rk in edge_by_rack:
            raise ValueError(f"Duplicate rack_id {rk!r} in SceneSpec.edges")
        edge_by_rack[rk] = SceneEdgeSpec(rack_id=rk, feeder_id=fd)

    ordered_edges = tuple(edge_by_rack[rk] for rk in CANONICAL_RACK_IDS)

    # Hotspot validation
    hs_raw = data["hotspot"]
    _check_exact_keys(
        hs_raw,
        required=("rack_id", "surface", "temperature_c", "sigma_m"),
        context="SceneSpec.hotspot",
    )
    hs_rack = _require_nonempty_str(hs_raw["rack_id"], "hotspot.rack_id", max_len=8)
    if hs_rack != "N4":
        raise ValueError(f"hotspot.rack_id must be 'N4', got {hs_rack!r}")
    hs_surface = _require_nonempty_str(hs_raw["surface"], "hotspot.surface", max_len=32)
    if hs_surface != "rear_connector":
        raise ValueError(f"hotspot.surface must be 'rear_connector', got {hs_surface!r}")
    hs_temp = _require_finite_float(hs_raw["temperature_c"], "hotspot.temperature_c", min_val=50.0, max_val=120.0)
    hs_sigma = _require_finite_float(hs_raw["sigma_m"], "hotspot.sigma_m", min_val=0.02, max_val=0.25)
    hotspot = HotspotSpec(rack_id=hs_rack, surface=hs_surface, temperature_c=hs_temp, sigma_m=hs_sigma)

    # Notes validation
    notes_raw = data["notes"]
    if not isinstance(notes_raw, (list, tuple)) or len(notes_raw) > 8:
        raise ValueError("SceneSpec.notes must be a list of at most 8 strings")
    notes_list: List[str] = []
    for idx, n in enumerate(notes_raw):
        notes_list.append(_require_nonempty_str(n, f"notes[{idx}]", max_len=240))

    return SceneSpec(
        schema_version=SCENE_SPEC_SCHEMA_VERSION,
        case_id=case_id,
        source_revision=source_revision,
        coordinate_system=coord_sys,
        viewport=viewport,
        racks=tuple(parsed_racks),
        feeders=tuple(parsed_feeders),
        edges=ordered_edges,
        hotspot=hotspot,
        notes=tuple(notes_list),
    )


def extract_feeder_graph(scene: Union[SceneSpec, Mapping[str, Any]]) -> Dict[str, str]:
    """Extract the canonical `{rack_id: feeder_id}` mapping from a SceneSpec or dict."""
    spec = scene if isinstance(scene, SceneSpec) else validate_scene_spec(scene)
    return {edge.rack_id: edge.feeder_id for edge in spec.edges}


def validate_repair_patch(
    raw: Union[Mapping[str, Any], RepairPatch, str, bytes],
    current: Union[SceneSpec, Mapping[str, Any]],
    images: Optional[Union[ImageManifest, Mapping[str, Any], Sequence[str]]] = None,
) -> RepairPatch:
    """Strictly validate a `repair_patch.v1` payload against `current` SceneSpec and `images` manifest."""
    data = _parse_raw_json_if_needed(raw, "RepairPatch")
    scene = current if isinstance(current, SceneSpec) else validate_scene_spec(current)
    manifest = ImageManifest.from_mapping(images)

    _check_exact_keys(
        data,
        required=(
            "schema_version",
            "case_id",
            "basis_scene_sha256",
            "target_revision_seen",
            "observed_render_edges",
            "target_sld_edges",
            "discrepancies",
            "operations",
            "confidence",
            "decision",
        ),
        optional=("reason", "abstain_reason", "notes"),
        context="RepairPatch",
    )

    if data["schema_version"] != REPAIR_PATCH_SCHEMA_VERSION:
        raise ValueError(f"Invalid RepairPatch schema_version: {data['schema_version']!r}")

    case_id = _require_nonempty_str(data["case_id"], "RepairPatch.case_id", max_len=64)
    if not _SAFE_ID_RE.match(case_id) or ".." in case_id:
        raise ValueError(f"Invalid RepairPatch.case_id: {case_id!r}")
    if case_id != scene.case_id:
        raise ValueError(f"RepairPatch.case_id {case_id!r} does not match SceneSpec.case_id {scene.case_id!r}")

    basis_sha = _require_nonempty_str(data["basis_scene_sha256"], "RepairPatch.basis_scene_sha256", max_len=64)
    if not _SHA256_RE.match(basis_sha):
        raise ValueError(f"RepairPatch.basis_scene_sha256 is not a valid 64-char lowercase hex SHA-256: {basis_sha!r}")
    expected_sha = scene.sha256()
    if basis_sha != expected_sha:
        raise ValueError(
            f"RepairPatch.basis_scene_sha256 {basis_sha!r} does not match current SceneSpec SHA-256 {expected_sha!r}"
        )

    target_rev = _require_nonempty_str(data["target_revision_seen"], "RepairPatch.target_revision_seen", max_len=16)
    if target_rev not in ("A", "B", "UNKNOWN"):
        raise ValueError(f"RepairPatch.target_revision_seen must be 'A', 'B', or 'UNKNOWN', got {target_rev!r}")
    if manifest.expected_target_revision is not None and target_rev != manifest.expected_target_revision:
        raise ValueError(
            f"RepairPatch.target_revision_seen {target_rev!r} does not match expected revision {manifest.expected_target_revision!r}"
        )

    confidence = _require_finite_float(data["confidence"], "RepairPatch.confidence", min_val=0.0, max_val=1.0)
    decision = _require_nonempty_str(data["decision"], "RepairPatch.decision", max_len=16)
    if decision not in ALLOWED_PATCH_DECISIONS:
        raise ValueError(f"RepairPatch.decision must be in {ALLOWED_PATCH_DECISIONS}, got {decision!r}")

    current_edges = extract_feeder_graph(scene)

    # Parse observed_render_edges
    obs_raw = data["observed_render_edges"]
    if not isinstance(obs_raw, (list, tuple)) or len(obs_raw) > 4:
        raise ValueError("RepairPatch.observed_render_edges must be a list of at most 4 items")
    seen_obs_racks: set[str] = set()
    obs_edges: List[ObservedEdgeEvidence] = []
    for idx, item in enumerate(obs_raw):
        _check_exact_keys(
            item,
            required=("rack_id", "feeder_id", "image_ref", "bbox_xyxy_px"),
            context=f"observed_render_edges[{idx}]",
        )
        rk = _require_nonempty_str(item["rack_id"], f"observed_render_edges[{idx}].rack_id", max_len=8)
        fd = _require_nonempty_str(item["feeder_id"], f"observed_render_edges[{idx}].feeder_id", max_len=8)
        if rk not in CANONICAL_RACK_IDS or fd not in CANONICAL_FEEDER_IDS:
            raise ValueError(f"Invalid rack_id/feeder_id in observed_render_edges[{idx}]: {rk!r} -> {fd!r}")
        if rk in seen_obs_racks:
            raise ValueError(f"Duplicate rack_id {rk!r} in observed_render_edges")
        seen_obs_racks.add(rk)

        img_ref = _require_safe_filename(item["image_ref"], f"observed_render_edges[{idx}].image_ref")
        if img_ref not in manifest.allowed_images:
            raise ValueError(f"Unwhitelisted image_ref {img_ref!r} in observed_render_edges[{idx}]")
        if "wiring" not in img_ref and "render" not in img_ref:
            raise ValueError(f"observed_render_edges[{idx}].image_ref must reference a wiring render image, got {img_ref!r}")

        img_meta = manifest.allowed_images[img_ref]
        w_px = int(img_meta.get("width_px", VIEWPORT_WIDTH_PX))
        h_px = int(img_meta.get("height_px", VIEWPORT_HEIGHT_PX))
        bbox = _validate_bbox_xyxy(
            item["bbox_xyxy_px"],
            f"observed_render_edges[{idx}].bbox_xyxy_px",
            width_px=w_px,
            height_px=h_px,
        )
        if manifest.render_corridors_px and rk in manifest.render_corridors_px:
            if not bboxes_intersect(bbox, manifest.render_corridors_px[rk]):
                raise ValueError(
                    f"observed_render_edges[{idx}] bbox {bbox} does not intersect {rk} cable corridor {manifest.render_corridors_px[rk]}"
                )
        if fd != current_edges[rk]:
            raise ValueError(
                f"observed_render_edges[{idx}] claims {rk}->{fd}, but rendered scene has {rk}->{current_edges[rk]}"
            )
        obs_edges.append(ObservedEdgeEvidence(rack_id=rk, feeder_id=fd, image_ref=img_ref, bbox_xyxy_px=bbox))

    # Parse target_sld_edges
    sld_raw = data["target_sld_edges"]
    if not isinstance(sld_raw, (list, tuple)) or len(sld_raw) > 4:
        raise ValueError("RepairPatch.target_sld_edges must be a list of at most 4 items")
    seen_sld_racks: set[str] = set()
    sld_edges: List[ObservedEdgeEvidence] = []
    for idx, item in enumerate(sld_raw):
        _check_exact_keys(
            item,
            required=("rack_id", "feeder_id", "image_ref", "bbox_xyxy_px"),
            context=f"target_sld_edges[{idx}]",
        )
        rk = _require_nonempty_str(item["rack_id"], f"target_sld_edges[{idx}].rack_id", max_len=8)
        fd = _require_nonempty_str(item["feeder_id"], f"target_sld_edges[{idx}].feeder_id", max_len=8)
        if rk not in CANONICAL_RACK_IDS or fd not in CANONICAL_FEEDER_IDS:
            raise ValueError(f"Invalid rack_id/feeder_id in target_sld_edges[{idx}]: {rk!r} -> {fd!r}")
        if rk in seen_sld_racks:
            raise ValueError(f"Duplicate rack_id {rk!r} in target_sld_edges")
        seen_sld_racks.add(rk)

        img_ref = _require_safe_filename(item["image_ref"], f"target_sld_edges[{idx}].image_ref")
        if img_ref not in manifest.allowed_images:
            raise ValueError(f"Unwhitelisted image_ref {img_ref!r} in target_sld_edges[{idx}]")
        if "sld" not in img_ref or img_ref == "old_sld_A.png":
            raise ValueError(f"target_sld_edges[{idx}].image_ref must reference the current SLD image, got {img_ref!r}")

        img_meta = manifest.allowed_images[img_ref]
        w_px = int(img_meta.get("width_px", VIEWPORT_WIDTH_PX))
        h_px = int(img_meta.get("height_px", VIEWPORT_HEIGHT_PX))
        bbox = _validate_bbox_xyxy(
            item["bbox_xyxy_px"],
            f"target_sld_edges[{idx}].bbox_xyxy_px",
            width_px=w_px,
            height_px=h_px,
        )
        if manifest.sld_branch_bboxes_px and rk in manifest.sld_branch_bboxes_px:
            if not bboxes_intersect(bbox, manifest.sld_branch_bboxes_px[rk]):
                raise ValueError(
                    f"target_sld_edges[{idx}] bbox {bbox} does not intersect {rk} SLD branch region {manifest.sld_branch_bboxes_px[rk]}"
                )
        sld_edges.append(ObservedEdgeEvidence(rack_id=rk, feeder_id=fd, image_ref=img_ref, bbox_xyxy_px=bbox))

    # Parse discrepancies
    disc_raw = data["discrepancies"]
    if not isinstance(disc_raw, (list, tuple)) or len(disc_raw) > 4:
        raise ValueError("RepairPatch.discrepancies must be a list of at most 4 items")
    seen_disc_racks: set[str] = set()
    discrepancies: List[EdgeDiscrepancy] = []
    for idx, item in enumerate(disc_raw):
        _check_exact_keys(
            item,
            required=(
                "rack_id",
                "observed_feeder",
                "required_feeder",
                "render_evidence_bbox_xyxy_px",
                "sld_evidence_bbox_xyxy_px",
                "reason",
            ),
            context=f"discrepancies[{idx}]",
        )
        rk = _require_nonempty_str(item["rack_id"], f"discrepancies[{idx}].rack_id", max_len=8)
        obs_f = _require_nonempty_str(item["observed_feeder"], f"discrepancies[{idx}].observed_feeder", max_len=8)
        req_f = _require_nonempty_str(item["required_feeder"], f"discrepancies[{idx}].required_feeder", max_len=8)
        if rk not in CANONICAL_RACK_IDS:
            raise ValueError(f"Invalid rack_id {rk!r} in discrepancies[{idx}]")
        if obs_f not in CANONICAL_FEEDER_IDS or req_f not in CANONICAL_FEEDER_IDS:
            raise ValueError(f"Invalid feeder ID in discrepancies[{idx}]: {obs_f!r} -> {req_f!r}")
        if obs_f == req_f:
            raise ValueError(f"discrepancies[{idx}] observed_feeder and required_feeder must differ ({obs_f!r})")
        if obs_f != current_edges[rk]:
            raise ValueError(
                f"discrepancies[{idx}] observed_feeder {obs_f!r} does not match current scene edge {rk}->{current_edges[rk]}"
            )
        if rk in seen_disc_racks:
            raise ValueError(f"Duplicate rack_id {rk!r} in discrepancies")
        seen_disc_racks.add(rk)

        r_bbox = _validate_bbox_xyxy(item["render_evidence_bbox_xyxy_px"], f"discrepancies[{idx}].render_evidence_bbox_xyxy_px")
        s_bbox = _validate_bbox_xyxy(item["sld_evidence_bbox_xyxy_px"], f"discrepancies[{idx}].sld_evidence_bbox_xyxy_px")
        if manifest.render_corridors_px and rk in manifest.render_corridors_px:
            if not bboxes_intersect(r_bbox, manifest.render_corridors_px[rk]):
                raise ValueError(
                    f"discrepancies[{idx}] render_evidence_bbox_xyxy_px {r_bbox} does not intersect {rk} cable corridor"
                )
        if manifest.sld_branch_bboxes_px and rk in manifest.sld_branch_bboxes_px:
            if not bboxes_intersect(s_bbox, manifest.sld_branch_bboxes_px[rk]):
                raise ValueError(
                    f"discrepancies[{idx}] sld_evidence_bbox_xyxy_px {s_bbox} does not intersect {rk} SLD branch region"
                )
        reason_str = _require_nonempty_str(item["reason"], f"discrepancies[{idx}].reason", max_len=320)
        discrepancies.append(
            EdgeDiscrepancy(
                rack_id=rk,
                observed_feeder=obs_f,
                required_feeder=req_f,
                render_evidence_bbox_xyxy_px=r_bbox,
                sld_evidence_bbox_xyxy_px=s_bbox,
                reason=reason_str,
            )
        )

    # Parse operations
    ops_raw = data["operations"]
    if not isinstance(ops_raw, (list, tuple)) or len(ops_raw) > 4:
        raise ValueError("RepairPatch.operations must be a list of at most 4 items")
    seen_op_racks: set[str] = set()
    operations: List[PatchOperation] = []
    for idx, item in enumerate(ops_raw):
        _check_exact_keys(
            item,
            required=("op", "rack_id", "from", "to"),
            context=f"operations[{idx}]",
        )
        op_name = _require_nonempty_str(item["op"], f"operations[{idx}].op", max_len=32)
        if op_name != "replace_feeder":
            raise ValueError(f"Unsupported operation op={op_name!r}; only 'replace_feeder' is allowed")
        rk = _require_nonempty_str(item["rack_id"], f"operations[{idx}].rack_id", max_len=8)
        from_f = _require_nonempty_str(item["from"], f"operations[{idx}].from", max_len=8)
        to_f = _require_nonempty_str(item["to"], f"operations[{idx}].to", max_len=8)
        if rk not in CANONICAL_RACK_IDS:
            raise ValueError(f"Invalid rack_id {rk!r} in operations[{idx}]")
        if from_f not in CANONICAL_FEEDER_IDS or to_f not in CANONICAL_FEEDER_IDS:
            raise ValueError(f"Invalid feeder ID in operations[{idx}]: {from_f!r} -> {to_f!r}")
        if from_f == to_f:
            raise ValueError(f"No-op operation on {rk}: 'from' and 'to' are both {from_f!r}")
        if from_f != current_edges[rk]:
            raise ValueError(
                f"Stale 'from' feeder on {rk}: operation specifies from={from_f!r}, but current scene has {current_edges[rk]!r}"
            )
        if rk in seen_op_racks:
            raise ValueError(f"Duplicate operation for rack_id {rk!r}")
        seen_op_racks.add(rk)
        operations.append(PatchOperation(op=op_name, rack_id=rk, from_feeder=from_f, to_feeder=to_f))

    # Cross-check discrepancies and operations
    if len(discrepancies) != len(operations):
        raise ValueError(
            f"Mismatch between discrepancies count ({len(discrepancies)}) and operations count ({len(operations)})"
        )
    disc_map = {d.rack_id: d for d in discrepancies}
    obs_map = {e.rack_id: e for e in obs_edges}
    sld_map = {e.rack_id: e for e in sld_edges}

    for op_item in operations:
        rk = op_item.rack_id
        if rk not in disc_map:
            raise ValueError(f"Operation for rack {rk} has no matching discrepancy record")
        d_item = disc_map[rk]
        if d_item.observed_feeder != op_item.from_feeder or d_item.required_feeder != op_item.to_feeder:
            raise ValueError(
                f"Discrepancy for {rk} ({d_item.observed_feeder}->{d_item.required_feeder}) "
                f"disagrees with operation ({op_item.from_feeder}->{op_item.to_feeder})"
            )
        if rk not in obs_map or obs_map[rk].feeder_id != op_item.from_feeder:
            raise ValueError(f"Operation for {rk} lacks matching observed_render_edges entry")
        if rk not in sld_map or sld_map[rk].feeder_id != op_item.to_feeder:
            raise ValueError(f"Operation for {rk} lacks matching target_sld_edges entry")

    # Optional reason / abstain_reason / notes
    reason_val: Optional[str] = None
    if "reason" in data and data["reason"] is not None:
        reason_val = _require_nonempty_str(data["reason"], "RepairPatch.reason", max_len=320)
    abstain_reason_val: Optional[str] = None
    if "abstain_reason" in data and data["abstain_reason"] is not None:
        abstain_reason_val = _require_nonempty_str(data["abstain_reason"], "RepairPatch.abstain_reason", max_len=320)
    notes_val: Optional[Tuple[str, ...]] = None
    if "notes" in data and data["notes"] is not None:
        if not isinstance(data["notes"], (list, tuple)) or len(data["notes"]) > 8:
            raise ValueError("RepairPatch.notes must be a list of at most 8 strings")
        notes_val = tuple(_require_nonempty_str(n, f"RepairPatch.notes[{i}]", max_len=240) for i, n in enumerate(data["notes"]))

    # Decision-specific invariants
    if decision == "repair":
        if len(operations) == 0:
            raise ValueError("decision='repair' requires at least one validated operation")
    elif decision == "no_change":
        if len(operations) != 0 or len(discrepancies) != 0:
            raise ValueError("decision='no_change' requires empty operations and empty discrepancies")
        if len(obs_edges) == 0 or len(sld_edges) == 0:
            raise ValueError("decision='no_change' requires non-empty observed_render_edges and target_sld_edges")
        if {e.rack_id: e.feeder_id for e in obs_edges} != {e.rack_id: e.feeder_id for e in sld_edges}:
            raise ValueError("decision='no_change' requires observed_render_edges and target_sld_edges to agree")
    elif decision == "abstain":
        if len(operations) != 0 or len(discrepancies) != 0:
            raise ValueError("decision='abstain' requires empty operations and empty discrepancies")
        if reason_val is None and abstain_reason_val is None and not notes_val:
            raise ValueError("decision='abstain' requires a reason, abstain_reason, or notes")

    return RepairPatch(
        schema_version=REPAIR_PATCH_SCHEMA_VERSION,
        case_id=case_id,
        basis_scene_sha256=basis_sha,
        target_revision_seen=target_rev,
        observed_render_edges=tuple(obs_edges),
        target_sld_edges=tuple(sld_edges),
        discrepancies=tuple(discrepancies),
        operations=tuple(operations),
        confidence=confidence,
        decision=decision,
        reason=reason_val,
        abstain_reason=abstain_reason_val,
        notes=notes_val,
    )


def apply_patch(
    current: Union[SceneSpec, Mapping[str, Any]],
    patch: Union[RepairPatch, Mapping[str, Any]],
    *,
    images: Optional[Union[ImageManifest, Mapping[str, Any], Sequence[str]]] = None,
) -> SceneSpec:
    """Apply a validated `RepairPatch` to a deep copy of `current` SceneSpec, modifying only `edges`."""
    scene0 = current if isinstance(current, SceneSpec) else validate_scene_spec(current)
    validated_patch = (
        patch
        if isinstance(patch, RepairPatch) and patch.basis_scene_sha256 == scene0.sha256() and patch.case_id == scene0.case_id
        else validate_repair_patch(patch, scene0, images=images)
    )

    if validated_patch.decision == "abstain":
        raise ValueError("Cannot apply a patch with decision='abstain'")

    raw_copy = copy.deepcopy(scene0.to_dict())
    edge_map = {e["rack_id"]: e["feeder_id"] for e in raw_copy["edges"]}

    for op in validated_patch.operations:
        if edge_map.get(op.rack_id) != op.from_feeder:
            raise ValueError(
                f"Operation expected {op.rack_id} on {op.from_feeder}, found {edge_map.get(op.rack_id)}"
            )
        edge_map[op.rack_id] = op.to_feeder

    raw_copy["edges"] = [
        {"rack_id": rk, "feeder_id": edge_map[rk]}
        for rk in CANONICAL_RACK_IDS
    ]
    # Preserve source_revision provenance ("A")
    raw_copy["source_revision"] = scene0.source_revision
    return validate_scene_spec(raw_copy)


def build_canonical_scene_spec(
    *,
    case_id: str = "drift_A_to_B",
    source_revision: str = "A",
    edges: Optional[Mapping[str, str]] = None,
    camera_pose: str = "rear_inspection",
    notes: Optional[Sequence[str]] = None,
) -> SceneSpec:
    """Construct a validated canonical `SceneSpec` for revision A or B."""
    edge_map = dict(edges) if edges is not None else (
        dict(CANONICAL_EDGES_A) if source_revision == "A" else dict(CANONICAL_EDGES_B)
    )
    raw = {
        "schema_version": SCENE_SPEC_SCHEMA_VERSION,
        "case_id": case_id,
        "source_revision": source_revision,
        "coordinate_system": CANONICAL_COORDINATE_SYSTEM,
        "viewport": {
            "width_px": VIEWPORT_WIDTH_PX,
            "height_px": VIEWPORT_HEIGHT_PX,
            "camera_pose": camera_pose,
        },
        "racks": [
            {
                "rack_id": rk,
                "center_x_m": CANONICAL_RACK_CENTERS_X_M[rk],
                "width_m": 0.6,
                "height_m": 2.0,
                "depth_m": 1.0,
                "connector_side": "rear",
                "body_temp_c": CANONICAL_RACK_BODY_TEMPS_C[rk],
                "connector_temp_c": CANONICAL_RACK_CONNECTOR_TEMPS_C[rk],
            }
            for rk in CANONICAL_RACK_IDS
        ],
        "feeders": [
            {
                "feeder_id": fid,
                "anchor_x_m": CANONICAL_FEEDER_ANCHORS_M[fid][0],
                "anchor_y_m": CANONICAL_FEEDER_ANCHORS_M[fid][1],
                "limit_a": CANONICAL_FEEDER_LIMITS_A[fid],
                "style": CANONICAL_FEEDER_STYLES[fid],
            }
            for fid in CANONICAL_FEEDER_IDS
        ],
        "edges": [
            {"rack_id": rk, "feeder_id": edge_map[rk]}
            for rk in CANONICAL_RACK_IDS
        ],
        "hotspot": {
            "rack_id": "N4",
            "surface": "rear_connector",
            "temperature_c": CANONICAL_HOTSPOT_TEMP_C,
            "sigma_m": CANONICAL_HOTSPOT_SIGMA_M,
        },
        "notes": list(notes) if notes is not None else ["inferred from old revision A and thermal input"],
    }
    return validate_scene_spec(raw)


def build_golden_repair_patch(
    scene0: Union[SceneSpec, Mapping[str, Any]],
    *,
    decision: str = "repair",
    target_revision_seen: str = "B",
    render_image_ref: str = "render0_wiring.png",
    sld_image_ref: str = "current_sld_B.png",
) -> RepairPatch:
    """Build a test-only golden `RepairPatch` (`source=fixture`) for rehearsal and mutation tests."""
    spec = scene0 if isinstance(scene0, SceneSpec) else validate_scene_spec(scene0)
    current_edges = extract_feeder_graph(spec)

    if decision == "repair":
        raw = {
            "schema_version": REPAIR_PATCH_SCHEMA_VERSION,
            "case_id": spec.case_id,
            "basis_scene_sha256": spec.sha256(),
            "target_revision_seen": target_revision_seen,
            "observed_render_edges": [
                {
                    "rack_id": "N2",
                    "feeder_id": current_edges["N2"],
                    "image_ref": render_image_ref,
                    "bbox_xyxy_px": [410, 150, 620, 420],
                },
                {
                    "rack_id": "N3",
                    "feeder_id": current_edges["N3"],
                    "image_ref": render_image_ref,
                    "bbox_xyxy_px": [660, 150, 880, 420],
                },
            ],
            "target_sld_edges": [
                {
                    "rack_id": "N2",
                    "feeder_id": "F2",
                    "image_ref": sld_image_ref,
                    "bbox_xyxy_px": [420, 200, 500, 420],
                },
                {
                    "rack_id": "N3",
                    "feeder_id": "F1",
                    "image_ref": sld_image_ref,
                    "bbox_xyxy_px": [680, 200, 760, 420],
                },
            ],
            "discrepancies": [
                {
                    "rack_id": "N2",
                    "observed_feeder": current_edges["N2"],
                    "required_feeder": "F2",
                    "render_evidence_bbox_xyxy_px": [410, 150, 620, 420],
                    "sld_evidence_bbox_xyxy_px": [420, 200, 500, 420],
                    "reason": "The cable visibly reaches F1 but the revised diagram routes N2 to F2.",
                },
                {
                    "rack_id": "N3",
                    "observed_feeder": current_edges["N3"],
                    "required_feeder": "F1",
                    "render_evidence_bbox_xyxy_px": [660, 150, 880, 420],
                    "sld_evidence_bbox_xyxy_px": [680, 200, 760, 420],
                    "reason": "The cable visibly reaches F2 but the revised diagram routes N3 to F1.",
                },
            ],
            "operations": [
                {"op": "replace_feeder", "rack_id": "N2", "from": current_edges["N2"], "to": "F2"},
                {"op": "replace_feeder", "rack_id": "N3", "from": current_edges["N3"], "to": "F1"},
            ],
            "confidence": 0.94,
            "decision": "repair",
        }
    elif decision == "no_change":
        raw = {
            "schema_version": REPAIR_PATCH_SCHEMA_VERSION,
            "case_id": spec.case_id,
            "basis_scene_sha256": spec.sha256(),
            "target_revision_seen": "A",
            "observed_render_edges": [
                {
                    "rack_id": rk,
                    "feeder_id": current_edges[rk],
                    "image_ref": render_image_ref,
                    "bbox_xyxy_px": list(DEFAULT_RENDER_CABLE_CORRIDORS_PX[rk]),
                }
                for rk in CANONICAL_RACK_IDS
            ],
            "target_sld_edges": [
                {
                    "rack_id": rk,
                    "feeder_id": current_edges[rk],
                    "image_ref": sld_image_ref,
                    "bbox_xyxy_px": list(DEFAULT_SLD_BRANCH_BBOXES_PX[rk]),
                }
                for rk in CANONICAL_RACK_IDS
            ],
            "discrepancies": [],
            "operations": [],
            "confidence": 0.96,
            "decision": "no_change",
        }
    elif decision == "abstain":
        raw = {
            "schema_version": REPAIR_PATCH_SCHEMA_VERSION,
            "case_id": spec.case_id,
            "basis_scene_sha256": spec.sha256(),
            "target_revision_seen": "UNKNOWN",
            "observed_render_edges": [],
            "target_sld_edges": [],
            "discrepancies": [],
            "operations": [],
            "confidence": 0.15,
            "decision": "abstain",
            "abstain_reason": "Cable paths or SLD branch connections are occluded or ambiguous.",
        }
    else:
        raise ValueError(f"Unsupported golden patch decision: {decision!r}")

    return validate_repair_patch(raw, spec)
