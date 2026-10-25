"""Google Gen AI SDK Managed Agent transport for BlackboxDC.

Configures `antigravity-preview-09-2026` and `gemini-4-argon` defaults, enforces
sandbox file handoff barriers (preventing any private truth file from entering
an agent interaction), and tracks token/latency accounting.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from blackbox.domain import compute_max_safe_current_a
from blackbox.render_sld import get_case_sld_mapping


DEFAULT_MANAGED_AGENT_MODEL = "antigravity-preview-09-2026"
DEFAULT_FOUNDATION_MODEL = "gemini-4-argon"


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

        # Ensure prompt_context does not contain private truth keys
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
        from blackbox.preflight import _load_dotenv_if_present

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
