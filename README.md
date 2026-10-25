# BlackboxDC (`G4A`)

**BlackboxDC** is a spatial, physical, and algorithmic evaluation suite designed for **Gemini 4 Argon (`gemini-4-argon`)** running on **Antigravity (`antigravity-preview-09-2026`)**.

## Project Tree

```text
G4A/
├── 00_MASTER_SPEC.md          # Master specification across Phases 00–07
├── README.md                  # Project overview and setup instructions
├── .env.example               # Template environment variables
├── .gitignore                 # Git ignore rules
├── requirements.txt           # Python package dependencies
├── artifacts/                 # Generated stimuli, logs, and evaluation reports
│   └── .gitkeep
├── blackbox/
│   ├── __init__.py            # Package metadata and version
│   ├── preflight.py           # Phase 00 environment & credential preflight checker
│   ├── static3d/              # Phase 01 static 3D perception & mental rotation
│   ├── dynamic/               # Phase 02 physical dynamics & counterfactuals
│   ├── puzzles/               # Phase 03 spatial & symbolic constraint puzzles
│   ├── client/                # Phase 04 Gemini 4 Argon SDK client & dry-run harness
│   ├── eval/                  # Phase 05 deterministic grading & metrics
│   └── dashboard/             # Phase 06 Streamlit visualization dashboard
└── tests/
    └── test_preflight.py      # Unit tests for preflight & secret redaction
```

## Phase 00 Setup & Verification

### 1. Create Virtual Environment & Install Dependencies
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure Environment Variables
Copy `.env.example` to `.env` and configure either your AI Studio API key or Vertex AI settings:
```bash
cp .env.example .env
```

### 3. Run Preflight Verification
Run the preflight CLI module to verify Python version, required libraries, CLI binaries, and credential mode (with strict secret redaction):
```bash
python -m blackbox.preflight
# Or output raw JSON:
python -m blackbox.preflight --json
```

### 4. Run Phase 00 Unit Tests
```bash
pytest tests/test_preflight.py
```
