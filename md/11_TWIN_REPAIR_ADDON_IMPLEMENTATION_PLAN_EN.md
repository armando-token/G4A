# BlackboxDC Twin Repair — implementation plan for the missing layer

**Date:** 25 October 2026 · **Target track:** Problem Statement Two, **More Dimensions** · **Implementation language:** Python + TypeScript/JavaScript · **Document status:** implementation specification, not a claim that the extension is already built.

> **Give this document to the coding agent phase by phase.** Stop after each exit gate, inspect the evidence, then continue. This plan covers the *new Twin Repair extension* to BlackboxDC. It does not ask the agent to rebuild the complete previous project or its historical four-turn campaign.

## 0. What we are proving

An operations engineer finds a hot rear connector in a data-center rack. Yesterday's digital twin shows the rack's electrical feeder connections as they were *before* a maintenance change. The engineer uploads the current, revised single-line diagram (SLD). Gemini 4 Argon, running inside a real Antigravity Managed Agent, inspects an **actual render** of the stale twin, identifies which visible feeder cables disagree with the revised SLD, proposes a bounded scene patch, and watches the scene after that patch is rendered. The local verifier checks the new electrical graph and recomputes the consequence of moving an 8 A workload away from the hot rack. The visible outcome is a changed engineering decision, not a nicer picture.

This is a deliberately synthetic, disclosed **as-built drift** case. The model does not create a physical safety certification. The demo never actuates a PDU, switch, breaker, or server.

### 0.1 The engineering example and expected consequence

| Quantity | Stale twin / SLD A | Current as-built SLD B |
|---|---:|---:|
| F1 limit | 44 A | 44 A |
| F2 limit | 50 A | 50 A |
| Rack currents | N1 36 A, N2 4 A, N3 4 A, N4 24 A | same |
| Feeder F1 | N1 + N2 = 40 A | N1 + N3 = 40 A |
| Feeder F2 | N3 + N4 = 28 A | N2 + N4 = 28 A |
| Candidate 8 A transfer from hot N4 | N3 seems safe if A is assumed | N3 would make F1 **48 A > 44 A** |
| Current safe receiver | N3 | **N2**: F1 remains 40 A, F2 becomes 28 A |

The rack connector is initially approximately 88.95 °C; the synthetic limit is 90 °C. With no action the 180 s simulation reaches approximately 98.83 °C. An 8 A offload to the **correct** receiver lowers the modeled N4 connector to approximately 52.53 °C at 180 s. Both candidate offloads can cool N4, so **thermal benefit alone cannot distinguish a safe electrical action**. The feeder topology matters.

The specific before/after graph score is **2 of 4 correct rack→feeder edges → 4 of 4**. The expected recommendation flips **N3 → N2**. These numbers are reference expectations for the local synthetic case; they must be recomputed by independent code and never supplied to the Critic as its answer. If the renderer cannot show F1/F2 cables clearly, the case does **not** meet the track, even if a hidden JSON graph changes.

Here an “8 A transfer” means moving an **8 A-equivalent IT workload** from servers in N4 to servers in a receiving rack. The system does **not** physically switch feeder cables. The 3D patch corrects the *software twin's description* of wiring that maintenance already changed. Real data centers can have dual-corded equipment and redundant A/B power paths; this four-rack, one-feeder-per-rack fixture is a deliberately simplified synthetic demonstration, not a production electrical design.

### 0.2 What is already available and what is genuinely missing

The reference implementation inspected locally is at `/Users/armandosilva/DATA/Beta` as of the last saved Oct 24 state. The actual event-day submission repository or cloud host may differ; Phase A therefore starts with an explicit source audit. The following are **reuse targets and interface references**, not a license to represent pre-event code as newly built event work:

| Existing component | Observed capability | New work required here |
|---|---|---|
| `blackbox_domain.py` | `simulate_network`, `apply_action`, electrothermal recurrence, calibration helpers | Invoke the existing physics path from the repaired graph; add only a small adapter and independent acceptance test. |
| `visual_v2/fixture.py` | `build_visual_v2_pair`; deterministic A/B SLD + RGB/thermal fixtures and private reference separation | Add the *stale A → current B* revision packet; keep B truth sealed from agents. |
| `visual_v2/sld.py` | SLD SVG/PNG generation and normalized evidence boxes | Produce a current as-built SLD B with legible revision mark and connections. |
| `visual_v2/scene_bridge.py` | Deterministic RGB/thermal capture, metadata, hotspot registration | Render a model-authored scene specification and both pre/post patch states. |
| `visual_v2/static3d/scene.js` | Three.js four-rack scene, thermal shader and inspection poses | **Parameterize the scene; explicitly draw F1/F2 buses and each rack feeder cable.** Current version hardcodes racks, N4 heat and camera and has no visible PDU→rack connections. |
| `visual_v2/static3d/capture.mjs` | Headless Chrome capture at 1280×800; optional approach stills | Add input scene path and Linux Chromium support. Current default path and Metal flags are macOS-specific. |
| `file_pilot.ManagedFileRunner` | Proven background Managed Agent run, model check, remote JSON retrieval, token accounting and cleanup | Reuse transport; make two narrow agent roles, immutable asset manifests and new JSON contracts. |
| `visual_v2/contracts.py`, `orchestrator.py`, `report.py` | Validation, visual evidence checks, campaign and dossier | Add `SceneSpec`, patch validator, render→critique→repair orchestration and concise evidence card. |

Prior A/B, 8-turn visual campaigns and historical tests are useful confidence in transport and physics. They are **not** evidence that the new creation-and-repair loop works, and must not be shown as event-day output. The user's earlier plan files `00_START_HERE.md` through `10_EVENT_GUIDE_PIVOT.md` remain historical reference. This file is the **single active specification for the extension**.

### 0.3 Success is an observable chain, not a narrative

`fresh revised SLD B → Builder/scene spec → render0 actual PNG → Argon Critic sees PNG + SLD B → validated Argon patch → render1 actual PNG → sealed local graph score → new safety computation → engineer approval state`

Each arrow must have an artifact, a hash and a timestamp. Minimum winning proof: one completed genuine Argon Managed Agent interaction that creates a **non-hardcoded** correction from pixels; image bytes and scene hashes differ; the visible cable route changes; graph accuracy rises to 4/4; the simulated 8 A receiver changes to N2; the entire live critical path is measured. Two Managed Agent roles (Builder and Critic) are the target, with separate remote environments and role-specific evidence.

### 0.4 Track, rules, and hard constraints

The private participant guide provided at the event says the submission must choose at least one of two tracks; **More Dimensions** specifically asks for creation, rendering, observation, critique and repair. Its judging priorities are live working demo with real input under three minutes (50%), concrete useful problem (25%), originality (15%) and a clear no-slides three-minute explanation (10%). It excludes generic image analyzers, Streamlit apps and dashboard-centered projects. It requires a public GitHub repository, a one-minute demo video and clear identification of work built during the event; submissions close **5:00 PM Pacific time**. Treat the event guide as authoritative over earlier assumptions. The public [event page](https://cerebralvalley.ai/e/gemini-4-argon-sf) emphasizes pushing Argon to its limits and separately lists a Best Use of Managed Agents prize.

**Build policy:** emphasize the new closed visual loop and its safety consequence. No simulation result is presented as a real electrical operating instruction. No anonymous public endpoint may spend API credit. Protect secrets, ownership rights and the distinction between pre-event design and event-day implementation.

## 1. Architecture and interfaces

~~~text
Operator: current SLD B PNG + case selection / uploaded changed drawing
    |
    +--> Public input vault: B image, initial currents/limits, case ID, hashes
    |
    +--> Builder Managed Agent (Argon; old A only for stale baseline)
    |      -> scene_spec.v1.json
    |      -> schema validation
    |      -> Three.js renderer -> render0_wiring.png + render0_thermal.png
    |
    +--> Critic Managed Agent (Argon; B + render0 images; no truth JSON)
           -> repair_patch.v1.json with visual evidence
           -> local patch whitelist + validator
           -> Three.js renderer -> render1_wiring.png + render1_thermal.png
           -> independent sealed verifier
           -> electrothermal safety recomputation
           -> result card: before/after, 2/4→4/4, N3→N2, approval needed
~~~

**Trust boundaries:** the agent can infer and propose a scene; it cannot rewrite physical limits, currents, hidden oracle, pass/fail thresholds, renderer executable, or actuation state. Scene specification and patch are **data** parsed by fixed code, never model-written JavaScript executed as the demo's critical path. The Critic gets the as-built drawing and actual rendered pixels; it does not receive the ground-truth edge table or the reference answer. The private scorer is called only after the patch file is locked.

### 1.1 Suggested extension tree

Place modules under the *actual current event-day repository* after Phase A maps its layout; names below are suggested, not grounds to duplicate existing modules.

~~~text
twin_repair/
  __init__.py
  contracts.py           # strict scene, patch and evidence validation
  challenge.py           # stale/current fixture; input manifest; truth seal
  physics_adapter.py     # graph -> existing simulation; action comparison
  render_bridge.py       # scene JSON -> Three.js capture; asset validation
  managed_roles.py       # Builder and Critic prompts; proven file transport
  orchestrator.py        # immutable run state machine, ledger, fail-closed gates
  verifier.py            # graph, image and physics acceptance criteria
  report.py              # minimal result card and export
  prompts/
    builder.txt
    critic.txt
  schemas/
    scene_spec_v1.schema.json
    repair_patch_v1.schema.json
  web/
    index.html           # single task page and before/after twin
    app.js
    styles.css
tests/
  test_twin_repair_contracts.py
  test_twin_repair_render.py
  test_twin_repair_orchestrator.py
  test_twin_repair_physics.py
~~~

Don't create a second simulation engine, fake provider, SLD drawing library, login system, 3D framework, or generic multiagent abstraction. Use the existing repository's package structure if it already has equivalent modules. Vendor or pin only indispensable dependencies.

### 1.2 Phase gates and execution order

| Phase | Build slice | Exit artifact | May proceed when |
|---|---|---|---|
| A | Audit, source map and frozen A/B challenge | `case_manifest.json`, sealed `truth.json`, baseline tests | Existing physics and A/B fixture are reproducible in today's repo. |
| B | Typed scene specification and truthful 3D cables | `scene_spec.json`, `render0_wiring.png`, `render0_thermal.png` | Different edge assignments visibly change routed cable pixels. |
| C | Local patch/verifier loop with fake model payloads | before/after render, local score, physics flip | Golden patch gives 2/4→4/4 and N3→N2; bad patch fails closed. |
| D | Genuine Argon Builder/Critic Managed Agent turns | interaction IDs, remote files, authentic patch | At least one live Critic patch succeeds without truth leakage. |
| E | End-to-end orchestration, timing and failure control | single `run.json` dossier | Fresh invocation completes correctly with honest elapsed time. |
| F | Judge-ready public web experience | public case page, bounded live run, before/after result | A first-time visitor understands and can test the loop without an explanation. |
| G | Final checks, video and submission | public repo and one-minute video | Links public; today's new work identified; submit on time. |

Do not start Phase F before Phase C; a polished page around an unverified 3D scene is counterproductive. Phase D is the first gate for any claim that Argon repaired the twin. If time runs short, a working Phase D with a simple static result page is more valuable than a sophisticated incomplete UI.

## 2. Phase A — audit and construct the as-built drift challenge

### A1. Confirm the actual source state before editing

1. In the event-day working directory record UTC time, Git root, branch, HEAD, `git status --short`, Python/Node/Chromium versions and the list of existing modules. Save as `runs/preflight/source_state.json`. Do not copy an old `/Users/armandosilva/DATA/Beta` tree into the public repo and claim it was built today.
2. Find the current equivalents of the ten components in §0.2 with `rg --files`, inspect exact signatures, and update a short `runs/preflight/reuse_map.md`. If an equivalent is absent, implement the smallest local adapter in the tree above.
3. Run the available current test commands once and save exit codes plus logs. The older reference has `checks_visual_v2.py` and several `checks*.py` scripts; execute only those actually present. Preserve source status so the extension's changes are easy to identify.
4. Confirm the provider credentials through a **one-call, low-budget** authenticated health check; never print the secret, service-account JSON or entire environment. Use the credential route already working on the actual machine. Do not redesign ADC/API authentication during this addon unless it is broken.

### A2. Define public input, private truth, and revision identity

Create a deterministic synthetic case `case_id` with `revision_old=A`, `revision_current=B`, and an explicit maintenance note such as `feeder reassignment N2↔N3 at 09:10`. The **notice is for the operator and private report**, not a substitute for visual inference. Public agent input consists of the relevant PNG images and permitted currents/limits. Private truth lives outside the remote agent source tree and outside any public web response.

Create one cheap control case from the same generator with `revision_current=A` and **no feeder change**. Its correct outcome is `no_change`, not the N2/N3 patch. This gives public visitors a second real input and tests that the agent is not hardcoded to repair every scene. Keep the primary B drift case as the judged demo.

Generate or reuse:

- `old_sld_A.png`, made from the original A graph; send it **only** to Builder.
- `current_sld_B.png`, made from B; send it to Critic and show it in the operator UI. Its rack/feeder associations must be legible at the size actually sent to the model. Put revision B in the drawing title block.
- `old_thermal_rear.png` or an equivalent N4 rear thermal image, shared where necessary. The hot spot stays on N4's rear connector; do not move it to make the repair easier.
- `public_state.json`: rack IDs/current A, feeder IDs/continuous limit A, sensor IDs, image dimensions, allowed scene bounds and action size 8 A. This file must **not** contain current B edge assignments, the safe receiver, desired patch or reference graph.
- `truth.json`: old and current edge tables, expected pre/post scores and expected physics results; store under a private path unavailable to agents and web clients.
- `case_manifest.json`: SHA-256 of every public asset, truth file and renderer version; explicit old/current revision labels; schema version; creation time. Freeze it for each run.

Keep A and B identical except the N2/N3 feeder swap and corresponding SLD pixels. Check currents, rack positions, thermal image, labels, feeder limits and simulation parameters are identical. A human should immediately see that the SLD is a credible electrical drawing rather than an arbitrary block diagram: two feeder rails F1/F2, breakers with ratings, four branch lines and four labeled rack loads, a title block, units in A, revision marker, and a legend. Use the actual drawing standard naming consistently; do **not** imply an ANSI/IEEE stamp or code compliance without an engineering review.

### A3. Independent numeric oracle

Implement `reference_case()` from the domain data and assert:

~~~text
sum(rack currents) = 68 A
A: feeder(N1,N2,N3,N4) = (F1,F1,F2,F2)
B: feeder(N1,N2,N3,N4) = (F1,F2,F1,F2)
initial feeder currents A and B = (40 A, 28 A)
B + transfer N4->N3 by 8 A: F1 = 48 A > 44 A; reject
B + transfer N4->N2 by 8 A: F1 = 40 A; F2 = 28 A; accept if thermal criterion also holds
No action at 180 s: N4 connector > 90 C
Correct 8 A transfer at 180 s: N4 connector < 90 C
~~~

Use the current `simulate_network` implementation for the thermal calculation and a separately written simple feeder-current summation for electrical checks. Do not encode the desired receiver as a special case in the physics function. Require charge/current conservation and a unique rack→feeder mapping. If the figures above change because the actual event-day model differs, stop and update the fixture plus this spec together; don't silently force expected outputs.

**Phase A tests:** A/B public diff test, A→A no-change control, SHA manifest verification, SLD readability at model input scale, 68 A conservation, 44/50 A limit check, safe/unsafe transfer test, and a privacy test that scans every agent-bound file for private `truth`, correct receiver and expected patch fields. **Exit:** both public cases are reproducible from seed and their private oracle is independent of the proposed model answer.

## 3. Phase B — turn the scene into a real, parameterized 3D twin

### B1. SceneSpec v1: a bounded, model-writable data contract

The Builder writes JSON; the renderer owns executable Three.js code. Define one strict `scene_spec_v1.schema.json` and mirror its checks in Python (`validate_scene_spec`). Reject unknown keys, NaN/Infinity, strings where numbers are required, duplicate IDs, missing racks, out-of-bounds coordinates, malformed color values, excessive lengths and non-canonical paths. The following is the *conceptual* contract; implement exact JSON Schema with all properties and `additionalProperties: false` at each object level:

~~~json
{
  "schema_version": "scene_spec.v1",
  "case_id": "...",
  "source_revision": "A",
  "coordinate_system": "meters; floor=y0; rear=positive_z",
  "viewport": {"width_px": 1280, "height_px": 800, "camera_pose": "rear_inspection"},
  "racks": [
    {"rack_id": "N1", "center_x_m": 0.0, "width_m": 0.6, "height_m": 2.0,
     "depth_m": 1.0, "connector_side": "rear", "body_temp_c": 29.5,
     "connector_temp_c": 36.0}
  ],
  "feeders": [
    {"feeder_id": "F1", "anchor_x_m": -0.45, "anchor_y_m": 2.20,
     "limit_a": 44.0, "style": "blue"}
  ],
  "edges": [
    {"rack_id": "N1", "feeder_id": "F1"}
  ],
  "hotspot": {"rack_id": "N4", "surface": "rear_connector",
               "temperature_c": 88.95030198217694, "sigma_m": 0.062},
  "notes": ["inferred from old revision A and thermal input"]
}
~~~

Do **not** let model output control `limit_a`, `current_a`, image size, material shader source, script URLs or hidden truth. The feeder limits above should be cross-checked against immutable public state; consider them display-only projections copied by the renderer from trusted state. Rack dimensions/camera must be bounded; for this challenge constrain exactly four ordered rack IDs N1–N4, feeder IDs F1–F2, one edge per rack, one hotspot on a known connector, temperatures in a plausible finite range, camera pose from a fixed whitelist. Treat `notes` as untrusted display text and render via `textContent`, never HTML.

**Key invariant:** the feeder connections in `edges` must determine both (a) the visual cable geometry and (b) the graph later used by physics. Do not keep a second hardcoded graph in the renderer or calculation path. The only independent duplicate is the sealed verifier oracle.

### B2. Parameterize the existing Three.js scene

Keep the established dimensions and thermal shader where practical. Refactor `scene.js` so an immutable validated spec is loaded *before* creating meshes. `capture.mjs` should receive the absolute approved spec path (or serve a content-addressed JSON under a run-local root) and pass a narrow URL such as `/scene.html?spec=<id>`. The browser never chooses arbitrary filesystem paths. The renderer must expose `READY`, `renderScene(mode, pose)`, `getMetadata()` and a stable error state. Preserve legacy no-spec mode only if older tests need it; the Twin Repair path **must require** a spec.

Implement visibly separate electrical elements:

1. A rear overhead F1 bus and F2 bus, spatially separated by at least 0.18 m in 3D and rendered with different high-contrast colors plus actual `F1`/`F2` geometry labels.
2. A branch feeder from its bus to each rack rear power inlet. The cable's geometry must be derived deterministically from `edges`: bus anchor → reserved lane/waypoint → rack inlet, with unique rack lane offset to avoid overlap. Use `THREE.TubeGeometry` or line geometry with a width that remains visible after screenshot compression. Avoid two cable paths fully occluding each other. Show colored outlet or badge at the endpoint, but do not rely on labels alone.
3. Rack faces, rear PDU post and N4 connector geometry tied to spec IDs. Keep the hot spot on the **rear** connector; the front face must not display an unexplained 89 °C patch. If the current shader uses hardcoded `RACK_SPECS[3]` and `uN4FaultCenter`, replace those references with the spec-derived hot rack and connector center.
4. Deterministic camera poses: `wiring_oblique` should make both buses and all branch cables countable; `rear_inspection` should preserve RGB/thermal registration and the thermal evidence. Do not ask the Critic to infer rear cables from a view where they are hidden.
5. An unobtrusive in-scene revision label `Twin from A`, rendered as geometry or HUD in the screenshot, makes the stale state honest. The Critic's target SLD has `As-built B`.

Suggested fixed world coordinates: four rack centers x = `0.00, 0.82, 1.64, 2.46` m; rack y = `1.0` m center; rack rear z ≈ `+0.5` m; connector face z ≈ `+0.555` m; cable buses behind/above racks, with one at y ≈ `2.20` and a distinct z lane. Tune exact camera position by looking at the captured image, not by trusting geometric intuition. Use the renderer's projection matrix to record each cable's approximate 2D corridor for image verification.

### B3. Capture on laptop and Linux cloud host

Extend the existing `capture.mjs` to accept `scene <spec-path> <out-dir>` and emit:

~~~text
render_wiring.png         1280×800, all feeder routes and labels visible
render_rgb_rear.png       1280×800, same pose as thermal
render_thermal_rear.png   1280×800, same pose as RGB
render_meta.json          spec SHA, revision, projected cable corridors, hotspot ROI, camera matrices
~~~

Check PNG decoding, output dimensions, nonempty alpha/color variance, sane file size, complete rack visibility and correct hotspot registration. Maintain the previous ≤3 px RGB/thermal registration target for the hot connector. A compressed PNG target of <512 KiB per image is reasonable if the existing transport needs it, but **visual legibility wins**; if compression destroys cable labels, reduce the transmitted crop or use the provider's accepted limit rather than hiding evidence.

Set `CHROME_EXECUTABLE_PATH` explicitly on Linux or use Playwright's installed Chromium if allowed. Replace the hardcoded `--use-angle=metal` with platform-appropriate flags; do not assume the cloud instance has a GPU. Test with software WebGL/SwiftShader and capture browser console, page errors, WebGL availability and context loss. Fix the static server path check using `path.relative(root, requested)` and reject upward/outside paths, not a string prefix test. Serve only the run's approved files and trusted bundled scripts. Pin Three.js and Playwright versions in lockfiles. Never load a CDN at judge time if it can fail with network conditions; bundle dependencies.

### B4. Visual truth test before using any model

Render two locally created specs identical in every field except N2/N3 feeder assignment. Compare the `render_wiring.png` images. Require:

- Their SHA-256 values differ; at least one nonempty changed pixel region overlaps each of the N2 and N3 cable corridors. A single changed text label does **not** satisfy this test: mask out HUD/label rectangles when checking cable geometry.
- N1/N4 cable pixels remain stable within a small anti-aliasing tolerance. Hotspot position in thermal image remains stable; feeder changes should not magically move the connector.
- Projected cable start/end metadata matches the assigned feeder and rack. A reviewer can identify F1/F2→N1–N4 by looking at the unannotated screenshot.
- Two captures with the same spec and Chromium build are identical or fall within a documented bounded pixel tolerance if driver differences prevent byte identity. Record the exact environment that produced each capture.

**Phase B exit:** the *observable rendered world* is graph-coupled, the wiring view is understandable to a human, and the renderer runs on the actual deployment platform. Until this exit, no agent call will prove visual self-repair.

## 4. Phase C — deterministic repair protocol and private verifier

Implement and test the loop with a **test-only fake agent payload** before paying for managed calls. Label fake payloads `source=fixture` and never use them as model evidence in the demo.

### C1. RepairPatch v1, deliberately narrow

~~~json
{
  "schema_version": "repair_patch.v1",
  "case_id": "...",
  "basis_scene_sha256": "64 lowercase hex characters",
  "target_revision_seen": "B",
  "observed_render_edges": [
    {"rack_id": "N2", "feeder_id": "F1",
     "image_ref": "render0_wiring.png", "bbox_xyxy_px": [0, 0, 1, 1]},
    {"rack_id": "N3", "feeder_id": "F2",
     "image_ref": "render0_wiring.png", "bbox_xyxy_px": [0, 0, 1, 1]}
  ],
  "target_sld_edges": [
    {"rack_id": "N2", "feeder_id": "F2",
     "image_ref": "current_sld_B.png", "bbox_xyxy_px": [0, 0, 1, 1]},
    {"rack_id": "N3", "feeder_id": "F1",
     "image_ref": "current_sld_B.png", "bbox_xyxy_px": [0, 0, 1, 1]}
  ],
  "discrepancies": [
    {"rack_id": "N2", "observed_feeder": "F1", "required_feeder": "F2",
     "render_evidence_bbox_xyxy_px": [0, 0, 1, 1],
     "sld_evidence_bbox_xyxy_px": [0, 0, 1, 1],
     "reason": "The cable visibly reaches F1 but the revised diagram routes N2 to F2."},
    {"rack_id": "N3", "observed_feeder": "F2", "required_feeder": "F1",
     "render_evidence_bbox_xyxy_px": [0, 0, 1, 1],
     "sld_evidence_bbox_xyxy_px": [0, 0, 1, 1],
     "reason": "The cable visibly reaches F2 but the revised diagram routes N3 to F1."}
  ],
  "operations": [
    {"op": "replace_feeder", "rack_id": "N2", "from": "F1", "to": "F2"},
    {"op": "replace_feeder", "rack_id": "N3", "from": "F2", "to": "F1"}
  ],
  "confidence": 0.87,
  "decision": "repair"
}
~~~

The example bounding boxes above are placeholders, **not valid predicted boxes**; actual values must be produced from pixels and be checked against image dimensions. Keep `observed_render_edges`, `target_sld_edges` and discrepancy evidence separate from `operations` so the Critic must state what it saw and what it proposes to change. Validate each evidence box as four finite increasing coordinates inside the image. Evidence references must name only whitelisted inputs. Require one discrepancy per operation, no conflicting duplicate operations, `from` equal to the current scene edge, `to` an existing feeder different from `from`, correct scene hash, and bounded string lengths. `decision` is exactly `repair`, `no_change`, or `abstain`. `repair` needs nonempty validated operations; `no_change` needs empty operations and matching visible edges in the control case; `abstain` needs empty operations plus a reason. No-change is a valid **control** result, not evidence of successful repair. Invalid patches never become passes through local correction.

For the generated canonical case, also require each render evidence box to intersect the appropriate projected cable corridor and each SLD evidence box to intersect the corresponding branch-line region from the fixture generator's private drawing metadata. Those metadata checks validate *where* the agent looked without sending the target graph to it. If a judge uploads an arbitrary drawing without trustworthy region metadata, report image evidence as unverified and require human review; a merely well-formed rectangle must not count as visual proof.

If the optional next experiment repairs hotspot geometry too, use a separate whitelisted operation with connector ROI evidence and an explicit thermal acceptance gate. **Do not add it to the first critical path.** The feeder swap is sufficient to demonstrate a visual repair with a consequential safety flip.

### C2. Local patch application

Suggested signatures:

~~~python
def validate_scene_spec(raw: dict, public_state: dict) -> SceneSpec: ...
def validate_repair_patch(raw: dict, current: SceneSpec, images: ImageManifest) -> RepairPatch: ...
def apply_patch(current: SceneSpec, patch: RepairPatch) -> SceneSpec: ...
def extract_feeder_graph(scene: SceneSpec) -> dict[str, str]: ...
def score_graph(graph: dict[str, str], sealed_truth: dict[str, str]) -> GraphScore: ...
def evaluate_actions(graph: dict[str, str], public_state: dict) -> list[ActionOutcome]: ...
~~~

`apply_patch` operates on a deep copy, changes only `edges`, **preserves** `source_revision=A` as provenance, and serializes a canonical JSON byte stream before hashing. Run metadata records `candidate_target_revision=B`; the render1 HUD should say `Candidate repair toward B` until the independent verifier accepts it. It must not modify currents, rack IDs, feeder limits, hotspot, geometry bounds or renderer code. Use an atomic write (`*.tmp` then rename) into a new `render1` directory; never overwrite render0 or the original scene. Keep `expected_from` semantics so an operation cannot silently apply to a different baseline.

### C3. Independent visual and electrical scoring

The scorer receives `scene_spec` and a private `truth.json` under a separate local process/module; the Critic does not. Report distinct metrics rather than hiding them in one composite number:

1. **Graph edge accuracy:** `correct_edges / 4`, with exact rack IDs and F1/F2 destinations. Acceptance for the canonical case: 2/4 before, 4/4 after.
2. **Rendered cable change:** changed pixel count inside projected N2/N3 cable corridors after excluding text/HUD regions, and screenshot SHA. A graph-only patch with unchanged pixels fails.
3. **Render integrity:** four rack silhouettes, two visible buses, four connected cable paths, valid PNG dimensions, no WebGL errors, thermal hotspot still on N4 rear connector. Where possible, use a color/corridor segmentation check against 3D projected endpoints; at minimum require both machine check and human visual inspection record.
4. **Thermal localization:** preserve previous connector hotspot overlap/localization threshold (for example IoU ≥0.3 if compatible with the current fixture) and registration ≤3 px. State the actual test and measured value in the report.
5. **Decision:** compute all allowed N4 offload candidates against the **post-repair graph** using the existing thermal simulation. Reject any action that violates feeder limits, fails the N4 temperature threshold, or breaks conservation. Report the measured 180 s temperatures/currents. Expected current B outcome: N3 is rejected at F1=48 A; N2 is eligible at F1=40 A and F2=28 A.

Never award a pass from a pretty screenshot alone or from physics calculated using an uncorrected graph. A successful run requires both visual improvement and current safety improvement. Avoid calling a baseline recommendation an operationally approved action; call it a hypothetical stale-twin recommendation.

### C4. Local negative and mutation tests

Run the golden fake payload, then mutate one property at a time: unchanged `repair` patch, inverse patch, N4 moved to wrong feeder, duplicate rack, forged scene hash, out-of-bounds evidence box, evidence from a file never shown to Critic, extra JSON key, nonfinite numeric, path traversal, wrong case/revision, `decision=abstain`, stale `from`, malformed JSON, and a patch that lowers graph accuracy. All must produce explicit rejected/abstained outcomes with immutable render0 intact. The A→A control must return `decision=no_change` with no operations; it must not output the canned N2/N3 swap. This is the simplest check against answer leakage or a hardcoded repair.

**Phase C exit:** a deterministic non-provider rehearsal produces correct before/after images, graph and physics flip; all unsafe/malformed mutations fail closed. Only now attach real Managed Agents.

## 5. Phase D — real Argon Managed Agents: Builder and visual Critic

### D1. Provider configuration and what the API actually supports

Use the official Interactions API and the already proven `file_pilot.ManagedFileRunner` adapter where present. Official [Antigravity agent documentation](https://ai.google.dev/gemini-api/docs/antigravity-agent) states that the default managed model is Gemini 3.8 Flash; **explicitly** set the underlying model to `gemini-4-argon`. Configure a bounded token budget and record the returned model in the dossier. The [Managed Agents quickstart](https://ai.google.dev/gemini-api/docs/managed-agents-quickstart) documents background execution and file retrieval; the older local runner already uses `client.environments.files.download`, so preserve the route verified on the user's account before trying an alternative SDK file snapshot API.

~~~python
interaction = client.interactions.create(
    agent="antigravity-preview-09-2026",
    input=[{"type": "text", "text": prompt}, *ordered_png_parts],
    environment={"type": "remote", "sources": safe_inline_public_sources},
    background=True,
    store=True,
    agent_config={
        "type": "antigravity",
        "model": "gemini-4-argon",
        "max_total_tokens": role_token_cap,
    },
    tools=[{"type": "code_execution"}],
)
~~~

Build `ordered_png_parts` exactly as the verified runner does: decoded valid PNG, within provider/input size limits, inline base64 with `type=image` and `mime_type=image/png`. Include a text manifest naming the images in their exact order and SHA-256. The Managed Agent gets **PNG**, not a raw video/PDF input in this extension. Give it enough CPU/runtime to inspect images and write one output JSON file; do not ask it to write or install a new renderer.

Mount a **different role-specific `/workspace/AGENTS.md`** in each fresh sandbox: Builder may write only `scene_spec.json` under its schema; Critic may write only `repair_patch.json` under its schema. Mount a tiny schema/validator helper as a public source if needed. This is the agent harness/skill boundary for the demonstration: each managed agent has its own objective, allowed tools, output contract and evidence set. Avoid a generic conversation swarm. Record the hash of each mounted instruction file so the judge can verify what each role was allowed to see.

**Parameter correction:** do **not** pass `generation_config.thinking_level` or `response_format` to Antigravity Managed Agent interactions. The direct [Gemini 4 Argon model](https://ai.google.dev/gemini-api/docs/models/gemini-4-argon) supports those capabilities, but the managed agent request surface and the tested file transport differ. Managed outputs here are JSON files written in the remote sandbox, then strictly parsed and validated locally. If a direct model fallback is added, label it a separate mode; never report it as a Managed Agent run.

Use `background=True`; poll `queued`/`in_progress` with a bounded interval and deadline, capture `completed`/`failed`/`incomplete`/cancelled, record interaction and environment IDs, snapshots, reported model, token usage and timing. `max_total_tokens` is best-effort and excludes cached tokens; maintain a second local total-including-cache ledger and reserve unknown usage after cancellation. Start with measured per-role caps (for example 15–25k tokens each) rather than 1M-token generation, then calibrate from a single real dry run. Do not launch simultaneous duplicate paid attempts automatically. The event asks to push the model's ability, **not** to maximize token spend.

### D2. Builder role: synthesize the stale twin from old evidence

Create a **fresh** remote environment for Builder. Give it only:

1. `old_sld_A.png`, old N4 thermal/RGB images if useful, and public rack/feeder inventory/limits;
2. `SceneSpec v1` schema/constraints and a short coordinate convention; and
3. instruction to infer rack positions, rear hot connector and **old feeder edges from SLD A**, then write exactly `/workspace/scene_spec.json`.

Do not supply current B, a correct patch, reference graph, private scorer, oracle numbers, B filename, post-change physics or the phrase “swap N2 and N3” to Builder. Do not let it pull arbitrary local files. Ask it to validate its JSON against the supplied schema inside its sandbox if practical and finish with a short message naming the file. The local program then retrieves that file, verifies it belongs to the Builder environment, validates schema/public-state consistency and hashes canonical bytes. If it infers A incorrectly, save the failure; do not silently replace it with hand-authored A. A minimal prebuilt A spec is permitted **only as an explicitly labeled backup demo**, not a successful Builder result.

The purpose of this role is to create a manipulable 3D twin from multimodal engineering evidence. Native image input and in-sandbox file creation matter more than a long narrative. The renderer turns its structured output into an actual 3D view and records `render0` artifacts. If the Builder cannot identify enough edges, stop rather than laundering uncertain topology into a safety recommendation.

### D3. Critic role: watch its predecessor's pixels, compare, repair

Create a **separate fresh** remote environment for Critic. Do not chain `previous_interaction_id` from Builder or share its remote filesystem: that would leak scene JSON and transcript contents. Give Critic exactly:

1. `current_sld_B.png` (current as-built engineering target);
2. `render0_wiring.png` (actual screenshot of Builder's stale twin, with all buses/cables in view);
3. `render0_thermal_rear.png` and/or `render0_rgb_rear.png` if needed to preserve the hotspot; and
4. a small public inventory/current-limit block, scene hash, image IDs and `RepairPatch v1` instructions.

Do **not** provide the Builder's `scene_spec.json`, the private B edge table, the reference answer, hidden oracle, score or expected action. The Critic must infer `observed_render_edges` from the pixels and `target_sld_edges`/discrepancies from SLD pixels. It writes exactly `/workspace/repair_patch.json`; the local controller downloads it from **that same environment**, locks its hash and validates it. A patch that merely repeats the known test fixture without image evidence should fail the evidence gate. The Critic may return `no_change` for matching A→A images or `abstain` if either drawing or render is ambiguous; neither counts as a successful repair demo.

Short role instruction blueprint (expand into exact `prompts/critic.txt` without embedding the answer):

~~~text
You are checking a rendered data-center digital twin against a newer as-built
electrical diagram. Image order: (1) current SLD; (2) actual wiring render;
(3) thermal rear render. Inspect cable routes in image 2 and branch lines in
image 1. State what you visibly observe with pixel bounding boxes in each
image. Propose only replace_feeder operations needed to reconcile the scene.
Do not infer from private files; do not modify loads, limits, temperature,
camera or renderer code. If the connections match, return decision=no_change;
if you cannot see them, return decision=abstain. Save strict JSON to
/workspace/repair_patch.json and stop.
~~~

This role must genuinely **watch the rendered product of the Builder**. A local screenshot is not a substitute for the image sent to the agent; save the hashes of the exact byte arrays uploaded. If official SDK image representation changes, adapt only this thin transport layer and re-run the native-image smoke test.

### D4. Role isolation and evidence checks

Record a per-role evidence object with `requested_model`, `returned_model`, `interaction_id`, `environment_id`, `status`, `native_image_input_sent`, image IDs/SHA/dimensions, mounted files, retrieved output file hash, elapsed seconds, token usage and whether code execution/file creation was observed. Never claim a role ran on Argon when the provider did not return/verifiably report Argon; mark `model_unverified` if necessary. Reject a Critic environment ID equal to Builder's, missing/corrupt remote output, local replacement of remote bytes, a JSON patch read from stdout instead of retrieved file, or a provider status other than the explicitly accepted terminal state.

Use one bounded test case before any campaign. If `completed` but no file exists, fail with `missing_remote_output`. If `incomplete` from budget, save it as incomplete and do not use its partial file as a pass. On timeout, cancel if the API supports it, then reconcile token usage later without starting an automatic retry. No remote credentials or raw base64 in log, repo or web response.

### D5. First real-provider acceptance

Run the canonical drift case once with real Builder and Critic. Require:

- Both interactions are actually Argon-backed Managed Agents, complete and use **distinct** environment IDs.
- Builder's `scene_spec.json` was retrieved and accepted without local rewriting; `render0_wiring.png` came from that spec.
- Critic input manifest contains the exact render0 screenshot hash and current SLD B hash, and excludes private truth.
- Critic's `repair_patch.json` is provider-generated, schema-valid, cites pixel evidence, and changes N2/N3 feeder assignments correctly; no hardcoded local patch was substituted.
- Re-rendered cable pixels and spec hash change; private edge score and physics outcome improve with no thermal regression.

If this fails, save the exact failure reason and one concise diagnostic; change *one* cause (image legibility, prompt, schema, transport or time cap), then repeat within an explicit cost ceiling. Do not keep running a broad matrix of paid calls while the judge-facing vertical slice remains broken.

**Phase D exit:** an authentic two-role, separated-environment run demonstrates Argon creating and visually repairing a 3D twin. Only then claim the extra Managed Agents value.

## 6. Phase E — end-to-end controller, measurement and fail-closed behavior

### E1. One run state machine

Implement `run_twin_repair(case_input, provider_mode, run_root) -> RunResult` with these monotonic states:

~~~text
CREATED -> INPUT_LOCKED -> BUILDER_RUNNING -> SCENE_VALIDATED
        -> RENDER0_VALIDATED -> CRITIC_RUNNING -> PATCH_VALIDATED
        -> RENDER1_VALIDATED -> SCORED -> PROPOSAL_PENDING_ENGINEER_REVIEW
        -> terminal PASS | CONTROL_NO_CHANGE | REJECTED | ABSTAINED | TIMEOUT | PROVIDER_ERROR
~~~

Any failure transitions to one terminal state and preserves prior artifacts. No state may overwrite an earlier file. Each run gets a random opaque ID, immutable input manifest, append-only event log, start/end timestamps in UTC and wall-clock elapsed seconds measured with a monotonic clock. Event records should contain references and hashes, not full base64 images or secrets. A run ID is not a filesystem path; normalize it and keep all writes inside one approved run directory.

The controller owns the sequence. Model output cannot trigger the next provider call, deploy code, modify the hidden truth or turn an electrical suggestion into action. The only action-like state is `PROPOSAL_PENDING_ENGINEER_REVIEW`, and the UI must say that a human must verify against actual site documentation and equipment before any real change.

### E2. Pseudocode for the critical path

~~~python
def run_twin_repair(case_input, provider, root):
    run = create_immutable_run(root, case_input)
    verify_public_manifest(run)

    builder_raw, builder_provenance = provider.run_builder(run.old_public_inputs)
    scene0 = validate_scene_spec(builder_raw, run.public_state)
    freeze_json(run / "scene0.json", scene0)
    images0 = render_and_validate(scene0, run / "render0")

    critic_inputs = select_exact_images(run.current_sld, images0)
    assert_no_private_truth(critic_inputs)
    patch_raw, critic_provenance = provider.run_critic(critic_inputs,
                                                       sha256(scene0))
    patch = validate_repair_patch(patch_raw, scene0, critic_inputs)
    freeze_json(run / "patch.json", patch)

    if patch.decision == "abstain":
        return finish_without_recommendation(run, "ABSTAINED")
    if patch.decision == "no_change":
        return verify_and_finish_control(run, scene0, images0,
                                         run.private_truth)

    scene1 = apply_patch(scene0, patch)
    freeze_json(run / "scene1.json", scene1)
    images1 = render_and_validate(scene1, run / "render1")

    score = private_score(scene0, scene1, images0, images1,
                          run.private_truth)
    actions = evaluate_actions(extract_feeder_graph(scene1),
                               run.public_state)
    result = decide_pass_or_reject(score, actions, builder_provenance,
                                   critic_provenance)
    write_redacted_report(run, result)
    return result
~~~

`private_score` runs only after `patch.json` is frozen. A `no_change` control passes only if the sealed graph was already 4/4; it is labeled `CONTROL_NO_CHANGE`, never counted as a successful repair. `decide_pass_or_reject` uses predeclared thresholds; do not move the goalposts because the first live output was inconvenient. Avoid all unexplained local “corrections” of model JSON. If local validation rejects an output, the run fails clearly; a new agent attempt gets a new run ID.

### E3. Timing and budget plan

Instrument eight spans: asset load, Builder API, Builder retrieval/validation, render0, Critic API, Critic retrieval/validation, render1, scorer/physics/UI. Record p50/p95 only when there are enough samples; with one case show the single observed duration, not a fabricated percentile. Target a fresh end-to-end run below **180 s** because the organizer asks for a live input under three minutes. Establish a **soft 150 s budget** to leave narration and network variability. In practice Managed Agent calls may take minutes, so benchmark first rather than promising a bound.

Practical latency adjustments, in order:

1. Delete unnecessary prompt text and agent file sources; send only the three images and tiny manifest needed.
2. Reuse fixed local renderer and local private verifier; do not ask Argon to regenerate code on every run.
3. Use one Builder and one Critic, not the prior four-turn A/B campaign, for the live path.
4. Crop or compress images *only while preserving cable legibility and evidence*. Keep full-resolution originals in dossier.
5. If live two-role execution still exceeds 180 s, run Builder before judging on an explicitly identified old A case, then let a judge supply/select a fresh revised B and run the **Critic→patch→rerender→verify** path live. Clearly label Builder and render0 as prepared; do not call that a fresh full two-agent live run. The full genuine two-role evidence remains available to inspect.
6. A direct Argon model call for Critic may be an optional fallback with JSON-schema output and `thinking_level`; show it as a **hybrid direct-model mode**, not as Best Use of Managed Agents. It is less aligned with that prize. Never silently swap modes.

Cap concurrent paid runs to one globally; set request-specific provider deadline, `max_total_tokens`, local known/unknown token ledger and a stop condition. The public web trigger must have server-side rate limits and a global run budget; keep a reserved judge access code so general traffic cannot exhaust the live demo before judging.

### E4. Dossier and replay semantics

Suggested immutable run contents:

~~~text
runs/<run-id>/
  manifest.json                 hashes, revision, source commit, creation time
  public/                       supplied images + public state
  private/truth.json             sealed; never served or committed
  roles/builder_provenance.json  redacted provider details
  roles/critic_provenance.json
  scene0.json                    raw provider file and canonical validated copy
  render0/*.png; render0/render_meta.json
  patch.json                     raw provider file and canonical validated copy
  scene1.json
  render1/*.png; render1/render_meta.json
  score.json                     private test results for local operator
  action_comparison.json         hypothetical electrical and thermal results
  result_public.json             redacted, no truth table/secret
  events.jsonl                   append-only timestamps and state transitions
~~~

Store raw and canonical files separately if formatting matters; record both hashes. A replay mode may redisplay an old dossier or rerun local rendering/scoring without any API call, but it must prominently say **replay**. It must never be shown as fresh model inference.

**Phase E exit:** one command completes the full live path or exits with an honest terminal failure and a complete dossier. Repeating the same input creates a new run with its own interaction IDs and timestamps; it cannot reuse a prior result unnoticed.

## 7. Phase F — the public web demo judges can understand and try

Build **one task page with four states**, not a dashboard or slide deck. It must explain itself in five seconds, run the same Phase E controller as the CLI, and show why repairing the 3D twin changes an engineering decision. Use the current Python web stack if present; otherwise a small FastAPI service plus bundled static HTML/CSS/Three.js is enough. Do not build a second simulation engine in the browser.

### F1. Four page states and exact information hierarchy

| State | Above-the-fold content at 1280×720 | Only action |
|---|---|---|
| **1. Incident** | Headline: **“The connector is hot. Is the wiring map still correct?”** Left column: thermal N4 image above the selected current SLD; right: initial 3D preview. Primary-case caption: “The old twin suggests moving an 8 A-equivalent workload to N3.” Badge: **Synthetic case**. The control uses its own caption; no hardcoded B labels. | **Run live repair**. A small case selector offers the primary drift case and a no-change control. |
| **2. Running** | Large actual render0 replaces the preview when ready. Four honest steps: Build twin → Render → Argon inspects render → Verify repair. Show elapsed time and current step, never a made-up percentage. Keep the SLD visible so viewers know the target. | None; the result opens automatically. |
| **3. Result** | The 3D before/after is the largest element. In the primary drift case, highlight only the two changed feeder cables N2/N3; provide a simple Before/After toggle, fixed rear-wiring camera, then optional drag-to-rotate. At right: **“Old choice N3: F1 = 48/44 A (over limit)”** and **“Repaired choice N2: F1 = 40/44 A”**. Show **2/4 → 4/4** verified links and `Human review required`. The no-change control instead says `Already matches; no repair made`. | **Inspect evidence**; do not hide the result behind a click. |
| **4. Evidence** | Collapsible section: current SLD, the exact render0 image Argon received, the two visual evidence boxes, bounded patch, render1, local verifier and model/interaction IDs. Prominent `Live`, `Prepared Builder`, or `Replay` status. | **Try another run** if quota remains. |

Use this plain-language caption under a successful repair: **“Argon corrected the twin's wiring before the operator relied on its old recommendation.”** For the no-change control, say **“The drawing and twin already agree; no repair was made.”** Keep technical terms, token counts, JSON and logs inside Evidence. Every number on screen must come from that run's redacted result object, not hardcoded HTML. A cable selected in the 3D view must resolve to the same rack→feeder edge used by the physics check. Provide text labels as well as red/green color.

Do not scroll during the three-minute pitch. The main result must fit at 1280×720 with readable labels from several meters away. Reserve at least half the horizontal space for the 3D before/after; avoid three equally narrow columns. Avoid autoplay animations that obscure the cable change. Use actual PNGs captured by the renderer for before/after proof even if an interactive Three.js scene is also shown.

### F2. Public access that permits testing without draining credits

The URL opens **without login**. Anyone can inspect a clearly labeled completed example, rotate the twin and compare the two candidate actions. The **Run live repair** button starts a new real Argon run on one of two *validated synthetic cases* while public quota remains; the no-change control must correctly report “no repair needed” rather than force the canonical patch. A judge access code entered in a form reserves live runs after public quota is exhausted. Never place that code or provider credentials in the URL or browser bundle. If both quotas are exhausted, disable the live button and say **“Live capacity reached; explore a recorded run”**; never silently serve a replay as live.

Keep one paid run active globally, a short bounded queue, per-visitor cooldown, server-side total run budget, role timeout and token ceilings. Return a queue position or a clear busy state. Do not accept arbitrary user images in the judged path until the fixed four-rack validation and scorer support them: an arbitrary uploaded plan cannot honestly receive a 4/4 ground-truth score. If upload is added later, label it **Experimental / unscored / human review**. The core judge journey should need at most one click after selecting a case.

### F3. Thin API, result contract and failures

| Endpoint | Purpose |
|---|---|
| `GET /health`, `GET /cases` | Availability and safe case labels; no secrets or private truth. |
| `POST /runs` | Validate selected case and rate limit; start a new server-side Phase E run; return opaque run ID. |
| `GET /runs/{id}` | Poll `queued/running/completed/failed`, stage, elapsed time and redacted result; no fake success. |
| `GET /runs/{id}/assets/{name}` | Serve only allowlisted SLD/thermal/render PNGs and public evidence JSON. |

Use `Cache-Control: no-store` for live state, safe content headers, path allowlists and a random opaque run ID. The page must recover from refresh by reopening the same run ID without launching another paid request. On timeout or agent abstention, show the last valid render and a direct message: **“Argon could not verify this wiring; no recommendation is issued.”** Never replace a failed live run with a successful recorded result. Keep API/service-account credentials server-side.

### F4. Presentation mode and acceptance tests

Add `?present=1`: fullscreen-friendly typography, no debug drawer, primary case preselected, one Enter-key action and automatic transition to Result. It uses the **same live backend**, not a fake shortcut. The guide asks for a three-minute live demo without slides; these four page states serve as the presentation. Rehearse on the venue-style projector and on a phone-sized browser for a judge opening the link independently.

Test the page with a first-time person who has not heard the pitch. Within 10 seconds they should answer **what is hot, what was wrong, what Argon changed, and why the decision changed**. Also verify: signed-out access; fresh run IDs; exact render0 hash matching Critic input; evidence/3D graph agreement; 48/44 and 40/44 values sourced from the run; no-change control; queue/quota behavior; refresh recovery; timeout/abstention; no private file access; measured live runtime. Run one complete rehearsal in less than three minutes **before claiming** that the full two-agent flow fits the judging slot.

If two live roles exceed the measured time budget, label the initial Builder scene **Prepared** and perform only Critic → patch → rerender → verification live with the selected current SLD. Say that distinction aloud. Replay is an explicitly labeled network fallback and does not count as a live success.

**Phase F exit:** a signed-out visitor understands the result, a quota-authorized judge starts a genuinely new run, and the screen itself makes the render→observe→repair→verified-decision chain unmistakable.

## 8. Phase G — verification matrix, submission evidence and stopping rule

### G1. Required verification matrix

| Check | Mode | Required outcome |
|---|---|---|
| Existing domain and visual tests | Local | Current repository baseline still passes; document any pre-existing failure separately. |
| A/B fixture reproducibility | Local | Same seed gives same public/truth hashes; only intended SLD revision and feeder graph change. |
| Renderer graph-coupling | Local/browser | Changing N2/N3 edges changes actual cable pixels in both corridors; unchanged parts stay stable. |
| Hotspot geometry | Local/browser | N4 rear connector remains localized; RGB/thermal registration and thermal threshold preserved. |
| Patch schema and adverse cases | Local | Wrong hash, duplicate operation, fabricated evidence, illegal feeder, no-op and arbitrary path all rejected. |
| Independent physics | Local | 68 A conserved; stale N3 looks feasible in A; N3 rejected at 48/44 A in B; N2 accepted with post-action limits. |
| Native image transport | One low-cost provider smoke | Exact PNG hashes recorded; provider sees images as image inputs, not just filenames in text. |
| Genuine Builder/Critic | Live provider | Two completed Argon Managed Agent roles, separate environments, retrieved files, no local substitution. |
| Hidden-oracle separation | Local/source audit | Neither prompt, mounted source, browser response nor screenshot metadata contains B truth or expected patch. |
| A→A control | Local + bounded provider if time | Agent returns `no_change` with no operations; it never outputs the canonical N2/N3 swap. |
| Full CLI and browser | Timed | A fresh input produces a new run; duration/status/usage reported honestly. |
| Failure behavior | Local/provider-mock | Timeout, `incomplete`, missing file, invalid JSON, renderer failure preserve dossier and cannot show PASS. |
| Public link and judge trial | Signed-out browser | Public example opens; bounded live run works with available quota or judge code; video and repository accessible. |

Do not commission a huge synthetic test farm. One canonical case, one counterfactual case, focused negative mutations and one genuine provider run establish the central claim. If a threshold fails, fix the specific causal defect instead of increasing test count or adding agents.

### G2. Evidence card and README claims

Generate a short `RESULTS_TWIN_REPAIR.md` from the run output. It must show:

- case and revision provenance; commit ID; exact build date;
- authentic Builder and Critic agent/model/interaction/environment IDs and statuses;
- input PNG and rendered PNG hashes; before/after thumbnail links;
- the Critic's cited discrepancies and the **actual** applied operations;
- graph score, cable-pixel change, hotspot/registration check, feeder currents, modeled thermal outcome;
- elapsed end-to-end time, provider usage and limitations;
- explicit labels for **event-day original work**, pre-event research/design, synthetic assets, prepared scenes and replay; and
- no claim of field-tested data-center electrical safety.

The public README should explain one human use case, one architectural diagram, how to run a local mock smoke test, how to supply credentials privately for a real run, where to view the result, and what was newly built today. Do not include private truth files in the public repository or commit generated provider traces that expose secrets. If the private oracle is needed for reproducibility, publish a safe public fixture generator with the truth produced locally and kept out of served API responses; document which generated results are synthetic.

### G3. One-minute video storyboard

Capture the **actual working run** and a short glimpse of the relevant code; no slide deck. Suggested cut:

~~~text
0–08 s   Current SLD B + hot rear thermal connector: “An old twin routes N2/N3 incorrectly.”
08–20 s  New/selected input and managed Argon run; show two role stages.
20–35 s  Before 3D wiring render, Critic's image-grounded patch, after render.
35–50 s  Measured decision flip: N3 would make F1 48/44 A; N2 keeps limits.
50–60 s  Browser/CLI result and code snippets showing scene spec, patch and local verifier.
~~~

The event projector slide the user photographed asks for a **one-minute demo video showing features, code and functionality built today** plus a **public GitHub link**, both publicly accessible. The event participant guide adds the 5:00 PM PDT deadline. Prepare the public app URL, video and repository early enough to verify them signed out; do not sacrifice the mandatory video/repository for hosting polish.

### G4. Stop/go and honest fallback

**Green (submit primary claim):** a real Argon Critic watched the actual 3D render, emitted a valid patch, rerender changed cables, sealed edge score improved to 4/4, safe receiver flipped to N2, and a fresh live flow or clearly labeled prepared-Builder variant is shown. **Yellow (submit limited claim):** rendered loop works locally but managed call is too slow; show recorded genuine interaction and a live local patch/render exercise, explicitly noting which steps were prepared. **Red (do not claim Twin Repair):** Critic was never given pixels, patch was scripted, cables did not change, graph/physics were hardcoded, model identity unverified, or the repaired graph remains unsafe. In Red, keep the verified older BlackboxDC demo but do not present it as satisfying More Dimensions.

## 9. Ready-to-run phase instructions for the coding agent

Give the coding agent this file and **one phase at a time**. These short task boundaries prevent a large autonomous build from drifting into another product. The coding agent may use Antigravity CLI for writing code; the **application's judged runtime** still must call the real Managed Agent API for Phase D.

1. **Phase A request:** “Implement only Phase A of `11_TWIN_REPAIR_ADDON_IMPLEMENTATION_PLAN_EN.md` in the current event-day repository. Audit existing modules first. Return paths changed, baseline/test commands with exact results, and the public/private asset manifest. Do not start B.”
2. **Phase B request:** “Implement only Phase B on top of the approved A assets. Make feeder edges visible as geometry controlled by SceneSpec. Produce both A and B renders and the cable-pixel delta proof on this machine. Return images, hashes and test results. Do not call the provider.”
3. **Phase C request:** “Implement only Phase C with test-only fake payloads. Show golden before/after graph score and safety flip, plus fail-closed mutations. Do not claim Argon repaired anything yet.”
4. **Phase D request:** “Attach the existing proven ManagedFileRunner to two new role prompts. Run exactly one bounded real Builder/Critic case, separate environments, record full provenance and report any failure honestly. Do not launch a campaign or change physics thresholds.”
5. **Phase E request:** “Integrate the approved components into one immutable `run_twin_repair` command, add timing/token/failure dossiers, then run a single fresh end-to-end test. Do not build the browser yet.”
6. **Phase F request:** “Implement the four judge-facing web states in Phase F, backed by the same live controller: understandable first screen, bounded public/judge run, automatic before/after result and collapsible evidence. Test a first-time visitor, signed-out access, quota and the three-minute presentation mode.”
7. **Phase G request:** “Run the final matrix, create the evidence card and record/edit a one-minute video from genuine output. Verify public links signed out and document exactly what was built during the event.”

After each phase, require a compact checkpoint with **implemented files, commands run, exact outputs, open defects, time spent and next gate**. If a phase fails, repair that gate before starting the next phase. No additional framework, model or camera simulation is required for this extension unless its absence is the measured blocker.

### 9.1 Command and configuration contract

Have the coding agent expose these stable commands, adapting the Python executable to the active virtual environment. The command names below are a proposed interface to implement, not a claim that they already exist:

~~~bash
python -m twin_repair.challenge build --seed 20261025 --out runs/fixtures/drift1
python -m twin_repair.render_bridge --spec runs/fixtures/drift1/scene_A.json --out runs/fixtures/drift1/render_A
python -m twin_repair.orchestrator mock --case runs/fixtures/drift1 --out runs/mock1
python -m twin_repair.orchestrator live --case runs/fixtures/drift1 --out runs/live1
python -m pytest -q tests/test_twin_repair_contracts.py tests/test_twin_repair_render.py tests/test_twin_repair_orchestrator.py tests/test_twin_repair_physics.py
python -m twin_repair.web --host 127.0.0.1 --port 8000 --runs runs
~~~

`mock` must use only labeled test payloads and zero provider calls. `live` must require credentials, run the authenticated Managed Agent path and print only a compact summary with run directory and result. All commands should exit nonzero on failure. Use explicit env vars for `CHROME_EXECUTABLE_PATH`, provider credential mode and operator token; don't parse secrets from CLI flags (which may leak in process listings). Pin the Python SDK, Pillow, validation library, Three.js, Playwright/Chromium and API service dependencies already in use. On the EC2 host, verify the actual browser binary and WebGL software path before starting paid model calls.

In the result JSON include machine-readable gate booleans: `builder_completed`, `critic_completed`, `argon_verified`, `distinct_environments`, `render0_valid`, `patch_valid`, `render1_valid`, `cable_pixels_changed`, `graph_improved`, `physics_safe`, and `human_review_required`. A single `passed=true` is valid only if every required gate is true. Preserve exact failures for diagnostics without dumping provider secrets.

## 10. Sources and implementation caveats

- [Gemini 4 Argon SF Hackathon public page](https://cerebralvalley.ai/e/gemini-4-argon-sf): public ambition statement and Managed Agents prize. The specific track/rules/judging/deadline above come from the event's participant guide supplied by the user, not from the shorter public page.
- [Antigravity Managed Agent documentation](https://ai.google.dev/gemini-api/docs/antigravity-agent): explicit Argon model selection, background execution and best-effort token budget.
- [Managed Agents quickstart](https://ai.google.dev/gemini-api/docs/managed-agents-quickstart): managed environment lifecycle and file retrieval.
- [Gemini 4 Argon model card](https://ai.google.dev/gemini-api/docs/models/gemini-4-argon): text/image input, text output and direct-model capabilities. The 1M context/long decode is **not** a reason to use a huge prompt in a three-minute demo.
- Local reference source inspected: `/Users/armandosilva/DATA/Beta` and the earlier rebuild-plan folder. That reference may lag the active EC2/event-day repository; Phase A must remap names and verify status before coding.

**Final engineering principle:** the extension succeeds only when the model-created 3D artifact is rendered, a separate Argon Managed Agent uses the *rendered pixels* to correct the artifact against the revised engineering drawing, and an independent local calculation proves the correction matters to a proposed operator decision. Keep that causal chain visible in code, test artifacts, browser and video.
