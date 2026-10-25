"""Builder and Critic Managed Agent roles and mock provider for BlackboxDC Twin Repair.

Implements Phase D of `11_TWIN_REPAIR_ADDON_IMPLEMENTATION_PLAN_EN.md`:
- Reuses Phase 3 transport primitives from `blackbox.managed` (`resolve_auth_config`,
  `build_genai_client`, `normalize_usage`, `redact_for_log`, `extract_interaction_steps`,
  `retrieve_environment_file`, `DEFAULT_MANAGED_AGENT_MODEL = "antigravity-preview-09-2026"`,
  `DEFAULT_FOUNDATION_MODEL = "gemini-4-argon"`).
- Role-specific `/workspace/AGENTS.md` builders (`build_builder_agents_md`,
  `build_critic_agents_md`) with recorded SHA-256 hashes.
- `LiveManagedTwinRepairProvider` launching isolated remote environments per role
  (never reusing `environment_id` or `previous_interaction_id`, never passing
  `thinking_level` or `response_format`), polling background execution, and retrieving
  `/workspace/scene_spec.json` and `/workspace/repair_patch.json`.
- `MockTwinRepairProvider` (`source="fixture"`, `argon_verified=False`) with distinct
  synthetic environment IDs (`env-mock-builder-001`, `env-mock-critic-002`) supporting
  normal (`drift_A_to_B` and `control_A_to_A`) and adverse modes (`abstain`, `timeout`,
  `missing_remote_output`, `same_environment_id`, `invalid_patch`, `forged_hash`).
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field
import hashlib
import io
import json
from pathlib import Path
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union
import uuid

from PIL import Image

from blackbox.managed import (
    DEFAULT_FOUNDATION_MODEL,
    DEFAULT_MANAGED_AGENT_MODEL,
    TERMINAL_STATUSES,
    _extract_inter_field,
    _has_code_execution_step,
    _scan_for_private_leaks,
    _validate_and_encode_png,
    build_genai_client,
    extract_interaction_steps,
    normalize_usage_snapshot,
    redact_for_log,
    resolve_auth_config,
    retrieve_environment_file,
)


normalize_usage = normalize_usage_snapshot

DEFAULT_ROLE_TOKEN_CAP: int = 25_000
PROMPTS_DIR: Path = Path(__file__).resolve().parent / "prompts"


def load_role_prompt_template(role: str) -> str:
    """Load `prompts/builder.txt` or `prompts/critic.txt` and verify zero private leaks."""
    norm = role.strip().lower()
    if norm not in ("builder", "critic", "critic_neutral"):
        raise ValueError(f"Unsupported role {role!r}; expected 'builder', 'critic', or 'critic_neutral'")
    prompt_path = PROMPTS_DIR / f"{norm}.txt"
    text = prompt_path.read_text(encoding="utf-8")
    _scan_for_private_leaks(text, f"{norm}_prompt_template")
    return text


def build_builder_agents_md() -> str:
    """Build `/workspace/AGENTS.md` for the Builder Managed Agent sandbox."""
    text = (
        "# BlackboxDC Twin Repair — Builder Sandbox (`AGENTS.md`)\n\n"
        "- Role: `builder` (isolated remote environment).\n"
        "- Allowed output file: `/workspace/scene_spec.json` (`schema_version = \"scene_spec.v1\"`).\n"
        "- Inputs: `old_sld_A.png`, `old_thermal_rear.png`, and `/workspace/public_state.json`.\n"
        "- Infer rack positions (`N1..N4`), the rear hot connector on `N4`, and the 4 feeder edges "
        "from the filled black junction dots (`●`) on `F1` (`y=210`) vs `F2` (`y=290`) in `old_sld_A.png`.\n"
        "- Write `/workspace/scene_spec.json` in a single Python `code_execution` step and stop.\n"
    )
    _scan_for_private_leaks(text, "builder_agents_md")
    return text


def build_critic_agents_md() -> str:
    """Build `/workspace/AGENTS.md` for the Visual Critic Managed Agent sandbox."""
    text = (
        "# BlackboxDC Twin Repair — Visual Critic Sandbox (`AGENTS.md`)\n\n"
        "- Role: `critic` (separate fresh remote environment; no shared filesystem with Builder).\n"
        "- Allowed output file: `/workspace/repair_patch.json` (`schema_version = \"repair_patch.v1\"`).\n"
        "- Inputs: Image 1 (current as-built SLD), Image 2 (`render0_wiring.png`), Image 3 (`render0_thermal_rear.png`), "
        "and `/workspace/public_state.json`.\n"
        "- Compare visible 3D feeder cables in `render0_wiring.png` against the branch junction dots in the current SLD.\n"
        "- Cite pixel bounding boxes `[x0, y0, x1, y1]` for observed render cables and SLD branch connections.\n"
        "- Write `/workspace/repair_patch.json` in a single Python `code_execution` step and stop.\n"
    )
    _scan_for_private_leaks(text, "critic_agents_md")
    return text


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_image_manifest_entry(name: str, path_or_bytes: Union[Path, str, bytes]) -> Tuple[Dict[str, Any], bytes]:
    if isinstance(path_or_bytes, (Path, str)):
        p = Path(path_or_bytes)
        raw = p.read_bytes()
    else:
        raw = bytes(path_or_bytes)
    with Image.open(io.BytesIO(raw)) as im:
        width, height = im.size
    entry = {
        "name": name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "width_px": int(width),
        "height_px": int(height),
        "size_bytes": len(raw),
    }
    return entry, raw


@dataclass
class RoleProvenance:
    """Audit and provenance record for a single Builder or Critic turn."""

    role: str
    source: str
    requested_agent: str
    requested_model: str
    returned_model: str
    argon_verified: bool
    interaction_id: str
    environment_id: str
    status: str
    native_image_input_sent: bool
    image_manifest: List[Dict[str, Any]]
    agents_md_sha256: str
    mounted_files: List[Dict[str, str]]
    retrieved_file_path: Optional[str]
    retrieved_sha256: Optional[str]
    elapsed_s: float
    usage: Dict[str, Any]
    code_execution_observed: bool
    error: Optional[str] = None
    steps: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return redact_for_log(asdict(self))

    def model_dump(self) -> Dict[str, Any]:
        return self.to_dict()

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


def _format_prompt_with_manifest(base_prompt: str, image_manifest: Sequence[Mapping[str, Any]], extra_context: Mapping[str, Any]) -> str:
    manifest_lines = [
        f"{idx + 1}. {item['name']} (sha256={item['sha256']}, {item['width_px']}x{item['height_px']} px, {item['size_bytes']} bytes)"
        for idx, item in enumerate(image_manifest)
    ]
    ctx_json = json.dumps(dict(extra_context), sort_keys=True)
    full_prompt = (
        f"{base_prompt.strip()}\n\n"
        f"### Attached Image Manifest\n"
        + "\n".join(manifest_lines)
        + f"\n\n### Public Context Summary\n`{ctx_json}`\n"
    )
    _scan_for_private_leaks(full_prompt, "formatted_role_prompt")
    return full_prompt


class LiveManagedTwinRepairProvider:
    """Genuine Antigravity Managed Agent provider (`antigravity-preview-09-2026` / `gemini-4-argon`)."""

    def __init__(
        self,
        *,
        client: Any = None,
        agent_name: str = DEFAULT_MANAGED_AGENT_MODEL,
        model_id: str = DEFAULT_FOUNDATION_MODEL,
        role_token_cap: int = DEFAULT_ROLE_TOKEN_CAP,
        poll_timeout_s: float = 150.0,
        poll_interval_s: float = 1.0,
        time_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.agent_name = agent_name
        self.model_id = model_id
        self.role_token_cap = int(role_token_cap)
        self.poll_timeout_s = float(poll_timeout_s)
        self.poll_interval_s = float(poll_interval_s)
        self.time_fn = time_fn
        self.sleep_fn = sleep_fn
        self._seen_environment_ids: set[str] = set()
        # Optional observer for live dashboards; receives only role, IDs, status, and step types.
        self.progress_fn: Optional[Callable[[Dict[str, Any]], None]] = None

    def _report_progress(self, role: str, interaction_id: str, environment_id: str, status: str, t0: float, inter: Any) -> None:
        if self.progress_fn is None:
            return
        try:
            raw_steps = _extract_inter_field(inter, "steps")
            step_types: List[str] = []
            if isinstance(raw_steps, (list, tuple)):
                for st in raw_steps:
                    st_type = st.get("type") if isinstance(st, Mapping) else getattr(st, "type", None)
                    step_types.append(str(st_type or type(st).__name__)[:40])
            self.progress_fn(
                {
                    "role": role,
                    "interaction_id": interaction_id,
                    "environment_id": environment_id,
                    "status": status,
                    "elapsed_s": round(max(0.0, float(self.time_fn()) - t0), 2),
                    "step_types": step_types[-20:],
                    "step_count": len(step_types),
                }
            )
        except Exception:
            pass

    def _ensure_client(self) -> Any:
        if self.client is None:
            self.client = build_genai_client(resolve_auth_config())
        return self.client

    def _execute_role(
        self,
        *,
        role: str,
        expected_filename: str,
        agents_md_text: str,
        prompt_text: str,
        public_state_payload: Mapping[str, Any],
        named_images: Sequence[Tuple[str, Union[Path, str, bytes]]],
    ) -> Tuple[Optional[Dict[str, Any]], Optional[bytes], RoleProvenance]:
        _scan_for_private_leaks(dict(public_state_payload), f"{role}_public_state")
        _scan_for_private_leaks(prompt_text, f"{role}_prompt")

        image_manifest: List[Dict[str, Any]] = []
        ordered_png_parts: List[Dict[str, str]] = []
        for img_name, img_src in named_images:
            entry, raw_bytes = _build_image_manifest_entry(img_name, img_src)
            image_manifest.append(entry)
            ordered_png_parts.append(_validate_and_encode_png(raw_bytes))

        agents_sha = sha256_text(agents_md_text)
        public_state_str = json.dumps(dict(public_state_payload), indent=2, sort_keys=True) + "\n"
        public_state_sha = sha256_text(public_state_str)

        safe_inline_public_sources = [
            {
                "type": "inline",
                "target": "AGENTS.md",
                "encoding": "utf-8",
                "content": agents_md_text,
            },
            {
                "type": "inline",
                "target": "public_state.json",
                "encoding": "utf-8",
                "content": public_state_str,
            },
        ]
        mounted_files = [
            {"target": "AGENTS.md", "sha256": agents_sha},
            {"target": "public_state.json", "sha256": public_state_sha},
        ]

        client = self._ensure_client()
        t0 = float(self.time_fn())
        created = client.interactions.create(
            agent=self.agent_name,
            input=[{"type": "text", "text": prompt_text}, *ordered_png_parts],
            environment={"type": "remote", "sources": safe_inline_public_sources},
            background=True,
            store=True,
            agent_config={
                "type": "antigravity",
                "model": self.model_id,
                "max_total_tokens": int(self.role_token_cap),
            },
            tools=[{"type": "code_execution"}],
        )

        interaction_id = str(
            _extract_inter_field(created, "id")
            or _extract_inter_field(created, "interaction_id")
            or f"inter-{uuid.uuid4().hex[:10]}"
        )
        environment_id = str(
            _extract_inter_field(created, "environment_id")
            or f"env-{interaction_id}"
        )

        latest = created
        status = str(_extract_inter_field(created, "status", "in_progress")).lower()
        if status == "canceled":
            status = "cancelled"

        self._report_progress(role, interaction_id, environment_id, status, t0, created)
        timed_out = False
        while status not in TERMINAL_STATUSES:
            if float(self.time_fn()) - t0 >= self.poll_timeout_s:
                timed_out = True
                try:
                    client.interactions.cancel(id=interaction_id)
                except Exception:
                    pass
                status = "timeout"
                break
            self.sleep_fn(self.poll_interval_s)
            latest = client.interactions.get(id=interaction_id)
            env_from_poll = _extract_inter_field(latest, "environment_id")
            if env_from_poll:
                environment_id = str(env_from_poll)
            status = str(_extract_inter_field(latest, "status", "in_progress")).lower()
            if status == "canceled":
                status = "cancelled"
            self._report_progress(role, interaction_id, environment_id, status, t0, latest)

        env_from_final = _extract_inter_field(latest, "environment_id")
        if env_from_final:
            environment_id = str(env_from_final)

        elapsed_s = round(max(0.0, float(self.time_fn()) - t0), 4)
        usage_snap = normalize_usage(_extract_inter_field(latest, "usage"))
        agent_cfg_obj = _extract_inter_field(latest, "agent_config")
        returned_model = str(
            _extract_inter_field(latest, "model")
            or _extract_inter_field(agent_cfg_obj, "model")
            or self.model_id
        )
        code_exec_ok = _has_code_execution_step(latest)
        steps = extract_interaction_steps(latest)

        raw_json: Optional[Dict[str, Any]] = None
        raw_bytes: Optional[bytes] = None
        retrieved_sha: Optional[str] = None
        retrieved_path: Optional[str] = None
        err_msg: Optional[str] = "Interaction timed out" if timed_out else None

        if status == "completed" and not timed_out:
            try:
                raw_json, raw_bytes, retrieved_sha, retrieved_path = retrieve_environment_file(
                    client=client,
                    environment_id=environment_id,
                    expected_filename=expected_filename,
                )
            except Exception as exc:
                err_msg = f"missing_remote_output: {exc.__class__.__name__}: {exc}"
                status = "missing_remote_output"

        argon_verified = bool(
            status == "completed"
            and "gemini-4-argon" in returned_model.lower()
            and raw_json is not None
        )

        prov = RoleProvenance(
            role=role,
            source="live_provider",
            requested_agent=self.agent_name,
            requested_model=self.model_id,
            returned_model=returned_model,
            argon_verified=argon_verified,
            interaction_id=interaction_id,
            environment_id=environment_id,
            status=status,
            native_image_input_sent=len(ordered_png_parts) > 0,
            image_manifest=image_manifest,
            agents_md_sha256=agents_sha,
            mounted_files=mounted_files,
            retrieved_file_path=retrieved_path,
            retrieved_sha256=retrieved_sha,
            elapsed_s=elapsed_s,
            usage=usage_snap.to_dict(),
            code_execution_observed=code_exec_ok or (raw_json is not None),
            error=err_msg,
            steps=steps,
        )
        return raw_json, raw_bytes, prov

    def run_builder(
        self,
        *,
        old_sld_path: Path,
        old_thermal_path: Path,
        public_state: Mapping[str, Any],
    ) -> Tuple[Optional[Dict[str, Any]], Optional[bytes], RoleProvenance]:
        base_prompt = load_role_prompt_template("builder")
        named_imgs: List[Tuple[str, Union[Path, str, bytes]]] = [
            ("old_sld_A.png", old_sld_path),
            ("old_thermal_rear.png", old_thermal_path),
        ]
        manifest_entries = [_build_image_manifest_entry(n, p)[0] for n, p in named_imgs]
        prompt = _format_prompt_with_manifest(
            base_prompt,
            manifest_entries,
            {"case_id": public_state.get("case_id", "drift_A_to_B"), "source_revision": "A"},
        )
        return self._execute_role(
            role="builder",
            expected_filename="scene_spec.json",
            agents_md_text=build_builder_agents_md(),
            prompt_text=prompt,
            public_state_payload=public_state,
            named_images=named_imgs,
        )

    def run_critic(
        self,
        *,
        current_sld_path: Path,
        render0_wiring_path: Path,
        render0_thermal_path: Path,
        public_state: Mapping[str, Any],
        basis_scene_sha256: str,
        render0_meta: Optional[Mapping[str, Any]] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[bytes], RoleProvenance]:
        sld_name = Path(current_sld_path).name
        # Judge-created challenges use a neutral filename and a prompt without revision hints.
        base_prompt = load_role_prompt_template("critic_neutral" if sld_name == "current_sld.png" else "critic")
        named_imgs: List[Tuple[str, Union[Path, str, bytes]]] = [
            (sld_name, current_sld_path),
            ("render0_wiring.png", render0_wiring_path),
            ("render0_thermal_rear.png", render0_thermal_path),
        ]
        manifest_entries = [_build_image_manifest_entry(n, p)[0] for n, p in named_imgs]
        critic_public_state = dict(public_state)
        critic_public_state["basis_scene_sha256"] = basis_scene_sha256
        critic_public_state["current_sld_image"] = sld_name
        prompt = _format_prompt_with_manifest(
            base_prompt,
            manifest_entries,
            {
                "case_id": public_state.get("case_id", "drift_A_to_B"),
                "basis_scene_sha256": basis_scene_sha256,
                "current_sld_image": sld_name,
            },
        )
        return self._execute_role(
            role="critic",
            expected_filename="repair_patch.json",
            agents_md_text=build_critic_agents_md(),
            prompt_text=prompt,
            public_state_payload=critic_public_state,
            named_images=named_imgs,
        )


def _corridor_interior_bbox(corridor: Any, fallback: List[int]) -> List[int]:
    """Extract a valid [x0, y0, x1, y1] bounding box inside a projected 3D cable corridor."""
    if isinstance(corridor, Mapping):
        raw_box = (
            corridor.get("bbox_xyxy_px")
            or corridor.get("bbox")
            or corridor.get("corridor_bbox_xyxy_px")
        )
        if isinstance(raw_box, (list, tuple)) and len(raw_box) == 4:
            x0, y0, x1, y1 = [int(round(float(v))) for v in raw_box]
            x0 = max(0, min(1276, x0))
            y0 = max(0, min(796, y0))
            x1 = max(x0 + 4, min(1280, x1))
            y1 = max(y0 + 4, min(800, y1))
            return [x0, y0, x1, y1]
    elif isinstance(corridor, (list, tuple)) and len(corridor) == 4:
        x0, y0, x1, y1 = [int(round(float(v))) for v in corridor]
        return [max(0, x0), max(0, y0), max(x0 + 4, min(1280, x1)), max(y0 + 4, min(800, y1))]
    return list(fallback)


class MockTwinRepairProvider:
    """Deterministic local test provider (`source="fixture"`, `argon_verified=False`).

    Supports modes:
    - `"normal"` (or `"golden"`): builds valid `scene_spec.v1` for Revision A and
      produces either `decision="repair"` (for `drift_A_to_B`) or `decision="no_change"`
      (for `control_A_to_A`).
    - `"abstain"`: Critic returns `decision="abstain"` with empty operations.
    - `"timeout"`: Critic times out (`status="timeout"`).
    - `"missing_remote_output"`: Critic completes without writing `/workspace/repair_patch.json`.
    - `"same_environment_id"`: Critic reuses Builder's `environment_id` (`env-mock-builder-001`).
    - `"invalid_patch"`: Critic emits an invalid operation (`to="F99"`).
    - `"forged_hash"`: Critic emits a patch with a forged `basis_scene_sha256`.
    """

    SUPPORTED_MODES: frozenset[str] = frozenset(
        {
            "normal",
            "golden",
            "abstain",
            "timeout",
            "missing_remote_output",
            "same_environment_id",
            "invalid_patch",
            "forged_hash",
        }
    )

    def __init__(
        self,
        mode: str = "normal",
        *,
        builder_env_id: str = "env-mock-builder-001",
        critic_env_id: str = "env-mock-critic-002",
    ) -> None:
        if mode not in self.SUPPORTED_MODES:
            raise ValueError(f"Unsupported MockTwinRepairProvider mode={mode!r}; expected one of {sorted(self.SUPPORTED_MODES)}")
        self.mode = mode
        self.builder_env_id = builder_env_id
        self.critic_env_id = builder_env_id if mode == "same_environment_id" else critic_env_id

    def run_builder(
        self,
        *,
        old_sld_path: Path,
        old_thermal_path: Path,
        public_state: Mapping[str, Any],
    ) -> Tuple[Optional[Dict[str, Any]], Optional[bytes], RoleProvenance]:
        _scan_for_private_leaks(dict(public_state), "mock_builder_public_state")
        agents_md = build_builder_agents_md()
        agents_sha = sha256_text(agents_md)
        pub_str = json.dumps(dict(public_state), indent=2, sort_keys=True) + "\n"

        named_imgs = [
            ("old_sld_A.png", old_sld_path),
            ("old_thermal_rear.png", old_thermal_path),
        ]
        image_manifest = [_build_image_manifest_entry(n, p)[0] for n, p in named_imgs]

        case_id = str(public_state.get("case_id", "drift_A_to_B"))
        scene_spec: Dict[str, Any] = {
            "schema_version": "scene_spec.v1",
            "case_id": case_id,
            "source_revision": "A",
            "coordinate_system": "meters; floor=y0; rear=positive_z",
            "viewport": {
                "width_px": 1280,
                "height_px": 800,
                "camera_pose": "rear_inspection",
            },
            "racks": [
                {
                    "rack_id": "N1",
                    "center_x_m": 0.0,
                    "width_m": 0.6,
                    "height_m": 2.0,
                    "depth_m": 1.0,
                    "connector_side": "rear",
                    "body_temp_c": 29.5,
                    "connector_temp_c": 36.0,
                },
                {
                    "rack_id": "N2",
                    "center_x_m": 0.82,
                    "width_m": 0.6,
                    "height_m": 2.0,
                    "depth_m": 1.0,
                    "connector_side": "rear",
                    "body_temp_c": 26.5,
                    "connector_temp_c": 25.0,
                },
                {
                    "rack_id": "N3",
                    "center_x_m": 1.64,
                    "width_m": 0.6,
                    "height_m": 2.0,
                    "depth_m": 1.0,
                    "connector_side": "rear",
                    "body_temp_c": 26.5,
                    "connector_temp_c": 25.0,
                },
                {
                    "rack_id": "N4",
                    "center_x_m": 2.46,
                    "width_m": 0.6,
                    "height_m": 2.0,
                    "depth_m": 1.0,
                    "connector_side": "rear",
                    "body_temp_c": 38.0,
                    "connector_temp_c": 88.95030198217694,
                },
            ],
            "feeders": [
                {
                    "feeder_id": "F1",
                    "anchor_x_m": -0.45,
                    "anchor_y_m": 2.24,
                    "limit_a": 44.0,
                    "style": "blue",
                },
                {
                    "feeder_id": "F2",
                    "anchor_x_m": -0.45,
                    "anchor_y_m": 2.02,
                    "limit_a": 50.0,
                    "style": "orange",
                },
            ],
            "edges": [
                {"rack_id": "N1", "feeder_id": "F1"},
                {"rack_id": "N2", "feeder_id": "F1"},
                {"rack_id": "N3", "feeder_id": "F2"},
                {"rack_id": "N4", "feeder_id": "F2"},
            ],
            "hotspot": {
                "rack_id": "N4",
                "surface": "rear_connector",
                "temperature_c": 88.95030198217694,
                "sigma_m": 0.062,
            },
            "notes": ["inferred from old revision A and thermal input"],
        }

        raw_bytes = (json.dumps(scene_spec, indent=2, sort_keys=True) + "\n").encode("utf-8")
        sha_hex = hashlib.sha256(raw_bytes).hexdigest()
        prov = RoleProvenance(
            role="builder",
            source="fixture",
            requested_agent=DEFAULT_MANAGED_AGENT_MODEL,
            requested_model=DEFAULT_FOUNDATION_MODEL,
            returned_model="mock-fixture-not-argon",
            argon_verified=False,
            interaction_id=f"mock-builder-{uuid.uuid4().hex[:8]}",
            environment_id=self.builder_env_id,
            status="completed",
            native_image_input_sent=True,
            image_manifest=image_manifest,
            agents_md_sha256=agents_sha,
            mounted_files=[
                {"target": "AGENTS.md", "sha256": agents_sha},
                {"target": "public_state.json", "sha256": sha256_text(pub_str)},
            ],
            retrieved_file_path="/workspace/scene_spec.json",
            retrieved_sha256=sha_hex,
            elapsed_s=0.015,
            usage={"noncached_tokens": 0, "cached_tokens": 0, "total_tokens": 0, "unknown_usage": False},
            code_execution_observed=True,
            error=None,
            steps=[{"step_index": 0, "type": "code_execution", "name": "write_scene_spec"}],
        )
        return scene_spec, raw_bytes, prov

    def run_critic(
        self,
        *,
        current_sld_path: Path,
        render0_wiring_path: Path,
        render0_thermal_path: Path,
        public_state: Mapping[str, Any],
        basis_scene_sha256: str,
        render0_meta: Optional[Mapping[str, Any]] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[bytes], RoleProvenance]:
        _scan_for_private_leaks(dict(public_state), "mock_critic_public_state")
        agents_md = build_critic_agents_md()
        agents_sha = sha256_text(agents_md)
        pub_str = json.dumps(dict(public_state), indent=2, sort_keys=True) + "\n"

        sld_name = Path(current_sld_path).name
        named_imgs = [
            (sld_name, current_sld_path),
            ("render0_wiring.png", render0_wiring_path),
            ("render0_thermal_rear.png", render0_thermal_path),
        ]
        image_manifest = [_build_image_manifest_entry(n, p)[0] for n, p in named_imgs]

        if self.mode == "timeout":
            prov = RoleProvenance(
                role="critic",
                source="fixture",
                requested_agent=DEFAULT_MANAGED_AGENT_MODEL,
                requested_model=DEFAULT_FOUNDATION_MODEL,
                returned_model="mock-fixture-not-argon",
                argon_verified=False,
                interaction_id=f"mock-critic-{uuid.uuid4().hex[:8]}",
                environment_id=self.critic_env_id,
                status="timeout",
                native_image_input_sent=True,
                image_manifest=image_manifest,
                agents_md_sha256=agents_sha,
                mounted_files=[
                    {"target": "AGENTS.md", "sha256": agents_sha},
                    {"target": "public_state.json", "sha256": sha256_text(pub_str)},
                ],
                retrieved_file_path=None,
                retrieved_sha256=None,
                elapsed_s=150.0,
                usage={"noncached_tokens": 30000, "cached_tokens": 0, "total_tokens": 30000, "unknown_usage": True},
                code_execution_observed=False,
                error="Interaction timed out waiting for Critic completion",
                steps=[],
            )
            return None, None, prov

        if self.mode == "missing_remote_output":
            prov = RoleProvenance(
                role="critic",
                source="fixture",
                requested_agent=DEFAULT_MANAGED_AGENT_MODEL,
                requested_model=DEFAULT_FOUNDATION_MODEL,
                returned_model="mock-fixture-not-argon",
                argon_verified=False,
                interaction_id=f"mock-critic-{uuid.uuid4().hex[:8]}",
                environment_id=self.critic_env_id,
                status="missing_remote_output",
                native_image_input_sent=True,
                image_manifest=image_manifest,
                agents_md_sha256=agents_sha,
                mounted_files=[
                    {"target": "AGENTS.md", "sha256": agents_sha},
                    {"target": "public_state.json", "sha256": sha256_text(pub_str)},
                ],
                retrieved_file_path=None,
                retrieved_sha256=None,
                elapsed_s=0.02,
                usage={"noncached_tokens": 0, "cached_tokens": 0, "total_tokens": 0, "unknown_usage": False},
                code_execution_observed=False,
                error="missing_remote_output: /workspace/repair_patch.json not found in sandbox",
                steps=[],
            )
            return None, None, prov

        # Extract projected cable corridors from render0_meta if available
        corridors = dict((render0_meta or {}).get("cable_corridors") or (render0_meta or {}).get("projected_cable_corridors") or {})
        n1_render_bbox = _corridor_interior_bbox(corridors.get("N1"), [150, 140, 380, 440])
        n2_render_bbox = _corridor_interior_bbox(corridors.get("N2"), [410, 150, 620, 420])
        n3_render_bbox = _corridor_interior_bbox(corridors.get("N3"), [660, 150, 880, 420])
        n4_render_bbox = _corridor_interior_bbox(corridors.get("N4"), [900, 150, 1140, 420])

        # SLD branch regions (1280x800 SLD layout matching DEFAULT_SLD_BRANCH_BBOXES_PX)
        sld_regions = dict(public_state.get("sld_branch_bboxes") or public_state.get("sld_branch_bboxes_px") or {})
        n1_sld_bbox = _corridor_interior_bbox(sld_regions.get("N1"), [160, 200, 240, 420])
        n2_sld_bbox = _corridor_interior_bbox(sld_regions.get("N2"), [420, 200, 500, 420])
        n3_sld_bbox = _corridor_interior_bbox(sld_regions.get("N3"), [680, 200, 760, 420])
        n4_sld_bbox = _corridor_interior_bbox(sld_regions.get("N4"), [940, 200, 1020, 420])

        case_id = str(public_state.get("case_id", "drift_A_to_B"))
        rev_curr = str(public_state.get("revision_current") or ("A" if "control" in case_id.lower() or sld_name.endswith("_A.png") else "B"))
        is_control = rev_curr == "A" or "control" in case_id.lower()

        active_sha = ("0" * 64) if self.mode == "forged_hash" else basis_scene_sha256

        if self.mode == "abstain":
            patch: Dict[str, Any] = {
                "schema_version": "repair_patch.v1",
                "case_id": case_id,
                "basis_scene_sha256": active_sha,
                "target_revision_seen": rev_curr,
                "observed_render_edges": [],
                "target_sld_edges": [],
                "discrepancies": [],
                "operations": [],
                "confidence": 0.15,
                "decision": "abstain",
                "abstain_reason": "Simulated visual ambiguity in feeder corridor inspection; abstaining fail-closed.",
            }
        elif is_control:
            patch = {
                "schema_version": "repair_patch.v1",
                "case_id": case_id,
                "basis_scene_sha256": active_sha,
                "target_revision_seen": "A",
                "observed_render_edges": [
                    {"rack_id": "N1", "feeder_id": "F1", "image_ref": "render0_wiring.png", "bbox_xyxy_px": n1_render_bbox},
                    {"rack_id": "N2", "feeder_id": "F1", "image_ref": "render0_wiring.png", "bbox_xyxy_px": n2_render_bbox},
                    {"rack_id": "N3", "feeder_id": "F2", "image_ref": "render0_wiring.png", "bbox_xyxy_px": n3_render_bbox},
                    {"rack_id": "N4", "feeder_id": "F2", "image_ref": "render0_wiring.png", "bbox_xyxy_px": n4_render_bbox},
                ],
                "target_sld_edges": [
                    {"rack_id": "N1", "feeder_id": "F1", "image_ref": sld_name, "bbox_xyxy_px": n1_sld_bbox},
                    {"rack_id": "N2", "feeder_id": "F1", "image_ref": sld_name, "bbox_xyxy_px": n2_sld_bbox},
                    {"rack_id": "N3", "feeder_id": "F2", "image_ref": sld_name, "bbox_xyxy_px": n3_sld_bbox},
                    {"rack_id": "N4", "feeder_id": "F2", "image_ref": sld_name, "bbox_xyxy_px": n4_sld_bbox},
                ],
                "discrepancies": [],
                "operations": [],
                "confidence": 0.96,
                "decision": "no_change",
            }
        else:
            patch = {
                "schema_version": "repair_patch.v1",
                "case_id": case_id,
                "basis_scene_sha256": active_sha,
                "target_revision_seen": "B",
                "observed_render_edges": [
                    {"rack_id": "N1", "feeder_id": "F1", "image_ref": "render0_wiring.png", "bbox_xyxy_px": n1_render_bbox},
                    {"rack_id": "N2", "feeder_id": "F1", "image_ref": "render0_wiring.png", "bbox_xyxy_px": n2_render_bbox},
                    {"rack_id": "N3", "feeder_id": "F2", "image_ref": "render0_wiring.png", "bbox_xyxy_px": n3_render_bbox},
                    {"rack_id": "N4", "feeder_id": "F2", "image_ref": "render0_wiring.png", "bbox_xyxy_px": n4_render_bbox},
                ],
                "target_sld_edges": [
                    {"rack_id": "N1", "feeder_id": "F1", "image_ref": sld_name, "bbox_xyxy_px": n1_sld_bbox},
                    {"rack_id": "N2", "feeder_id": "F2", "image_ref": sld_name, "bbox_xyxy_px": n2_sld_bbox},
                    {"rack_id": "N3", "feeder_id": "F1", "image_ref": sld_name, "bbox_xyxy_px": n3_sld_bbox},
                    {"rack_id": "N4", "feeder_id": "F2", "image_ref": sld_name, "bbox_xyxy_px": n4_sld_bbox},
                ],
                "discrepancies": [
                    {
                        "rack_id": "N2",
                        "observed_feeder": "F1",
                        "required_feeder": "F2",
                        "render_evidence_bbox_xyxy_px": n2_render_bbox,
                        "sld_evidence_bbox_xyxy_px": n2_sld_bbox,
                        "reason": "The cable visibly reaches F1 blue bus in render0_wiring.png, but current_sld_B.png routes N2 to F2.",
                    },
                    {
                        "rack_id": "N3",
                        "observed_feeder": "F2",
                        "required_feeder": "F1",
                        "render_evidence_bbox_xyxy_px": n3_render_bbox,
                        "sld_evidence_bbox_xyxy_px": n3_sld_bbox,
                        "reason": "The cable visibly reaches F2 orange bus in render0_wiring.png, but current_sld_B.png routes N3 to F1.",
                    },
                ],
                "operations": [
                    {"op": "replace_feeder", "rack_id": "N2", "from": "F1", "to": "F2"},
                    {"op": "replace_feeder", "rack_id": "N3", "from": "F2", "to": "F1"},
                ],
                "confidence": 0.94,
                "decision": "repair",
            }
            if self.mode == "invalid_patch":
                patch["operations"][0]["to"] = "F99"

        raw_bytes = (json.dumps(patch, indent=2, sort_keys=True) + "\n").encode("utf-8")
        sha_hex = hashlib.sha256(raw_bytes).hexdigest()
        prov = RoleProvenance(
            role="critic",
            source="fixture",
            requested_agent=DEFAULT_MANAGED_AGENT_MODEL,
            requested_model=DEFAULT_FOUNDATION_MODEL,
            returned_model="mock-fixture-not-argon",
            argon_verified=False,
            interaction_id=f"mock-critic-{uuid.uuid4().hex[:8]}",
            environment_id=self.critic_env_id,
            status="completed",
            native_image_input_sent=True,
            image_manifest=image_manifest,
            agents_md_sha256=agents_sha,
            mounted_files=[
                {"target": "AGENTS.md", "sha256": agents_sha},
                {"target": "public_state.json", "sha256": sha256_text(pub_str)},
            ],
            retrieved_file_path="/workspace/repair_patch.json",
            retrieved_sha256=sha_hex,
            elapsed_s=0.018,
            usage={"noncached_tokens": 0, "cached_tokens": 0, "total_tokens": 0, "unknown_usage": False},
            code_execution_observed=True,
            error=None,
            steps=[{"step_index": 0, "type": "code_execution", "name": "write_repair_patch"}],
        )
        return copy.deepcopy(patch), raw_bytes, prov
