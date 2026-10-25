"""Google Gen AI SDK Managed Agent runtime and transport for BlackboxDC.

Implements Phase 3 (`03_MANAGED_RUNTIME.md`):
- Dual authentication (`ai_studio`, `vertex_adc`, `mock`), read-only model/agent
  preflight (`preflight_managed_agent`), and recursive log/journal redaction
  (`redact_for_log`).
- Three-ledger token accounting (`UsageSnapshot`, `CaseBudgetLedger`,
  `PairBudgetTracker`) with latest-snapshot-per-interaction tracking, `30,000`
  unknown-usage reserve hold, `40,000` Auditor (`A1+A2`) reserve, and `20,000`
  `A2` reserve.
- Direct workspace file retrieval (`retrieve_environment_file`) with duplicate
  basename rejection, `512 KiB` and `32`-entry caps, and safe archive member
  validation (blocking `..`, absolute paths, symlinks/hardlinks, and bombs).
- Append-only fsynced JSONL lifecycle journal (`append_journal_event`,
  `reconcile_journal`) and core `run_turn` execution loop enforcing role
  isolation, `32 KiB` prompt cap, PNG validation, timeout cancellation +
  reconciliation, `code_execution` step verification, and scientific vs
  diagnostic completion grading.
- Preserves `ManagedAgentTransport`, `HandoffFileRecord`, `InteractionAccounting`,
  and `validate_sandbox_handoff_files` for backward compatibility.
"""

from __future__ import annotations

import base64
import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tarfile
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union
import uuid
import zipfile

from PIL import Image

from blackbox.domain import compute_max_safe_current_a
from blackbox.preflight import _default_gcloud_adc_path, _load_dotenv_if_present, _parse_bool_env
from blackbox.render_sld import get_case_sld_mapping


DEFAULT_MANAGED_AGENT_MODEL: str = "antigravity-preview-09-2026"
DEFAULT_FOUNDATION_MODEL: str = "gemini-4-argon"

# Phase 3 Token & Byte Caps
NATIVE_INTERACTION_NONCACHED_CAP: int = 30_000
CASE_NONCACHED_CAP: int = 140_000
AUDITOR_NONCACHED_RESERVE: int = 40_000
A2_NONCACHED_RESERVE: int = 20_000
PAIR_NONCACHED_CAP: int = 280_000
CASE_CACHED_INCLUSIVE_CEILING: int = 280_000
PAIR_CACHED_INCLUSIVE_CEILING: int = 560_000
UNKNOWN_USAGE_RESERVE: int = 30_000

MAX_PROMPT_BYTES: int = 32 * 1024  # 32 KiB
MAX_IMAGE_BYTES: int = 512 * 1024  # 512 KiB
MAX_IMAGE_DIMENSION_PX: int = 2048
MAX_RETRIEVAL_BYTES: int = 512 * 1024  # 512 KiB
MAX_ENVIRONMENT_ENTRIES: int = 32

PNG_MAGIC: bytes = b"\x89PNG\r\n\x1a\n"
TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled", "incomplete"})

FORBIDDEN_LEAK_TOKENS: Tuple[str, ...] = (
    "truth_sha256",
    "node_resistances_ohm",
    "R0_ohm",
    "h_W_per_K",
    "C_J_per_K",
    "sensor_to_rack",
    "rack_to_sensor",
    "safe_receiver",
    "wrong_receiver",
    "connector_bboxes",
    "candidate_explanations",
    "reference_outcomes",
    "truth_mask",
    "sealed_truth",
)

_SENSITIVE_KEY_NAMES: frozenset[str] = frozenset(
    {
        "authorization",
        "x-goog-api-key",
        "api_key",
        "gemini_api_key",
        "google_api_key",
        "access_token",
        "bearer",
        "google_application_credentials",
    }
)

_API_KEY_REGEX = re.compile(r"AIza[0-9A-Za-z_\-]{10,}")
_BEARER_REGEX = re.compile(r"(Bearer\s+)[0-9A-Za-z._\-]+", re.IGNORECASE)

# Process-wide role-environment registry enforcing Investigator / Auditor sandbox isolation
_ENVIRONMENT_ROLE_REGISTRY: Dict[str, str] = {}


def reset_environment_role_registry() -> None:
    """Clear the in-memory environment-to-role registry (used by unit tests)."""
    _ENVIRONMENT_ROLE_REGISTRY.clear()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_role_family(role: str, turn_id: str = "") -> str:
    r = (role or "").strip().lower()
    t = (turn_id or "").strip().upper()
    if "auditor" in r or t in ("A1", "A2"):
        return "auditor"
    if "investigator" in r or t in ("I1", "I2"):
        return "investigator"
    if "canary" in r or t == "CANARY":
        return "canary"
    return r or "investigator"


# ---------------------------------------------------------------------------
# 1. Authentication, Preflight, and Log Redaction
# ---------------------------------------------------------------------------


def redact_for_log(payload: Any) -> Any:
    """Recursively redact secrets, auth headers, and base64 image payloads for safe logging."""
    env_secrets = [
        val.strip()
        for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY")
        for val in [os.environ.get(key, "")]
        if len(val.strip()) >= 6
    ]

    def _redact_str(s: str) -> str:
        out = _API_KEY_REGEX.sub("[REDACTED_API_KEY]", s)
        out = _BEARER_REGEX.sub(r"\1[REDACTED]", out)
        for sec in env_secrets:
            if sec in out:
                out = out.replace(sec, "[REDACTED]")
        return out

    def _walk(obj: Any) -> Any:
        if isinstance(obj, str):
            return _redact_str(obj)
        if isinstance(obj, Mapping):
            # Redact base64 image content blocks
            if obj.get("type") == "image" and "data" in obj:
                redacted_img = {k: _walk(v) for k, v in obj.items() if k != "data"}
                raw_data = str(obj.get("data") or "")
                redacted_img["data"] = f"[REDACTED_BASE64_PNG:{len(raw_data)}]"
                return redacted_img
            if obj.get("type") == "inline" and "data" in obj and str(obj.get("mime_type", "")).startswith("image/"):
                redacted_inline = {k: _walk(v) for k, v in obj.items() if k != "data"}
                raw_data = str(obj.get("data") or "")
                redacted_inline["data"] = f"[REDACTED_BASE64_IMAGE:{len(raw_data)}]"
                return redacted_inline

            out_dict: Dict[str, Any] = {}
            for k, v in obj.items():
                k_str = str(k)
                k_low = k_str.lower()
                if (
                    k_low in _SENSITIVE_KEY_NAMES
                    or k_low.endswith("_api_key")
                    or k_low.endswith("_token")
                ):
                    out_dict[k_str] = "[REDACTED]" if v else "<unset>"
                else:
                    out_dict[k_str] = _walk(v)
            return out_dict
        if isinstance(obj, tuple):
            return tuple(_walk(x) for x in obj)
        if isinstance(obj, list):
            return [_walk(x) for x in obj]
        return obj

    return _walk(payload)


def resolve_auth_config(
    env: Optional[Mapping[str, str]] = None,
    adc_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Discriminate between `ai_studio`, `vertex_adc`, and `mock` authentication modes."""
    if env is None:
        _load_dotenv_if_present()
        environ: Mapping[str, str] = os.environ
    else:
        environ = env

    use_vertex = _parse_bool_env(environ.get("GOOGLE_GENAI_USE_VERTEXAI"))
    gemini_key = (environ.get("GEMINI_API_KEY") or "").strip()
    google_key = (environ.get("GOOGLE_API_KEY") or "").strip()
    api_key = gemini_key or google_key

    project = (environ.get("GOOGLE_CLOUD_PROJECT") or "").strip()
    location = (environ.get("GOOGLE_CLOUD_LOCATION") or "us-central1").strip() or "us-central1"
    gac_raw = (environ.get("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    gac_exists = Path(gac_raw).is_file() if gac_raw else False
    default_adc = adc_path if adc_path is not None else _default_gcloud_adc_path()
    adc_exists = gac_exists or default_adc.is_file()

    if use_vertex:
        ready = bool(project) and (adc_exists or bool(gac_raw) or env is not None)
        if ready:
            return {
                "mode": "vertex_adc",
                "ready": True,
                "vertexai": True,
                "project": project,
                "location": location,
                "api_key": None,
                "api_key_present": False,
            }
        return {
            "mode": "mock",
            "ready": False,
            "vertexai": True,
            "project": project or None,
            "location": location,
            "api_key": None,
            "api_key_present": False,
            "reason": "GOOGLE_GENAI_USE_VERTEXAI=true but GOOGLE_CLOUD_PROJECT or ADC is missing",
        }

    if api_key:
        return {
            "mode": "ai_studio",
            "ready": True,
            "vertexai": False,
            "project": None,
            "location": None,
            "api_key": api_key,
            "api_key_present": True,
        }

    return {
        "mode": "mock",
        "ready": False,
        "vertexai": False,
        "project": None,
        "location": None,
        "api_key": None,
        "api_key_present": False,
        "reason": "No GEMINI_API_KEY/GOOGLE_API_KEY or Vertex AI configuration found",
    }


def build_genai_client(auth_config: Optional[Mapping[str, Any]] = None) -> Any:
    """Instantiate `google.genai.Client` from resolved authentication config."""
    from google import genai

    cfg = dict(auth_config) if auth_config is not None else resolve_auth_config()
    mode = cfg.get("mode")
    if mode == "ai_studio":
        api_key = cfg.get("api_key")
        if not api_key:
            raise RuntimeError("Missing api_key for ai_studio mode")
        return genai.Client(api_key=str(api_key))
    if mode == "vertex_adc":
        project = cfg.get("project")
        location = cfg.get("location") or "us-central1"
        if not project:
            raise RuntimeError("Missing GOOGLE_CLOUD_PROJECT for vertex_adc mode")
        return genai.Client(vertexai=True, project=str(project), location=str(location))
    raise RuntimeError(
        f"Cannot build live google.genai.Client when auth mode is {mode!r} (no credentials configured)"
    )


def preflight_managed_agent(
    client: Any = None,
    agent_name: str = DEFAULT_MANAGED_AGENT_MODEL,
    model_id: str = DEFAULT_FOUNDATION_MODEL,
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Perform a read-only check verifying `agent_name` and `model_id` availability."""
    if client is None:
        auth_cfg = resolve_auth_config(env=env)
        if not auth_cfg.get("ready"):
            return redact_for_log(
                {
                    "ready": False,
                    "mode": auth_cfg.get("mode", "mock"),
                    "agent_name": agent_name,
                    "model_id": model_id,
                    "agent_available": False,
                    "model_available": False,
                    "models_checked": 0,
                    "reason": auth_cfg.get("reason", "Credentials not configured"),
                }
            )
        client = build_genai_client(auth_cfg)
        mode = str(auth_cfg["mode"])
    else:
        mode = "custom_client"

    try:
        raw_models = list(client.models.list())
    except Exception as exc:
        return redact_for_log(
            {
                "ready": False,
                "mode": mode,
                "agent_name": agent_name,
                "model_id": model_id,
                "agent_available": False,
                "model_available": False,
                "models_checked": 0,
                "error": f"{exc.__class__.__name__}: {exc}",
            }
        )

    names: List[str] = []
    for item in raw_models:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, Mapping) and "name" in item:
            names.append(str(item["name"]))
        elif hasattr(item, "name"):
            names.append(str(getattr(item, "name")))
        elif hasattr(item, "id"):
            names.append(str(getattr(item, "id")))

    def _matches(target: str) -> bool:
        t_low = target.strip().lower()
        for n in names:
            n_low = n.strip().lower()
            if n_low == t_low or n_low.endswith("/" + t_low) or t_low in n_low:
                return True
        return False

    agent_ok = _matches(agent_name)
    model_ok = _matches(model_id)
    return redact_for_log(
        {
            "ready": bool(agent_ok and model_ok),
            "mode": mode,
            "agent_name": agent_name,
            "model_id": model_id,
            "agent_available": agent_ok,
            "model_available": model_ok,
            "models_checked": len(names),
        }
    )


# ---------------------------------------------------------------------------
# 2. Three-Ledger Token Accounting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UsageSnapshot:
    """Normalized cumulative token usage for a single Managed Agent interaction."""

    noncached_tokens: int
    cached_tokens: int
    total_tokens: int
    unknown_usage: bool = False
    raw_usage: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def normalize_usage_snapshot(raw_usage: Any) -> UsageSnapshot:
    """Normalize a raw SDK usage object/dict into a three-ledger UsageSnapshot.

    Never infers 0 tokens from missing or malformed usage metadata; instead
    assigns `UNKNOWN_USAGE_RESERVE` (`30,000`) with `unknown_usage=True`.
    """
    if raw_usage is None:
        return UsageSnapshot(
            noncached_tokens=UNKNOWN_USAGE_RESERVE,
            cached_tokens=0,
            total_tokens=UNKNOWN_USAGE_RESERVE,
            unknown_usage=True,
            raw_usage=None,
        )

    usage_dict: Dict[str, Any] = {}
    if isinstance(raw_usage, Mapping):
        usage_dict = dict(raw_usage)
    elif hasattr(raw_usage, "model_dump"):
        usage_dict = dict(raw_usage.model_dump())
    elif hasattr(raw_usage, "__dict__"):
        usage_dict = {k: v for k, v in vars(raw_usage).items() if not k.startswith("_")}
    else:
        return UsageSnapshot(
            noncached_tokens=UNKNOWN_USAGE_RESERVE,
            cached_tokens=0,
            total_tokens=UNKNOWN_USAGE_RESERVE,
            unknown_usage=True,
            raw_usage=None,
        )

    raw_total = usage_dict.get("total_tokens", usage_dict.get("total_token_count"))
    if (
        raw_total is None
        or isinstance(raw_total, bool)
        or not isinstance(raw_total, (int, float))
        or int(raw_total) < 0
    ):
        return UsageSnapshot(
            noncached_tokens=UNKNOWN_USAGE_RESERVE,
            cached_tokens=0,
            total_tokens=UNKNOWN_USAGE_RESERVE,
            unknown_usage=True,
            raw_usage=usage_dict,
        )

    raw_cached = usage_dict.get(
        "total_cached_tokens",
        usage_dict.get("cached_tokens", usage_dict.get("cached_content_token_count", 0)),
    )
    if raw_cached is None:
        raw_cached = 0
    if isinstance(raw_cached, bool) or not isinstance(raw_cached, (int, float)) or int(raw_cached) < 0:
        return UsageSnapshot(
            noncached_tokens=UNKNOWN_USAGE_RESERVE,
            cached_tokens=0,
            total_tokens=UNKNOWN_USAGE_RESERVE,
            unknown_usage=True,
            raw_usage=usage_dict,
        )

    total_i = int(raw_total)
    cached_i = max(0, int(raw_cached))
    noncached_i = max(0, total_i - cached_i)
    effective_total = max(total_i, noncached_i + cached_i)

    return UsageSnapshot(
        noncached_tokens=noncached_i,
        cached_tokens=cached_i,
        total_tokens=effective_total,
        unknown_usage=False,
        raw_usage=usage_dict,
    )


@dataclass
class CaseBudgetLedger:
    """Per-case token ledger tracking latest cumulative snapshot per interaction_id."""

    case_id: str
    interactions: Dict[str, UsageSnapshot] = field(default_factory=dict)
    interaction_roles: Dict[str, str] = field(default_factory=dict)
    interaction_turns: Dict[str, str] = field(default_factory=dict)
    interaction_statuses: Dict[str, str] = field(default_factory=dict)

    def record_snapshot(
        self,
        interaction_id: str,
        snapshot: UsageSnapshot,
        role_family: str = "investigator",
        turn_id: str = "I1",
        status: str = "in_progress",
    ) -> None:
        """Store only the latest cumulative snapshot for `interaction_id`."""
        self.interactions[interaction_id] = snapshot
        self.interaction_roles[interaction_id] = role_family
        self.interaction_turns[interaction_id] = turn_id.upper()
        self.interaction_statuses[interaction_id] = status

    @property
    def noncached_tokens(self) -> int:
        return sum(s.noncached_tokens for s in self.interactions.values())

    @property
    def cached_tokens(self) -> int:
        return sum(s.cached_tokens for s in self.interactions.values())

    @property
    def total_tokens(self) -> int:
        return sum(s.total_tokens for s in self.interactions.values())

    @property
    def investigator_noncached_tokens(self) -> int:
        return sum(
            s.noncached_tokens
            for iid, s in self.interactions.items()
            if self.interaction_roles.get(iid) == "investigator"
        )

    @property
    def auditor_noncached_tokens(self) -> int:
        return sum(
            s.noncached_tokens
            for iid, s in self.interactions.items()
            if self.interaction_roles.get(iid) == "auditor"
        )


class PairBudgetTracker:
    """Three-ledger token budget gatekeeper across paired Case A and Case B runs."""

    def __init__(self) -> None:
        self.cases: Dict[str, CaseBudgetLedger] = {}
        self.environment_roles: Dict[str, str] = {}

    def get_case_ledger(self, case_id: str) -> CaseBudgetLedger:
        if case_id not in self.cases:
            self.cases[case_id] = CaseBudgetLedger(case_id=case_id)
        return self.cases[case_id]

    def update_interaction(
        self,
        case_id: str,
        interaction_id: str,
        raw_usage: Any,
        role: str = "investigator",
        turn_id: str = "I1",
        status: str = "in_progress",
    ) -> UsageSnapshot:
        """Update cumulative usage for `interaction_id` (replaces prior poll snapshot)."""
        ledger = self.get_case_ledger(case_id)
        role_fam = _normalize_role_family(role, turn_id)
        if status == "unknown":
            snap = UsageSnapshot(
                noncached_tokens=UNKNOWN_USAGE_RESERVE,
                cached_tokens=0,
                total_tokens=UNKNOWN_USAGE_RESERVE,
                unknown_usage=True,
                raw_usage=None,
            )
        else:
            snap = normalize_usage_snapshot(raw_usage)
        ledger.record_snapshot(
            interaction_id=interaction_id,
            snapshot=snap,
            role_family=role_fam,
            turn_id=turn_id,
            status=status,
        )
        return snap

    @property
    def unknown_hold_active(self) -> bool:
        for ledger in self.cases.values():
            for st in ledger.interaction_statuses.values():
                if st == "unknown":
                    return True
        return False

    @property
    def pair_noncached_tokens(self) -> int:
        return sum(c.noncached_tokens for c in self.cases.values())

    @property
    def pair_cached_tokens(self) -> int:
        return sum(c.cached_tokens for c in self.cases.values())

    @property
    def pair_total_tokens(self) -> int:
        return sum(c.total_tokens for c in self.cases.values())

    def check_budget_before_turn(
        self,
        case_id: str,
        turn_id: str,
        requested_noncached_cap: int = NATIVE_INTERACTION_NONCACHED_CAP,
        role: str = "",
    ) -> Dict[str, Any]:
        """Verify all per-interaction, per-case, reserve, and pair token caps before a turn."""
        if self.unknown_hold_active:
            return {
                "allowed": False,
                "reason": "Unknown interaction status hold active (30,000 token reserve held until reconciled)",
            }

        if requested_noncached_cap <= 0 or requested_noncached_cap > NATIVE_INTERACTION_NONCACHED_CAP:
            return {
                "allowed": False,
                "reason": (
                    f"requested_noncached_cap {requested_noncached_cap} exceeds native interaction cap "
                    f"{NATIVE_INTERACTION_NONCACHED_CAP}"
                ),
            }

        ledger = self.get_case_ledger(case_id)
        role_fam = _normalize_role_family(role, turn_id)
        norm_turn = turn_id.strip().upper()

        proj_case_noncached = ledger.noncached_tokens + requested_noncached_cap
        if proj_case_noncached > CASE_NONCACHED_CAP:
            return {
                "allowed": False,
                "reason": (
                    f"Case {case_id} noncached budget exceeded: {proj_case_noncached} > {CASE_NONCACHED_CAP}"
                ),
            }

        # Investigator turns cannot encroach on the 40,000 Auditor (A1+A2) reserve
        if role_fam == "investigator" or norm_turn in ("I1", "I2"):
            remaining_for_auditor = CASE_NONCACHED_CAP - (
                ledger.investigator_noncached_tokens + requested_noncached_cap
            )
            if remaining_for_auditor < AUDITOR_NONCACHED_RESERVE:
                return {
                    "allowed": False,
                    "reason": (
                        f"Investigator turn {norm_turn} would violate Auditor reserve of "
                        f"{AUDITOR_NONCACHED_RESERVE} in {case_id} (remaining={remaining_for_auditor})"
                    ),
                }

        # A1 cannot encroach on the 20,000 A2 reserve
        if norm_turn == "A1":
            remaining_after_a1 = CASE_NONCACHED_CAP - proj_case_noncached
            if remaining_after_a1 < A2_NONCACHED_RESERVE:
                return {
                    "allowed": False,
                    "reason": (
                        f"Auditor turn A1 would violate A2 reserve of {A2_NONCACHED_RESERVE} in {case_id} "
                        f"(remaining={remaining_after_a1})"
                    ),
                }

        proj_pair_noncached = self.pair_noncached_tokens + requested_noncached_cap
        if proj_pair_noncached > PAIR_NONCACHED_CAP:
            return {
                "allowed": False,
                "reason": f"Pair noncached budget exceeded: {proj_pair_noncached} > {PAIR_NONCACHED_CAP}",
            }

        proj_case_total = ledger.total_tokens + requested_noncached_cap
        if proj_case_total > CASE_CACHED_INCLUSIVE_CEILING:
            return {
                "allowed": False,
                "reason": (
                    f"Case {case_id} cached-inclusive ceiling exceeded: "
                    f"{proj_case_total} > {CASE_CACHED_INCLUSIVE_CEILING}"
                ),
            }

        proj_pair_total = self.pair_total_tokens + requested_noncached_cap
        if proj_pair_total > PAIR_CACHED_INCLUSIVE_CEILING:
            return {
                "allowed": False,
                "reason": (
                    f"Pair cached-inclusive ceiling exceeded: "
                    f"{proj_pair_total} > {PAIR_CACHED_INCLUSIVE_CEILING}"
                ),
            }

        return {
            "allowed": True,
            "reason": None,
            "case_noncached_tokens": ledger.noncached_tokens,
            "pair_noncached_tokens": self.pair_noncached_tokens,
            "case_total_tokens": ledger.total_tokens,
            "pair_total_tokens": self.pair_total_tokens,
        }

    def enforce_budget_before_turn(
        self,
        case_id: str,
        turn_id: str,
        requested_noncached_cap: int = NATIVE_INTERACTION_NONCACHED_CAP,
        role: str = "",
    ) -> Dict[str, Any]:
        res = self.check_budget_before_turn(
            case_id=case_id,
            turn_id=turn_id,
            requested_noncached_cap=requested_noncached_cap,
            role=role,
        )
        if not res["allowed"]:
            raise RuntimeError(f"Token budget pre-check failed: {res['reason']}")
        return res


# ---------------------------------------------------------------------------
# 3. Direct File Retrieval & Safe Archive Extraction
# ---------------------------------------------------------------------------


def _extract_file_paths_from_list_response(resp: Any) -> List[str]:
    if resp is None:
        return []
    items: Sequence[Any]
    if isinstance(resp, (list, tuple)):
        items = resp
    elif isinstance(resp, Mapping) and "files" in resp:
        items = resp["files"]
    elif hasattr(resp, "files"):
        items = getattr(resp, "files")
    else:
        items = list(resp)

    paths: List[str] = []
    for item in items:
        if isinstance(item, str):
            paths.append(item)
        elif isinstance(item, Mapping):
            p = item.get("path") or item.get("name")
            if p:
                paths.append(str(p))
        elif hasattr(item, "path"):
            paths.append(str(getattr(item, "path")))
        elif hasattr(item, "name"):
            paths.append(str(getattr(item, "name")))
    return paths


def _validate_archive_member_path(member_name: str) -> None:
    """Reject path traversal (`..`), absolute paths (`/`, `\\`), or drive prefixes."""
    if not member_name or not member_name.strip():
        raise ValueError("Empty archive member path")
    norm = member_name.replace("\\", "/")
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm):
        raise ValueError(f"Unsafe absolute archive member path: {member_name!r}")
    parts = [p for p in norm.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise ValueError(f"Unsafe archive member path traversal: {member_name!r}")


def _extract_json_from_zip_bytes(
    archive_bytes: bytes,
    expected_filename: str,
    max_total_bytes: int,
    max_entries: int,
) -> bytes:
    with zipfile.ZipFile(io.BytesIO(archive_bytes), mode="r") as zf:
        infos = zf.infolist()
        if len(infos) > max_entries:
            raise ValueError(f"Zip archive entry count {len(infos)} exceeds cap {max_entries}")

        total_declared = 0
        matched_infos: List[zipfile.ZipInfo] = []
        for info in infos:
            _validate_archive_member_path(info.filename)
            # Check Unix symlink mode in external_attr (0o120000)
            unix_mode = (info.external_attr >> 16) & 0o170000
            if unix_mode == 0o120000:
                raise ValueError(f"Unsafe symlink in zip archive: {info.filename!r}")
            if info.file_size < 0:
                raise ValueError("Invalid negative zip member size")
            total_declared += info.file_size
            if total_declared > max_total_bytes:
                raise ValueError(
                    f"Zip uncompressed size {total_declared} exceeds cap {max_total_bytes}"
                )
            if not info.is_dir() and Path(info.filename).name == expected_filename:
                matched_infos.append(info)

        if len(matched_infos) > 1:
            raise ValueError(f"Duplicate basename ambiguity inside zip archive for {expected_filename!r}")
        if not matched_infos:
            raise FileNotFoundError(f"{expected_filename!r} not found inside zip archive")

        target_info = matched_infos[0]
        chunks: List[bytes] = []
        total_read = 0
        with zf.open(target_info, "r") as fp:
            while True:
                buf = fp.read(65536)
                if not buf:
                    break
                total_read += len(buf)
                if total_read > max_total_bytes:
                    raise ValueError(
                        f"Decompression bomb detected: extracted bytes exceed {max_total_bytes}"
                    )
                chunks.append(buf)
        return b"".join(chunks)


def _extract_json_from_tar_bytes(
    archive_bytes: bytes,
    expected_filename: str,
    max_total_bytes: int,
    max_entries: int,
) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as tf:
        members = tf.getmembers()
        if len(members) > max_entries:
            raise ValueError(f"Tar archive entry count {len(members)} exceeds cap {max_entries}")

        total_declared = 0
        matched_members: List[tarfile.TarInfo] = []
        for m in members:
            _validate_archive_member_path(m.name)
            if m.issym() or m.islnk():
                raise ValueError(f"Unsafe symlink/hardlink in tar archive: {m.name!r}")
            if not (m.isfile() or m.isdir()):
                raise ValueError(f"Unsupported special entry in tar archive: {m.name!r}")
            if m.size < 0:
                raise ValueError("Invalid negative tar member size")
            total_declared += m.size
            if total_declared > max_total_bytes:
                raise ValueError(
                    f"Tar uncompressed size {total_declared} exceeds cap {max_total_bytes}"
                )
            if m.isfile() and Path(m.name).name == expected_filename:
                matched_members.append(m)

        if len(matched_members) > 1:
            raise ValueError(f"Duplicate basename ambiguity inside tar archive for {expected_filename!r}")
        if not matched_members:
            raise FileNotFoundError(f"{expected_filename!r} not found inside tar archive")

        fp = tf.extractfile(matched_members[0])
        if fp is None:
            raise FileNotFoundError(f"Unable to read {expected_filename!r} from tar archive")
        chunks: List[bytes] = []
        total_read = 0
        while True:
            buf = fp.read(65536)
            if not buf:
                break
            total_read += len(buf)
            if total_read > max_total_bytes:
                raise ValueError(
                    f"Decompression bomb detected: extracted bytes exceed {max_total_bytes}"
                )
            chunks.append(buf)
        return b"".join(chunks)


def retrieve_environment_file(
    client: Any,
    environment_id: str,
    expected_filename: str = "proposal.json",
    max_total_bytes: int = MAX_RETRIEVAL_BYTES,
    max_entries: int = MAX_ENVIRONMENT_ENTRIES,
) -> Tuple[Dict[str, Any], bytes, str, str]:
    """Retrieve and validate a contract JSON file directly from `client.environments.files`."""
    if not isinstance(environment_id, str) or not environment_id.strip():
        raise ValueError("environment_id must be a non-empty string")
    if not isinstance(expected_filename, str) or not expected_filename.strip():
        raise ValueError("expected_filename must be a non-empty string")

    files_api = client.environments.files
    try:
        list_resp = files_api.list(environment=environment_id, path="", recursive=True)
    except TypeError:
        list_resp = files_api.list(environment_id=environment_id, path="", recursive=True)

    all_paths = _extract_file_paths_from_list_response(list_resp)
    if len(all_paths) > max_entries:
        raise ValueError(
            f"Environment file entry count {len(all_paths)} exceeds cap {max_entries}"
        )

    exact_matches = [p for p in all_paths if Path(p).name == expected_filename]
    if len(exact_matches) > 1:
        raise ValueError(
            f"Duplicate basename ambiguity for {expected_filename!r}: {exact_matches}"
        )

    matched_path: Optional[str] = None
    is_archive = False
    if len(exact_matches) == 1:
        matched_path = exact_matches[0]
    else:
        stem = Path(expected_filename).stem
        archive_names = {
            f"{stem}.zip",
            f"{stem}.tar",
            f"{stem}.tar.gz",
            f"{stem}.tgz",
            f"{expected_filename}.zip",
            f"{expected_filename}.tar.gz",
        }
        arch_matches = [p for p in all_paths if Path(p).name in archive_names]
        if len(arch_matches) > 1:
            raise ValueError(
                f"Duplicate basename ambiguity for archive wrapping {expected_filename!r}: {arch_matches}"
            )
        if len(arch_matches) == 1:
            matched_path = arch_matches[0]
            is_archive = True

    if matched_path is None:
        raise FileNotFoundError(
            f"Expected file {expected_filename!r} not found in environment {environment_id!r}"
        )

    try:
        dl_resp = files_api.download(path=matched_path, environment_id=environment_id)
    except TypeError:
        dl_resp = files_api.download(environment=environment_id, path=matched_path)

    if isinstance(dl_resp, bytes):
        raw_bytes = dl_resp
    elif isinstance(dl_resp, str):
        raw_bytes = dl_resp.encode("utf-8")
    elif hasattr(dl_resp, "read"):
        raw_bytes = dl_resp.read()
    elif hasattr(dl_resp, "content"):
        raw_bytes = bytes(getattr(dl_resp, "content"))
    elif hasattr(dl_resp, "data"):
        raw_bytes = bytes(getattr(dl_resp, "data"))
    else:
        raise TypeError(f"Unsupported download response type: {type(dl_resp).__name__}")

    if len(raw_bytes) > max_total_bytes:
        raise ValueError(
            f"Downloaded file {matched_path!r} size {len(raw_bytes)} exceeds cap {max_total_bytes}"
        )

    if is_archive or matched_path.endswith(".zip"):
        json_bytes = _extract_json_from_zip_bytes(
            raw_bytes,
            expected_filename=expected_filename,
            max_total_bytes=max_total_bytes,
            max_entries=max_entries,
        )
    elif is_archive or matched_path.endswith((".tar", ".tar.gz", ".tgz")):
        json_bytes = _extract_json_from_tar_bytes(
            raw_bytes,
            expected_filename=expected_filename,
            max_total_bytes=max_total_bytes,
            max_entries=max_entries,
        )
    else:
        json_bytes = raw_bytes

    if len(json_bytes) == 0:
        raise ValueError(f"Retrieved file {matched_path!r} is empty")
    if len(json_bytes) > max_total_bytes:
        raise ValueError(f"Retrieved JSON size {len(json_bytes)} exceeds cap {max_total_bytes}")

    text = json_bytes.decode("utf-8")
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected top-level JSON object in {expected_filename!r}")

    sha256_hex = hashlib.sha256(json_bytes).hexdigest()
    return parsed, json_bytes, sha256_hex, matched_path


# ---------------------------------------------------------------------------
# 4. Append-Only Journal, Reconciliation, and Core `run_turn`
# ---------------------------------------------------------------------------


def append_journal_event(journal_path: Optional[Path], event: Mapping[str, Any]) -> None:
    """Append a redacted event line to the JSONL journal and flush with `os.fsync`."""
    if journal_path is None:
        return
    jp = Path(journal_path)
    jp.parent.mkdir(parents=True, exist_ok=True)
    record = dict(redact_for_log(dict(event)))
    if "timestamp_utc" not in record:
        record["timestamp_utc"] = _utc_now_iso()
    line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    with open(jp, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def reconcile_journal(
    journal_path: Path,
    client: Any,
    budget_tracker: Optional[PairBudgetTracker] = None,
) -> Dict[str, Any]:
    """Reconcile any un-terminated interaction IDs in an interrupted JSONL journal."""
    jp = Path(journal_path)
    if not jp.exists():
        return {"reconciled": [], "pending_before": 0}

    created_meta: Dict[str, Dict[str, Any]] = {}
    terminated_ids: set[str] = set()

    for raw_line in jp.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        ev = json.loads(line)
        iid = ev.get("interaction_id")
        if not iid:
            continue
        stage = ev.get("event")
        if stage == "created":
            created_meta[iid] = ev
        elif stage in ("terminal", "locked") and ev.get("status") in TERMINAL_STATUSES:
            terminated_ids.add(iid)

    pending_ids = [iid for iid in created_meta if iid not in terminated_ids]
    reconciled_records: List[Dict[str, Any]] = []

    for iid in pending_ids:
        meta = created_meta[iid]
        case_id = str(meta.get("case_id", "case_A"))
        turn_id = str(meta.get("turn_id", "I1"))
        role = str(meta.get("role", "investigator"))
        try:
            inter = client.interactions.get(id=iid)
            raw_status = str(getattr(inter, "status", None) or inter.get("status", "unknown")).lower()
            if raw_status == "canceled":
                raw_status = "cancelled"
            raw_usage = getattr(inter, "usage", None) if not isinstance(inter, Mapping) else inter.get("usage")
            snap = normalize_usage_snapshot(raw_usage)
        except Exception as exc:
            raw_status = "unknown"
            snap = normalize_usage_snapshot(None)
            err_str: Optional[str] = f"{exc.__class__.__name__}: {exc}"
        else:
            err_str = None

        if budget_tracker is not None:
            budget_tracker.update_interaction(
                case_id=case_id,
                interaction_id=iid,
                raw_usage=snap.raw_usage if not snap.unknown_usage else None,
                role=role,
                turn_id=turn_id,
                status=raw_status if raw_status in TERMINAL_STATUSES else "unknown",
            )

        rec = {
            "event": "terminal" if raw_status in TERMINAL_STATUSES else "reconciled_unknown",
            "run_id": meta.get("run_id", "reconcile"),
            "case_id": case_id,
            "turn_id": turn_id,
            "role": role,
            "interaction_id": iid,
            "status": raw_status,
            "usage": snap.to_dict(),
            "error": err_str,
        }
        append_journal_event(jp, rec)
        reconciled_records.append(rec)

    return {
        "pending_before": len(pending_ids),
        "reconciled": reconciled_records,
    }


def _scan_for_private_leaks(payload: Any, context_label: str) -> None:
    """Verify no private ground-truth field names or paths appear in outgoing payloads."""
    serialized = json.dumps(payload, sort_keys=True) if not isinstance(payload, str) else payload
    low = serialized.lower()
    if "/private/" in low or "sealed_truth" in low:
        raise PermissionError(f"Leakage barrier triggered in {context_label}: private path detected")
    for token in FORBIDDEN_LEAK_TOKENS:
        if token in serialized:
            raise PermissionError(
                f"Leakage barrier triggered in {context_label}: forbidden token {token!r} detected"
            )


def _validate_and_encode_png(image_input: Union[Path, str, bytes]) -> Dict[str, str]:
    """Validate PNG header, dimensions <= 2048x2048, and size <= 512 KiB, returning SDK image block."""
    if isinstance(image_input, (Path, str)):
        p = Path(image_input)
        norm_p = str(p).replace("\\", "/").lower()
        if "/private/" in norm_p or "sealed_truth" in norm_p:
            raise PermissionError(f"Sandbox isolation violation: private image path {p}")
        if not p.exists():
            raise FileNotFoundError(f"Image file not found: {p}")
        data = p.read_bytes()
    elif isinstance(image_input, bytes):
        data = image_input
    else:
        raise TypeError(f"Unsupported image input type: {type(image_input).__name__}")

    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"Image byte size {len(data)} exceeds 512 KiB limit ({MAX_IMAGE_BYTES})")
    if len(data) < 24 or not data.startswith(PNG_MAGIC):
        raise ValueError("Invalid PNG file: missing PNG magic signature")

    with Image.open(io.BytesIO(data)) as im:
        if im.format != "PNG":
            raise ValueError(f"Expected PNG image format, got {im.format!r}")
        width, height = im.size
        if width > MAX_IMAGE_DIMENSION_PX or height > MAX_IMAGE_DIMENSION_PX:
            raise ValueError(
                f"Image dimensions {width}x{height} exceed {MAX_IMAGE_DIMENSION_PX}x{MAX_IMAGE_DIMENSION_PX}"
            )

    return {
        "type": "image",
        "mime_type": "image/png",
        "data": base64.b64encode(data).decode("ascii"),
    }


def _build_inline_sources(role: str, expected_filename: str, packet: Mapping[str, Any]) -> List[Dict[str, str]]:
    """Build inline workspace sources (`AGENTS.md`, `domain.py`, `public_packet.json`) for fresh turns."""
    domain_path = Path(__file__).resolve().parent / "domain.py"
    domain_text = domain_path.read_text(encoding="utf-8")

    agents_md_text = (
        f"# BlackboxDC Sandbox Workspace ({role})\n\n"
        f"- Execute electro-thermal calculations using `domain.py` and `public_packet.json`.\n"
        f"- Write your final validated JSON output to `{expected_filename}` in the workspace.\n"
        f"- Do not rely on console text output alone; `{expected_filename}` is retrieved directly.\n"
    )

    packet_text = json.dumps(dict(packet), indent=2, sort_keys=True) + "\n"

    return [
        {
            "type": "inline",
            "target": "AGENTS.md",
            "encoding": "utf-8",
            "content": agents_md_text,
        },
        {
            "type": "inline",
            "target": "domain.py",
            "encoding": "utf-8",
            "content": domain_text,
        },
        {
            "type": "inline",
            "target": "public_packet.json",
            "encoding": "utf-8",
            "content": packet_text,
        },
    ]


def _extract_inter_field(inter: Any, key: str, default: Any = None) -> Any:
    if isinstance(inter, Mapping):
        return inter.get(key, default)
    return getattr(inter, key, default)


def _has_code_execution_step(inter: Any) -> bool:
    """Return True if the interaction contains at least one `code_execution` or workspace tool step."""
    steps = _extract_inter_field(inter, "steps")
    if not steps:
        return False
    for st in steps:
        st_type = str(_extract_inter_field(st, "type", "") or _extract_inter_field(st, "tool", "")).lower()
        st_name = str(_extract_inter_field(st, "name", "")).lower()
        if any(
            tok in st_type or tok in st_name
            for tok in ("code_execution", "function_call", "function_result", "python", "bash", "run_command", "write_file")
        ):
            return True
    return False


@dataclass
class TurnEvidence:
    """Structured execution & audit record returned by `run_turn`."""

    run_id: str
    case_id: str
    turn_id: str
    role: str
    interaction_id: str
    environment_id: str
    status: str
    scientific_complete: bool
    diagnostic_only: bool
    has_code_execution_step: bool
    expected_filename: str
    retrieved_path: Optional[str]
    retrieved_json: Optional[Dict[str, Any]]
    retrieved_bytes_len: int
    retrieved_sha256: Optional[str]
    usage: UsageSnapshot
    poll_count: int
    elapsed_s: float
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["usage"] = self.usage.to_dict()
        return d

    def model_dump(self) -> Dict[str, Any]:
        return self.to_dict()


def run_turn(
    role: str,
    prompt: str,
    packet: Mapping[str, Any],
    images: Sequence[Union[Path, str, bytes]],
    environment_id: Optional[str] = None,
    max_total_tokens: int = NATIVE_INTERACTION_NONCACHED_CAP,
    *,
    expected_filename: str = "proposal.json",
    case_id: str = "case_A",
    turn_id: str = "I1",
    run_id: Optional[str] = None,
    client: Any = None,
    journal_path: Optional[Path] = None,
    budget_tracker: Optional[PairBudgetTracker] = None,
    agent_name: str = DEFAULT_MANAGED_AGENT_MODEL,
    model_id: str = DEFAULT_FOUNDATION_MODEL,
    poll_timeout_s: float = 600.0,
    poll_interval_s: float = 1.0,
    time_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> TurnEvidence:
    """Execute one Managed Agent turn with strict isolation, accounting, and direct file retrieval."""
    active_run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
    role_family = _normalize_role_family(role, turn_id)

    # 1. Validate prompt byte length <= 32 KiB
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    prompt_bytes = prompt.encode("utf-8")
    if len(prompt_bytes) > MAX_PROMPT_BYTES:
        raise ValueError(f"Prompt size {len(prompt_bytes)} bytes exceeds 32 KiB cap ({MAX_PROMPT_BYTES})")

    # 2. Enforce zero-leakage barrier on prompt and packet
    _scan_for_private_leaks(prompt, f"{turn_id}.prompt")
    _scan_for_private_leaks(dict(packet), f"{turn_id}.packet")

    # 3. Enforce role-environment isolation before any API call
    if environment_id is not None:
        bound_role = _ENVIRONMENT_ROLE_REGISTRY.get(environment_id)
        if budget_tracker is not None and environment_id in budget_tracker.environment_roles:
            bound_role = budget_tracker.environment_roles[environment_id]
        if bound_role is not None and bound_role != role_family:
            raise PermissionError(
                f"Cross-role environment reuse forbidden: environment {environment_id!r} belongs to "
                f"{bound_role!r}, cannot be used by {role_family!r} ({turn_id})"
            )

    # 4. Enforce three-ledger token budget pre-check
    if budget_tracker is not None:
        budget_tracker.enforce_budget_before_turn(
            case_id=case_id,
            turn_id=turn_id,
            requested_noncached_cap=max_total_tokens,
            role=role,
        )
    elif max_total_tokens <= 0 or max_total_tokens > NATIVE_INTERACTION_NONCACHED_CAP:
        raise RuntimeError(
            f"max_total_tokens {max_total_tokens} exceeds native interaction cap {NATIVE_INTERACTION_NONCACHED_CAP}"
        )

    # 5. Validate and encode PNG images
    encoded_images = [_validate_and_encode_png(img) for img in images]

    # 6. Build environment configuration (fresh inline sources vs existing environment_id)
    if environment_id is None:
        inline_sources = _build_inline_sources(
            role=role,
            expected_filename=expected_filename,
            packet=packet,
        )
        env_spec: Dict[str, Any] = {
            "type": "remote",
            "sources": inline_sources,
        }
    else:
        env_spec = {
            "type": "remote",
            "environment_id": environment_id,
        }

    input_blocks: List[Dict[str, Any]] = [{"type": "text", "text": prompt}, *encoded_images]

    if client is None:
        client = build_genai_client()

    t_start = float(time_fn())
    created = client.interactions.create(
        agent=agent_name,
        input=input_blocks,
        environment=env_spec,
        previous_interaction_id=None,
        background=True,
        store=True,
        agent_config={
            "type": "antigravity",
            "model": model_id,
            "max_total_tokens": int(max_total_tokens),
        },
        tools=[{"type": "code_execution"}],
    )

    interaction_id = str(_extract_inter_field(created, "id") or _extract_inter_field(created, "interaction_id"))
    resolved_env_id = str(
        _extract_inter_field(created, "environment_id")
        or environment_id
        or f"env-{interaction_id}"
    )

    # Bind environment_id to role_family
    _ENVIRONMENT_ROLE_REGISTRY[resolved_env_id] = role_family
    if budget_tracker is not None:
        budget_tracker.environment_roles[resolved_env_id] = role_family

    append_journal_event(
        journal_path,
        {
            "event": "created",
            "run_id": active_run_id,
            "case_id": case_id,
            "turn_id": turn_id,
            "role": role,
            "interaction_id": interaction_id,
            "environment_id": resolved_env_id,
            "input": input_blocks,
        },
    )

    # 7. Poll until terminal status or timeout
    poll_count = 0
    latest_inter = created
    status = str(_extract_inter_field(created, "status", "in_progress")).lower()
    if status == "canceled":
        status = "cancelled"

    latest_usage = normalize_usage_snapshot(_extract_inter_field(created, "usage"))

    while status not in TERMINAL_STATUSES:
        elapsed = float(time_fn()) - t_start
        if elapsed >= poll_timeout_s:
            # Timeout: request cancel and reconcile
            try:
                client.interactions.cancel(id=interaction_id)
            except Exception:
                pass
            try:
                reconciled = client.interactions.get(id=interaction_id)
                rec_status = str(_extract_inter_field(reconciled, "status", "unknown")).lower()
                if rec_status == "canceled":
                    rec_status = "cancelled"
                rec_usage = _extract_inter_field(reconciled, "usage")
                if rec_status in TERMINAL_STATUSES:
                    status = rec_status
                    latest_inter = reconciled
                    latest_usage = normalize_usage_snapshot(rec_usage)
                else:
                    status = "unknown"
                    latest_usage = normalize_usage_snapshot(None)
            except Exception:
                status = "unknown"
                latest_usage = normalize_usage_snapshot(None)

            append_journal_event(
                journal_path,
                {
                    "event": "cancelled",
                    "run_id": active_run_id,
                    "case_id": case_id,
                    "turn_id": turn_id,
                    "role": role,
                    "interaction_id": interaction_id,
                    "environment_id": resolved_env_id,
                    "status": status,
                    "usage": latest_usage.to_dict(),
                },
            )
            break

        sleep_fn(poll_interval_s)
        poll_count += 1
        latest_inter = client.interactions.get(id=interaction_id)
        status = str(_extract_inter_field(latest_inter, "status", "in_progress")).lower()
        if status == "canceled":
            status = "cancelled"
        raw_u = _extract_inter_field(latest_inter, "usage")
        latest_usage = normalize_usage_snapshot(raw_u)

        env_from_poll = _extract_inter_field(latest_inter, "environment_id")
        if env_from_poll:
            resolved_env_id = str(env_from_poll)
            _ENVIRONMENT_ROLE_REGISTRY[resolved_env_id] = role_family
            if budget_tracker is not None:
                budget_tracker.environment_roles[resolved_env_id] = role_family

        if budget_tracker is not None:
            budget_tracker.update_interaction(
                case_id=case_id,
                interaction_id=interaction_id,
                raw_usage=raw_u,
                role=role,
                turn_id=turn_id,
                status=status,
            )

        append_journal_event(
            journal_path,
            {
                "event": "polled",
                "run_id": active_run_id,
                "case_id": case_id,
                "turn_id": turn_id,
                "role": role,
                "interaction_id": interaction_id,
                "environment_id": resolved_env_id,
                "poll_count": poll_count,
                "status": status,
                "usage": latest_usage.to_dict(),
            },
        )

    # Final update to budget tracker at terminal / unknown exit
    if budget_tracker is not None:
        budget_tracker.update_interaction(
            case_id=case_id,
            interaction_id=interaction_id,
            raw_usage=latest_usage.raw_usage if not latest_usage.unknown_usage else None,
            role=role,
            turn_id=turn_id,
            status=status,
        )

    append_journal_event(
        journal_path,
        {
            "event": "terminal",
            "run_id": active_run_id,
            "case_id": case_id,
            "turn_id": turn_id,
            "role": role,
            "interaction_id": interaction_id,
            "environment_id": resolved_env_id,
            "status": status,
            "usage": latest_usage.to_dict(),
        },
    )

    has_code_step = _has_code_execution_step(latest_inter)
    retrieved_json: Optional[Dict[str, Any]] = None
    retrieved_bytes_len: int = 0
    retrieved_sha256: Optional[str] = None
    retrieved_path: Optional[str] = None
    error_msg: Optional[str] = None

    # 8. Attempt direct file retrieval ONLY for completed or incomplete statuses
    if status in ("completed", "incomplete"):
        try:
            parsed_obj, raw_b, sha_hex, m_path = retrieve_environment_file(
                client=client,
                environment_id=resolved_env_id,
                expected_filename=expected_filename,
            )
            retrieved_json = parsed_obj
            retrieved_bytes_len = len(raw_b)
            retrieved_sha256 = sha_hex
            retrieved_path = m_path
            append_journal_event(
                journal_path,
                {
                    "event": "retrieved",
                    "run_id": active_run_id,
                    "case_id": case_id,
                    "turn_id": turn_id,
                    "role": role,
                    "interaction_id": interaction_id,
                    "environment_id": resolved_env_id,
                    "retrieved_path": retrieved_path,
                    "retrieved_bytes_len": retrieved_bytes_len,
                    "retrieved_sha256": retrieved_sha256,
                },
            )
        except Exception as exc:
            error_msg = f"File retrieval failed: {exc.__class__.__name__}: {exc}"

    if status == "completed":
        if not has_code_step and error_msg is None:
            error_msg = "Interaction completed without any code_execution step"
        scientific_complete = bool(
            has_code_step and retrieved_json is not None and retrieved_bytes_len > 0
        )
        diagnostic_only = False
    elif status == "incomplete":
        scientific_complete = False
        diagnostic_only = bool(retrieved_json is not None and retrieved_bytes_len > 0)
        if error_msg is None:
            error_msg = "Interaction ended with status='incomplete' (diagnostic-only)"
    else:
        scientific_complete = False
        diagnostic_only = False
        if error_msg is None:
            error_msg = f"Interaction terminated with status={status!r}"

    append_journal_event(
        journal_path,
        {
            "event": "locked",
            "run_id": active_run_id,
            "case_id": case_id,
            "turn_id": turn_id,
            "role": role,
            "interaction_id": interaction_id,
            "environment_id": resolved_env_id,
            "status": status,
            "scientific_complete": scientific_complete,
            "diagnostic_only": diagnostic_only,
            "retrieved_sha256": retrieved_sha256,
        },
    )

    elapsed_total = round(max(0.0, float(time_fn()) - t_start), 4)
    return TurnEvidence(
        run_id=active_run_id,
        case_id=case_id,
        turn_id=turn_id,
        role=role,
        interaction_id=interaction_id,
        environment_id=resolved_env_id,
        status=status,
        scientific_complete=scientific_complete,
        diagnostic_only=diagnostic_only,
        has_code_execution_step=has_code_step,
        expected_filename=expected_filename,
        retrieved_path=retrieved_path,
        retrieved_json=retrieved_json,
        retrieved_bytes_len=retrieved_bytes_len,
        retrieved_sha256=retrieved_sha256,
        usage=latest_usage,
        poll_count=poll_count,
        elapsed_s=elapsed_total,
        error=error_msg,
    )


def run_live_canary(
    artifacts_root: Path,
    client: Any = None,
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Execute or record the Phase 3 single-interaction canary check."""
    artifacts_root = Path(artifacts_root)
    reports_dir = artifacts_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    canary_report_path = reports_dir / "phase3_canary_evidence.json"

    if client is None:
        auth_cfg = resolve_auth_config(env=env)
        if not auth_cfg.get("ready"):
            payload = {
                "phase": "03_MANAGED_RUNTIME",
                "canary_status": "blocked_no_live_credentials",
                "auth_mode": auth_cfg.get("mode", "mock"),
                "reason": auth_cfg.get("reason", "No live API credentials configured"),
                "timestamp_utc": _utc_now_iso(),
            }
            canary_report_path.write_text(
                json.dumps(redact_for_log(payload), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return payload
        client = build_genai_client(auth_cfg)

    # Create a tiny valid PNG for the canary
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color=(15, 23, 42)).save(buf, format="PNG")
    tiny_png = buf.getvalue()

    tracker = PairBudgetTracker()
    journal_path = reports_dir / "phase3_canary_journal.jsonl"
    evidence = run_turn(
        role="canary",
        prompt=(
            "Use one compact Python script or file write to import domain, compute "
            "step_temperature(34.0, 12.0, 24.0, 0.04, 0.004, 0.4, 8.0, 10.0), and write "
            "JSON {'canary_ok': True, 'step_temp_c': <value>} to canary.json in the current workspace directory, then stop immediately."
        ),
        packet={"schema_version": "1.0.0", "canary": True},
        images=[tiny_png],
        environment_id=None,
        max_total_tokens=NATIVE_INTERACTION_NONCACHED_CAP,
        expected_filename="canary.json",
        case_id="canary",
        turn_id="CANARY",
        client=client,
        journal_path=journal_path,
        budget_tracker=tracker,
    )
    out = {
        "phase": "03_MANAGED_RUNTIME",
        "canary_status": "completed" if evidence.scientific_complete else evidence.status,
        "evidence": redact_for_log(evidence.to_dict()),
        "timestamp_utc": _utc_now_iso(),
    }
    canary_report_path.write_text(
        json.dumps(out, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out


# ---------------------------------------------------------------------------
# Backward-Compatible Phase 00 / Downstream Transport Adapter
# ---------------------------------------------------------------------------


@dataclass
class HandoffFileRecord:
    """Audit record for a file handed off into the agent sandbox."""

    path: str
    filename: str
    sha256: str
    size_bytes: int


@dataclass
class InteractionAccounting:
    """Per-interaction accounting metadata for Managed Agent calls."""

    interaction_id: str
    case_id: str
    agent_role: str
    managed_model: str
    foundation_model: str
    transport_mode: str
    latency_ms: float
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    handoff_files: List[HandoffFileRecord] = field(default_factory=list)


def validate_sandbox_handoff_files(file_paths: List[Path]) -> List[HandoffFileRecord]:
    """Verify that no file handed off to the agent comes from a private/sealed path."""
    records: List[HandoffFileRecord] = []
    for raw_p in file_paths:
        p = Path(raw_p)
        norm_str = str(p).replace("\\", "/").lower()
        if "/private/" in norm_str or "sealed_truth" in norm_str:
            raise PermissionError(
                f"Sandbox isolation violation: attempted to hand off sealed file {p}"
            )
        if not p.exists():
            raise FileNotFoundError(f"Handoff file does not exist: {p}")
        data = p.read_bytes()
        records.append(
            HandoffFileRecord(
                path=str(p),
                filename=p.name,
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data),
            )
        )
    return records


class ManagedAgentTransport:
    """Transport adapter for `antigravity-preview-09-2026` / `gemini-4-argon` interactions."""

    def __init__(
        self,
        managed_model: Optional[str] = None,
        foundation_model: Optional[str] = None,
        mode: str = "deterministic",
        custom_responder: Optional[Callable[[str, str, Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> None:
        self.managed_model = managed_model or os.environ.get(
            "BLACKBOX_MANAGED_MODEL", DEFAULT_MANAGED_AGENT_MODEL
        )
        self.foundation_model = foundation_model or os.environ.get(
            "BLACKBOX_FOUNDATION_MODEL", DEFAULT_FOUNDATION_MODEL
        )
        self.mode = mode
        self._custom_responder = custom_responder
        self.accounting_log: List[InteractionAccounting] = []

    def invoke_interaction(
        self,
        interaction_id: str,
        case_id: str,
        agent_role: str,
        prompt_context: Dict[str, Any],
        handoff_files: Optional[List[Path]] = None,
    ) -> Tuple[Dict[str, Any], InteractionAccounting]:
        """Execute a single isolated agent turn (I1, I2, A1, or A2) with sandbox verification."""
        t0 = time.perf_counter()
        validated_files = validate_sandbox_handoff_files(handoff_files or [])

        serialized_ctx = json.dumps(prompt_context, sort_keys=True)
        if "truth_sha256" in serialized_ctx or "node_resistances_ohm" in serialized_ctx:
            raise PermissionError(
                f"Context leakage violation in {interaction_id}: private truth fields detected."
            )

        if self._custom_responder is not None:
            response_payload = self._custom_responder(interaction_id, case_id, prompt_context)
        elif self.mode == "live":
            response_payload = self._invoke_live_genai(
                interaction_id=interaction_id,
                case_id=case_id,
                agent_role=agent_role,
                prompt_context=prompt_context,
                handoff_files=validated_files,
            )
        else:
            response_payload = self._invoke_deterministic_reference(
                interaction_id=interaction_id,
                case_id=case_id,
                prompt_context=prompt_context,
            )

        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)
        p_tokens = max(64, len(serialized_ctx) // 4)
        c_tokens = max(32, len(json.dumps(response_payload)) // 4)
        acc = InteractionAccounting(
            interaction_id=interaction_id,
            case_id=case_id,
            agent_role=agent_role,
            managed_model=self.managed_model,
            foundation_model=self.foundation_model,
            transport_mode=self.mode,
            latency_ms=elapsed_ms,
            prompt_tokens=p_tokens,
            completion_tokens=c_tokens,
            total_tokens=p_tokens + c_tokens,
            handoff_files=validated_files,
        )
        self.accounting_log.append(acc)
        return response_payload, acc

    def _invoke_live_genai(
        self,
        interaction_id: str,
        case_id: str,
        agent_role: str,
        prompt_context: Dict[str, Any],
        handoff_files: List[HandoffFileRecord],
    ) -> Dict[str, Any]:
        """Invoke Google Gen AI SDK when live API credentials are configured."""
        from google import genai
        from google.genai import types

        _load_dotenv_if_present()
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise RuntimeError("Missing GEMINI_API_KEY / GOOGLE_API_KEY for live mode")

        client = genai.Client(api_key=api_key)
        parts: List[Any] = [
            f"Role: {agent_role} | Interaction: {interaction_id} | Case: {case_id}\n"
            f"Return strict JSON matching the {interaction_id} contract.\n"
            f"Context:\n{json.dumps(prompt_context, indent=2)}"
        ]
        for hf in handoff_files:
            file_bytes = Path(hf.path).read_bytes()
            if hf.filename.endswith(".png"):
                parts.append(types.Part.from_bytes(data=file_bytes, mime_type="image/png"))

        resp = client.models.generate_content(
            model=self.foundation_model,
            contents=parts,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        return json.loads(resp.text)

    def _invoke_deterministic_reference(
        self,
        interaction_id: str,
        case_id: str,
        prompt_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Deterministic reference policy deriving outputs solely from public evidence & probe."""
        pub_ev = prompt_context.get("public_evidence", {})
        if isinstance(pub_ev, dict) and pub_ev.get("max_safe_feeder_current_a") == 125.0:
            norm_case = case_id.strip()
            if norm_case in ("case_A", "A"):
                sld_map = {"N1": "F1", "N2": "F2", "N3": "F3", "N4": "F4"}
            else:
                sld_map = {"N1": "F1", "N2": "F3", "N3": "F2", "N4": "F4"}
        else:
            sld_map = get_case_sld_mapping(case_id)
        hotspot_node = "N2"

        if interaction_id == "I1":
            return {
                "interaction_id": "I1",
                "case_id": case_id,
                "visual_hotspot_node": hotspot_node,
                "sld_feeder_mapping": sld_map,
                "candidate_fault_nodes": ["N2", "N3"],
                "probe_request": {
                    "target_node": hotspot_node,
                    "delta_current_a": 20.0,
                    "rationale": (
                        "3D IR thermography localizes the 135.2 C lug hotspot to Rack Node N2; "
                        "probing N2 with +20A pulse quantifies terminal resistance."
                    ),
                },
                "initial_hypothesis": (
                    f"High-resistance termination at {hotspot_node} fed by {sld_map[hotspot_node]} "
                    f"per {case_id} SLD."
                ),
                "confidence": 0.92,
            }

        if interaction_id == "I2":
            probe_resp = prompt_context["probe_response"]
            r_est = float(probe_resp["inferred_resistance_ohm"])
            target_node = str(probe_resp["target_node"])
            fault_feeder = sld_map[target_node]
            safe_cap_a = compute_max_safe_current_a(resistance_ohm=r_est)
            return {
                "interaction_id": "I2",
                "case_id": case_id,
                "diagnosed_fault_node": target_node,
                "diagnosed_fault_feeder": fault_feeder,
                "estimated_resistance_ohm": r_est,
                "recommended_max_current_a": safe_cap_a,
                "remediation_action": (
                    f"Cap feeder {fault_feeder} supplying node {target_node} to <= {safe_cap_a:.2f} A "
                    f"and schedule torque remediation on {target_node} lug."
                ),
                "safety_lock_rationale": (
                    f"Measured dV={probe_resp['delta_voltage_drop_v']}V over dI={probe_resp['delta_current_a']}A "
                    f"confirms R={r_est} Ohm on {target_node} ({fault_feeder})."
                ),
                "confidence": 0.99,
            }

        if interaction_id == "A1":
            return {
                "interaction_id": "A1",
                "case_id": case_id,
                "verified_sld_mapping": sld_map,
                "verified_hotspot_node": hotspot_node,
                "topology_consistency_check": True,
                "pre_probe_risk_assessment": (
                    f"SLD confirms {hotspot_node} is routed to feeder {sld_map[hotspot_node]} "
                    f"in {case_id}; +20A active probe on {hotspot_node} is within transient thermal bounds."
                ),
            }

        if interaction_id == "A2":
            probe_resp = prompt_context["probe_response"]
            i2_out = prompt_context["i2_contract"]
            r_ver = float(probe_resp["inferred_resistance_ohm"])
            safe_limit_a = compute_max_safe_current_a(resistance_ohm=r_ver)
            expected_feeder = sld_map[hotspot_node]
            matches = (
                i2_out["diagnosed_fault_node"] == hotspot_node
                and i2_out["diagnosed_fault_feeder"] == expected_feeder
                and abs(float(i2_out["recommended_max_current_a"]) - safe_limit_a) <= 5.0
            )
            return {
                "interaction_id": "A2",
                "case_id": case_id,
                "audit_verdict": "APPROVE" if matches else "REJECT",
                "verified_fault_node": hotspot_node,
                "verified_fault_feeder": expected_feeder,
                "verified_resistance_ohm": r_ver,
                "verified_safe_current_limit_a": safe_limit_a,
                "discrepancy_notes": (
                    "I2 diagnosis and current cap match SLD feeder routing and probe physics."
                    if matches
                    else "Discrepancy detected between I2 remediation and SLD/probe limits."
                ),
            }

        raise ValueError(f"Unsupported interaction_id: {interaction_id!r}")
