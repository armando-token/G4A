# Phase 03 — Managed Antigravity Runtime Specification (`03_MANAGED_RUNTIME.md`)

## Overview
Phase 03 defines the managed runtime adapter (`blackbox/runtime.py`) for executing multi-turn, role-isolated **Investigator** (`I1`, `I2`) and **Auditor** (`A1`, `A2`) interactions against **Gemini 4 Argon (`gemini-4-argon`)** inside **Antigravity (`antigravity-preview-09-2026`)**. It enforces strict credential and base64 image redaction, background interaction lifecycle polling, restart journal recovery, direct remote environment file collection with safe extraction, and three-ledger token accounting.

---

## 1. Authentication Modes & Read-Only Preflight

| Mode | Trigger Condition | Client Initialization & Behavior |
| :--- | :--- | :--- |
| **`ai_studio`** | `GEMINI_API_KEY` or `GOOGLE_API_KEY` set and `GOOGLE_GENAI_USE_VERTEXAI=false` | `genai.Client(api_key=...)` against Google AI Studio endpoints. |
| **`vertex_adc`** | `GOOGLE_GENAI_USE_VERTEXAI=true` with `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, and valid ADC | `genai.Client(vertexai=True, project=..., location=...)` using Application Default Credentials. |
| **`mock`** | Explicit `mode="mock"` or deterministic offline test harness | In-memory scripted/fault-injectable `MockGenAIClient` supporting every lifecycle state and failure mode without network calls. |

### Read-Only Preflight & Redaction Policy
- Verifies target agent (`antigravity-preview-09-2026`) and model (`gemini-4-argon`) readiness without mutating remote state.
- **Zero Secret & Payload Leakage**:
  - Raw API keys, bearer tokens, and ADC private keys are replaced with `"[REDACTED]"`.
  - Inline base64-encoded image buffers (`rgb.png`, `thermal.png`, `sld.png`) are replaced in persisted request logs and journal entries with `{"redacted_base64": True, "sha256": "<digest>", "byte_length": <n>}`.

---

## 2. Core `run_turn` Contract & Role Environment Isolation

### Signature
```python
run_turn(
    role: str,
    prompt: str,
    packet: Mapping[str, Any],
    images: Sequence[Path | Mapping[str, Any]],
    environment_id: str | None = None,
    max_total_tokens: int = 30000,
)
```

### Request Shape (`client.interactions.create`)
- **`agent`**: `"antigravity-preview-09-2026"`
- **`model`**: `"gemini-4-argon"`
- **`background`**: `True`
- **`store`**: `True`
- **`agent_config`**: `{"type": "antigravity", "model": "gemini-4-argon", "max_total_tokens": max_total_tokens}`
- **`tools`**: `[{"type": "code_execution"}]`
- **Fresh Turn (`environment_id is None`, e.g., `I1` or `A1`)**:
  - Attaches public JSON evidence (`telemetry.json`, `records.json`) and 24-bit RGB visual assets (`rgb.png`, `thermal.png`, `sld.png`) as remote inline sources.
  - Receives and persists the newly provisioned `environment_id` from the interaction response.
- **Follow-Up Turn (`environment_id is not None`, e.g., `I2` or `A2`)**:
  - Passes `environment_id=environment_id` and explicitly enforces `previous_interaction_id=None` so state continuity flows through the isolated managed environment workspace.
- **Strict Investigator / Auditor Isolation**:
  - The runtime maintains separate `environment_id` bindings per role (`investigator` vs. `auditor`). Attempting to reuse an Investigator `environment_id` for an Auditor turn (or vice versa) raises a `RuntimeError` prior to dispatching any RPC.

---

## 3. Lifecycle Polling, Timeout Reconciliation, Journal Recovery & Safe File Retrieval

### Interaction Lifecycle States
The polling loop inspects `client.interactions.get(interaction_id)` across all terminal and non-terminal states:
- **Non-terminal**: `queued`, `in_progress`
- **Terminal**: `completed`, `failed`, `cancelled`, `incomplete`, `unknown`

### Timeout Cancellation & Reconciliation
- If wall-clock polling exceeds `turn_timeout_s`:
  1. Invokes `client.interactions.cancel(interaction_id)`.
  2. Re-polls `client.interactions.get(interaction_id)` to reconcile final terminal status and capture final cumulative token usage before recording `timeout_cancelled`.

### Restart Journal Recovery
- Before creating a new interaction for `(case_id, role, turn_id)`, the runtime writes an append-only JSONL journal entry (`turn_journal.jsonl`) recording `interaction_id`, `environment_id`, `request_sha256`, and `status`.
- On process restart, if an existing journal entry is found in `queued` or `in_progress`, the runtime resumes polling that `interaction_id` rather than issuing a duplicate creation request.

### Direct File Retrieval & Safe Archive Extraction
- Retrieves remote workspace artifacts via `client.environments.files.list(environment_id=...)` and `client.environments.files.download(environment_id=..., file_id=...)`.
- **Security & Integrity Guards**:
  - Rejects path traversal (`..`), absolute paths (`/etc/...`), and symlinks in remote filenames or archive members.
  - Enforces decompression bomb limits (maximum extracted file count and total byte ceiling).
  - **Duplicate Basename Rejection**: Rejects any environment file list or archive containing colliding basenames (e.g., `sub1/verdict.json` and `sub2/verdict.json`) to prevent silent artifact overwrite.

---

## 4. Three-Ledger Token Accounting & Reserves

| Ledger / Cap | Limit (Tokens) | Enforcement Rule |
| :--- | :--- | :--- |
| **Per-Interaction Native Cap** | `30,000` (noncached) | Passed via `max_total_tokens=30000`; any turn exceeding `30,000` noncached tokens is flagged as a budget breach. |
| **Case Noncached Ledger** | `140,000` per case | Tracks cumulative noncached tokens across `I1 + I2 + A1 + A2` for a single case (`case_A` or `case_B`). |
| **Auditor Reserve** | `40,000` noncached | Reserved from the `140,000` case cap before `A1` begins (`I1 + I2 <= 100,000`). |
| **Auditor `A2` Reserve** | `20,000` noncached | Reserved inside the Auditor allocation so `A2` is guaranteed at least `20,000` tokens after `A1`. |
| **Case Cached-Inclusive Ceiling** | `280,000` per case | Total tokens including cached input tokens (`noncached + cached <= 280,000` per case). |
| **Pair Cached-Inclusive Ceiling** | `560,000` per pair | Total cached-inclusive tokens across the paired run (`case_A + case_B <= 560,000`). |
| **Unknown Usage Fallback Reserve** | `30,000` noncached | If a terminal or failed interaction omits usage metadata, the ledger charges a conservative `30,000` noncached token reserve. |

- **Deduplicated Snapshot Rule**: Repeated polling snapshots for the same `interaction_id` overwrite (rather than sum) that interaction's entry using the latest cumulative snapshot.

---

## 5. Mock Verification Matrix & Single Live Canary Specification

### Mock Verification Matrix (`tests/test_runtime.py`)
1. **Happy-Path 4-Turn Cycle (`I1 -> I2 -> A1 -> A2`)**: Verifies fresh vs. follow-up `environment_id` handling, `previous_interaction_id=None`, role isolation, and artifact download.
2. **Role Cross-Contamination Rejection**: Verifies passing Investigator's `environment_id` to Auditor raises `RuntimeError`.
3. **Timeout & Cancel Reconciliation**: Simulates stuck `in_progress` interaction, verifies `cancel()` call and post-cancel usage reconciliation.
4. **Restart Journal Recovery**: Simulates crash mid-turn and verifies idempotent resume from `turn_journal.jsonl`.
5. **Unsafe File & Duplicate Basename Rejection**: Verifies rejection of `../` traversal, symlinks, and duplicate basenames during environment file download.
6. **Three-Ledger Budget Enforcement**: Verifies `30,000` per-turn cap, `40,000` Auditor reserve, `20,000` `A2` reserve, `140,000` case noncached cap, `280,000`/`560,000` cached-inclusive ceilings, and `30,000` unknown-usage fallback charge.
7. **Redaction Verification**: Confirms zero leakage of API keys or raw base64 image payloads in logs or journal files.

### Single Live Canary Specification
- Executes a single bounded read/compute canary turn (`max_total_tokens <= 30,000`) against `antigravity-preview-09-2026` / `gemini-4-argon` when live credentials are configured, verifying live interaction creation, `code_execution` tool invocation, terminal `completed` polling, environment file download, and ledger recording.
