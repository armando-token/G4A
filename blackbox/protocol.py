"""Four-turn black-box interaction protocol state machine for BlackboxDC.

Provides:
1. Phase 4 (`04_AGENT_PROTOCOL_AND_CONTRACTS.md`) `VisualV2ProtocolOrchestrator`,
   `run_visual_v2_case_protocol`, `VisualV2CaseRunResult`, and
   `DeterministicVisualV2MockClient`:
   - Orchestrates the 4-turn `visual-v2` workflow (`I1 -> L1 -> Probe -> I2 -> L2 -> A1 -> L3 -> A2 -> L4 -> Oracle`)
     via `managed.run_turn`.
   - Enforces per-turn input manifests (3 PNG SHA-256 digests + `public_packet.json` SHA-256),
     hash-chained locks (`L1..L4`), single-probe budget after `L1`, `I2` abstention when
     `I1.unknown_critical_edges` is non-empty, `I1`/`I2` vs `A1`/`A2` sandbox environment
     isolation, `A1` zero-Investigator-leakage input barrier, `L3` lock before `A2` receives
     `I2` plan, and post-`L4` private truth unsealing via `GuardedOracleTruthLoader`.
   - Supports both `"guided_diagnostic"` mode and `"blind_diagnostic"` mode with strictly
     separated protocol versions, outcome ledgers, and token budget trackers.
2. Backward-compatible Phase 00 `CaseProtocolRunResult` and `BlackboxProtocolStateMachine`.
"""

from __future__ import annotations

import base64
import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional, Sequence, Tuple

from PIL import Image, ImageDraw

from blackbox.agent_harness import (
    TURN_EXPECTED_FILENAMES,
    build_a1_prompt,
    build_a2_prompt,
    build_i1_prompt,
    build_i2_prompt,
)
from blackbox.contracts import (
    AuditorA1Contract,
    AuditorA2Contract,
    AuditChallengeV2,
    ArtifactLockRecordV2,
    CANONICAL_RACKS,
    FORBIDDEN_CONTRACT_KEYS,
    INVESTIGATOR_ONLY_KEYS,
    IndependentGraphV2,
    InvestigatorI1Contract,
    InvestigatorI2Contract,
    LOCK_STAGE_ORDER,
    PlanV2,
    ProbeResponse,
    ProposalV2,
    SafetyLockRecord,
    build_reference_audit_challenge_v2,
    build_reference_independent_graph_v2,
    build_reference_plan_v2,
    build_reference_proposal_v2,
    canonical_json_bytes,
    canonical_sha256,
    create_lock_record_v2,
    normalize_action_id,
    normalize_rack_id,
    validate_contract_hygiene,
    verify_lock_chain_v2,
    verify_plan_against_inferred_network,
)
from blackbox.fixture import (
    PublicCaseEvidence,
    SealedWorldTruth,
    build_private_world,
    export_public_packet,
)
from blackbox.managed import (
    DEFAULT_FOUNDATION_MODEL,
    DEFAULT_MANAGED_AGENT_MODEL,
    ManagedAgentTransport,
    NATIVE_INTERACTION_NONCACHED_CAP,
    PairBudgetTracker,
    TurnEvidence,
    append_journal_event,
    reset_environment_role_registry,
    run_turn,
)
from blackbox.oracle import (
    CaseEvaluationReportV2,
    GuardedOracleTruthLoader,
    evaluate_ab_campaign_v2,
    evaluate_case_v2,
)
from blackbox.probe import (
    ProbeReceipt,
    SingleShotProbeSession,
)
from blackbox.render_sld import (
    parse_sld_png_topology,
    render_sld_png,
)
from blackbox.scene_bridge import (
    EXPECTED_CONNECTOR_PIXEL_BBOX,
    EXPECTED_HOTSPOT_PIXEL_CENTER,
    SCENE_HEIGHT,
    SCENE_WIDTH,
    project_connector_bbox,
    verify_thermal_hotspot_alignment,
    verify_visual_artifact,
)


DiagnosticMode = Literal["guided_diagnostic", "blind_diagnostic"]

PROTOCOL_VERSION_BY_MODE: Dict[str, str] = {
    "guided_diagnostic": "visual-v2/guided_diagnostic",
    "blind_diagnostic": "visual-v2/blind_diagnostic",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_json(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return _sha256_bytes(raw)


def _normalize_case_ids(case_id: str) -> Tuple[Literal["A", "B"], str]:
    raw = str(case_id).strip().upper()
    if raw in ("A", "CASE_A"):
        return "A", "case_A"
    if raw in ("B", "CASE_B"):
        return "B", "case_B"
    raise ValueError(f"Unsupported case_id={case_id!r}; expected 'A', 'B', 'case_A', or 'case_B'")


def ensure_case_visual_inputs(
    case_id: str,
    output_dir: Path,
) -> Dict[str, Path]:
    """Ensure `sld.png`, `rear_aisle_rgb.png`, and `rear_aisle_thermal.png` exist in `output_dir`.

    Uses `render_sld_png` for `sld.png` and generates deterministic 1280x800 24-bit RGB
    rear-aisle renders with the N4 connector hotspot centered at `(1110, 359)` (`[1079, 325, 1143, 393]`)
    when pre-captured 3D renders are not already present in `output_dir`.
    """
    short_id, _ = _normalize_case_ids(case_id)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    sld_path = out / "sld.png"
    if not sld_path.exists():
        render_sld_png(case_id=short_id, output_path=sld_path, svg_output_path=out / "sld.svg")

    rgb_path = out / "rear_aisle_rgb.png"
    if not rgb_path.exists():
        alt_rgb = out / "rgb.png"
        if alt_rgb.exists():
            rgb_path.write_bytes(alt_rgb.read_bytes())
        else:
            img_rgb = Image.new("RGB", (SCENE_WIDTH, SCENE_HEIGHT), color=(22, 28, 38))
            draw_rgb = ImageDraw.Draw(img_rgb)
            # Draw 4 rear-aisle rack bays N1..N4
            rack_x_centers = {"N1": 210, "N2": 500, "N3": 790, "N4": 1080}
            for rk, cx in rack_x_centers.items():
                draw_rgb.rectangle([cx - 110, 150, cx + 110, 650], fill=(38, 46, 58), outline=(90, 108, 130), width=2)
            # N4 rear visible connector housing at [1079, 325, 1143, 393]
            bx0, by0, bx1, by1 = EXPECTED_CONNECTOR_PIXEL_BBOX
            draw_rgb.rectangle([bx0, by0, bx1, by1], fill=(185, 95, 55), outline=(230, 140, 80), width=2)
            img_rgb.save(rgb_path, format="PNG", optimize=True)

    thermal_path = out / "rear_aisle_thermal.png"
    if not thermal_path.exists():
        alt_thermal = out / "thermal.png"
        if alt_thermal.exists():
            thermal_path.write_bytes(alt_thermal.read_bytes())
        else:
            img_th = Image.new("RGB", (SCENE_WIDTH, SCENE_HEIGHT), color=(14, 10, 42))
            draw_th = ImageDraw.Draw(img_th)
            rack_x_centers = {"N1": 210, "N2": 500, "N3": 790, "N4": 1080}
            for rk, cx in rack_x_centers.items():
                fill_c = (48, 22, 95) if rk == "N1" else (24, 16, 68)
                draw_th.rectangle([cx - 110, 150, cx + 110, 650], fill=fill_c, outline=(55, 35, 110), width=2)
            # Localized N4 connector hotspot inside [1079, 325, 1143, 393] peaking at (1110, 359)
            bx0, by0, bx1, by1 = EXPECTED_CONNECTOR_PIXEL_BBOX
            draw_th.rectangle([bx0, by0, bx1, by1], fill=(225, 155, 35))
            draw_th.ellipse([1098, 347, 1122, 371], fill=(250, 235, 180))
            img_th.putpixel((1110, 359), (255, 255, 255))
            img_th.save(thermal_path, format="PNG", optimize=True)

    return {
        "sld": sld_path,
        "rgb": rgb_path,
        "thermal": thermal_path,
    }


def build_turn_input_manifest(
    turn_id: Literal["I1", "A1"],
    case_id: str,
    sld_path: Path,
    rgb_path: Path,
    thermal_path: Path,
    public_packet: Mapping[str, Any],
) -> Dict[str, Any]:
    """Build and verify the per-turn input manifest for visual turns (`I1` and `A1`)."""
    sld_info = verify_visual_artifact(sld_path)
    rgb_info = verify_visual_artifact(rgb_path)
    thermal_info = verify_visual_artifact(thermal_path)
    packet_sha = _sha256_json(dict(public_packet))

    return {
        "turn_id": turn_id,
        "case_id": case_id,
        "input_png_sha256": {
            "sld": sld_info["sha256"],
            "rgb": rgb_info["sha256"],
            "thermal": thermal_info["sha256"],
        },
        "images": {
            "sld.png": sld_info,
            "rear_aisle_rgb.png": rgb_info,
            "rear_aisle_thermal.png": thermal_info,
        },
        "public_packet_sha256": packet_sha,
    }


# ---------------------------------------------------------------------------
# Phase 4: Deterministic Visual V2 Mock Client for Offline Dry-Run & Tests
# ---------------------------------------------------------------------------


class _MockInteractionsNamespace:
    def __init__(self, parent: "DeterministicVisualV2MockClient") -> None:
        self._parent = parent

    def create(self, **kwargs: Any) -> Dict[str, Any]:
        return self._parent._handle_create(**kwargs)

    def get(self, id: str, **kwargs: Any) -> Dict[str, Any]:
        return self._parent._handle_get(id=id, **kwargs)

    def cancel(self, id: str, **kwargs: Any) -> Dict[str, Any]:
        return self._parent._handle_cancel(id=id, **kwargs)


class _MockEnvironmentFilesNamespace:
    def __init__(self, parent: "DeterministicVisualV2MockClient") -> None:
        self._parent = parent

    def list(self, environment: Optional[str] = None, environment_id: Optional[str] = None, **kwargs: Any) -> List[Dict[str, str]]:
        env_id = str(environment or environment_id or "")
        return self._parent._handle_files_list(env_id)

    def download(self, path: str, environment: Optional[str] = None, environment_id: Optional[str] = None, **kwargs: Any) -> bytes:
        env_id = str(environment or environment_id or "")
        return self._parent._handle_files_download(env_id, path)


class _MockEnvironmentsNamespace:
    def __init__(self, parent: "DeterministicVisualV2MockClient") -> None:
        self.files = _MockEnvironmentFilesNamespace(parent)


class DeterministicVisualV2MockClient:
    """Offline Managed Agent mock client executing real SLD/thermal vision and physics.

    Simulates `client.interactions.create`/`get` and `client.environments.files.list`/`download`
    by running:
    - `parse_sld_png_topology` on the actual `sld.png` bytes passed into `I1` and `A1`.
    - `verify_thermal_hotspot_alignment` and `project_connector_bbox` on `rear_aisle_thermal.png`.
    - `infer_r0_from_voltage_drop`, `fit_thermal_parameters_from_ramp`, and `simulate_world`
      (via `build_reference_plan_v2` and `build_reference_audit_challenge_v2`) on the actual
      `public_packet.json` and `probe_receipt`.
    """

    def __init__(
        self,
        *,
        artifact_mutators: Optional[Mapping[str, Callable[[Dict[str, Any]], Dict[str, Any]]]] = None,
        raw_bytes_overrides: Optional[Mapping[str, bytes]] = None,
        live_provider_simulation: bool = False,
        custom_environment_id_fn: Optional[Callable[[str, int], str]] = None,
    ) -> None:
        self.interactions = _MockInteractionsNamespace(self)
        self.environments = _MockEnvironmentsNamespace(self)
        self.artifact_mutators: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = dict(
            artifact_mutators or {}
        )
        self.raw_bytes_overrides: Dict[str, bytes] = dict(raw_bytes_overrides or {})
        self.live_provider_simulation = bool(live_provider_simulation)
        self.custom_environment_id_fn = custom_environment_id_fn

        self._interaction_counter: int = 0
        self._env_counter: int = 0
        self._interactions: Dict[str, Dict[str, Any]] = {}
        self._env_files: Dict[str, Dict[str, bytes]] = {}
        self._env_state: Dict[str, Dict[str, Any]] = {}
        self.create_calls: List[Dict[str, Any]] = []

    def _extract_inline_packet(self, env_spec: Mapping[str, Any]) -> Dict[str, Any]:
        for src in env_spec.get("sources", []) or []:
            if isinstance(src, Mapping) and src.get("target") == "public_packet.json":
                return json.loads(str(src.get("content", "{}")))
        return {}

    def _extract_embedded_state_from_prompt(self, prompt_text: str) -> Dict[str, Any]:
        """Parse embedded JSON blocks from `I2` or `A2` prompt text if needed."""
        blocks: List[Dict[str, Any]] = []
        decoder = json.JSONDecoder()
        idx = 0
        while idx < len(prompt_text):
            brace = prompt_text.find("{", idx)
            if brace < 0:
                break
            try:
                obj, end_idx = decoder.raw_decode(prompt_text, brace)
                if isinstance(obj, dict):
                    blocks.append(obj)
                idx = end_idx
            except json.JSONDecodeError:
                idx = brace + 1
        merged: Dict[str, Any] = {}
        for b in blocks:
            if "schema_version" in b and b.get("role") == "investigator_proposal":
                merged["i1_proposal"] = b
            elif "schema_version" in b and b.get("role") == "investigator_plan":
                merged["i2_plan"] = b
            elif "schema_version" in b and b.get("role") == "auditor_visual":
                merged["a1_graph"] = b
            elif "receipt_id" in b and "kind" in b and "value" in b:
                merged["probe_receipt"] = b
            else:
                merged.update(b)
        return merged

    def _parse_images_from_input(
        self, input_blocks: Sequence[Mapping[str, Any]]
    ) -> Tuple[Dict[str, str], List[int], Dict[str, str]]:
        """Decode the 3 PNG blocks (`sld`, `rgb`, `thermal`) and run actual pixel parsers."""
        image_blocks = [b for b in input_blocks if isinstance(b, Mapping) and b.get("type") == "image"]
        if len(image_blocks) != 3:
            raise ValueError(f"Expected 3 PNG image blocks in visual turn, got {len(image_blocks)}")

        raw_sld = base64.b64decode(str(image_blocks[0]["data"]))
        raw_rgb = base64.b64decode(str(image_blocks[1]["data"]))
        raw_thermal = base64.b64decode(str(image_blocks[2]["data"]))

        png_hashes = {
            "sld": _sha256_bytes(raw_sld),
            "rgb": _sha256_bytes(raw_rgb),
            "thermal": _sha256_bytes(raw_thermal),
        }

        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            sld_p = tdir / "sld.png"
            th_p = tdir / "thermal.png"
            sld_p.write_bytes(raw_sld)
            th_p.write_bytes(raw_thermal)
            sld_parsed = parse_sld_png_topology(sld_p)
            verify_thermal_hotspot_alignment(th_p, expected_center=EXPECTED_HOTSPOT_PIXEL_CENTER)

        bbox = project_connector_bbox()
        return dict(sld_parsed["mapping"]), list(bbox), png_hashes

    def _infer_turn_id_from_prompt(self, prompt_text: str, has_images: bool, env_id: str) -> str:
        if "proposal.json" in prompt_text:
            return "I1"
        if "plan.json" in prompt_text and "audit_challenge.json" not in prompt_text:
            return "I2"
        if "independent_graph.json" in prompt_text and "audit_challenge.json" not in prompt_text:
            return "A1"
        if "audit_challenge.json" in prompt_text:
            return "A2"
        if has_images:
            return "A1" if "auditor" in env_id else "I1"
        return "A2" if "auditor" in env_id else "I2"

    def _handle_create(self, **kwargs: Any) -> Dict[str, Any]:
        self.create_calls.append(copy.deepcopy(kwargs))
        self._interaction_counter += 1
        iid = f"inter-v2-{self._interaction_counter:04d}"

        input_blocks = list(kwargs.get("input") or [])
        prompt_text = ""
        for blk in input_blocks:
            if isinstance(blk, Mapping) and blk.get("type") == "text":
                prompt_text += str(blk.get("text", ""))

        env_spec = dict(kwargs.get("environment") or {})
        existing_env_id = env_spec.get("environment_id")
        has_images = any(isinstance(b, Mapping) and b.get("type") == "image" for b in input_blocks)

        if existing_env_id:
            env_id = str(existing_env_id)
        else:
            self._env_counter += 1
            role_prefix = "auditor" if "independent_graph.json" in prompt_text else "investigator"
            if self.custom_environment_id_fn is not None:
                env_id = self.custom_environment_id_fn(role_prefix, self._env_counter)
            else:
                env_id = f"env-{role_prefix}-{self._env_counter:03d}"
            self._env_files.setdefault(env_id, {})
            self._env_state.setdefault(env_id, {})
            inline_pkt = self._extract_inline_packet(env_spec)
            if inline_pkt:
                self._env_state[env_id]["public_packet"] = inline_pkt

        turn_id = self._infer_turn_id_from_prompt(prompt_text, has_images, env_id)
        expected_filename = TURN_EXPECTED_FILENAMES[turn_id]
        state = self._env_state.setdefault(env_id, {})
        pub_pkt = state.get("public_packet") or {}
        case_id = str(pub_pkt.get("case_id", "case_A"))
        if case_id in ("A", "B"):
            case_id = f"case_{case_id}"

        if turn_id == "I1":
            sld_map, bbox, png_hashes = self._parse_images_from_input(input_blocks)
            state["sld_map"] = sld_map
            state["bbox"] = bbox
            state["png_hashes"] = png_hashes
            prop_obj = build_reference_proposal_v2(
                case_id=case_id,
                sld_power_map=sld_map,
                bbox_xyxy_px=bbox,
                measurement_type="voltage_drop",
            )
            payload_dict = prop_obj.model_dump()
        elif turn_id == "I2":
            embedded = self._extract_embedded_state_from_prompt(prompt_text)
            prop_dict = embedded.get("i1_proposal") or state.get("i1_proposal_dict")
            receipt_dict = embedded.get("probe_receipt")
            if prop_dict is not None:
                prop_model = ProposalV2.model_validate(prop_dict)
                sld_map = prop_model.power_map()
                blocking_unc = len(prop_model.unknown_critical_edges) > 0
                case_id = prop_model.case_id
            else:
                sld_map = state.get("sld_map") or {"N1": "F1", "N2": "F1", "N3": "F2", "N4": "F2"}
                blocking_unc = False
            plan_obj = build_reference_plan_v2(
                case_id=case_id,
                public_packet=pub_pkt if pub_pkt else None,
                sld_power_map=sld_map,
                probe_receipt=receipt_dict,
                blocking_graph_uncertainty=blocking_unc,
            )
            payload_dict = plan_obj.model_dump()
        elif turn_id == "A1":
            sld_map, bbox, png_hashes = self._parse_images_from_input(input_blocks)
            state["sld_map"] = sld_map
            state["bbox"] = bbox
            state["png_hashes"] = png_hashes
            graph_obj = build_reference_independent_graph_v2(
                case_id=case_id,
                sld_power_map=sld_map,
                input_png_sha256=png_hashes,
                bbox_xyxy_px=bbox,
            )
            payload_dict = graph_obj.model_dump()
        else:  # A2
            embedded = self._extract_embedded_state_from_prompt(prompt_text)
            a1_dict = embedded.get("a1_graph") or state.get("a1_graph_dict")
            i2_dict = embedded.get("i2_plan")
            a1_model = IndependentGraphV2.model_validate(a1_dict)
            i2_model = PlanV2.model_validate(i2_dict)
            challenge_obj = build_reference_audit_challenge_v2(
                case_id=a1_model.case_id,
                a1_graph=a1_model,
                i2_plan=i2_model,
            )
            payload_dict = challenge_obj.model_dump()

        if turn_id in self.artifact_mutators:
            payload_dict = self.artifact_mutators[turn_id](copy.deepcopy(payload_dict))

        if turn_id == "I1":
            state["i1_proposal_dict"] = copy.deepcopy(payload_dict)
        elif turn_id == "A1":
            state["a1_graph_dict"] = copy.deepcopy(payload_dict)

        if turn_id in self.raw_bytes_overrides:
            file_bytes = self.raw_bytes_overrides[turn_id]
        else:
            file_bytes = json.dumps(payload_dict, indent=2, sort_keys=True).encode("utf-8")

        self._env_files.setdefault(env_id, {})[expected_filename] = file_bytes

        usage = {
            "input_tokens": 1200,
            "output_tokens": 600,
            "cached_tokens": 200,
            "thoughts_tokens": 150,
            "tool_use_tokens": 100,
            "total_tokens": 2250,
        }
        record = {
            "id": iid,
            "interaction_id": iid,
            "environment_id": env_id,
            "status": "completed",
            "usage": usage,
            "steps": [
                {
                    "type": "code_execution",
                    "name": "python",
                    "code": f"# Computed and wrote {expected_filename}",
                }
            ],
        }
        self._interactions[iid] = record
        return copy.deepcopy(record)

    def _handle_get(self, id: str, **kwargs: Any) -> Dict[str, Any]:
        if id not in self._interactions:
            raise KeyError(f"Unknown interaction id {id!r}")
        return copy.deepcopy(self._interactions[id])

    def _handle_cancel(self, id: str, **kwargs: Any) -> Dict[str, Any]:
        if id in self._interactions:
            self._interactions[id]["status"] = "cancelled"
            return copy.deepcopy(self._interactions[id])
        return {"id": id, "status": "cancelled"}

    def _handle_files_list(self, env_id: str) -> List[Dict[str, str]]:
        files = self._env_files.get(env_id, {})
        return [{"path": name, "name": name} for name in sorted(files.keys())]

    def _handle_files_download(self, env_id: str, path: str) -> bytes:
        files = self._env_files.get(env_id, {})
        basename = Path(path).name
        if path in files:
            return files[path]
        if basename in files:
            return files[basename]
        raise FileNotFoundError(f"File {path!r} not found in mock environment {env_id!r}")


# ---------------------------------------------------------------------------
# Phase 4: VisualV2 Case Run Result & Orchestrator State Machine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VisualV2CaseRunResult:
    """Complete result of executing the 4-turn `visual-v2` protocol for one case (`A` or `B`)."""

    case_id: str
    short_case_id: Literal["A", "B"]
    diagnostic_mode: DiagnosticMode
    protocol_version: str
    i1_input_manifest: Dict[str, Any]
    a1_input_manifest: Dict[str, Any]
    i1_proposal: ProposalV2
    probe_receipt: ProbeReceipt
    i2_plan: PlanV2
    a1_graph: IndependentGraphV2
    a2_challenge: AuditChallengeV2
    locks: Dict[str, ArtifactLockRecordV2]
    lock_list: List[ArtifactLockRecordV2]
    turn_evidences: Dict[str, TurnEvidence]
    evaluation_report: CaseEvaluationReportV2
    zero_leakage_verified: bool
    budget_summary: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "short_case_id": self.short_case_id,
            "diagnostic_mode": self.diagnostic_mode,
            "protocol_version": self.protocol_version,
            "i1_input_manifest": copy.deepcopy(self.i1_input_manifest),
            "a1_input_manifest": copy.deepcopy(self.a1_input_manifest),
            "i1_proposal": self.i1_proposal.model_dump(),
            "probe_receipt": self.probe_receipt.to_dict(),
            "i2_plan": self.i2_plan.model_dump(),
            "a1_graph": self.a1_graph.model_dump(),
            "a2_challenge": self.a2_challenge.model_dump(),
            "locks": {k: v.model_dump() for k, v in self.locks.items()},
            "lock_list": [v.model_dump() for v in self.lock_list],
            "turn_evidences": {k: v.to_dict() for k, v in self.turn_evidences.items()},
            "evaluation_report": self.evaluation_report.to_dict(),
            "zero_leakage_verified": self.zero_leakage_verified,
            "budget_summary": copy.deepcopy(self.budget_summary),
        }


class VisualV2ProtocolOrchestrator:
    """Four-turn `visual-v2` protocol orchestrator enforcing locks, isolation, and probe barriers."""

    def __init__(
        self,
        *,
        client: Any = None,
        agent_name: str = DEFAULT_MANAGED_AGENT_MODEL,
        model_id: str = DEFAULT_FOUNDATION_MODEL,
        journal_dir: Optional[Path] = None,
        locks_dir: Optional[Path] = None,
    ) -> None:
        self.client = client
        self.agent_name = agent_name
        self.model_id = model_id
        self.journal_dir = Path(journal_dir) if journal_dir is not None else None
        self.locks_dir = Path(locks_dir) if locks_dir is not None else None

        # Strictly separated budget trackers and outcome ledgers per diagnostic mode
        self.budget_trackers_by_mode: Dict[DiagnosticMode, PairBudgetTracker] = {
            "guided_diagnostic": PairBudgetTracker(),
            "blind_diagnostic": PairBudgetTracker(),
        }
        self.outcomes_by_mode: Dict[DiagnosticMode, List[VisualV2CaseRunResult]] = {
            "guided_diagnostic": [],
            "blind_diagnostic": [],
        }

    def _persist_lock_record(
        self,
        lock_record: ArtifactLockRecordV2,
        diagnostic_mode: DiagnosticMode,
        journal_path: Optional[Path],
    ) -> None:
        if self.locks_dir is not None:
            mode_dir = self.locks_dir / diagnostic_mode / lock_record.case_id
            mode_dir.mkdir(parents=True, exist_ok=True)
            lock_file = mode_dir / f"{lock_record.stage}.json"
            lock_file.write_text(
                json.dumps(lock_record.model_dump(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        append_journal_event(
            journal_path,
            {
                "event": "locked",
                "case_id": lock_record.case_id,
                "stage": lock_record.stage,
                "interaction_id": lock_record.interaction_id,
                "environment_id": lock_record.environment_id,
                "canonical_sha256": lock_record.canonical_sha256,
                "chain_hash": lock_record.chain_hash,
                "previous_lock_hash": lock_record.previous_lock_hash,
                "status": "completed",
            },
        )

    @staticmethod
    def _verify_a1_zero_investigator_leakage(
        a1_packet: Mapping[str, Any],
        a1_prompt: str,
    ) -> None:
        """Verify `A1` receives ZERO `I1`/`I2` proposal, plan, or probe receipt fields."""
        forbidden_a1_keys = set(INVESTIGATOR_ONLY_KEYS) | {
            "i1_proposal",
            "i2_plan",
            "probe_receipt",
            "i1_lock_hash",
            "l1_lock_hash",
            "l2_lock_hash",
        }
        leaked_keys = forbidden_a1_keys.intersection(a1_packet.keys())
        if leaked_keys:
            raise PermissionError(
                f"Auditor A1 isolation violation: Investigator/probe keys leaked into A1 packet: {sorted(leaked_keys)}"
            )
        for tok in ("i1_proposal", "i2_plan", "probe_receipt", "selected_action_id", "post_measurement_parameters"):
            if tok in a1_prompt:
                raise PermissionError(
                    f"Auditor A1 isolation violation: Investigator token {tok!r} leaked into A1 prompt"
                )

    def run_case(
        self,
        case_id: str = "case_A",
        *,
        diagnostic_mode: DiagnosticMode = "guided_diagnostic",
        truth_loader: Optional[GuardedOracleTruthLoader] = None,
        public_packet: Optional[Mapping[str, Any]] = None,
        bundle_dir: Optional[Path] = None,
        client: Any = None,
        budget_tracker: Optional[PairBudgetTracker] = None,
        require_live_evidence: bool = False,
        enforce_guided_measurement_check: bool = False,
        before_turn_hooks: Optional[Mapping[str, Callable[[GuardedOracleTruthLoader], None]]] = None,
    ) -> VisualV2CaseRunResult:
        """Execute the 12-step `visual-v2` 4-turn state machine for one case (`A` or `B`)."""
        if diagnostic_mode not in ("guided_diagnostic", "blind_diagnostic"):
            raise ValueError(
                f"Unsupported diagnostic_mode={diagnostic_mode!r}; expected 'guided_diagnostic' or 'blind_diagnostic'"
            )
        protocol_version = PROTOCOL_VERSION_BY_MODE[diagnostic_mode]
        short_id, canonical_case_id = _normalize_case_ids(case_id)

        active_client = client or self.client or DeterministicVisualV2MockClient()
        active_loader = truth_loader if truth_loader is not None else GuardedOracleTruthLoader(short_id)

        if budget_tracker is not None:
            # Verify caller does not mix guided_diagnostic and blind_diagnostic on the same tracker
            bound_mode = getattr(budget_tracker, "_diagnostic_mode", None)
            if bound_mode is not None and bound_mode != diagnostic_mode:
                raise PermissionError(
                    f"Cannot mix {diagnostic_mode!r} run into PairBudgetTracker bound to {bound_mode!r}"
                )
            setattr(budget_tracker, "_diagnostic_mode", diagnostic_mode)
            active_budget = budget_tracker
        else:
            active_budget = self.budget_trackers_by_mode[diagnostic_mode]
            setattr(active_budget, "_diagnostic_mode", diagnostic_mode)

        harness_mode = "guided_diagnostic" if diagnostic_mode == "guided_diagnostic" else "unassisted"
        if public_packet is None:
            priv_for_export = build_private_world(short_id)
            pub_pkt = export_public_packet(priv_for_export, diagnostic_mode=harness_mode)
            pub_pkt["diagnostic_mode"] = diagnostic_mode
        else:
            pub_pkt = copy.deepcopy(dict(public_packet))
            pub_pkt["diagnostic_mode"] = diagnostic_mode

        # Ensure no private truth keys exist in public_packet
        validate_contract_hygiene(pub_pkt)

        journal_path = (
            (self.journal_dir / f"{diagnostic_mode}_{canonical_case_id}.jsonl")
            if self.journal_dir is not None
            else None
        )

        hooks = dict(before_turn_hooks or {})

        with tempfile.TemporaryDirectory() as tmp_visual_dir:
            vis_dir = Path(bundle_dir) if bundle_dir is not None else Path(tmp_visual_dir)
            img_paths = ensure_case_visual_inputs(short_id, vis_dir)
            sld_path = img_paths["sld"]
            rgb_path = img_paths["rgb"]
            thermal_path = img_paths["thermal"]

            # ------------------------------------------------------------------
            # Step 1: Build per-turn input manifest for I1 (3 PNGs + public_packet.json)
            # ------------------------------------------------------------------
            i1_manifest = build_turn_input_manifest(
                turn_id="I1",
                case_id=canonical_case_id,
                sld_path=sld_path,
                rgb_path=rgb_path,
                thermal_path=thermal_path,
                public_packet=pub_pkt,
            )

            if "I1" in hooks:
                hooks["I1"](active_loader)

            # ------------------------------------------------------------------
            # Step 2: Execute I1 (role="investigator_proposal", environment_id=None)
            # ------------------------------------------------------------------
            harness_mode = "guided_diagnostic" if diagnostic_mode == "guided_diagnostic" else "unassisted"
            i1_prompt = build_i1_prompt(
                case_id=canonical_case_id,
                diagnostic_mode=harness_mode,
            )
            i1_evidence = run_turn(
                role="investigator_proposal",
                prompt=i1_prompt,
                packet=pub_pkt,
                images=[sld_path, rgb_path, thermal_path],
                environment_id=None,
                max_total_tokens=NATIVE_INTERACTION_NONCACHED_CAP,
                expected_filename="proposal.json",
                case_id=canonical_case_id,
                turn_id="I1",
                client=active_client,
                journal_path=journal_path,
                budget_tracker=active_budget,
                agent_name=self.agent_name,
                model_id=self.model_id,
            )
            if i1_evidence.retrieved_json is None:
                raise RuntimeError(f"I1 failed to produce proposal.json: {i1_evidence.error}")

            # ------------------------------------------------------------------
            # Step 3: Validate ProposalV2, verify zero truth access, commit L1_I1_PROPOSAL
            # ------------------------------------------------------------------
            i1_proposal = ProposalV2.model_validate(i1_evidence.retrieved_json)
            if active_loader.truth_access_count > 0 or active_loader.premature_access_attempts > 0:
                raise PermissionError(
                    "Zero-leakage barrier violated: private truth access attempted during or before I1"
                )
            if enforce_guided_measurement_check and diagnostic_mode == "guided_diagnostic":
                if i1_proposal.selected_measurement.type != "voltage_drop":
                    raise ValueError(
                        f"guided_diagnostic mode requires selected_measurement.type == 'voltage_drop', "
                        f"got {i1_proposal.selected_measurement.type!r}"
                    )

            i1_raw_bytes = canonical_json_bytes(i1_proposal)
            l1_record = create_lock_record_v2(
                stage="L1_I1_PROPOSAL",
                case_id=canonical_case_id,
                contract_obj=i1_proposal,
                raw_file_bytes=i1_raw_bytes,
                environment_id=i1_evidence.environment_id,
                interaction_id=i1_evidence.interaction_id,
                previous_lock_hash=None,
            )
            active_loader.register_lock(l1_record)
            self._persist_lock_record(l1_record, diagnostic_mode, journal_path)
            frozen_i1_sha256 = l1_record.canonical_sha256

            # ------------------------------------------------------------------
            # Step 4: Execute single local diagnostic probe via GuardedOracleTruthLoader
            # ------------------------------------------------------------------
            probe_receipt = active_loader.issue_scoped_probe(
                kind=i1_proposal.selected_measurement.type,
                target=i1_proposal.selected_measurement.target_asset,
                i1_lock_hash=l1_record.canonical_sha256,
            )

            # ------------------------------------------------------------------
            # Step 5: Check if I1 reported unknown_critical_edges (requires I2 abstention)
            # ------------------------------------------------------------------
            requires_abstention = len(i1_proposal.unknown_critical_edges) > 0

            if "I2" in hooks:
                hooks["I2"](active_loader)

            # ------------------------------------------------------------------
            # Step 6: Execute I2 (reusing I1 environment_id, images=[], compact state packet)
            # ------------------------------------------------------------------
            i2_state_packet: Dict[str, Any] = {
                "case_id": canonical_case_id,
                "diagnostic_mode": diagnostic_mode,
                "protocol_version": protocol_version,
                "i1_lock_hash": l1_record.canonical_sha256,
                "probe_receipt": probe_receipt.to_dict(),
                "i1_proposal": i1_proposal.model_dump(),
                "requires_abstention": requires_abstention,
                "telemetry": pub_pkt.get("telemetry", {}),
            }
            i2_prompt = build_i2_prompt(
                case_id=canonical_case_id,
                i1_lock_hash=l1_record.canonical_sha256,
                probe_receipt=probe_receipt.to_dict(),
                i1_summary=i1_proposal.model_dump(),
            )
            i2_evidence = run_turn(
                role="investigator_plan",
                prompt=i2_prompt,
                packet=i2_state_packet,
                images=[],
                environment_id=i1_evidence.environment_id,
                max_total_tokens=NATIVE_INTERACTION_NONCACHED_CAP,
                expected_filename="plan.json",
                case_id=canonical_case_id,
                turn_id="I2",
                client=active_client,
                journal_path=journal_path,
                budget_tracker=active_budget,
                agent_name=self.agent_name,
                model_id=self.model_id,
            )
            if i2_evidence.retrieved_json is None:
                raise RuntimeError(f"I2 failed to produce plan.json: {i2_evidence.error}")

            # ------------------------------------------------------------------
            # Step 7: Validate PlanV2, verify I1 immutability, verify predictions, commit L2
            # ------------------------------------------------------------------
            if canonical_sha256(i1_proposal) != frozen_i1_sha256:
                raise RuntimeError("I1 proposal was mutated after probe receipt was issued")

            i2_plan = PlanV2.model_validate(i2_evidence.retrieved_json)
            if requires_abstention:
                if i2_plan.decision != "abstain" or i2_plan.selected_action_id != "NO_ACTION":
                    raise ValueError(
                        "I2 must abstain (decision='abstain', selected_action_id='NO_ACTION') "
                        "when I1 unknown_critical_edges is non-empty"
                    )

            plan_consistency = verify_plan_against_inferred_network(i2_plan)
            if not plan_consistency["consistent"]:
                raise ValueError(
                    f"I2 PlanV2 predictions do not match its own inferred network: {plan_consistency['mismatches'][:3]}"
                )

            i2_raw_bytes = canonical_json_bytes(i2_plan)
            l2_record = create_lock_record_v2(
                stage="L2_I2_PLAN",
                case_id=canonical_case_id,
                contract_obj=i2_plan,
                raw_file_bytes=i2_raw_bytes,
                environment_id=i2_evidence.environment_id,
                interaction_id=i2_evidence.interaction_id,
                previous_lock_hash=l1_record.chain_hash,
            )
            active_loader.register_lock(l2_record)
            self._persist_lock_record(l2_record, diagnostic_mode, journal_path)

            if "A1" in hooks:
                hooks["A1"](active_loader)

            # ------------------------------------------------------------------
            # Step 8: Execute A1 (fresh environment_id=None, same 3 PNGs + public_packet, ZERO I1/I2 fields)
            # ------------------------------------------------------------------
            a1_manifest = build_turn_input_manifest(
                turn_id="A1",
                case_id=canonical_case_id,
                sld_path=sld_path,
                rgb_path=rgb_path,
                thermal_path=thermal_path,
                public_packet=pub_pkt,
            )
            a1_prompt = build_a1_prompt(
                case_id=canonical_case_id,
                input_image_hashes=a1_manifest["input_png_sha256"],
            )
            self._verify_a1_zero_investigator_leakage(pub_pkt, a1_prompt)

            a1_evidence = run_turn(
                role="auditor_visual",
                prompt=a1_prompt,
                packet=pub_pkt,
                images=[sld_path, rgb_path, thermal_path],
                environment_id=None,
                max_total_tokens=NATIVE_INTERACTION_NONCACHED_CAP,
                expected_filename="independent_graph.json",
                case_id=canonical_case_id,
                turn_id="A1",
                client=active_client,
                journal_path=journal_path,
                budget_tracker=active_budget,
                agent_name=self.agent_name,
                model_id=self.model_id,
            )
            if a1_evidence.environment_id == i1_evidence.environment_id:
                raise PermissionError(
                    f"Investigator/Auditor isolation violation: A1 reused I1 environment_id {i1_evidence.environment_id!r}"
                )
            if a1_evidence.retrieved_json is None:
                raise RuntimeError(f"A1 failed to produce independent_graph.json: {a1_evidence.error}")

            # ------------------------------------------------------------------
            # Step 9: Validate IndependentGraphV2, verify PNG hashes, commit L3 BEFORE A2
            # ------------------------------------------------------------------
            a1_graph = IndependentGraphV2.model_validate(a1_evidence.retrieved_json)
            expected_png_hashes = a1_manifest["input_png_sha256"]
            for png_key in ("sld", "rgb", "thermal"):
                actual_h = a1_graph.input_png_sha256.get(png_key)
                expected_h = expected_png_hashes[png_key]
                if actual_h != expected_h:
                    raise ValueError(
                        f"A1 input_png_sha256[{png_key!r}] mismatch: got {actual_h!r}, expected {expected_h!r}"
                    )

            a1_raw_bytes = canonical_json_bytes(a1_graph)
            l3_record = create_lock_record_v2(
                stage="L3_A1_GRAPH",
                case_id=canonical_case_id,
                contract_obj=a1_graph,
                raw_file_bytes=a1_raw_bytes,
                environment_id=a1_evidence.environment_id,
                interaction_id=a1_evidence.interaction_id,
                previous_lock_hash=l2_record.chain_hash,
            )
            active_loader.register_lock(l3_record)
            self._persist_lock_record(l3_record, diagnostic_mode, journal_path)

            if "A2" in hooks:
                hooks["A2"](active_loader)

            # ------------------------------------------------------------------
            # Step 10: Execute A2 (reusing A1 environment_id, images=[], compact state packet)
            # ------------------------------------------------------------------
            if "L3_A1_GRAPH" not in active_loader.locks:
                raise PermissionError("Cannot execute A2 before L3_A1_GRAPH is committed")

            a2_state_packet: Dict[str, Any] = {
                "case_id": canonical_case_id,
                "diagnostic_mode": diagnostic_mode,
                "protocol_version": protocol_version,
                "l2_lock_hash": l2_record.canonical_sha256,
                "l3_lock_hash": l3_record.canonical_sha256,
                "i2_plan": i2_plan.model_dump(),
                "probe_receipt": probe_receipt.to_dict(),
                "a1_graph": a1_graph.model_dump(),
            }
            a2_prompt = build_a2_prompt(
                case_id=canonical_case_id,
                a1_lock_hash=l3_record.canonical_sha256,
                i2_lock_hash=l2_record.canonical_sha256,
                a1_graph=a1_graph.model_dump(),
                i2_plan=i2_plan.model_dump(),
                probe_receipt=probe_receipt.to_dict(),
            )
            a2_evidence = run_turn(
                role="auditor_challenge",
                prompt=a2_prompt,
                packet=a2_state_packet,
                images=[],
                environment_id=a1_evidence.environment_id,
                max_total_tokens=NATIVE_INTERACTION_NONCACHED_CAP,
                expected_filename="audit_challenge.json",
                case_id=canonical_case_id,
                turn_id="A2",
                client=active_client,
                journal_path=journal_path,
                budget_tracker=active_budget,
                agent_name=self.agent_name,
                model_id=self.model_id,
            )
            if a2_evidence.retrieved_json is None:
                raise RuntimeError(f"A2 failed to produce audit_challenge.json: {a2_evidence.error}")

            # ------------------------------------------------------------------
            # Step 11: Validate AuditChallengeV2 and commit Lock L4_A2_CHALLENGE
            # ------------------------------------------------------------------
            a2_challenge = AuditChallengeV2.model_validate(a2_evidence.retrieved_json)
            a2_raw_bytes = canonical_json_bytes(a2_challenge)
            l4_record = create_lock_record_v2(
                stage="L4_A2_CHALLENGE",
                case_id=canonical_case_id,
                contract_obj=a2_challenge,
                raw_file_bytes=a2_raw_bytes,
                environment_id=a2_evidence.environment_id,
                interaction_id=a2_evidence.interaction_id,
                previous_lock_hash=l3_record.chain_hash,
            )
            active_loader.register_lock(l4_record)
            self._persist_lock_record(l4_record, diagnostic_mode, journal_path)

            # Verify zero truth access occurred prior to L4 completion
            zero_leakage_before_eval = (
                active_loader.truth_access_count == 0
                and active_loader.premature_access_attempts == 0
                and active_loader.is_fully_locked
            )
            if not zero_leakage_before_eval:
                raise PermissionError(
                    "Zero-leakage barrier failed: private truth accessed before all 4 locks L1..L4 were committed"
                )

            # ------------------------------------------------------------------
            # Step 12: Only AFTER L1..L4 are committed, call oracle.evaluate_case_v2
            # ------------------------------------------------------------------
            turn_evidences = {
                "I1": i1_evidence,
                "I2": i2_evidence,
                "A1": a1_evidence,
                "A2": a2_evidence,
            }
            live_ev_payload: Optional[Dict[str, Any]] = None
            if getattr(active_client, "live_provider_simulation", False) or not isinstance(
                active_client, DeterministicVisualV2MockClient
            ):
                live_ev_payload = {
                    tid: {
                        "response_id": ev.interaction_id,
                        "model": self.model_id,
                        "request_sha256": _sha256_bytes(f"req-{tid}-{ev.interaction_id}".encode("utf-8")),
                        "response_sha256": ev.retrieved_sha256
                        or _sha256_bytes(f"resp-{tid}-{ev.interaction_id}".encode("utf-8")),
                        "latency_ms": max(1.0, float(ev.elapsed_s) * 1000.0 + 10.0),
                        "usage": ev.usage.to_dict(),
                    }
                    for tid, ev in turn_evidences.items()
                }

            lock_list = [l1_record, l2_record, l3_record, l4_record]
            eval_report = evaluate_case_v2(
                i1_proposal=i1_proposal,
                probe_receipt=probe_receipt,
                i2_plan=i2_plan,
                a1_graph=a1_graph,
                a2_challenge=a2_challenge,
                locks=lock_list,
                truth_loader=active_loader,
                turn_evidences=live_ev_payload,
                require_live_evidence=require_live_evidence,
            )

            result = VisualV2CaseRunResult(
                case_id=canonical_case_id,
                short_case_id=short_id,
                diagnostic_mode=diagnostic_mode,
                protocol_version=protocol_version,
                i1_input_manifest=i1_manifest,
                a1_input_manifest=a1_manifest,
                i1_proposal=i1_proposal,
                probe_receipt=probe_receipt,
                i2_plan=i2_plan,
                a1_graph=a1_graph,
                a2_challenge=a2_challenge,
                locks={rec.stage: rec for rec in lock_list},
                lock_list=lock_list,
                turn_evidences=turn_evidences,
                evaluation_report=eval_report,
                zero_leakage_verified=zero_leakage_before_eval,
                budget_summary=active_budget.to_dict() if hasattr(active_budget, "to_dict") else {},
            )
            self.outcomes_by_mode[diagnostic_mode].append(result)
            return result


def run_visual_v2_case_protocol(
    case_id: str = "case_A",
    *,
    diagnostic_mode: DiagnosticMode = "guided_diagnostic",
    truth_loader: Optional[GuardedOracleTruthLoader] = None,
    public_packet: Optional[Mapping[str, Any]] = None,
    bundle_dir: Optional[Path] = None,
    client: Any = None,
    budget_tracker: Optional[PairBudgetTracker] = None,
    journal_dir: Optional[Path] = None,
    locks_dir: Optional[Path] = None,
    require_live_evidence: bool = False,
    enforce_guided_measurement_check: bool = False,
    before_turn_hooks: Optional[Mapping[str, Callable[[GuardedOracleTruthLoader], None]]] = None,
) -> VisualV2CaseRunResult:
    """Convenience runner executing the 4-turn `visual-v2` protocol for one case."""
    orchestrator = VisualV2ProtocolOrchestrator(
        client=client,
        journal_dir=journal_dir,
        locks_dir=locks_dir,
    )
    return orchestrator.run_case(
        case_id=case_id,
        diagnostic_mode=diagnostic_mode,
        truth_loader=truth_loader,
        public_packet=public_packet,
        bundle_dir=bundle_dir,
        client=client,
        budget_tracker=budget_tracker,
        require_live_evidence=require_live_evidence,
        enforce_guided_measurement_check=enforce_guided_measurement_check,
        before_turn_hooks=before_turn_hooks,
    )


# ---------------------------------------------------------------------------
# Backward-Compatible Phase 00 Protocol State Machine
# ---------------------------------------------------------------------------


@dataclass
class CaseProtocolRunResult:
    """Complete locked transcript and accounting for a single case (A or B)."""

@dataclass
class CaseProtocolRunResult:
    """Complete record of the 4 locked interactions (I1, I2, A1, A2) and 1 probe for a case."""

    case_id: str
    i1: InvestigatorI1Contract
    probe_response: ProbeResponse
    i2: InvestigatorI2Contract
    a1: AuditorA1Contract
    a2: AuditorA2Contract
    locks: Dict[str, SafetyLockRecord] = field(default_factory=dict)
    zero_leakage_verified: bool = True
    accounting: List[InteractionAccounting] = field(default_factory=list)
    accounting_summary: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "i1": self.i1.model_dump(),
            "probe_response": self.probe_response.model_dump(),
            "i2": self.i2.model_dump(),
            "a1": self.a1.model_dump(),
            "a2": self.a2.model_dump(),
            "locks": {k: v.model_dump() for k, v in self.locks.items()},
            "zero_leakage_verified": self.zero_leakage_verified,
            "accounting": [asdict(a) for a in self.accounting],
            "accounting_summary": self.accounting_summary,
        }


class BlackboxProtocolStateMachine:
    """Enforces the I1 -> Probe -> I2 and A1 -> A2 protocol and safety locks."""

    def __init__(
        self,
        transport: Optional[ManagedAgentTransport] = None,
        sandbox_dir: Optional[Path] = None,
        *,
        public_evidence: Optional[PublicCaseEvidence] = None,
        sealed_truth: Optional[SealedWorldTruth] = None,
    ) -> None:
        if isinstance(transport, PublicCaseEvidence):
            # Positional call: BlackboxProtocolStateMachine(pub_ev, sealed_truth, transport)
            public_evidence = transport
            if isinstance(sandbox_dir, SealedWorldTruth):
                sealed_truth = sandbox_dir
                sandbox_dir = None
            transport = None
        self.public_evidence: Optional[PublicCaseEvidence] = public_evidence
        self._sealed_truth: Optional[SealedWorldTruth] = sealed_truth
        self.transport: ManagedAgentTransport = transport or ManagedAgentTransport(mode="deterministic")
        self.sandbox_dir = Path(sandbox_dir) if sandbox_dir is not None else None
        self.probe_session: Optional[SingleShotProbeSession] = (
            SingleShotProbeSession(
                truth=sealed_truth,
                ambient_temp_c=public_evidence.ambient_temp_c if public_evidence is not None else 24.0,
            )
            if sealed_truth is not None
            else None
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
        if self._sealed_truth is None:
            raise RuntimeError("No sealed_truth bound to BlackboxProtocolStateMachine")
        self._truth_unsealed = True
        return self._sealed_truth

    @staticmethod
    def verify_zero_leakage_in_public_bundle(public_evidence: PublicCaseEvidence) -> bool:
        """Verify that public evidence contains no sealed truth fields or private paths."""
        pub_dict = asdict(public_evidence)
        serialized = json.dumps(pub_dict, sort_keys=True)
        forbidden_tokens = [
            "truth_sha256",
            "node_resistances_ohm",
            "degraded_node_id",
            "degraded_feeder_id",
            "true_max_safe_current_a",
            "/private/",
            "sealed_truth",
        ]
        for token in forbidden_tokens:
            if token in serialized:
                raise PermissionError(f"Zero-leakage violation: forbidden token {token!r} in public bundle.")

        for p_str in (public_evidence.sld_image_path, public_evidence.scene_3d_image_path):
            p = Path(p_str)
            if "private" in p.parts:
                raise PermissionError(f"Zero-leakage violation: private path {p} exposed to agent.")
        return True

    def _create_lock(self, interaction_id: str, case_id: str, payload: Dict[str, Any]) -> SafetyLockRecord:
        if self._truth_unsealed:
            raise RuntimeError(
                f"Protocol lock violation: truth was unsealed before locking {interaction_id} for {case_id}."
            )
        rec = SafetyLockRecord(
            interaction_id=interaction_id,  # type: ignore[arg-type]
            case_id=case_id,
            payload_sha256=_sha256_json(payload),
            locked_at_utc=_utc_now(),
            private_truth_accessed_before_lock=self._truth_unsealed,
            payload=payload,
        )
        self.locks[interaction_id] = rec
        return rec

    def run_case_protocol(
        self,
        public_evidence: Optional[PublicCaseEvidence] = None,
        probe_session: Optional[SingleShotProbeSession] = None,
    ) -> CaseProtocolRunResult:
        """Execute I1, single probe, I2, A1, and A2, locking all 4 before any grading."""
        active_pub = public_evidence or self.public_evidence
        active_probe = probe_session or self.probe_session
        if active_pub is None or active_probe is None:
            raise ValueError("public_evidence and probe_session must be provided")

        case_id = active_pub.case_id
        case_sandbox: Optional[Path] = None
        if self.sandbox_dir is not None:
            case_sandbox = self.sandbox_dir / case_id
            case_sandbox.mkdir(parents=True, exist_ok=True)

        zero_leak_ok = self.verify_zero_leakage_in_public_bundle(active_pub)
        pub_dict = asdict(active_pub)
        image_paths = [
            Path(p)
            for p in (active_pub.sld_image_path, active_pub.scene_3d_image_path)
            if Path(p).exists()
        ]

        locks: Dict[str, SafetyLockRecord] = {}
        accounting_records: List[InteractionAccounting] = []

        # 1. Investigator I1 (Pre-probe visual + telemetry analysis & probe selection)
        i1_raw, acc_i1 = self.transport.invoke_interaction(
            interaction_id="I1",
            case_id=case_id,
            agent_role="investigator",
            prompt_context={"public_evidence": pub_dict},
            handoff_files=image_paths,
        )
        i1_contract = InvestigatorI1Contract.model_validate(i1_raw)
        locks["I1"] = self._create_lock("I1", case_id, i1_contract.model_dump())
        accounting_records.append(acc_i1)
        if case_sandbox is not None:
            (case_sandbox / "I1_output.json").write_text(
                json.dumps(i1_contract.model_dump(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )

        # 2. Auditor A1 (Independent pre-probe visual & SLD topology verification)
        a1_raw, acc_a1 = self.transport.invoke_interaction(
            interaction_id="A1",
            case_id=case_id,
            agent_role="auditor",
            prompt_context={"public_evidence": pub_dict},
            handoff_files=image_paths,
        )
        a1_contract = AuditorA1Contract.model_validate(a1_raw)
        locks["A1"] = self._create_lock("A1", case_id, a1_contract.model_dump())
        accounting_records.append(acc_a1)
        if case_sandbox is not None:
            (case_sandbox / "A1_output.json").write_text(
                json.dumps(a1_contract.model_dump(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )

        # 3. Single Active Voltage-Drop Probe
        probe_response = active_probe.execute_probe(i1_contract.probe_request)

        # 4. Investigator I2 (Post-probe resistance inference & safe current cap lock)
        i2_raw, acc_i2 = self.transport.invoke_interaction(
            interaction_id="I2",
            case_id=case_id,
            agent_role="investigator",
            prompt_context={
                "public_evidence": pub_dict,
                "i1_contract": i1_contract.model_dump(),
                "probe_response": probe_response.model_dump(),
            },
            handoff_files=image_paths,
        )
        i2_contract = InvestigatorI2Contract.model_validate(i2_raw)
        locks["I2"] = self._create_lock("I2", case_id, i2_contract.model_dump())
        accounting_records.append(acc_i2)
        if case_sandbox is not None:
            (case_sandbox / "I2_output.json").write_text(
                json.dumps(i2_contract.model_dump(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )

        # 5. Auditor A2 (Independent post-probe safety verification)
        a2_raw, acc_a2 = self.transport.invoke_interaction(
            interaction_id="A2",
            case_id=case_id,
            agent_role="auditor",
            prompt_context={
                "public_evidence": pub_dict,
                "a1_contract": a1_contract.model_dump(),
                "i2_contract": i2_contract.model_dump(),
                "probe_response": probe_response.model_dump(),
            },
            handoff_files=image_paths,
        )
        a2_contract = AuditorA2Contract.model_validate(a2_raw)
        locks["A2"] = self._create_lock("A2", case_id, a2_contract.model_dump())
        accounting_records.append(acc_a2)
        if case_sandbox is not None:
            (case_sandbox / "A2_output.json").write_text(
                json.dumps(a2_contract.model_dump(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )

        return CaseProtocolRunResult(
            case_id=case_id,
            i1=i1_contract,
            probe_response=probe_response,
            i2=i2_contract,
            a1=a1_contract,
            a2=a2_contract,
            locks=locks,
            zero_leakage_verified=zero_leak_ok,
            accounting=accounting_records,
            accounting_summary=(
                self.transport.accounting_summary()
                if hasattr(self.transport, "accounting_summary")
                else {
                    "interactions": [asdict(a) for a in accounting_records]
                }
            ),
        )
