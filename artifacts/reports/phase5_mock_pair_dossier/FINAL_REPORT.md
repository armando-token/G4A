# BlackboxDC Visual-V2 Counterfactual A/B Campaign Dossier (`FINAL_REPORT.md`)

- **Campaign Run ID**: `phase5_mock_pair_dossier`
- **Execution Mode**: `offline_deterministic`
- **Diagnostic Mode / Protocol Version**: `guided_diagnostic` (`visual-v2/guided_diagnostic`)
- **Strict Pair Passed (`strict_pair_passed`)**: `True`
- **Strict Case A Passed (`strict_case_A`)**: `True`
- **Strict Case B Passed (`strict_case_B`)**: `True`
- **Physical Safety Certified (`physical_safety_certified`)**: `False`

## 1. Precise Counterfactual Hypothesis & Paired Intervention

This campaign tests whether an autonomous multimodal Investigator (`I1`, `I2`) and a sandbox-isolated Auditor (`A1`, `A2`) can jointly:
1. Localize a degraded rear power connector hotspot on rack `N4` (`[1079, 325, 1143, 393]` in `1280x800` rear-aisle renders) and bind telemetry sensors (`SNS_K1->N1`, `SNS_K2->N4`, `SNS_K3->N2`, `SNS_K4->N3`).
2. Disambiguate electrical contact degradation (`R0 = 0.040 Ohm`, `h = 0.4 W/K`, `C = 8.0 J/K`) from cooling degradation (`R0 = 0.020 Ohm`, `h = 0.2 W/K`, `C = 4.0 J/K`) using a single post-`L1` active diagnostic probe (`voltage_drop` on `N4`).
3. Reverse the selected 8.0 A load-migration receiver when the Single-Line Diagram (`sld.png`) swaps `N2` and `N3` feeder busbars between **Case A** (`F1->{N1,N2}`, `F2->{N3,N4}` -> safe action `MIGRATE_N4_TO_N3_8A`) and **Case B** (`F1->{N1,N3}`, `F2->{N2,N4}` -> safe action `MIGRATE_N4_TO_N2_8A`), while `rear_aisle_rgb.png`, `rear_aisle_thermal.png`, and substantive `public_packet.json` telemetry are held byte-for-byte identical.

## 2. Event Relevance: Argon Multimodal, Long-Context, Code Execution & Managed Agent Isolation

- **Native Multimodal Perception**: `I1` and `A1` ingest raw `1280x800` PNGs (`sld.png`, `rear_aisle_rgb.png`, `rear_aisle_thermal.png`) to trace vector busbar junction dots vs jump arches and localize the `N4` connector housing.
- **Sandbox Code Execution (`domain.py`)**: `I2` and `A2` execute Python code inside their managed workspaces to invert the temperature-dependent resistance law, fit `(h, C)` over the 13-sample thermal ramp, and simulate 19-point (`0..180s`, `dt=10s`) forward Euler trajectories across all three candidate actions.
- **Cryptographic & Sandbox Isolation**: `Investigator` (`I1`, `I2`) and `Auditor` (`A1`, `A2`) execute in disjoint managed `environment_id`s; `A1` is locked at `L3_A1_GRAPH` before `A2` ever receives `I2`'s plan (`L2_I2_PLAN`); private truth is sealed behind `GuardedOracleTruthLoader` until all four hash-chained locks (`L1..L4`) are committed.

## 3. Case Execution & Token Accounting Table

- **Requested Agent / Model**: `antigravity-preview-09-2026` / `gemini-4-argon` (Returned: `gemini-4-argon`)
- **Case A Noncached / Cached-Inclusive Tokens**: `8200` / `9000`
- **Case B Noncached / Cached-Inclusive Tokens**: `8200` / `9000`
- **Pair Noncached Tokens (`<= 280000` cap)**: `16400` (`token_budget_within_limits=True`)

| Case | Turn | Lock Stage | Interaction ID | Environment ID | Status | Elapsed (s) | Noncached Tokens | Cached Tokens | Total Tokens | Unknown Usage |
| :--- | :--- | :--- | :--- | :--- | :--- | ---: | ---: | ---: | ---: | :--- |
| `case_A` | `I1` | `L1_I1_PROPOSAL` | `inter-v2-0001` | `env-investigator-001` | `completed` | 0.057 | 2050 | 200 | 2250 | `False` |
| `case_A` | `I2` | `L2_I2_PLAN` | `inter-v2-0002` | `env-investigator-001` | `completed` | 0.021 | 2050 | 200 | 2250 | `False` |
| `case_A` | `A1` | `L3_A1_GRAPH` | `inter-v2-0003` | `env-auditor-002` | `completed` | 0.047 | 2050 | 200 | 2250 | `False` |
| `case_A` | `A2` | `L4_A2_CHALLENGE` | `inter-v2-0004` | `env-auditor-002` | `completed` | 0.021 | 2050 | 200 | 2250 | `False` |
| `case_B` | `I1` | `L1_I1_PROPOSAL` | `inter-v2-0005` | `env-investigator-003` | `completed` | 0.044 | 2050 | 200 | 2250 | `False` |
| `case_B` | `I2` | `L2_I2_PLAN` | `inter-v2-0006` | `env-investigator-003` | `completed` | 0.020 | 2050 | 200 | 2250 | `False` |
| `case_B` | `A1` | `L3_A1_GRAPH` | `inter-v2-0007` | `env-auditor-004` | `completed` | 0.044 | 2050 | 200 | 2250 | `False` |
| `case_B` | `A2` | `L4_A2_CHALLENGE` | `inter-v2-0008` | `env-auditor-004` | `completed` | 0.019 | 2050 | 200 | 2250 | `False` |

## 4. Exact Input SHA-256 Digests & Cross-Case Invariants

- **Cross-Case Input Invariants Passed**: `True`
- **`rear_aisle_rgb.png` Identical Across A & B**: `True`
- **`rear_aisle_thermal.png` Identical Across A & B**: `True`
- **Substantive `public_packet.json` Identical Across A & B**: `True`
- **`sld.png` Distinct Between A & B**: `True`

| Input Artifact | Case A SHA-256 | Case B SHA-256 | Relationship |
| :--- | :--- | :--- | :--- |
| `inputs/sld.png` | `0912ed15abc45921ee5e140ab4a5b60a87c67815af255d62ee056d378a1dd489` | `1ca852a6e9f846e56e38c735a5e88eb703fbc14f19d4cdcc3e90291af4143915` | `DISTINCT=True` |
| `inputs/rear_aisle_rgb.png` | `078444bac2d0a309733e07653c5b145fb3021ed0b95fd825bc7745fc374f7f38` | `078444bac2d0a309733e07653c5b145fb3021ed0b95fd825bc7745fc374f7f38` | `IDENTICAL=True` |
| `inputs/rear_aisle_thermal.png` | `2c29654fddec6815c5591b661a7c5e2130c073e4c0dee0fa4207b873105b33d2` | `2c29654fddec6815c5591b661a7c5e2130c073e4c0dee0fa4207b873105b33d2` | `IDENTICAL=True` |
| `inputs/public_packet.json` (raw) | `463dac8f803ca01939558398f84c5e6cdaad8fa5ae2583b926a220355b4fd830` | `463dac8f803ca01939558398f84c5e6cdaad8fa5ae2583b926a220355b4fd830` | `CASE_ID_TAGGED` |
| `inputs/public_packet.json` (substantive) | `c824b1217603da94bf6d7a6b2a96424c1d0be2b0d20a23b890c44a624dbc6ec6` | `c824b1217603da94bf6d7a6b2a96424c1d0be2b0d20a23b890c44a624dbc6ec6` | `IDENTICAL=True` |

## 5. Visual Perception: `I1` & `A1` Topology Graphs and Connector Localization

- **I1 Graph Reversal (`N2`/`N3` Feeder Swap Verified)**: `True`
- **A1 Graph Reversal (`N2`/`N3` Feeder Swap Verified)**: `True`

| Case | Role | Power Edges (`N1..N4`) | Sensor Edges (`SNS_K1..K4`) | Hotspot BBox `[x0,y0,x1,y1]` | IoU vs GT | Localized Pass |
| :--- | :--- | :--- | :--- | :--- | ---: | :--- |
| `case_A` | `I1` | `{"N1": "F1", "N2": "F1", "N3": "F2", "N4": "F2"}` | `{"SNS_K1": "N1", "SNS_K2": "N4", "SNS_K3": "N2", "SNS_K4": "N3"}` | `[1079, 325, 1143, 393]` | 1.0000 | `True` |
| `case_A` | `A1` | `{"N1": "F1", "N2": "F1", "N3": "F2", "N4": "F2"}` | `{"SNS_K1": "N1", "SNS_K2": "N4", "SNS_K3": "N2", "SNS_K4": "N3"}` | `[1079, 325, 1143, 393]` | 1.0000 | `True` |
| `case_B` | `I1` | `{"N1": "F1", "N2": "F2", "N3": "F1", "N4": "F2"}` | `{"SNS_K1": "N1", "SNS_K2": "N4", "SNS_K3": "N2", "SNS_K4": "N3"}` | `[1079, 325, 1143, 393]` | 1.0000 | `True` |
| `case_B` | `A1` | `{"N1": "F1", "N2": "F2", "N3": "F1", "N4": "F2"}` | `{"SNS_K1": "N1", "SNS_K2": "N4", "SNS_K3": "N2", "SNS_K4": "N3"}` | `[1079, 325, 1143, 393]` | 1.0000 | `True` |

## 6. Selected Remediation Actions & PDU Current Margins (`F1 <= 44.0 A`, `F2 <= 50.0 A`)

- **Safe Receiver Reversal (`A -> MIGRATE_N4_TO_N3_8A`, `B -> MIGRATE_N4_TO_N2_8A`)**: `True`
- **Case A Selected Action / Decision / Auditor Verdict**: `MIGRATE_N4_TO_N3_8A` / `intervene` / `accept`
- **Case B Selected Action / Decision / Auditor Verdict**: `MIGRATE_N4_TO_N2_8A` / `intervene` / `accept`

| Case | Candidate Action | `N4` `t=180s` (°C) | `F1` Current (A) | `F1` Margin (A) | `F2` Current (A) | `F2` Margin (A) | Thermal Violation | PDU Overload | Safe |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | :--- | :--- | :--- |
| `case_A` | `NO_ACTION` | 98.83 | 40.00 | 4.00 | 28.00 | 22.00 | `True` | `False` | `False` |
| `case_A` | `MIGRATE_N4_TO_N2_8A` | 52.53 | 48.00 | -4.00 | 20.00 | 30.00 | `False` | `True` | `False` |
| `case_A` | `MIGRATE_N4_TO_N3_8A` | 52.53 | 40.00 | 4.00 | 28.00 | 22.00 | `False` | `False` | `True` |
| `case_B` | `NO_ACTION` | 98.83 | 40.00 | 4.00 | 28.00 | 22.00 | `True` | `False` | `False` |
| `case_B` | `MIGRATE_N4_TO_N2_8A` | 52.53 | 40.00 | 4.00 | 28.00 | 22.00 | `False` | `False` | `True` |
| `case_B` | `MIGRATE_N4_TO_N3_8A` | 52.53 | 48.00 | -4.00 | 20.00 | 30.00 | `False` | `True` | `False` |

## 7. 19-Point Electro-Thermal Forecasts, Parameter Fits & `+1.0°C` Sensitivity Canary

| Metric | Case A | Case B | Tolerance / Requirement |
| :--- | ---: | ---: | :--- |
| Fitted `R0_ohm` (`N4`) | 0.040000 | 0.040000 | `abs_err <= 0.002 Ohm` (GT `0.040`) |
| Fitted `h_W_per_K` (`N4`) | 0.400000 | 0.400000 | `rel_err <= 5%` (GT `0.400`) |
| Fitted `C_J_per_K` (`N4`) | 8.000000 | 8.000000 | `rel_err <= 5%` (GT `8.000`) |
| Max 19-Point Trajectory RMSE (°C) | 0.000000 | 0.000000 | `<= 0.35°C` |
| Max PDU Current Error (A) | 0.00000000 | 0.00000000 | `<= 1e-6 A` |
| Auditor `A2` Recomputation RMSE (°C) | 0.00000000 | 0.00000000 | `<= 1e-5°C` |
| Auditor `+1.0°C` Canary Max Shift (°C) | 1.000000 | 1.000000 | `> 0.10°C` (`baseline=98.83°C -> 98.83°C`) |

## 8. 13-Boolean Strict Acceptance Matrix, Ablations & Attempt Disclosure

### 8.1 13-Boolean Case Classification Matrix

| # | Boolean Criterion | `case_A` | `case_B` |
| ---: | :--- | :--- | :--- |
| 1 | `input_integrity` | `True` | `True` |
| 2 | `native_multimodal_path` | `True` | `True` |
| 3 | `investigator_perception` | `True` | `True` |
| 4 | `measurement` | `True` | `True` |
| 5 | `model_and_action` | `True` | `True` |
| 6 | `forecast` | `True` | `True` |
| 7 | `auditor_independence` | `True` | `True` |
| 8 | `auditor_challenge` | `True` | `True` |
| 9 | `operational` | `True` | `True` |
| 10 | `provenance` | `True` | `True` |
| 11 | `numerical_evidence_valid` | `True` | `True` |
| 12 | `operational_complete` | `True` | `True` |
| 13 | `strict_case_passed` | `True` | `True` |

### 8.2 Comparison & Ablation Baselines

| Baseline ID | Fair Baseline | Privileged Upper Bound | Case A Action (`Safe`) | Case B Action (`Safe`) | Cases Safe (`/2`) | Notes |
| :--- | :--- | :--- | :--- | :--- | ---: | :--- |
| `no_action` | `True` | `False` | `NO_ACTION` (`False`) | `NO_ACTION` (`False`) | 0 | No load migration (selected_action_id='NO_ACTION'); N4 exceeds 90.0 C thermal limit in both cases. |
| `fixed_n2_policy` | `True` | `False` | `MIGRATE_N4_TO_N2_8A` (`False`) | `MIGRATE_N4_TO_N2_8A` (`True`) | 1 | SLD-blind heuristic that always migrates 8 A from N4 to N2; overloads F1 (48 A > 44 A) in Case A and succeeds only in Case B. |
| `privileged_oracle` | `False` | `True` | `MIGRATE_N4_TO_N3_8A` (`True`) | `MIGRATE_N4_TO_N2_8A` (`True`) | 2 | Privileged upper bound with direct private truth access (not a fair baseline); selects N3 in Case A and N2 in Case B. |
| `no_sld_variant` | `True` | `False` | `NO_ACTION` (`False`) | `NO_ACTION` (`False`) | 0 | Ablation withholding sld.png: unknown_critical_edges=['N2', 'N3'] forces blocking_graph_uncertainty=True and decision='abstain' (NO_ACTION), preventing blind PDU overload. |

### 8.3 Failed / Incomplete Attempt Disclosure & Historical `installation-v1` Note

- **Failed or Incomplete Runs in Current Campaign (`len=0`)**: `[]`
- **Historical Protocol Disclosure**: Any prior `installation-v1` artifacts are superseded historical scaffolding; only `visual-v2` runs verified by `verify_and_replay_bundle` count toward `strict_pair_passed`.

## 9. Explicit Scope & Safety Limitations

- **Advisory Simulation Only (`physical_safety_certified = False`)**: All outputs are uncertified synthetic electro-thermal simulations (`physical_safety_certified: false`).
- **Sample Size (`N = 2` Paired Counterfactual)**: Evaluates a single two-world counterfactual pair (`case_A` vs `case_B`) isolating SLD feeder topology reversal.
- **Single Fault Family**: Models resistive rear-connector contact degradation (`R0 = 0.040 Ohm`) vs local cooling loss (`h = 0.2 W/K`) on rack `N4`.
- **Simplified Lumped Electro-Thermal ODE**: Uses a first-order lumped RC thermal model (`dt = 10s`, `0..180s`) and steady busbar current summation; does not model 3D CFD airflow recirculation, transient inrush, breaker trip curves, or arc-flash hazards.
- **No Physical Actuation**: Neither `Investigator` nor `Auditor` connects to physical PDUs, BMS controllers, or live electrical switchgear.

## 10. Verification & Offline Replay Commands

```bash
# Run full test suite
pytest -q

# Offline deterministic verification & replay of this evidence bundle (zero API calls)
python -m blackbox.evidence --verify .
```
