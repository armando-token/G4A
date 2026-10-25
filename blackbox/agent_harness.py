"""Phase 4 role-specific AGENTS.md and turn prompt builders for BlackboxDC.

Implements:
- `build_role_agents_md(role: str)` for `"investigator"` and `"auditor"`.
- `build_i1_prompt(case_id: str, diagnostic_mode: str = "guided_diagnostic")`
- `build_i2_prompt(case_id: str, i1_lock_hash: str, probe_receipt: Mapping[str, Any], i1_summary: Mapping[str, Any])`
- `build_a1_prompt(case_id: str, input_image_hashes: Mapping[str, str])`
- `build_a2_prompt(case_id: str, a1_lock_hash: str, i2_lock_hash: str, a1_graph: Mapping[str, Any], i2_plan: Mapping[str, Any], probe_receipt: Mapping[str, Any])`

Guarantees:
- Every generated `AGENTS.md` and prompt is compact (`< 16 KiB`).
- Passes `_scan_for_private_leaks` and pre-probe zero-answer checks (never leaks `"N4"`,
  connector bounding box coordinates, case-specific feeder mappings, or probe measurement
  values in `AGENTS.md`, `I1`, or `A1`).
"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping

from blackbox.fixture import _normalize_network_case_id
from blackbox.managed import _scan_for_private_leaks

SCHEMA_VERSION_VISUAL_V2: str = "visual-v2"
MAX_HARNESS_PROMPT_BYTES: int = 16 * 1024  # 16 KiB

TURN_EXPECTED_FILENAMES: Dict[str, str] = {
    "I1": "proposal.json",
    "I2": "plan.json",
    "A1": "independent_graph.json",
    "A2": "audit_challenge.json",
}

# Pre-probe forbidden answer tokens that must NEVER appear in AGENTS.md, I1 prompt, or A1 prompt
_PRE_PROBE_FORBIDDEN_SUBSTRINGS: tuple[str, ...] = (
    "N4",
    "1079",
    "1143",
    "1110.36",
    "358.99",
    "700",
    "316",
    "1.209409",
    "88.9503",
    "52.5348",
    "98.829",
    "0.040",
    "0.04",
    "SNS_K2",
    "F1->N2",
    "F2->N3",
    "F1->N3",
    "F2->N2",
    "safe_receiver",
    "wrong_receiver",
    "R0_ohm",
    "h_W_per_K",
    "C_J_per_K",
    "connector_bboxes",
)


def _normalize_harness_role(role: str) -> str:
    if not isinstance(role, str) or not role.strip():
        raise ValueError("role must be a non-empty string")
    norm = role.strip().lower()
    if norm in ("investigator", "i1", "i2"):
        return "investigator"
    if norm in ("auditor", "a1", "a2"):
        return "auditor"
    raise ValueError(f"Unsupported role {role!r}; expected 'investigator' or 'auditor'")


def _assert_compact_and_leak_free(
    text: str,
    context_label: str,
    *,
    is_pre_probe: bool = False,
) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"{context_label} must be a non-empty string")
    raw_bytes = text.encode("utf-8")
    if len(raw_bytes) >= MAX_HARNESS_PROMPT_BYTES:
        raise ValueError(
            f"{context_label} byte length {len(raw_bytes)} exceeds 16 KiB limit ({MAX_HARNESS_PROMPT_BYTES})"
        )
    _scan_for_private_leaks(text, context_label)
    if is_pre_probe:
        for tok in _PRE_PROBE_FORBIDDEN_SUBSTRINGS:
            if tok in text:
                raise PermissionError(
                    f"Pre-probe answer/coordinate leak in {context_label}: forbidden token {tok!r}"
                )
    return text


def _compact_json(data: Mapping[str, Any]) -> str:
    return json.dumps(dict(data), sort_keys=True, separators=(",", ":"))


def build_role_agents_md(role: str) -> str:
    """Build the role-specific `AGENTS.md` workspace instruction file (`investigator` vs `auditor`)."""
    norm_role = _normalize_harness_role(role)
    if norm_role == "investigator":
        role_title = "INVESTIGATOR (Turns I1 & I2)"
        turn_files = "`proposal.json` (Turn I1) and `plan.json` (Turn I2)"
        role_mission = (
            "You are the BlackboxDC Investigator responsible for visual localization, single-shot "
            "diagnostic probe selection (Turn I1), and post-probe parameter identification and "
            "safe workload migration planning (Turn I2)."
        )
    else:
        role_title = "INDEPENDENT AUDITOR (Turns A1 & A2)"
        turn_files = "`independent_graph.json` (Turn A1) and `audit_challenge.json` (Turn A2)"
        role_mission = (
            "You are the independent BlackboxDC Auditor responsible for zero-trust visual topology "
            "extraction prior to seeing Investigator outputs (Turn A1), followed by quantitative "
            "verification or challenge of the Investigator's post-probe plan (Turn A2)."
        )

    content = f"""# BlackboxDC Managed Workspace Instructions — {role_title}

## 1. Mission & Role Isolation
{role_mission}
Your execution environment is strictly isolated from the opposite role.

## 2. Mounted Workspace Files
The following files are mounted in your current working directory:
- `AGENTS.md`: These workspace operating instructions.
- `domain.py`: Authoritative electro-thermal physics kernel (`step_temperature`, `simulate_world`, `apply_migration`, `infer_r0_from_voltage_drop`, `fit_thermal_parameters_from_ramp`).
- `public_packet.json`: Publicly observable sensor ramp time series, component limits, diagnostic menu, and maintenance records.

## 3. Native Multimodal Image Handling (CRITICAL)
- On visual turns (`I1` and `A1`), the PNG visual captures (`rgb.png`, `thermal.png`, `sld.png`) are attached **directly as native multimodal image parts** in the conversation input.
- **They are NOT files on the workspace filesystem.**
- Do **NOT** search the filesystem (`find`, `ls`, `glob`) for `.png` files, and do **NOT** use `PIL`, `cv2`, or pixel-scraping scripts as a substitute for your native multimodal visual perception.
- Inspect the attached images directly to read rack placards, sensor labels, connector hotspot locations, and Single-Line Diagram (SLD) bus junction dots and non-connected crossing arches.

## 4. Python `code_execution` & Compact Artifact Output
- Always use the Python `code_execution` tool to load `public_packet.json`, invoke `domain.py` functions for parameter fitting and trajectory simulation, and write your output JSON file directly to disk: {turn_files}.
- Write **compact, valid JSON** directly to the required filename (`schema_version` must be `"visual-v2"`).
- Do **NOT** echo long numeric arrays or full trajectory dumps to `stdout`; the harness retrieves your output file directly from the workspace filesystem.

## 5. Uncertainty, Abstention & Anti-Hallucination Policy
- Never guess, fabricate, or hardcode fallback bounding box coordinates, sensor bindings, or case-dependent feeder topologies.
- If feeder connectivity in the active SLD is ambiguous, if required diagnostic measurements are missing or corrupted, or if the least-squares thermal ramp fit residual exceeds physical tolerance, explicitly state your uncertainty and set `"abstained": true` in your output JSON.
"""
    return _assert_compact_and_leak_free(content, f"AGENTS.md[{norm_role}]", is_pre_probe=True)


def build_i1_prompt(case_id: str, diagnostic_mode: str = "guided_diagnostic") -> str:
    """Build the Turn I1 Investigator prompt (< 16 KiB, zero private truth or answer leaks)."""
    _, canonical_case_id = _normalize_network_case_id(case_id)
    if diagnostic_mode not in ("guided_diagnostic", "unassisted"):
        raise ValueError(
            f"Unsupported diagnostic_mode {diagnostic_mode!r}; expected 'guided_diagnostic' or 'unassisted'"
        )

    if diagnostic_mode == "guided_diagnostic":
        probe_instruction = (
            "In `guided_diagnostic` mode, request the `'voltage_drop'` diagnostic probe on the "
            "visually identified overheated source rack to disambiguate its baseline electrical "
            "resistance from cooling degradation."
        )
    else:
        probe_instruction = (
            "In `unassisted` mode, select at most one diagnostic measurement from `diagnostic_menu` "
            "(`'voltage_drop'`, `'temperature_snapshot'`, or `'controller_setting'`) on a target rack "
            "that maximally disambiguates the root cause of the thermal ramp."
        )

    prompt = f"""# Turn I1 — Investigator Pre-Probe Visual Diagnosis & Probe Proposal
- **Case Identifier**: `{canonical_case_id}`
- **Diagnostic Mode**: `{diagnostic_mode}`
- **Required Output File**: `proposal.json` (`schema_version`: `"visual-v2"`)

## Instructions
1. **Multimodal Visual Inspection (Native Image Parts)**:
   - Inspect the three attached PNG images directly as native multimodal inputs (do NOT search the workspace filesystem for `.png` files or use `PIL`/`cv2` as a substitute for visual perception):
     - Rear-aisle RGB capture: map physical rack bays to the anonymous telemetry sensor tags listed in `public_packet.json`.
     - Rear-aisle infrared thermal capture: localize the overheated rear connector bounding box `[x_min, y_min, x_max, y_max]` (and center `[x, y]`) in `1280x800` pixel coordinates.
     - Active Single-Line Diagram (`sld.png`): trace the filled bus junction dots (`F1` vs `F2`) and non-connected semicircular bridge arches for every rack bay. Treat any `[OBSOLETE - SUPERSEDED BY ACTIVE SLD]` text note in `public_packet.json` as obsolete.
2. **Diagnostic Probe Selection**:
   - {probe_instruction}
3. **Workspace Computation & File Output**:
   - Use Python `code_execution` to inspect `public_packet.json` and write your compact JSON proposal directly to `proposal.json` without printing large arrays to stdout.
   - Required top-level keys in `proposal.json`:
     - `"schema_version"`: `"visual-v2"`
     - `"case_id"`: `"{canonical_case_id}"`
     - `"turn_id"`: `"I1"`
     - `"hotspot_rack"`: identified overheated rack ID
     - `"hotspot_sensor"`: corresponding sensor ID
     - `"hotspot_bbox"`: `[x_min, y_min, x_max, y_max]` integer pixel coordinates of the overheated connector
     - `"sensor_binding"`: complete mapping of each sensor tag to its physical rack ID
     - `"feeder_mapping"`: mapping of each rack ID to `"F1"` or `"F2"` from the active SLD
     - `"diagnostic_request"`: `{{"kind": ..., "target": ..., "rationale": ...}}`
     - `"candidate_hypotheses"`: `["electrical", "cooling"]`
     - `"abstained"`: `false` (or `true` with explanation if visual evidence is ambiguous; never guess coordinates or feeder topology)
"""
    return _assert_compact_and_leak_free(prompt, f"I1.prompt[{canonical_case_id}]", is_pre_probe=True)


def build_i2_prompt(
    case_id: str,
    i1_lock_hash: str,
    probe_receipt: Mapping[str, Any],
    i1_summary: Mapping[str, Any],
) -> str:
    """Build the Turn I2 Investigator post-probe parameter fit & migration plan prompt (< 16 KiB)."""
    _, canonical_case_id = _normalize_network_case_id(case_id)
    if not isinstance(i1_lock_hash, str) or not i1_lock_hash.strip():
        raise ValueError("i1_lock_hash must be a non-empty string")
    if not isinstance(probe_receipt, Mapping) or not probe_receipt:
        raise ValueError("probe_receipt must be a non-empty mapping")
    if not isinstance(i1_summary, Mapping) or not i1_summary:
        raise ValueError("i1_summary must be a non-empty mapping")

    _scan_for_private_leaks(dict(probe_receipt), "I2.probe_receipt")
    _scan_for_private_leaks(dict(i1_summary), "I2.i1_summary")

    receipt_json = _compact_json(probe_receipt)
    i1_json = _compact_json(i1_summary)

    prompt = f"""# Turn I2 — Investigator Post-Probe Parameter Identification & Safe Migration Plan
- **Case Identifier**: `{canonical_case_id}`
- **Locked Turn I1 SHA-256 (`i1_lock_hash`)**: `{i1_lock_hash.strip()}`
- **Required Output File**: `plan.json` (`schema_version`: `"visual-v2"`)

## Signed Diagnostic Probe Receipt
```json
{receipt_json}
```

## Locked Turn I1 Proposal Summary
```json
{i1_json}
```

## Instructions
1. **Physics Identification via `domain.py` (`code_execution`)**:
   - Use Python `code_execution` to load `public_packet.json` and import `domain`.
   - Call `domain.infer_r0_from_voltage_drop` using the signed probe measurement and $t=0$ temperature/current to recover cold resistance.
   - Call `domain.fit_thermal_parameters_from_ramp` on the preceding ramp (`t = -120..0 s`) to recover thermal conductance and heat capacity and disambiguate `"electrical"` vs `"cooling"`.
2. **Counterfactual 180 s Trajectory & +1 °C Sensitivity Canary**:
   - Using the active SLD feeder mapping locked in Turn I1, call `domain.simulate_world` for each allowed 8.0 A migration receiver (`"N2"`, `"N3"`) over `t = 0, 10, ..., 180 s` (19 samples).
   - Select the receiver rack that satisfies all thermal (`< 90.0 °C`), rack current, and PDU feeder current (`F1 <= 44.0 A`, `F2 <= 50.0 A`) limits.
   - Compute the `+1.0 °C` initial-temperature perturbation sensitivity canary on the source rack to verify positive causal response at $t = 180\\text{{ s}}$.
3. **Compact File Output**:
   - Write your final JSON object directly to `plan.json` (do NOT echo full trajectories to stdout) with keys:
     - `"schema_version"`: `"visual-v2"`
     - `"case_id"`: `"{canonical_case_id}"`
     - `"turn_id"`: `"I2"`
     - `"i1_lock_hash"`: `"{i1_lock_hash.strip()}"`
     - `"probe_receipt_sha256"`: receipt SHA-256 from the signed probe receipt
     - `"confirmed_hypothesis"`: `"electrical"` or `"cooling"`
     - `"inferred_parameters"`: fitted physical parameters and `"max_residual"`
     - `"migration"`: `{{"source": ..., "target": ..., "amps": 8.0}}`
     - `"predicted_trajectory"`: 19-sample trajectory (`time_s`, `temps_C`, `pdu_currents_A`, `verdict`)
     - `"sensitivity_canary"`: `delta_t0_C=1.0`, `baseline_t180_C`, `perturbed_t180_C`, `delta_t180_C`
     - `"abstained"`: `false` (or `true` if fit residual or feeder limits cannot be satisfied)
"""
    return _assert_compact_and_leak_free(prompt, f"I2.prompt[{canonical_case_id}]", is_pre_probe=False)


def build_a1_prompt(case_id: str, input_image_hashes: Mapping[str, str]) -> str:
    """Build the Turn A1 Independent Auditor pre-probe visual topology prompt (< 16 KiB)."""
    _, canonical_case_id = _normalize_network_case_id(case_id)
    if not isinstance(input_image_hashes, Mapping) or not input_image_hashes:
        raise ValueError("input_image_hashes must be a non-empty mapping of image SHA-256 digests")

    _scan_for_private_leaks(dict(input_image_hashes), "A1.input_image_hashes")
    hashes_json = _compact_json(input_image_hashes)

    prompt = f"""# Turn A1 — Independent Auditor Pre-Probe Visual & Topology Extraction
- **Case Identifier**: `{canonical_case_id}`
- **Required Output File**: `independent_graph.json` (`schema_version`: `"visual-v2"`)

## Attached Multimodal Image SHA-256 Digests
```json
{hashes_json}
```

## Instructions
1. **Zero-Trust Native Multimodal Inspection**:
   - You are operating in an isolated Auditor environment with zero access to Investigator proposals or diagnostic probe receipts.
   - Inspect the attached PNG images (`rgb.png`, `thermal.png`, `sld.png`) directly as native multimodal message parts. Do **NOT** search the workspace filesystem for `.png` files or use `PIL`/`cv2` scripts as a substitute for visual perception.
2. **Independent SLD Graph & Hotspot Localization**:
   - Trace the active Single-Line Diagram (`sld.png`): identify which feeder bus (`F1` at 44 A limit or `F2` at 50 A limit) feeds each rack bay by inspecting the filled junction dots vs. non-connected crossing bridge arches.
   - Verify that any `[OBSOLETE - SUPERSEDED BY ACTIVE SLD]` maintenance note in `public_packet.json` is rejected in favor of the active SLD drawing.
   - Inspect the rear-aisle RGB and thermal captures to bind each anonymous sensor tag to its physical rack bay and localize the overheated connector bounding box `[x_min, y_min, x_max, y_max]`.
3. **Compact File Output**:
   - Use Python `code_execution` to write your independent audit record directly to `independent_graph.json` with keys:
     - `"schema_version"`: `"visual-v2"`
     - `"case_id"`: `"{canonical_case_id}"`
     - `"turn_id"`: `"A1"`
     - `"input_image_hashes"`: `{hashes_json}`
     - `"feeder_mapping"`: mapping of each rack ID to `"F1"` or `"F2"`
     - `"edges"`: sorted list of `"F1-N..."` / `"F2-N..."` edges
     - `"hotspot_rack"`: visually identified overheated rack ID
     - `"hotspot_sensor"`: visually identified overheated sensor ID
     - `"hotspot_bbox"`: `[x_min, y_min, x_max, y_max]`
     - `"obsolete_note_rejected"`: `true`
     - `"abstained"`: `false` (or `true` if visual evidence is ambiguous; never guess coordinates or feeder topology)
"""
    return _assert_compact_and_leak_free(prompt, f"A1.prompt[{canonical_case_id}]", is_pre_probe=True)


def build_a2_prompt(
    case_id: str,
    a1_lock_hash: str,
    i2_lock_hash: str,
    a1_graph: Mapping[str, Any],
    i2_plan: Mapping[str, Any],
    probe_receipt: Mapping[str, Any],
) -> str:
    """Build the Turn A2 Independent Auditor post-probe verification/challenge prompt (< 16 KiB)."""
    _, canonical_case_id = _normalize_network_case_id(case_id)
    if not isinstance(a1_lock_hash, str) or not a1_lock_hash.strip():
        raise ValueError("a1_lock_hash must be a non-empty string")
    if not isinstance(i2_lock_hash, str) or not i2_lock_hash.strip():
        raise ValueError("i2_lock_hash must be a non-empty string")
    if not isinstance(a1_graph, Mapping) or not a1_graph:
        raise ValueError("a1_graph must be a non-empty mapping")
    if not isinstance(i2_plan, Mapping) or not i2_plan:
        raise ValueError("i2_plan must be a non-empty mapping")
    if not isinstance(probe_receipt, Mapping) or not probe_receipt:
        raise ValueError("probe_receipt must be a non-empty mapping")

    _scan_for_private_leaks(dict(a1_graph), "A2.a1_graph")
    _scan_for_private_leaks(dict(i2_plan), "A2.i2_plan")
    _scan_for_private_leaks(dict(probe_receipt), "A2.probe_receipt")

    a1_json = _compact_json(a1_graph)
    i2_json = _compact_json(i2_plan)
    receipt_json = _compact_json(probe_receipt)

    prompt = f"""# Turn A2 — Independent Auditor Quantitative Verification & Challenge
- **Case Identifier**: `{canonical_case_id}`
- **Locked Turn A1 SHA-256 (`a1_lock_hash`)**: `{a1_lock_hash.strip()}`
- **Locked Turn I2 SHA-256 (`i2_lock_hash`)**: `{i2_lock_hash.strip()}`
- **Required Output File**: `audit_challenge.json` (`schema_version`: `"visual-v2"`)

## Signed Diagnostic Probe Receipt
```json
{receipt_json}
```

## Auditor Locked Turn A1 Topology (`a1_graph`)
```json
{a1_json}
```

## Investigator Locked Turn I2 Plan (`i2_plan`)
```json
{i2_json}
```

## Instructions
1. **Independent Physics Re-Simulation (`code_execution`)**:
   - Using Python `code_execution` and `domain.py` in your Auditor workspace, independently compute the baseline resistance from `probe_receipt`, fit thermal parameters from `public_packet.json`, and simulate the Investigator's proposed migration (`i2_plan["migration"]`) under your locked Turn A1 feeder topology (`a1_graph["feeder_mapping"]`).
2. **Verification & Challenge Rules**:
   - Check whether `i2_plan` matches your independent parameter identification, 180 s temperature trajectory, and PDU feeder currents (`F1 <= 44.0 A`, `F2 <= 50.0 A`, rack temperatures `< 90.0 °C`).
   - Output `"audit_decision": "APPROVE"` **only** if the Investigator's plan is physically accurate and operationally safe (`"verdict": "safe"`).
   - Output `"audit_decision": "CHALLENGE"` with explicit `"challenge_reasons"` if `i2_plan` causes an `F1` or `F2` PDU overload (`48.0 A > 44.0 A`), thermal violation (`>= 90.0 °C`), wrong hypothesis (`"cooling"`), or trajectory fabrication.
   - Output `"audit_decision": "ABSTAIN"` if required evidence is missing or unidentifiable.
3. **Compact File Output**:
   - Write your final JSON object directly to `audit_challenge.json` (do NOT echo long arrays to stdout) with keys:
     - `"schema_version"`: `"visual-v2"`
     - `"case_id"`: `"{canonical_case_id}"`
     - `"turn_id"`: `"A2"`
     - `"a1_lock_hash"`: `"{a1_lock_hash.strip()}"`
     - `"i2_lock_hash"`: `"{i2_lock_hash.strip()}"`
     - `"probe_receipt_sha256"`: receipt SHA-256 from `probe_receipt`
     - `"audit_decision"`: `"APPROVE" | "CHALLENGE" | "ABSTAIN"`
     - `"independent_parameters"`: independently fitted parameters
     - `"independent_source_t180_C"`: independently simulated $t=180\\text{{ s}}$ source rack temperature
     - `"independent_pdu_currents_A"`: `{{"F1": ..., "F2": ...}}` under the proposed migration
     - `"independent_verdict"`: `"safe"` or `"unsafe, ..."`
     - `"challenge_reasons"`: list of strings (empty if `"APPROVE"`)
     - `"abstained"`: `false` (or `true` if abstaining)
"""
    return _assert_compact_and_leak_free(prompt, f"A2.prompt[{canonical_case_id}]", is_pre_probe=False)


__all__ = [
    "SCHEMA_VERSION_VISUAL_V2",
    "MAX_HARNESS_PROMPT_BYTES",
    "TURN_EXPECTED_FILENAMES",
    "build_role_agents_md",
    "build_i1_prompt",
    "build_i2_prompt",
    "build_a1_prompt",
    "build_a2_prompt",
]
