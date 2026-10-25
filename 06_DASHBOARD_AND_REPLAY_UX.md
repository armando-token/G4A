# Phase 6 — Dashboard, Replay UX, Judge Script, Video Storyboard & Deployment (`06_DASHBOARD_AND_REPLAY_UX.md`)

> **Superseded for the hackathon submission:** The newly released participant guide lists Streamlit applications and dashboard-led projects under Projects to Avoid. Do not implement this phase as the judged centerpiece. Use `10_EVENT_GUIDE_PIVOT.md` for the current Track Two live render/critique/repair application. The offline dashboard below is only a post-submission research/replay tool.

---

## 1. Event-Day Priority & Operational Boundaries

1. **Primary Submission Deliverables First (Before 5:00 PM PDT)**:
   - Public GitHub repository URL containing the reproducible codebase, unit tests, and verified evidence bundle (`artifacts/reports/phase5_live_pair_dossier/`).
   - Required 1-minute demo video (`58–60 seconds`).
2. **Optional Read-Only Replay Showcase Second**:
   - The Streamlit replay console (`blackbox/dashboard.py`) is a **strictly offline, read-only research and verification tool** that renders sealed `visual-v2` evidence bundles (`CaseView` / `PairView` loaded via `blackbox/evidence.py`).
   - It never invokes live provider APIs, never accesses `/private/` ground-truth files, and runs with all cloud credentials (`GOOGLE_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_APPLICATION_CREDENTIALS`) unset.

---

## 2. Six-Panel Visual Causal Audit Architecture

The primary **"Visual causal audit"** replay view presents six ordered panels designed so an evaluator can audit the counterfactual A/B experiment in under two minutes:

### Panel 1 — Incident and Stakes
- **Operational Context**: 4 server racks (`N1..N4`) fed by 2 PDU busbars (`F1` with `44.0 A` limit; `F2` with `50.0 A` limit), total current conserved at `68.0 A` (`N1=36A`, `N2=4A`, `N3=4A`, `N4=24A`).
- **Fault Trigger**: Rack `N4` exhibits a degraded rear power connector hotspot at `t = 0 s` (`61.07 °C` after a 120 s step-load ramp) on a trajectory toward `98.83 °C` at `t = 180 s`, exceeding the `90.0 °C` synthetic thermal ceiling.
- **Required Remediation**: Shift an `8.0 A` workload quantum from `N4` to an eligible receiver rack (`N2` or `N3`) without overloading either `F1` (`<= 44.0 A`) or `F2` (`<= 50.0 A`).
- **Inline Glossary**: Concise definitions for **PDU** (Power Distribution Unit busbar), **Rack** (`N1..N4` compute bays), **SLD** (Single-Line Diagram `sld.png`), and **Lock Chain** (`L1..L4` SHA-256 commitments).
- **Advisory Banner**: Prominent notice that all recommendations are **advisory simulation outputs only** (`physical_safety_certified = False`) with zero physical actuation.

### Panel 2 — Physical View (3D Scene & Multimodal Captures)
- **Visual Assets**:
  - Optional interactive/static 3D rear-aisle preview alongside the exact `1280×800` native multimodal inputs (`rear_aisle_rgb.png` and `rear_aisle_thermal.png`) rendered side-by-side.
  - Shared pixel crosshair and coordinate readout (`[x, y]` in `1280×800` image space).
- **Overlay Toggle**:
  - Switch between **Raw Agent Input** (unannotated PNGs exactly as passed to `I1` and `A1`), **Investigator `I1` Bounding Box**, and **Auditor `A1` Bounding Box** (`[1080, 325, 1144, 394]` / `[1078, 324, 1143, 393]` vs ground-truth `N4` connector `[1079, 325, 1143, 393]`).
- **Cryptographic Invariance Readout**:
  - Displays `IoU` (`0.9554` / `0.9703` in Case A; `0.9554` / `0.9420` in Case B) and exact SHA-256 digests confirming `rear_aisle_rgb.png` (`078444ba...`) and `rear_aisle_thermal.png` (`2c29654f...`) are byte-for-byte **IDENTICAL** across Case A and Case B.

### Panel 3 — Only One Thing Changes (Counterfactual SLD Intervention)
- **Side-by-Side Single-Line Diagrams (`sld.png`)**:
  - Displays **Case A `sld.png`** (`F1 -> {N1, N2}`, `F2 -> {N3, N4}`, SHA-256 `0912ed15...`) next to **Case B `sld.png`** (`F1 -> {N1, N3}`, `F2 -> {N2, N4}`, SHA-256 `1ca852a6...`).
  - Includes a user-controlled toggle to overlay an explanatory highlight around the swapped `N2`/`N3` feeder junction dots (`●`) and non-connected crossing bridge arches (`∩`) vs. viewing the raw unannotated SLD image sent to the agents.
- **Controlled Evidence Manifest Table**:
  - Side-by-side SHA-256 comparison proving that `rear_aisle_rgb.png` (`IDENTICAL`), `rear_aisle_thermal.png` (`IDENTICAL`), and substantive `public_packet.json` telemetry (`IDENTICAL`, SHA-256 `c824b121...`) are held fixed while only `sld.png` (`DISTINCT`) changes between Case A and Case B.

### Panel 4 — What the Agents Did (4-Turn Protocol & Sandbox Isolation)
- **Dual-Track Execution Timeline**:
  - **Track 1 — Investigator (`I1 -> Scoped Probe -> I2`)**:
    - `I1` (`proposal.json`, lock `L1_I1_PROPOSAL`): Reads the 3 PNGs, extracts the 4 feeder edges and 4 sensor edges, localizes the `N4` rear connector bbox, and requests a single `voltage_drop` probe on `N4`.
    - `Scoped Probe` (`probe_receipt.json`): Deterministic local probe session verifies `L1_I1_PROPOSAL` and returns `V_drop = 1.209409 V` on `N4`.
    - `I2` (`plan.json`, lock `L2_I2_PLAN`): Reuses the Investigator `environment_id`, runs Python `code_execution` against `domain.py` to infer `R0 = 0.040 Ohm`, `h = 0.400 W/K`, `C = 8.000 J/K`, simulates all 3 actions over `0..180 s`, and locks the safe migration plan.
  - **Track 2 — Independent Auditor (`A1 -> A2`)**:
    - `A1` (`independent_graph.json`, lock `L3_A1_GRAPH`): Executes in a **fresh, disjoint `environment_id`** (`3c05e360...` vs Investigator `3cf5af50...` in Case A; `f1bd0245...` vs `fdc6cb57...` in Case B). Receives only the 3 PNGs and `public_packet.json` — **zero access** to `proposal.json`, `plan.json`, or `probe_receipt.json`.
    - `A2` (`audit_challenge.json`, lock `L4_A2_CHALLENGE`): Reuses the Auditor `environment_id`, compares `A1`'s locked topology against `I2`'s inferred network, independently recomputes all 19-point trajectories and the `+1.0 °C` sensitivity canary on `N4`, and issues `verdict = "accept"`.
- **Interactive Metadata**:
  - Displays truncated `interaction_id` and `environment_id` badges with copyable full IDs, turn status (`completed`), elapsed wall time, and lock chain SHA-256 hashes.

### Panel 5 — Decision and Physics (Counterfactual Reversal & Trajectories)
- **Candidate Action Comparison Table (`NO_ACTION`, `MIGRATE_N4_TO_N2_8A`, `MIGRATE_N4_TO_N3_8A`)**:
  - Shows `N4` temperature at `t = 180 s` (`98.83 °C` under `NO_ACTION` vs `52.53 °C` after `8.0 A` migration), `F1` current vs `44.0 A` limit, `F2` current vs `50.0 A` limit, thermal/PDU safety flags, and data provenance (`I2` `plan.json` vs `A2` `audit_challenge.json` recomputation).
- **Counterfactual Decision Reversal**:
  - **Case A**: Migrating `8 A` to `N2` overloads `F1` (`36 + 4 + 8 = 48.0 A > 44.0 A`); migrating to `N3` keeps `F1 = 40.0 A <= 44.0 A` and `F2 = 28.0 A <= 50.0 A` -> selects **`MIGRATE_N4_TO_N3_8A`**.
  - **Case B**: Migrating `8 A` to `N3` overloads `F1` (`36 + 4 + 8 = 48.0 A > 44.0 A`); migrating to `N2` keeps `F1 = 40.0 A <= 44.0 A` and `F2 = 28.0 A <= 50.0 A` -> selects **`MIGRATE_N4_TO_N2_8A`**.
- **19-Point Electro-Thermal Trajectory Charts (`0..180 s`, `dt = 10 s`)**:
  - Plots `N1..N4` temperature trajectories (`°C` vs `90.0 °C` limit) and `F1`/`F2` busbar currents (`A` vs `44.0 A`/`50.0 A` limits), plus the `+1.0 °C` initial-temperature perturbation canary on `N4`.
- **Uncertainty & Abstention Handling**:
  - Displays `unknown_critical_edges` and `blocking_graph_uncertainty` status (showing how withholding `sld.png` in the `no_sld_variant` ablation triggers safe abstention `decision = "abstain"` rather than a blind 50/50 guess).

### Panel 6 — Proof and Limits (13-Boolean Strict Matrix, Baselines & Disclaimers)
- **13-Boolean Strict Acceptance Matrix**:
  - Renders all 13 per-case boolean gates (`input_integrity` through `strict_case_passed`) for `case_A` and `case_B`, plus the top-level `strict_pair_passed` badge.
- **Direct Artifact & Provenance Inspector**:
  - Expandable viewers for `proposal.json`, `probe_receipt.json`, `plan.json`, `independent_graph.json`, `audit_challenge.json`, `locks.jsonl`, `manifest.json`, and token usage ledgers (`154,021` pair noncached tokens `<= 280,000` ceiling).
- **Four Comparison & Ablation Baselines**:
  - Compares the live `visual-v2` pair (`2/2` cases safe) against `no_action` (`0/2`), `fixed_n2_policy` (`1/2`, fails Case A via `48 A > 44 A` `F1` overload), `no_sld_variant` (`0/2`, safely abstains when `sld.png` is withheld), and `privileged_oracle` (`2/2`, upper bound with direct truth access).
- **Explicit Scope & Safety Disclaimers**:
  - Clearly states: synthetic simulation only (`physical_safety_certified = False`), `N = 2` paired counterfactual world design, single fault family (connector contact resistance vs cooling degradation), lumped first-order RC electro-thermal ODE (`dt = 10 s`), and zero connection to physical switchgear or BMS hardware.

---

## 3. Data/API Layer (`blackbox/evidence.py`) & Dashboard (`blackbox/dashboard.py`)

1. **Pure Data Layer (`CaseView`, `PairView`, `load_pair_view`)**:
   - `blackbox/evidence.py` provides typed, immutable views (`CaseView` and `PairView`) produced by `verify_and_replay_bundle(bundle_dir)` / `load_pair_view(bundle_dir)`.
   - Every bundle load verifies `manifest.json` SHA-256 entries, the 4-stage `locks.jsonl` hash chain (`L1..L4`), cross-case input invariants, and oracle recomputation before populating the UI.
2. **Read-Only UI Guarantees (`blackbox/dashboard.py`)**:
   - Uses content-hash-keyed caching so switching between Case A and Case B is instantaneous without stale state.
   - Enforces path-traversal protection (`Path.resolve()` confined to the selected bundle root).
   - If an inspected bundle is incomplete, tampered, or failed any gate (`strict_pair_passed == False`), renders an immediate high-contrast **INCOMPLETE / FAILED VERIFICATION** banner at the top of the page.

---

## 4. Minimal Linux Server Deployment (`127.0.0.1:8501` + Caddy HTTPS)

To host the read-only replay showcase on a Linux VM without exposing Streamlit directly or mounting any API credentials:

1. **Systemd Service (`deploy/blackbox-dashboard.service`)**:
   - Runs as an unprivileged user (`ubuntu`) with `GOOGLE_API_KEY=""`, `GEMINI_API_KEY=""`, and `GOOGLE_APPLICATION_CREDENTIALS=""` explicitly cleared.
   - Binds Streamlit strictly to loopback (`--server.address 127.0.0.1 --server.port 8501 --server.headless true`).
2. **Caddy Reverse Proxy (`deploy/Caddyfile`)**:
   - Terminates HTTPS (or HTTP on `:8080` for local/LAN inspection), sets security headers (`X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`, `Referrer-Policy: no-referrer`), and reverse-proxies WebSocket + HTTP traffic to `127.0.0.1:8501`.

```bash
# Start service and reload Caddy
sudo cp deploy/blackbox-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now blackbox-dashboard.service
sudo caddy validate --config deploy/Caddyfile
sudo systemctl reload caddy
```

---

## 5. Judge-Facing Live Walkthrough Script (90–120 Seconds)

- **0:00–0:20 — The Counterfactual Question**:
  "In data-center incident response, text logs often lag behind active electrical drawings. We test a strict counterfactual: we hold the `1280×800` rear-aisle RGB image, infrared thermal image, and 120-second sensor ramp byte-for-byte identical across two cases, and swap only two feeder junction dots (`N2` and `N3`) on the Single-Line Diagram `sld.png`."
- **0:20–0:45 — Physical View & Single-Line Diagram Swap**:
  "In Panel 2, both the Investigator (`I1`) and the isolated Auditor (`A1`) localize the glowing rear power connector on Rack `N4` (`IoU > 0.94`) and bind `SNS_K2 -> N4`. In Panel 3, you can see the only difference between Case A and Case B: in Case A, `N2` lands on `F1` (`44 A` limit) and `N3` lands on `F2` (`50 A` limit); in Case B, `N2` and `N3` are swapped."
- **0:45–1:15 — Isolated Managed Agents & Physics Execution**:
  "In Panel 4, the Investigator locks `proposal.json` (`L1`), receives a single signed voltage-drop measurement (`1.209409 V` on `N4`), and uses Python `code_execution` in `I2` to recover `R0 = 0.040 Ohm`, `h = 0.4 W/K`, `C = 8.0 J/K` (`L2`). Next, a fresh Auditor environment (`A1`, `L3`) extracts the graph from the raw PNGs with zero access to the Investigator's outputs, and in `A2` (`L4`) re-simulates all 19-point trajectories and a `+1.0 °C` sensitivity canary."
- **1:15–1:45 — Reversal Proof & Honest Boundaries**:
  "In Panel 5 and Panel 6, the safe 8 A migration flips from `MIGRATE_N4_TO_N3_8A` in Case A to `MIGRATE_N4_TO_N2_8A` in Case B — avoiding a `48 A > 44 A` overload on `F1`. Every artifact is hash-chained (`L1..L4`), verifiable offline in 2 seconds with zero API calls (`python -m blackbox.campaign replay`), and explicitly marked `physical_safety_certified = False`."

---

## 6. One-Minute Demo Video Storyboard (58–60 Seconds)

| Time Window | Visual / Screen Capture | Voiceover / Caption Script |
| :--- | :--- | :--- |
| **0:00–0:10** | Split-screen title card + Panel 1 incident summary (`N4` rear connector `61.1 °C -> 98.8 °C`, `90 °C` limit, `8 A` load shift). | "Rack `N4`'s rear power connector is overheating toward `98.8 °C`. We must shift `8 A` to `N2` or `N3` without overloading feeder `F1` (`44 A` max) or `F2` (`50 A` max)." |
| **0:10–0:22** | Panel 2 (`1280×800` RGB + IR thermal hotspot bbox `[1080, 325, 1144, 394]`) -> Panel 3 (`sld.png` Case A vs Case B toggle + SHA-256 table). | "Across Case A and Case B, the RGB capture, thermal capture, and sensor telemetry are byte-for-byte identical. Only the Single-Line Diagram `sld.png` swaps `N2` and `N3` between `F1` and `F2`." |
| **0:22–0:36** | Panel 4 timeline: Investigator (`I1 -> Probe -> I2`) and disjoint Auditor (`A1 -> A2`) with live `interaction_id` and `environment_id` badges. | "Running on `antigravity-preview-09-2026` with `gemini-4-argon`, the Investigator locks its visual graph, takes one voltage-drop probe, and runs Python `code_execution` to fit `R0, h, C`. An isolated Auditor extracts the graph independently before auditing `I2`'s plan." |
| **0:36–0:48** | Panel 5 decision table + 19-point trajectory plots showing `MIGRATE_N4_TO_N3_8A` (Case A) vs `MIGRATE_N4_TO_N2_8A` (Case B) and `48 A > 44 A` overload rejection. | "That single visual diagram change reverses the safe decision: `MIGRATE_N4_TO_N3_8A` in Case A versus `MIGRATE_N4_TO_N2_8A` in Case B, preventing a `48 A` overload on `F1`." |
| **0:48–0:56** | Terminal running `python -m blackbox.campaign replay --bundle artifacts/reports/phase5_live_pair_dossier` + Panel 6 13-boolean matrix (`strict_pair_passed = True`). | "Offline replay cryptographically verifies all four `L1..L4` locks, 19-point trajectories (`RMSE <= 1e-6 °C`), and the `+1 °C` canary across all 13 strict gates." |
| **0:56–1:00** | Closing card with GitHub repo URL, `strict_pair_passed = True`, and `physical_safety_certified = False` disclaimer. | "Reproducible offline in one command. Advisory simulation only — `physical_safety_certified = False`." |
