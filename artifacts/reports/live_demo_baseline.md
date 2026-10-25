# Live judge demo — baseline and evidence

- Baseline: HEAD 339f517. After this change the full suite passes (184 tests, including 24 in `tests/test_judge_live.py`).
- Recorded run `live1` is unchanged. It is still served at `/` and at `/live?run=live1` (result_public.json sha256 prefix aef6e0dffa6015d4).

## What was added
- **Phase A, async API** (`twin_repair/web/live_api.py`):
  - Flow: `POST /api/live/challenges` → `POST /api/live/runs` (Idempotency-Key) → `GET /api/live/runs/{id}` polling, plus `/actions`.
  - Shared busy lock with the legacy paid runs, quotas and restart recovery.
  - The page is served at `/live`.
- **Phase B, fresh drawings** (`blackbox/render_sld.py`, `twin_repair/judge_live.py`): every challenge renders a new as-built SLD from the selected mapping. The drawing number and file name never reveal the answer.
- **Phase C, independent verification**:
  - The Critic uses a neutral prompt (`twin_repair/prompts/critic_neutral.txt`).
  - The score is recomputed from the mapping that was hashed at challenge time.
  - The report has no canonical fallbacks. A missing value is an error, never a fabricated PASS.
- **Phase D, 3D viewer** (`twin_repair/web/live/viewer3d.js`):
  - Three.js PBR racks, busbars and cable tubes.
  - Animated cable repair, N4 heat glow and per-feeder loads.
  - Renders on demand; DPR is capped on phones.
  - Falls back to the recorded PNG render when WebGL is unavailable.
- **Phase E, responsive UI** (`live.html`, `live.css`, `live.js`):
  - Decision / Agents live / Evidence tabs.
  - Before/After toggle and counterfactual action selector.
  - 44 px tap targets; no horizontal overflow at 360, 390 or 1600 px.
- **Phase F, tests** (`tests/test_judge_live.py`):
  - leak barrier and blind challenges;
  - mock outcomes and negative paths (abstain, invalid patch, forged hash, timeout);
  - input tampering, API idempotency, quotas, busy, crash and recovery;
  - heartbeat coalescing.

## Production canary (full mode, drawing B)
- Run `jr-e1ea6cfda88c`:
  - Terminal state PASS, action SAFE_RECEIVER_FOUND, argon_verified=true, distinct environments.
  - Wall time 173 s: Builder 57 s / 7 steps, Critic 105 s / 7 steps, model gemini-4-argon.
- Headline: "The old recommendation would overload F1. The repaired twin selects N2."
- Figures:
  - Old choice N3: F1 48/44 A.
  - Repaired choice N2: F1 40/44 A, F2 28/50 A.
  - N4 at 180 s: 52.53 °C.
- Page HTML gzip ≈3 KB; JavaScript gzip ≈210 KB (Three.js), loaded as ES modules.

Screenshots are in `live_demo_screens/`.
