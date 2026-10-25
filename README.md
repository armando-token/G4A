# BlackboxDC (`G4A`) — Counterfactual Visual & Electro-Thermal Causal Audit for Managed Agents

## Core Technical Result (2-Minute Overview)

**With all other evidence held byte-for-byte identical** (`1280×800` `rear_aisle_rgb.png`, `1280×800` `rear_aisle_thermal.png`, and substantive `public_packet.json` sensor ramp telemetry), **a visual topology swap of `N2`/`N3` feeder branches on the Single-Line Diagram (`sld.png`) reverses a safety-critical `8.0 A` load-migration recommendation** (`MIGRATE_N4_TO_N3_8A` in **Case A** vs `MIGRATE_N4_TO_N2_8A` in **Case B**), and **a sandbox-isolated Auditor Managed Agent (`antigravity-preview-09-2026` / `gemini-4-argon`) independently extracts the visual topology (`A1`) before verifying the Investigator's 19-point electro-thermal trajectories (`A2`)**.

| Dimension | Case A (`case_A`) | Case B (`case_B`) | Invariant / Verification |
| :--- | :--- | :--- | :--- |
| **`rear_aisle_rgb.png` (`1280×800`)** | `078444bac2d0a309...` | `078444bac2d0a309...` | **Byte-for-byte IDENTICAL** |
| **`rear_aisle_thermal.png` (`1280×800`)** | `2c29654fddec6815...` | `2c29654fddec6815...` | **Byte-for-byte IDENTICAL** |
| **Substantive `public_packet.json`** | `c824b1217603da94...` | `c824b1217603da94...` | **Byte-for-byte IDENTICAL** |
| **Active SLD (`sld.png`, `1280×800`)** | `0912ed15abc45921...` (`F1->{N1,N2}`, `F2->{N3,N4}`) | `1ca852a6e9f846e5...` (`F1->{N1,N3}`, `F2->{N2,N4}`) | **Counterfactual `N2`/`N3` Swap (`DISTINCT`)** |
| **`I1` / `A1` Hotspot Localization (`N4`)** | `[1080, 325, 1144, 394]` (`IoU=0.955`) / `[1078, 324, 1143, 393]` (`IoU=0.970`) | `[1080, 325, 1144, 394]` (`IoU=0.955`) / `[1080, 325, 1144, 395]` (`IoU=0.942`) | Localized on `N4` rear connector (`IoU >= 0.30`) |
| **Scoped Post-`L1` Probe (`N4`)** | `voltage_drop = 1.209409 V` | `voltage_drop = 1.209409 V` | Single probe after `L1_I1_PROPOSAL` lock |
| **Fitted `N4` Parameters (`I2`)** | `R0=0.040 Ohm`, `h=0.400 W/K`, `C=8.000 J/K` | `R0=0.040 Ohm`, `h=0.400 W/K`, `C=8.000 J/K` | Disambiguates electrical vs cooling fault |
| **Selected Safe Action (`I2`)** | **`MIGRATE_N4_TO_N3_8A`** (`F1=40A<=44A`, `F2=28A<=50A`) | **`MIGRATE_N4_TO_N2_8A`** (`F1=40A<=44A`, `F2=28A<=50A`) | **Counterfactual Decision Reversed** |
| **Rejected Unsafe Alternative** | `MIGRATE_N4_TO_N2_8A` (`F1=48A > 44A` overload) | `MIGRATE_N4_TO_N3_8A` (`F1=48A > 44A` overload) | Prevents `F1` busbar overload & `98.83°C` thermal violation |
| **Isolated Auditor Verdict (`A2`)** | `accept` (`RMSE=3.1e-7 °C`, `+1°C` canary `= 1.0°C`) | `accept` (`RMSE=3.1e-7 °C`, `+1°C` canary `= 1.0°C`) | Disjoint `environment_id` + `L1..L4` hash chain |
| **13-Boolean Strict Gate** | `strict_case_passed = True` (`13/13`) | `strict_case_passed = True` (`13/13`) | **`strict_pair_passed = True`** |

---

## Quickstart Paths

### Path 1 — Offline Verification & Replay Dashboard (No Credentials / No Network Required)

Verify the archived live `antigravity-preview-09-2026` / `gemini-4-argon` campaign dossier (`artifacts/reports/phase5_live_pair_dossier`), recompute all SHA-256 locks and 19-point physics trajectories offline, run the test suite, or launch the read-only replay UI:

```bash
# 1. Create virtual environment and install pinned dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Cryptographically verify & replay the archived live A/B campaign bundle (zero API calls)
python -m blackbox.campaign replay --bundle artifacts/reports/phase5_live_pair_dossier

# 3. Run the full offline test suite (unit, negative oracle, tamper-detection, and replay tests)
pytest -q

# 4. Launch the read-only Streamlit replay console (bound to 127.0.0.1:8501, zero API keys needed)
STREAMLIT_SERVER_HEADLESS=true .venv/bin/streamlit run blackbox/dashboard.py --server.address 127.0.0.1 --server.port 8501
```

### Path 2 — Live Managed Agent Campaign Execution (Deliberate Paid Run)

Execute a fresh 8-interaction (`I1`, `I2`, `A1`, `A2` across `case_A` and `case_B`) live campaign on `antigravity-preview-09-2026` (`gemini-4-argon`):

```bash
# 1. Configure credentials in .env (GOOGLE_API_KEY / GEMINI_API_KEY for AI Studio, or Vertex ADC)
cp .env.example .env

# 2. Run preflight verification (redacts secrets; writes preflight report)
python -m blackbox.campaign preflight --mode ai_studio

# 3. Execute a live paired A/B campaign with strict 280,000 noncached token ceiling
python -m blackbox.campaign live-pair \
  --mode ai_studio \
  --campaign-id live_pair_run_01 \
  --max-pair-noncached 280000
```

---

## Verified Live Campaign Dossier Summary

The archived live campaign bundle is stored at [`artifacts/reports/phase5_live_pair_dossier/`](artifacts/reports/phase5_live_pair_dossier/) with its human-readable executive report at [`artifacts/reports/phase5_live_pair_dossier/FINAL_REPORT.md`](artifacts/reports/phase5_live_pair_dossier/FINAL_REPORT.md):

- **Provider / Agent / Model**: Google GenAI Interactions API (`antigravity-preview-09-2026`, foundation model `gemini-4-argon`).
- **Role Sandbox Isolation**:
  - `case_A`: Investigator `environment_id = 3cf5af50a09cbe0dc9b6680e1544627f` (`I1`, `I2`) vs Auditor `environment_id = 3c05e3607ce48e5684a09c42d797c586` (`A1`, `A2`).
  - `case_B`: Investigator `environment_id = fdc6cb576b75a9c93cd32d42ae3ce138` (`I1`, `I2`) vs Auditor `environment_id = f1bd02455827b94ebc4d3d9c8bee0315` (`A1`, `A2`).
- **Token Accounting**:
  - `case_A` noncached / total tokens: `76,193` / `116,773`
  - `case_B` noncached / total tokens: `77,828` / `118,390`
  - Pair noncached tokens: `154,021 <= 280,000` budget ceiling (`token_budget_within_limits = True`).

---

## Repository Structure

```text
G4A/
├── 00_MASTER_SPEC.md                        # Master architecture & phase roadmap
├── 01_DOMAIN_AND_FIXTURE.md                 # Electro-thermal ODE kernel & A/B counterfactual fixture spec
├── 02_VISUAL_ASSETS.md                      # 3D scene rendering & active SLD vector/raster spec
├── 03_MANAGED_RUNTIME.md                    # Managed Agents SDK lifecycle, polling, and 3-ledger budget spec
├── 04_AGENT_PROTOCOL_AND_CONTRACTS.md       # 4-turn L1..L4 protocol, Pydantic contracts & 4-layer oracle spec
├── 05_CAMPAIGN_EXECUTION_AND_REPORTING.md   # 13-gate acceptance matrix, ablations & tamper-evident bundle spec
├── 06_DASHBOARD_AND_REPLAY_UX.md            # 6-panel replay UX, judge script, video storyboard & deployment
├── blackbox/
│   ├── domain.py                            # Authoritative electro-thermal physics & OLS parameter identification
│   ├── fixture.py                           # Sealed private truth, redacted public packet & A/B world builder
│   ├── probe.py                             # Single-shot SHA-256 signed diagnostic probe session
│   ├── render_sld.py                        # Deterministic SLD SVG/PNG renderer & pixel junction parser
│   ├── scene_bridge.py                      # 1280x800 3D RGB/thermal renderer & camera projection bridge
│   ├── managed.py                           # Managed Agent turn runner, file retrieval & 3-ledger budget tracker
│   ├── agent_harness.py                     # Role-isolated AGENTS.md & compact I1/I2/A1/A2 prompt builders
│   ├── contracts.py                         # Strict Pydantic v2 contracts (ProposalV2, PlanV2, IndependentGraphV2, AuditChallengeV2)
│   ├── protocol.py                          # 4-turn VisualV2ProtocolOrchestrator & hash-chained L1..L4 locks
│   ├── oracle.py                            # GuardedOracleTruthLoader & 4-layer scientific/operational evaluator
│   ├── evidence.py                          # Tamper-evident bundle sealer, 13-gate verifier & CaseView/PairView loader
│   ├── campaign.py                          # CLI entrypoint (preflight, dry-run, live-pair, replay, ablations)
│   └── dashboard.py                         # Read-only 6-panel Streamlit replay & causal audit application
├── deploy/
│   ├── Caddyfile                            # Automatic HTTPS reverse proxy config to 127.0.0.1:8501
│   └── blackbox-dashboard.service           # Hardened systemd unit with cloud API keys explicitly cleared
├── artifacts/reports/phase5_live_pair_dossier/ # Verified live A/B campaign evidence bundle & FINAL_REPORT.md
└── tests/                                   # Comprehensive pytest suite (domain, fixture, visual, managed, protocol, oracle, campaign, dashboard)
```

---

## BlackboxDC Twin Repair (`twin_repair/`) — Closed-Loop 3D Digital Twin Render, Visual Critique & Electro-Thermal Repair

Implemented per [`md/11_TWIN_REPAIR_ADDON_IMPLEMENTATION_PLAN_EN.md`](md/11_TWIN_REPAIR_ADDON_IMPLEMENTATION_PLAN_EN.md), **`twin_repair/`** adds an interactive 3D digital-twin repair loop (Track Two live render → visual critique → structured patch → 3D re-render → electro-thermal safety verification) on top of the `blackbox/` physics and Managed Agent transport kernel:

1. **Builder Managed Agent (`antigravity-preview-09-2026` / `gemini-4-argon`)**: Reconstructs an initial `scene_spec.v1` (`scene0.json`) in an isolated remote sandbox from historical commissioning drawings (`old_sld_A.png`) and rear thermal imagery (`old_thermal_rear.png`).
2. **Parameterized 3D Three.js Renderer (`blackbox/static3d/scene.js` + `twin_repair/render_bridge.py`)**: Renders `1280×800` `render_wiring.png`, `render_rgb_rear.png`, and `render_thermal_rear.png` with overhead `F1` (`#1d8aff`, `44 A`) and `F2` (`#ff8c1a`, `50 A`) busbars separated by `0.382 m >= 0.20 m` in 3D space and graph-coupled `THREE.TubeGeometry` feeder drop cables.
3. **Visual Critic Managed Agent (`antigravity-preview-09-2026` / `gemini-4-argon`)**: Runs in a **second, disjoint remote sandbox** (`distinct_environments = True`), compares `render0/render_wiring.png` against the current as-built SLD (`current_sld_B.png`), localizes the drifted `N2` and `N3` feeder cables with pixel bounding boxes, and emits a hash-bound `repair_patch.v1` (`patch.json`).
4. **Deterministic Patch Application, 3D Re-Render & Sealed Verifier (`twin_repair/verifier.py`)**: Applies `replace_feeder` operations (`N2: F1 -> F2`, `N3: F2 -> F1`), re-renders `render1/render_wiring.png`, verifies cable pixel changes inside the projected `N2`/`N3` corridors (`8,326` and `8,502` changed pixels with HUD masked out), upgrades graph accuracy from `2/4 -> 4/4`, and reverses the load-shift recommendation from unsafe `N3` (`F1 = 48.0/44.0 A` overload) to safe `N2` (`F1 = 40.0/44.0 A`, `F2 = 28.0/50.0 A`, `N4(180s) = 52.53°C <= 85.0°C`).

### Verified Live Twin-Repair Run (`runs/live1` / [`RESULTS_TWIN_REPAIR.md`](RESULTS_TWIN_REPAIR.md))

| Metric / Gate | Live Run Value (`twin-repair-a9a617846fdc`) | Gate Status |
| :--- | :--- | :--- |
| **Builder Managed Agent (`gemini-4-argon`)** | `v1_Chd2RzdlYXFxb0pMekR4ZElQZzRTSzJRTRIXdkc3ZWFxcW9KTHpEeGRJUGc0U0syUU0` (`env=edf75e56c487e37f0b63f4bc8e5b204c`) | `builder_completed = True` |
| **Visual Critic Managed Agent (`gemini-4-argon`)** | `v1_Chc2bTdlYXJYcko2UFMwX2dQaHNYVTZRTRIXNm03ZWFyWHJKNlBTMF9nUGhzWFU2UU0` (`env=26e8d5e5c359ebb9ba783aa4e4391aff`) | `critic_completed = True` |
| **Sandbox Environment Isolation** | `edf75e56c487e37f0b63f4bc8e5b204c != 26e8d5e5c359ebb9ba783aa4e4391aff` | `distinct_environments = True` |
| **3D Cable Pixel Delta (`render0` -> `render1`)** | `N2 = 8,326 px`, `N3 = 8,502 px` (`N1 = 0 px`, `N4 = 0 px`, HUD excluded) | `cable_pixels_changed = True` |
| **Hotspot Registration & IoU (`N4`)** | `0.608 px <= 3.0 px`, `IoU = 1.000` | `render0_valid = True`, `render1_valid = True` |
| **As-Built Graph Accuracy** | `2/4 -> 4/4` (`N2: F1->F2`, `N3: F2->F1`) | `graph_improved = True` |
| **Electro-Thermal Decision Consequence** | Stale twin: `N3` (`F1=48/44 A` overload) → Repaired twin: `N2` (`F1=40/44 A`, `F2=28/50 A`, `52.53°C`) | `physics_safe = True` |
| **Token Usage (Builder + Critic)** | `39,019` noncached tokens (`76,429` total tokens) | `argon_verified = True`, `passed = True` |

### Twin Repair CLI & Interactive 3D Web App Commands

```bash
# 1. Generate deterministic drift (A->B) and control (A->A) challenge fixtures
python -m twin_repair.challenge build --seed 20261025 --out runs/fixtures/drift1
python -m twin_repair.challenge build --seed 20261025 --case-type control_A_to_A --out runs/fixtures/control1

# 2. Render 3D scene specification via headless Chromium + Three.js
python -m twin_repair.render_bridge --spec runs/fixtures/drift1/scene_A.json --out runs/fixtures/drift1/render_A

# 3. Run offline mock rehearsal or live Argon Managed Agent workflow
python -m twin_repair.orchestrator mock --case runs/fixtures/drift1 --out runs/mock1
python -m twin_repair.orchestrator live --case runs/fixtures/drift1 --out runs/live1 --max-noncached-tokens 80000
python -m twin_repair.orchestrator replay --run runs/live1

# 4. Launch the 4-state Three.js + Starlette interactive web experience (supports ?present=1)
python -m twin_repair.web --host 127.0.0.1 --port 8000 --runs runs
```

### Provenance & Scope Disclosure

- **Event-Day Original Extension (`twin_repair/` + parameterized `blackbox/static3d/`)**: Closed-loop 3D digital twin scene specification (`SceneSpec v1`), graph-coupled 3D cable renderer, visual Critic patch protocol (`RepairPatch v1`), sealed geometric/pixel/physics verifier, and 4-state interactive Three.js web experience.
- **Pre-Event Reused Baseline (`blackbox/domain.py`, `blackbox/managed.py`)**: Lumped RC electro-thermal ODE solver and Google GenAI Managed Agent polling/file-retrieval transport (documented in [`runs/preflight/reuse_map.md`](runs/preflight/reuse_map.md)).

---

## Explicit Scope & Safety Boundaries

- **Advisory Simulation Only (`physical_safety_certified = False`)**: Every contract and report enforces `physical_safety_certified: false`. No output is certified for physical electrical utility or data-center operations.
- **Sample Size (`N = 2` Paired Counterfactual)**: Evaluates a controlled two-world counterfactual pair (`case_A` vs `case_B`) isolating SLD feeder branch reversal.
- **Single Fault Family**: Models resistive rear-connector heating (`R0 = 0.040 Ohm`, `h = 0.4 W/K`, `C = 8.0 J/K`) versus local cooling degradation (`R0 = 0.020 Ohm`, `h = 0.2 W/K`, `C = 4.0 J/K`) on rack `N4`.
- **Simplified Lumped Electro-Thermal ODE**: Uses a first-order lumped RC thermal model (`dt = 10 s`, horizon `0..180 s`) and steady busbar current summation; does not model 3D CFD airflow recirculation, AC harmonic distortion, transient inrush, or breaker trip curves.
- **No Physical Actuation**: Neither the Investigator nor the Auditor connects to physical PDUs, switchgear, or building management systems.
