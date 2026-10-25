# Phase 02 — Deterministic Visual Assets Specification (`02_VISUAL_ASSETS.md`)

## Overview
Phase 02 generates deterministic, self-contained visual stimuli for **BlackboxDC** without external network calls or non-deterministic rendering artifacts:
1. **Single-Line Diagram (`blackbox/render_sld.py`)**: `1280×800` IEC 61082-1 / IEC 60617-inspired technical schematic rendered to both vector SVG (`sld.svg`) and 24-bit RGB PNG (`sld.png`, `< 512 KiB`).
2. **3D Rear-Aisle Captures (`blackbox/render_3d.py`)**: `1280×800` 24-bit RGB (`rear_aisle_rgb.png`) and false-color infrared thermography (`rear_aisle_thermal.png`) captures binding anonymous telemetry sensors (`SNS_K1..SNS_K4`) to physical rack bays (`N1..N4`).

---

## 1. Single-Line Diagram (SLD) Geometry Specification (`1280×800`)

| Element | Coordinate / Geometry Target | Specification |
| :--- | :--- | :--- |
| **Canvas** | `1280 × 800`, 24-bit RGB (`mode="RGB"`) | Deterministic vector SVG + raster PNG (`< 512 KiB`), no alpha channel. |
| **Feeder Bus `F1`** | `y = 210`, `x = 100..1180` | Label: `F1 BUS — 44A LIMIT`. |
| **Feeder Bus `F2`** | `y = 290`, `x = 100..1180` | Label: `F2 BUS — 50A LIMIT`. |
| **Rack Columns** | `x = 200, 460, 720, 980` | Columns for `N1` (`200`), `N2` (`460`), `N3` (`720`), `N4` (`980`). |
| **Junction Dots** | Radius `r = 7` at `(x, 210)` or `(x, 290)` | Filled junction dot drawn **only** on the connected feeder bus. |
| **Crossing Arches** | Semicircular bridge `r = 12`, `y = 278..302` | Drawn over `F2` (`y = 290`) whenever a vertical drop originates at `F1` (`y = 210`); **no dot** at `(x, 290)`. |
| **Branch Switches** | Centered at `y = 390` (`y = 370..410`) | IEC-style single-pole disconnect/breaker symbol on each rack branch. |
| **Rack Load Boxes** | `y = 520..632`, `x ± 88` | `N1 (36A / 40A MAX)`, `N2 (4A / 16A MAX)`, `N3 (4A / 16A MAX)`, `N4 (24A / 32A MAX)`. |
| **Sensor Boxes** | `y = 652..702`, `x ± 68` | `SNS_K1` (`x=200`), `SNS_K3` (`x=460`), `SNS_K4` (`x=720`), `SNS_K2` (`x=980`). |
| **Title / Footer** | Header `y = 24..104`, Footer `y = 736..780` | Includes `ACTIVE REVISION` and `TRAINING / SIMULATION — NOT FOR CONSTRUCTION`. |

### Textual Invariance Rule
Case A (`{F1-N1, F1-N2, F2-N3, F2-N4}`) and Case B (`{F1-N1, F1-N3, F2-N2, F2-N4}`) SLD files contain **identical text strings, labels, fonts, and legends**. Neither `"CASE A"`/`"CASE B"` nor textual feeder mappings (`"F1->N2"`, etc.) appear in the diagram; only the vertical branch origin, filled junction dots, and crossing arches on columns `x = 460` (`N2`) and `x = 720` (`N3`) differ.

---

## 2. 3D Rear-Aisle Camera & Projection Parameters (`1280×800`)

| Parameter | Value | Description |
| :--- | :--- | :--- |
| **Resolution** | `1280 × 800` (`mode="RGB"`) | Shared rear-aisle viewport across RGB and thermal captures. |
| **Rack Bays (`N1..N4`)** | Left-to-right bay centers | Physical rear cabinet doors with high-contrast rack placards (`N1`, `N2`, `N3`, `N4`) and sensor tags (`SNS_K1`, `SNS_K3`, `SNS_K4`, `SNS_K2`). |
| **Connector Bounding Boxes** | `N1: [88, 220, 172, 316]`<br>`N2: [292, 220, 376, 316]`<br>`N3: [496, 220, 580, 316]`<br>`N4: [700, 220, 784, 316]` | 2D projected `[x_min, y_min, x_max, y_max]` coordinates of the four rear power connectors. |
| **Thermal Hotspot** | `N4` (`SNS_K2`) at `88.95 °C` | Concentrated high-intensity thermal bloom inside `N4` connector bbox `[700, 220, 784, 316]`; `N1` warm (`36.0 °C`), `N2`/`N3` cool (`25.0 °C`). |

---

## 3. Cross-Case SHA-256 Invariants

| Asset Pair | Invariant | Rationale |
| :--- | :--- | :--- |
| `sld_A.svg` vs `sld_B.svg` | `sha256(A) != sha256(B)` | Branch geometry swaps between `N2` (`x=460`) and `N3` (`x=720`). |
| `sld_A.png` vs `sld_B.png` | `sha256(A) != sha256(B)` | Rasterized junction dots and crossing arches swap between `N2` and `N3`. |
| `rear_aisle_rgb` (`A` vs `B`) | `sha256(A) == sha256(B)` | Rear-aisle physical bay appearance and sensor tags are identical across cases. |
| `rear_aisle_thermal` (`A` vs `B`) | `sha256(A) == sha256(B)` | Initial thermal state at $t = 0\text{ s}$ is byte-for-byte identical across cases. |
