# BlackboxDC — Master Specification (Phase 00–07)

## Executive Summary
**BlackboxDC** is a deterministic, reproducible benchmark and harness evaluating spatial reasoning, 3D visual perception, dynamic physical causal inference, and structured tool-augmented problem solving for **Gemini 4 Argon (`gemini-4-argon`)** inside **Antigravity (`antigravity-preview-09-2026`)**.

---

## Architecture & Phase Roadmap

| Phase | Module / Directory | Description |
| :--- | :--- | :--- |
| **Phase 00** | `blackbox/preflight.py`, `.env.example`, `requirements.txt` | Environment configuration, secret-safe credential discrimination (AI Studio vs. Vertex AI ADC), Python/CLI dependency verification. |
| **Phase 01** | `blackbox/static3d/` | Procedural 3D voxel & polyhedral scene generation, orthographic/perspective multi-view rendering, mental rotation, and occlusion tasks. |
| **Phase 02** | `blackbox/dynamic/` | 2D/3D deterministic rigid-body and particle dynamics simulation, trajectory prediction, counterfactual collision tasks, and video frame encoding. |
| **Phase 03** | `blackbox/puzzles/` | Algorithmic & spatial constraint puzzles (nonograms, sliding block, circuit routing, laser-mirror optics) with exact verification. |
| **Phase 04** | `blackbox/client/` | Unified `google-genai` SDK wrapper supporting `gemini-4-argon`, structured outputs, retry/backoff, token accounting, and local dry-run mocking. |
| **Phase 05** | `blackbox/eval/` | Automated grading engine, calibration metrics (ECE, Brier score), bootstrap confidence intervals, and JSONL artifact logging. |
| **Phase 06** | `blackbox/dashboard/` | Interactive Streamlit analytics suite for inspecting failure modes, multi-view stimuli, model reasoning traces, and scorecards. |
| **Phase 07** | `scripts/run_suite.py`, `tests/` | End-to-end smoke suite, deterministic seed verification, and full benchmark report generation. |

---

## Phase 00 Specification Details

### 1. Environment & Dependency Requirements
- **Python**: `>= 3.11`
- **Core Python Libraries**: `google-genai`, `numpy`, `streamlit`, `Pillow`, `pandas`, `pydantic`, `httpx`, `pytest`
- **External CLI Tools**: `node`, `npm`, `ffmpeg`, `chromium` (or `google-chrome`)

### 2. Credential Discrimination & Secret Safety
- Supports two primary authentication pathways:
  1. **Google AI Studio API Key**: Detected via `GEMINI_API_KEY` or `GOOGLE_API_KEY` when `GOOGLE_GENAI_USE_VERTEXAI` is `false` (or unset).
  2. **Vertex AI Application Default Credentials (ADC)**: Enabled when `GOOGLE_GENAI_USE_VERTEXAI=true` alongside `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, and either `GOOGLE_APPLICATION_CREDENTIALS` or standard gcloud ADC (`~/.config/gcloud/application_default_credentials.json`).
- **Zero Secret Leakage Policy**: `blackbox/preflight.py` must never output, log, or serialize raw API keys, tokens, or credential payloads. Any inspected environment variable values must be strictly redacted (`[REDACTED]`) or reported solely as boolean presence/length-free status indicators.
