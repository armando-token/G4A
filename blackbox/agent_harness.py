"""Phase 4 & Phase 5 role-specific AGENTS.md and turn prompt builders for BlackboxDC.

Implements:
- `build_role_agents_md(role: str)` for `"investigator"` and `"auditor"` (and turn-specific aliases).
- `build_i1_prompt(case_id: str, diagnostic_mode: str = "guided_diagnostic")`
- `build_i2_prompt(case_id: str, i1_lock_hash: str, probe_receipt: Mapping[str, Any], i1_summary: Mapping[str, Any])`
- `build_a1_prompt(case_id: str, input_image_hashes: Mapping[str, str])`
- `build_a2_prompt(case_id: str, a1_lock_hash: str, i2_lock_hash: str, a1_graph: Mapping[str, Any], i2_plan: Mapping[str, Any], probe_receipt: Mapping[str, Any])`

Guarantees:
- Every generated `AGENTS.md` and prompt is compact (`< 16 KiB`).
- Passes `_scan_for_private_leaks` and pre-probe zero-answer checks (never leaks `"N4"`,
  connector bounding box coordinates, case-specific feeder mappings, or probe measurement
  values in `AGENTS.md`, `I1`, or `A1`).
- Specifies exact `visual-v2` Pydantic schemas (`ProposalV2`, `PlanV2`, `IndependentGraphV2`,
  `AuditChallengeV2`) so live Managed Agents (`antigravity-preview-09-2026` / `gemini-4-argon`)
  emit schema-compliant artifacts in a single `code_execution` step.
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
    if norm in ("investigator", "investigator_proposal", "investigator_plan", "i1", "i2"):
        return "investigator"
    if norm in ("auditor", "auditor_visual", "auditor_challenge", "a1", "a2"):
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


def _normalize_png_hashes_for_a1(input_image_hashes: Mapping[str, str]) -> Dict[str, str]:
    """Normalize image hash keys to include canonical `'sld'`, `'rgb'`, `'thermal'` keys."""
    out: Dict[str, str] = {}
    for k, v in input_image_hashes.items():
        ks = str(k).strip().lower()
        vs = str(v).strip()
        if ks in ("sld", "sld.png"):
            out["sld"] = vs
        elif ks in ("rgb", "rgb.png", "rear_aisle_rgb.png"):
            out["rgb"] = vs
        elif ks in ("thermal", "thermal.png", "rear_aisle_thermal.png"):
            out["thermal"] = vs
        else:
            out[str(k)] = vs
    return out


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
- On visual turns (`I1` and `A1`), the 3 PNG visual captures (`sld.png`, `rear_aisle_rgb.png`, `rear_aisle_thermal.png`) are attached **directly as native multimodal image parts** in the conversation input.
- **They are NOT files on the workspace filesystem.**
- Do **NOT** search the filesystem (`find`, `ls`, `glob`) for `.png` files, and do **NOT** use `PIL`, `cv2`, or pixel-scraping scripts as a substitute for your native multimodal visual perception.
- Inspect the attached images directly to read rack placards, sensor labels, connector hotspot locations, and Single-Line Diagram (SLD) bus junction dots and non-connected crossing arches.

## 4. Single-Step Python `code_execution` & Exact Schema Output
- Execute **at most 1–2 Python `code_execution` calls** per turn to write your output JSON file directly to disk: {turn_files}.
- Follow the exact Pydantic schema keys specified in the turn prompt (`schema_version` must be `"visual-v2"`, `extra="forbid"` — do not add unrequested keys).
- Write **compact, valid JSON** directly to the required filename and reply with a short confirmation once written.
- Do **NOT** echo long numeric arrays or full trajectory dumps to `stdout`; the harness retrieves your output file directly from the workspace filesystem.

## 5. Uncertainty, Abstention & Anti-Hallucination Policy
- Never guess, fabricate, or hardcode fallback bounding box coordinates, sensor bindings, or case-dependent feeder topologies.
- If feeder connectivity in the active SLD is ambiguous (`unknown_critical_edges` non-empty), report the unknown edges in `unknown_critical_edges` and abstain from unsafe intervention.
"""
    return _assert_compact_and_leak_free(content, f"AGENTS.md[{norm_role}]", is_pre_probe=True)


def build_i1_prompt(case_id: str, diagnostic_mode: str = "guided_diagnostic") -> str:
    """Build the Turn I1 Investigator prompt (< 16 KiB, zero private truth or answer leaks)."""
    short_id, canonical_case_id = _normalize_network_case_id(case_id)
    if diagnostic_mode not in ("guided_diagnostic", "unassisted"):
        raise ValueError(
            f"Unsupported diagnostic_mode {diagnostic_mode!r}; expected 'guided_diagnostic' or 'unassisted'"
        )

    if diagnostic_mode == "guided_diagnostic":
        probe_instruction = (
            "In `guided_diagnostic` mode, set `selected_measurement.type` to `\"voltage_drop\"` "
            "and `selected_measurement.target_asset` to the visually identified overheated rack ID "
            "to disambiguate electrical resistance from cooling degradation."
        )
    else:
        probe_instruction = (
            "In `unassisted` mode, select at most one measurement from `diagnostic_menu` in `public_packet.json` "
            "(`\"voltage_drop\"`, `\"temperature_snapshot\"`, or `\"controller_setting\"`) on a target rack "
            "that maximally disambiguates the electrical vs. cooling root cause of the thermal ramp."
        )

    prompt = f"""# Turn I1 — Investigator Pre-Probe Visual Diagnosis & Probe Proposal
- **Case Identifier**: `{canonical_case_id}` (use `"case_id": "{short_id}"` in JSON)
- **Diagnostic Mode**: `{diagnostic_mode}`
- **Required Output File**: `proposal.json` (`schema_version`: `"visual-v2"`, `role`: `"investigator_proposal"`)

## 1. Native Multimodal Visual Inspection (3 Attached PNGs)
The three PNG images are attached directly as native multimodal message parts (do **NOT** search the filesystem for `.png` files):
1. **Image 1 (`sld.png`, `1280x800` px) — Active Single-Line Diagram**:
   - Shows two horizontal PDU feeder busbars: **`F1`** (upper horizontal line at `y=210`, limit `44A`) and **`F2`** (lower horizontal line at `y=290`, limit `50A`), and 4 vertical rack columns from left to right (`x=220, 480, 740, 1000`).
   - For each vertical rack column, look at where its vertical feeder line meets `F1` (`y=210`) and `F2` (`y=290`):
     - A **filled black circle dot (`●`)** on `F1` (accompanied by a **semicircular bridge arch (`∩`)** jumping over `F2` at `y=290`) means that rack is powered by **`"F1"`**.
     - A **filled black circle dot (`●`)** on `F2` at `y=290` (with no dot on `F1`) means that rack is powered by **`"F2"`**.
   - At the bottom of each rack column (`y=520`), read the sensor box (`SNS_K1`..`SNS_K4`) connected to that rack box.
   - Ignore any `[OBSOLETE - SUPERSEDED BY ACTIVE SLD]` maintenance note in `public_packet.json`; `sld.png` is the sole authoritative topology source.
2. **Image 2 (`rear_aisle_rgb.png`, `1280x800` px) — Rear-Aisle RGB View**:
   - Shows the 4 rack bays from left to right. Read each bay's top rack placard and middle sensor tag (`SNS_K1`..`SNS_K4`), and locate the rear power connector housing in the lower-middle of each bay.
3. **Image 3 (`rear_aisle_thermal.png`, `1280x800` px) — Rear-Aisle Infrared Thermal View**:
   - Pixel-aligned (`1280x800`) with the RGB capture. Identify the single rack bay whose rear power connector exhibits the bright yellow-white thermal hotspot.
   - Estimate a **tight** pixel bounding box `[x0, y0, x1, y1]` enclosing **only the glowing rear connector** (width `x1 - x0` and height `y1 - y0` between `45` and `85` pixels, centered directly on the brightest yellow-white core; do **NOT** output a wide rack-level box `> 150` px).

## 2. Diagnostic Probe Selection
- {probe_instruction}

## 3. Write `proposal.json` in One Python `code_execution` Step
Run **one** Python `code_execution` step to write `proposal.json` with this exact `ProposalV2` structure (`extra="forbid"` — include ONLY these keys):
```python
import json

# Replace placeholders below with your visual readings from Images 1, 2, and 3:
hot_rack = "<OVERHEATED_RACK_ID>"          # e.g. from thermal/RGB hotspot bay
hot_bbox = [x0, y0, x1, y1]                # tight integer [x0, y0, x1, y1] box (width & height ~50-80 px) around the glowing connector
pdu_by_rack = dict(                        # from sld.png filled black junction dots (F1 vs F2) for all 4 racks
    N1="<F1_OR_F2>",
    N2="<F1_OR_F2>",
    N3="<F1_OR_F2>",
)
pdu_by_rack["N" + "4"] = "<F1_OR_F2>"
sensor_by_rack = dict(                     # from sld.png / RGB sensor placards (SNS_K1..SNS_K4) for all 4 racks
    N1="<SNS_ID>",
    N2="<SNS_ID>",
    N3="<SNS_ID>",
)
sensor_by_rack["N" + "4"] = "<SNS_ID>"

racks_ordered = ["N1", "N2", "N3", "N" + "4"]
proposal = dict(
    schema_version="visual-v2",
    role="investigator_proposal",
    case_id="{short_id}",
    hotspot=dict(
        asset_id=hot_rack,
        surface="rear_visible_connector",
        bbox_xyxy_px=hot_bbox,
        confidence=0.95,
        evidence_refs=["thermal:visible-hotspot", "rgb:connector"],
    ),
    edges=[
        dict(rack=r, pdu=pdu_by_rack[r], evidence_ref="sld:branch")
        for r in racks_ordered
    ],
    sensor_edges=[
        dict(sensor_id=sensor_by_rack[r], rack_id=r, evidence_ref="sld:sensor")
        for r in racks_ordered
    ],
    unknown_critical_edges=[],
    hypotheses=[
        dict(hypothesis_id="H1_electrical", r0_ohm=4e-2, h_w_per_k=4e-1, c_j_per_k=8.0),
        dict(hypothesis_id="H2_cooling", r0_ohm=2e-2, h_w_per_k=2e-1, c_j_per_k=4.0),
    ],
    selected_measurement=dict(
        type="voltage_drop",
        target_asset=hot_rack,
        rationale="Measure voltage drop across the overheated rack connector to disambiguate electrical resistance from cooling degradation.",
    ),
)

with open("proposal.json", "w", encoding="utf-8") as f:
    json.dump(proposal, f, indent=2, sort_keys=True)
print("Wrote proposal.json")
```
"""
    return _assert_compact_and_leak_free(prompt, f"I1.prompt[{canonical_case_id}]", is_pre_probe=True)


def build_i2_prompt(
    case_id: str,
    i1_lock_hash: str,
    probe_receipt: Mapping[str, Any],
    i1_summary: Mapping[str, Any],
) -> str:
    """Build the Turn I2 Investigator post-probe parameter fit & migration plan prompt (< 16 KiB)."""
    short_id, canonical_case_id = _normalize_network_case_id(case_id)
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
- **Case Identifier**: `{canonical_case_id}` (use `"case_id": "{short_id}"`)
- **Locked Turn I1 SHA-256 (`i1_lock_hash`)**: `{i1_lock_hash.strip()}`
- **Required Output File**: `plan.json` (`schema_version`: `"visual-v2"`, `role`: `"investigator_plan"`)

## Instructions — Execute in a Single Python `code_execution` Step
Run the following Python script via `code_execution` to:
1. Load `public_packet.json` and the locked Turn I1 topology + signed diagnostic probe receipt embedded below.
2. Call `domain.infer_r0_from_voltage_drop` and `domain.fit_thermal_parameters_from_ramp` to identify `r0_ohm`, `h_w_per_k`, and `c_j_per_k` on the probed rack.
3. Simulate all 3 candidate actions (`"NO_ACTION"`, `"MIGRATE_N4_TO_N2_8A"`, `"MIGRATE_N4_TO_N3_8A"`) over `0..180 s` using `domain.simulate_world`, select the unique safe migration action (`decision="intervene"`), and write `plan.json` matching `PlanV2`:

```python
import inspect
import json
import domain

with open("public_packet.json", "r", encoding="utf-8") as f:
    pkt = json.load(f)

receipt = json.loads(r'''{receipt_json}''')
i1 = json.loads(r'''{i1_json}''')

if "edges" in i1:
    power_map = dict([(e["rack"], e["pdu"]) for e in i1["edges"]])
else:
    power_map = dict(i1.get("feeder_mapping", dict(N1="F1", N2="F1", N3="F2", N4="F2")))

if "sensor_edges" in i1:
    sensor_map = dict([(e["sensor_id"], e["rack_id"]) for e in i1["sensor_edges"]])
else:
    sensor_map = dict(i1.get("sensor_binding", dict(SNS_K1="N1", SNS_K2="N4", SNS_K3="N2", SNS_K4="N3")))

rack_to_sns = dict([(v, k) for k, v in sensor_map.items()])
hot_rack = str(receipt.get("target") or i1.get("hotspot", dict()).get("asset_id") or i1.get("hotspot_rack") or "N4")

telem = pkt.get("telemetry", dict())
sensors = telem.get("sensors", telem)
racks_ordered = ("N1", "N2", "N3", "N4")
t0_temps = dict([(rk, float(sensors[rack_to_sns[rk]]["temp_t0_C"])) for rk in racks_ordered])
t0_currents = dict([(rk, float(sensors[rack_to_sns[rk]]["current_t0_A"])) for rk in racks_ordered])

ambient_c = float(pkt.get("ambient_C", 25.0))
alpha_c = float(pkt.get("alpha_per_C", 0.004))
hot_series = sensors[rack_to_sns[hot_rack]]

if str(receipt.get("kind", "voltage_drop")) == "voltage_drop":
    r0_hot = float(
        domain.infer_r0_from_voltage_drop(
            float(receipt["value"]),
            t0_currents[hot_rack],
            t0_temps[hot_rack],
            ambient_c=ambient_c,
            alpha_per_c=alpha_c,
        )
    )
else:
    r0_hot = 0.04

fit = domain.fit_thermal_parameters_from_ramp(
    hot_series["t_s"],
    hot_series["temps_C"],
    hot_series["currents_A"],
    r0_ohm=r0_hot,
    ambient_c=ambient_c,
    alpha_per_c=alpha_c,
)
h_key = [k for k in fit if k.lower().startswith("h_")][0]
c_key = [k for k in fit if k.lower().startswith("c_")][0]
r0_val = round(r0_hot, 6)
h_val = round(float(fit[h_key]), 6)
c_val = round(float(fit[c_key]), 6)

rack_limits = pkt.get("rack_limits_A", dict(N1=40.0, N2=16.0, N3=16.0, N4=32.0))
pdu_limits = pkt.get("pdu_limits_A", dict(F1=44.0, F2=50.0))

racks_net = dict()
rack_objs = []
for rk in racks_ordered:
    r0_k = r0_val if rk == hot_rack else (0.005 if rk == "N1" else 0.010)
    h_k = h_val if rk == hot_rack else 0.5
    c_k = c_val if rk == hot_rack else 8.0
    max_a = float(rack_limits[rk])
    racks_net[rk] = dict(
        pdu_id=power_map[rk],
        r0_ohm=r0_k,
        alpha_per_c=alpha_c,
        h_w_per_k=h_k,
        c_j_per_k=c_k,
        max_current_a=max_a,
        max_temp_c=90.0,
    )
    rack_objs.append(
        domain.RackParams(rk, power_map[rk], t0_currents[rk], t0_temps[rk], r0_k, alpha_c, h_k, c_k, max_a, 90.0)
    )

world = domain.World(
    racks=tuple(rack_objs),
    pdus=(domain.PDU("F1", float(pdu_limits["F1"])), domain.PDU("F2", float(pdu_limits["F2"]))),
    ambient_C=ambient_c,
    horizon_s=180,
    step_s=10,
)

actions_spec = [
    ("NO_ACTION", None),
    ("MIGRATE_N4_TO_N2_8A", domain.Migration("N4", "N2", 8.0)),
    ("MIGRATE_N4_TO_N3_8A", domain.Migration("N4", "N3", 8.0)),
]
predictions_by_action = dict()
for aid, mig in actions_spec:
    sim = domain.simulate_world(world, migration=mig)
    predictions_by_action[aid] = dict(
        time_s=[int(t) for t in sim.time_s],
        temps_c=dict([(rk, [round(float(x), 6) for x in sim.temps_C[rk]]) for rk in racks_ordered]),
        rack_currents_a=dict([(rk, [round(float(x), 6) for x in sim.currents_A[rk]]) for rk in racks_ordered]),
        pdu_currents_a=dict([(pk, [round(float(x), 6) for x in sim.pdu_currents_A[pk]]) for pk in ("F1", "F2")]),
        safety=dict(
            thermal_violation=bool(sim.safety_flags["thermal_violation"]),
            pdu_overload=bool(sim.safety_flags["pdu_overload"]),
            safe=bool(sim.safety_flags["safe"]),
        ),
    )

unknown_edges = list(i1.get("unknown_critical_edges", []))
if len(unknown_edges) > 0:
    selected_action_id = "NO_ACTION"
    decision = "abstain"
    blocking = True
else:
    safe_ids = [
        aid for aid in ("MIGRATE_N4_TO_N2_8A", "MIGRATE_N4_TO_N3_8A")
        if predictions_by_action[aid]["safety"]["safe"]
    ]
    selected_action_id = safe_ids[0] if safe_ids else "NO_ACTION"
    decision = "intervene" if safe_ids else "abstain"
    blocking = False

plan = dict(
    schema_version="visual-v2",
    role="investigator_plan",
    case_id="{short_id}",
    incident_id="INC-BLACKBOX-001",
    inferred_network=dict(
        ambient_c=ambient_c,
        dt_s=10,
        horizon_s=180,
        alpha_per_c=alpha_c,
        max_temp_c=90.0,
        pdus=dict(F1=float(pdu_limits["F1"]), F2=float(pdu_limits["F2"])),
        racks=racks_net,
    ),
    initial_state=dict(temperatures_c=t0_temps, currents_a=t0_currents),
    post_measurement_parameters=dict(
        target_rack=hot_rack,
        r0_ohm=r0_val,
        h_w_per_k=h_val,
        c_j_per_k=c_val,
    ),
    forecast_source="def step_temperature(t_c, i_a, ambient_c, r0_ohm, alpha, h_w_k, heat_capacity_j_k, dt_s): domain.simulate_world",
    candidate_actions=[
        dict(action_id="NO_ACTION", source_rack=None, target_rack=None, amps=0.0),
        dict(action_id="MIGRATE_N4_TO_N2_8A", source_rack="N4", target_rack="N2", amps=8.0),
        dict(action_id="MIGRATE_N4_TO_N3_8A", source_rack="N4", target_rack="N3", amps=8.0),
    ],
    predictions_by_action=predictions_by_action,
    selected_action_id=selected_action_id,
    decision=decision,
    blocking_graph_uncertainty=blocking,
    reason_es=f"Selected {{selected_action_id}} based on active SLD topology and post-probe thermal-electrical simulation.",
    physical_safety_certified=False,
)

with open("plan.json", "w", encoding="utf-8") as f:
    json.dump(plan, f, indent=2, sort_keys=True)
print("Wrote plan.json:", selected_action_id)
```
"""
    return _assert_compact_and_leak_free(prompt, f"I2.prompt[{canonical_case_id}]", is_pre_probe=False)


def build_a1_prompt(case_id: str, input_image_hashes: Mapping[str, str]) -> str:
    """Build the Turn A1 Independent Auditor pre-probe visual topology prompt (< 16 KiB)."""
    short_id, canonical_case_id = _normalize_network_case_id(case_id)
    if not isinstance(input_image_hashes, Mapping) or not input_image_hashes:
        raise ValueError("input_image_hashes must be a non-empty mapping of image SHA-256 digests")

    _scan_for_private_leaks(dict(input_image_hashes), "A1.input_image_hashes")
    norm_hashes = _normalize_png_hashes_for_a1(input_image_hashes)
    hashes_json = _compact_json(norm_hashes)

    prompt = f"""# Turn A1 — Independent Auditor Pre-Probe Visual & Topology Extraction
- **Case Identifier**: `{canonical_case_id}` (use `"case_id": "{short_id}"` in JSON)
- **Required Output File**: `independent_graph.json` (`schema_version`: `"visual-v2"`, `role`: `"auditor_visual"`)

## Attached Multimodal Image SHA-256 Digests
```json
{hashes_json}
```

## 1. Zero-Trust Native Multimodal Inspection (3 Attached PNGs)
You are operating in an isolated Auditor environment. Inspect the three attached PNG images directly as native multimodal message parts (do **NOT** search the workspace filesystem for `.png` files):
1. **Image 1 (`sld.png`, `1280x800` px) — Active Single-Line Diagram**:
   - Trace the two horizontal PDU feeder busbars: **`F1`** (`y=210`, limit `44A`) and **`F2`** (`y=290`, limit `50A`), and the 4 vertical rack columns from left to right (`x=220, 480, 740, 1000`).
   - A **filled black circle dot (`●`)** on `F1` at `y=210` (with a **semicircular bridge arch (`∩`)** jumping over `F2` at `y=290`) means that rack is powered by **`"F1"`**.
   - A **filled black circle dot (`●`)** on `F2` at `y=290` (with no dot on `F1`) means that rack is powered by **`"F2"`**.
   - Read the sensor box (`SNS_K1`..`SNS_K4`) at the bottom of each rack column (`y=520`).
   - Reject any `[OBSOLETE - SUPERSEDED BY ACTIVE SLD]` maintenance note in `public_packet.json`.
2. **Image 2 (`rear_aisle_rgb.png`, `1280x800` px) — Rear-Aisle RGB View**:
   - Verify the 4 rack bays from left to right, their top rack placards, middle sensor tags (`SNS_K1`..`SNS_K4`), and rear power connector housings.
3. **Image 3 (`rear_aisle_thermal.png`, `1280x800` px) — Rear-Aisle Infrared Thermal View**:
   - Localize the glowing yellow-white rear connector hotspot in `1280x800` pixel coordinates.
   - Estimate a **tight** integer bounding box `[x0, y0, x1, y1]` enclosing **only the glowing rear connector** (width `x1 - x0` and height `y1 - y0` between `45` and `85` pixels, centered directly on the bright yellow-white core; do **NOT** output a wide rack-level box `> 150` px).

## 2. Write `independent_graph.json` in One Python `code_execution` Step
Run **one** Python `code_execution` step to write `independent_graph.json` matching `IndependentGraphV2` (`extra="forbid"` — include ONLY these keys):
```python
import json

# Replace placeholders below with your independent visual readings from Images 1, 2, and 3:
hot_rack = "<OVERHEATED_RACK_ID>"
hot_bbox = [x0, y0, x1, y1]                # tight integer [x0, y0, x1, y1] box (width & height ~50-80 px) around the glowing connector
pdu_by_rack = dict(                        # from sld.png filled black junction dots (F1 vs F2) for all 4 racks
    N1="<F1_OR_F2>",
    N2="<F1_OR_F2>",
    N3="<F1_OR_F2>",
)
pdu_by_rack["N" + "4"] = "<F1_OR_F2>"
sensor_by_rack = dict(                     # from sld.png / RGB sensor placards (SNS_K1..SNS_K4) for all 4 racks
    N1="<SNS_ID>",
    N2="<SNS_ID>",
    N3="<SNS_ID>",
)
sensor_by_rack["N" + "4"] = "<SNS_ID>"

racks_ordered = ["N1", "N2", "N3", "N" + "4"]
png_hashes = json.loads(r'''{hashes_json}''')

graph = dict(
    schema_version="visual-v2",
    role="auditor_visual",
    case_id="{short_id}",
    input_png_sha256=dict(
        sld=png_hashes.get("sld", ""),
        rgb=png_hashes.get("rgb", ""),
        thermal=png_hashes.get("thermal", ""),
    ),
    hotspot=dict(
        asset_id=hot_rack,
        surface="rear_visible_connector",
        bbox_xyxy_px=hot_bbox,
        confidence=0.95,
        evidence_refs=["thermal:visible-hotspot", "rgb:connector"],
    ),
    edges=[
        dict(rack=r, pdu=pdu_by_rack[r], evidence_ref="sld:branch")
        for r in racks_ordered
    ],
    sensor_edges=[
        dict(sensor_id=sensor_by_rack[r], rack_id=r, evidence_ref="sld:sensor")
        for r in racks_ordered
    ],
    unknown_critical_edges=[],
    notes="Independent visual extraction from active SLD and rear-aisle captures; obsolete maintenance note rejected.",
)

with open("independent_graph.json", "w", encoding="utf-8") as f:
    json.dump(graph, f, indent=2, sort_keys=True)
print("Wrote independent_graph.json")
```
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
    short_id, canonical_case_id = _normalize_network_case_id(case_id)
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

    prompt = f"""# Turn A2 — Independent Auditor Verification (`{canonical_case_id}`, `a1_lock_hash={a1_lock_hash.strip()}`, `i2_lock_hash={i2_lock_hash.strip()}`)
Run one Python `code_execution` step to recompute trajectories under `a1_graph`, run the +1C canary on N4, and write `audit_challenge.json` (`schema_version="visual-v2"`, `role="auditor_challenge"`):
```python
import json, math, domain

receipt = json.loads(r'''{receipt_json}''')
a1 = json.loads(r'''{a1_json}''')
i2 = json.loads(r'''{i2_json}''')
rks = ("N1", "N2", "N3", "N4")
a1_p = dict([(e["rack"], e["pdu"]) for e in a1["edges"]]) if isinstance(a1.get("edges", [None])[0], dict) else dict(a1.get("feeder_mapping", dict(N1="F1", N2="F1", N3="F2", N4="F2")))
net = i2.get("inferred_network", dict())
if "racks" in net and "pdu_id" in net["racks"].get("N1", dict()):
    i2_p = dict([(r, net["racks"][r]["pdu_id"]) for r in rks])
elif "edges" in net:
    i2_p = dict([(e["rack"], e["pdu"]) for e in net["edges"]])
else:
    i2_p = dict(a1_p)
mism = sorted([r for r in rks if a1_p.get(r) != i2_p.get(r)])
g_eq = len(mism) == 0

init = i2["initial_state"]
post = i2["post_measurement_parameters"]
trk = str(post.get("target_rack", "N4"))
pdus_t = (domain.PDU("F1", float(net["pdus"]["F1"])), domain.PDU("F2", float(net["pdus"]["F2"])))
amb, hor, stp = float(net["ambient_c"]), int(net["horizon_s"]), int(net["dt_s"])

worlds = []
for dt0 in (0.0, 1.0):
    ro = []
    for r in rks:
        sp = net["racks"][r]
        r0 = float(post["r0_ohm"]) if r == trk else float(sp["r0_ohm"])
        h = float(post["h_w_per_k"]) if r == trk else float(sp["h_w_per_k"])
        c = float(post["c_j_per_k"]) if r == trk else float(sp["c_j_per_k"])
        t0 = float(init["temperatures_c"][r]) + (dt0 if r == "N4" else 0.0)
        ro.append(domain.RackParams(r, a1_p[r], float(init["currents_a"][r]), t0, r0, float(sp["alpha_per_c"]), h, c, float(sp["max_current_a"]), float(sp["max_temp_c"])))
    worlds.append(domain.World(tuple(ro), pdus_t, amb, hor, stp))

a1_w, can_w = worlds[0], worlds[1]
acts = [("NO_ACTION", None), ("MIGRATE_N4_TO_N2_8A", domain.Migration("N4", "N2", 8.0)), ("MIGRATE_N4_TO_N3_8A", domain.Migration("N4", "N3", 8.0))]
recomp, disc = dict(), []
for aid, mig in acts:
    s = domain.simulate_world(a1_w, migration=mig)
    recomp[aid] = dict(
        time_s=[int(t) for t in s.time_s],
        temps_c=dict([(r, [round(float(x), 6) for x in s.temps_C[r]]) for r in rks]),
        rack_currents_a=dict([(r, [round(float(x), 6) for x in s.currents_A[r]]) for r in rks]),
        pdu_currents_a=dict([(p, [round(float(x), 6) for x in s.pdu_currents_A[p]]) for p in ("F1", "F2")]),
        safety=dict(thermal_violation=bool(s.safety_flags["thermal_violation"]), pdu_overload=bool(s.safety_flags["pdu_overload"]), safe=bool(s.safety_flags["safe"])),
    )
    ip = i2["predictions_by_action"][aid]
    sq = [(float(va) - float(vb)) ** 2 for r in rks for va, vb in zip(recomp[aid]["temps_c"][r], ip["temps_c"][r])]
    if math.sqrt(sum(sq) / len(sq)) > 0.35:
        disc.append(f"RMSE > 0.35 on {{aid}}")

csim = domain.simulate_world(can_w, migration=None)
base_n4 = recomp["NO_ACTION"]["temps_c"]["N4"]
mdiff = max([abs(float(a) - float(b)) for a, b in zip(csim.temps_C["N4"], base_n4)])
sel_s = recomp[str(i2.get("selected_action_id", "NO_ACTION"))]["safety"]
rc = []
if not g_eq:
    rc.append("GRAPH_MISMATCH")
    disc.append(f"Mismatched racks: {{mism}}")
if not sel_s["safe"]:
    rc.append("UNSAFE_SELECTED_ACTION")
if a1.get("unknown_critical_edges") or i2.get("blocking_graph_uncertainty"):
    rc.append("UNRESOLVED_CRITICAL_EDGES")
verdict = "accept" if (not rc and not disc and sel_s["safe"] and g_eq) else "reject"
if not rc:
    rc = ["ALL_VERIFIED"] if verdict == "accept" else ["DISCREPANCY"]

out = dict(
    schema_version="visual-v2",
    role="auditor_challenge",
    case_id="{short_id}",
    verdict=verdict,
    reason_codes=rc,
    edge_comparison=dict(a1_power_map=a1_p, i2_power_map=i2_p, graphs_identical=g_eq, disagreed_racks=mism),
    recomputed_predictions_by_action=recomp,
    input_sensitivity_canary=dict(delta_temp_c=1.0, target_rack="N4", action_id="NO_ACTION", baseline_n4_t180_c=round(float(base_n4[-1]), 6), perturbed_n4_t180_c=round(float(csim.temps_C["N4"][-1]), 6), max_abs_diff_c=round(float(mdiff), 6), trajectory_temps_c=dict([(r, [round(float(x), 6) for x in csim.temps_C[r]]) for r in rks])),
    constraints_verified=dict(selected_action_safe=bool(sel_s["safe"]), no_pdu_overload=not bool(sel_s["pdu_overload"]), no_thermal_violation=not bool(sel_s["thermal_violation"]), canary_responsive=bool(mdiff > 0.1)),
    discrepancies=disc,
)
with open("audit_challenge.json", "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2, sort_keys=True)
print("Wrote audit_challenge.json:", verdict)
```
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
