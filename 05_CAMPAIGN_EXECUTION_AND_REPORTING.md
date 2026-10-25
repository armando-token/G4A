# Phase 05 — Precommitted A/B Campaign, Live Run & Evidence Dossier (`05_CAMPAIGN_EXECUTION_AND_REPORTING.md`)

## Overview
Phase 05 defines the precommitted paired A/B campaign runner (`blackbox/campaign.py`) and tamper-evident evidence dossier generator (`blackbox/evidence.py`) for **BlackboxDC**. It enforces pre-execution cryptographic commitment of all stimuli, schemas, prompts, thresholds, and opaque case labels before any Managed Agent interaction occurs, evaluates 13 independent boolean gates per case plus the paired gate (`strict_pair_passed`), computes 4 ablation baselines, and supports offline tamper-evident replay.

---

## 1. Precommitment & Preflight Freeze (`pair_manifest.json`)

Before any mock or live interaction is dispatched, `preflight` freezes and signs the campaign precommitment manifest containing:
1. **`fixture_version`**: Schema version of the physical domain and paired fixture (`"1.0.0"` / `"visual-v2"`).
2. **`private_truth_sha256`**: Sealed SHA-256 digests of `build_private_world("A")` and `build_private_world("B")`.
3. **`public_packet_controlled_sha256`**: Controlled-content SHA-256 of `export_public_packet` excluding `case_id`, proving byte-for-byte identity of non-SLD public inputs across Case A and Case B.
4. **Exact Visual Asset SHA-256s**:
   - `case_A/inputs/sld.png`, `case_A/inputs/rear_aisle_rgb.png`, `case_A/inputs/rear_aisle_thermal.png`
   - `case_B/inputs/sld.png`, `case_B/inputs/rear_aisle_rgb.png`, `case_B/inputs/rear_aisle_thermal.png`
   - Enforces the 5 cross-case SHA-256 invariants (`rgb` identical, `thermal` identical, `sld` distinct, `telemetry` identical, `records` identical).
5. **Renderer & Runtime Versions**: Browser/WebGL version, Three.js revision, Python version, `numpy`/`pydantic` versions, `domain.py` SHA-256, target agent (`antigravity-preview-09-2026`), and model (`gemini-4-argon`).
6. **Role Prompts & JSON Schema Hashes**: Canonical SHA-256 digests of `AGENTS.md` (`investigator`, `auditor`), `I1`/`I2`/`A1`/`A2` prompt templates, and Pydantic JSON schemas (`ProposalV2`, `PlanV2`, `IndependentGraphV2`, `AuditChallengeV2`).
7. **Declared Diagnostic Mode**: `"guided_diagnostic"` or `"blind_diagnostic"` (strictly separated; never mixed in a single campaign or budget ledger).
8. **Oracle Acceptance Thresholds & Token Budgets**:
   - Visual `IoU >= 0.30` (or center-in-bbox with area fraction `< 0.03`), trajectory `RMSE <= 0.35 °C`, PDU current error `<= 1e-6 A`, `+1.0 °C` sensitivity canary `max_abs_diff_c > 1e-6`.
   - Token caps: `30,000` per turn noncached, `140,000` per case noncached (`40,000` Auditor reserve, `20,000` `A2` reserve), `280,000` case / `560,000` pair cached-inclusive ceiling.
9. **Opaque Case IDs & Sealed Label Map**: Assigns opaque run identifiers to Case A and Case B during execution with a local sealed label-to-ID map unsealed only at post-`L4` scoring.
10. **Planned Comparisons & Ablations**: Precommits the paired A/B action-reversal test (`MIGRATE_N4_TO_N3_8A` in A vs. `MIGRATE_N4_TO_N2_8A` in B) and the 4 deterministic ablation baselines.

---

## 2. Campaign CLI Commands (`python -m blackbox.campaign`)

```bash
# 1. Freeze precommitment manifest, verify visual/fixture hashes & auth readiness
python -m blackbox.campaign preflight --output-dir artifacts/campaign_preflight

# 2. Run deterministic offline mock paired campaign (Case A + Case B)
python -m blackbox.campaign mock-pair --output-dir artifacts/campaign_mock --mode guided_diagnostic

# 3. Run live Managed Agent paired campaign (antigravity-preview-09-2026 / gemini-4-argon)
python -m blackbox.campaign live-pair --output-dir artifacts/campaign_live --mode guided_diagnostic

# 4. Tamper-evident offline replay & verification of an existing campaign bundle
python -m blackbox.campaign replay --bundle-dir artifacts/campaign_live
```

---

## 3. Thirteen Independent Boolean Gates per Case + `strict_pair_passed`

Each case (`case_A` and `case_B`) is graded across **13 boolean gates** recorded in `score.json`:

| # | Gate Name | Requirement |
| :--- | :--- | :--- |
| 1 | `input_integrity` | All input PNGs and `public_packet.json` match precommitted SHA-256 digests and pass zero-leakage checks. |
| 2 | `native_multimodal_path` | `I1` and `A1` receive the 3 PNGs as native multimodal image blocks with `degraded_2d_fallback == False`. |
| 3 | `investigator_perception` | `I1` localizes `hotspot.asset_id == "N4"` (`IoU >= 0.30` or center inside `[1079, 325, 1143, 393]`), extracts exact active SLD `edges`, and binds `SNS_K1..SNS_K4`. |
| 4 | `measurement` | Single probe receipt issued **after** `L1_I1_PROPOSAL` lock on `N4`; `voltage_drop` selected in `guided_diagnostic` mode; zero post-hoc probe attempts. |
| 5 | `model_and_action` | `I2` recovers `(R0=0.04, h=0.4, C=8.0)` within tolerance and selects the safe 8.0 A migration (`MIGRATE_N4_TO_N3_8A` in A, `MIGRATE_N4_TO_N2_8A` in B). |
| 6 | `forecast` | `I2` 19-sample temperature trajectory matches `domain.simulate_world` (`RMSE <= 0.35 °C`) and PDU currents match within `<= 1e-6 A`. |
| 7 | `auditor_independence` | `A1` executes in a separate `environment_id` from `I1`/`I2`, receives zero Investigator/probe fields, verifies `input_png_sha256`, and locks `L3_A1_GRAPH` before `A2`. |
| 8 | `auditor_challenge` | `A2` compares `a1_power_map` vs. `i2_power_map`, recomputes all 3 actions, runs the `+1.0 °C` sensitivity canary, and issues the correct `verdict` (`"accept"` on valid safe plan, `"reject"` on unsafe/inconsistent plan). |
| 9 | `operational` | Selected action maintains all rack temperatures `< 90.0 °C`, rack currents `<= max_current_A`, `F1 <= 44.0 A`, `F2 <= 50.0 A`, and `physical_safety_certified == False`. |
| 10 | `provenance` | Unbroken `L1 -> L2 -> L3 -> L4` cryptographic hash chain (`verify_lock_chain_v2`), zero pre-`L4` truth access, and all 4 contract files validated. |
| 11 | `numerical_evidence_valid` | Composite scientific gate (`input_integrity and investigator_perception and measurement and model_and_action and forecast and auditor_independence and auditor_challenge and operational and provenance`). |
| 12 | `operational_complete` | All 4 turns (`I1`, `I2`, `A1`, `A2`) reached terminal `"completed"` status with `has_code_execution_step == True`, non-empty retrieved workspace files, and all token budgets respected. |
| 13 | `strict_case_passed` | Requires `numerical_evidence_valid and operational_complete and native_multimodal_path` (and `live_provider_evidence == True` for live campaigns). |

- **Pair-Level Gate (`strict_pair_passed`)**:
  - `strict_pair_passed = case_A.strict_case_passed and case_B.strict_case_passed and (case_A.selected_action_id == "MIGRATE_N4_TO_N3_8A") and (case_B.selected_action_id == "MIGRATE_N4_TO_N2_8A")`.
  - If either case has an incomplete turn (as in the historical `installation-v1` predecessor run), `operational_complete` and `strict_pair_passed` are `False` even if local numerical checks succeed.

---

## 4. Four Deterministic Ablation Baselines

| Ablation ID | Policy Definition | Outcome on Case A | Outcome on Case B | Pair Pass |
| :--- | :--- | :--- | :--- | :--- |
| **`no_action`** | Holds `N4` at `24.0 A` (`NO_ACTION`) in both cases | Fails (`N4` reaches `98.83 °C >= 90.0 °C`, `"unsafe, thermal"`) | Fails (`N4` reaches `98.83 °C >= 90.0 °C`, `"unsafe, thermal"`) | `False` (`0/2`) |
| **`fixed_n2_policy`** | Always migrates `8.0 A` from `N4 -> N2` ignoring the active SLD | Fails (`F1 = 48.0 A > 44.0 A`, `"unsafe, F1 overload"`) | Passes (`F1 = 40.0 A`, `F2 = 28.0 A`, `"safe"`) | `False` (`1/2`) |
| **`privileged_oracle`** | Full ground-truth physics & SLD upper bound (`N4 -> N3` in A, `N4 -> N2` in B) | Passes (`52.53 °C`, `F1 = 40.0 A`, `F2 = 28.0 A`, `"safe"`) | Passes (`52.53 °C`, `F1 = 40.0 A`, `F2 = 28.0 A`, `"safe"`) | `True` (`2/2`) |
| **`no_sld_variant`** | Removes `sld.png` (`unknown_critical_edges` non-empty); requires compliant abstention (`NO_ACTION`) | Abstains safely from guessing topology (`decision="abstain"`) | Abstains safely from guessing topology (`decision="abstain"`) | `False` (Abstain) |

---

## 5. Evidence Bundle Layout & Tamper-Evident Offline Replay

```text
<campaign_output_dir>/
├── pair_manifest.json              # Precommitted hashes, schemas, budgets, and cross-case invariants
├── FINAL_REPORT.md                 # Human-readable executive dossier, gate table, and ablation comparison
├── replay_manifest.json            # Generated by `replay`: hash verification & recomputed gate parity
├── case_A/
│   ├── inputs/
│   │   ├── sld.png
│   │   ├── rear_aisle_rgb.png
│   │   ├── rear_aisle_thermal.png
│   │   └── public_packet.json
│   ├── turns/
│   │   ├── I1_request.json         # Redacted request payload (base64 images replaced with SHA-256 + length)
│   │   ├── I1_response.json        # Terminal TurnEvidence & token usage snapshot
│   │   ├── I1_steps.json           # Redacted interaction tool/code_execution step trace
│   │   ├── I2_request.json
│   │   ├── I2_response.json
│   │   ├── I2_steps.json
│   │   ├── A1_request.json
│   │   ├── A1_response.json
│   │   ├── A1_steps.json
│   │   ├── A2_request.json
│   │   ├── A2_response.json
│   │   └── A2_steps.json
│   ├── remote_artifacts/
│   │   ├── proposal.json           # Raw retrieved I1 artifact
│   │   ├── plan.json               # Raw retrieved I2 artifact
│   │   ├── independent_graph.json  # Raw retrieved A1 artifact
│   │   └── audit_challenge.json    # Raw retrieved A2 artifact
│   ├── locks.jsonl                 # L1_I1_PROPOSAL, L2_I2_PLAN, L3_A1_GRAPH, L4_A2_CHALLENGE
│   ├── probe_receipt.json          # Signed single-shot ProbeReceipt
│   └── score.json                  # 13 boolean gates + quantitative metrics for Case A
└── case_B/
    ├── inputs/...
    ├── turns/...
    ├── remote_artifacts/...
    ├── locks.jsonl
    ├── probe_receipt.json
    └── score.json
```

### Tamper-Evident Offline Replay Rules (`replay`)
1. Re-hashes every file in `inputs/` and `remote_artifacts/` and compares against `pair_manifest.json` and `locks.jsonl`.
2. Re-verifies `verify_lock_chain_v2` across `L1 -> L2 -> L3 -> L4` and checks `probe_receipt.json` signature and `i1_proposal_hash` binding.
3. Re-validates `ProposalV2`, `PlanV2`, `IndependentGraphV2`, and `AuditChallengeV2` and re-runs `evaluate_case_v2` offline against `build_private_world`.
4. Any byte modification to a contract JSON, lock record, probe receipt, or input image triggers an immediate replay verification failure.
