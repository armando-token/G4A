"""Predecessor installation-v1 physical fault simulation lab (Case A & Case B).

Provides:
1. Phase 7C (`07_HISTORICAL_RECONSTRUCTION_AND_FULL_PARITY.md`) predecessor 2D
   `installation-v1` lab reusing `blackbox.domain` physics while keeping
   `INSTALLATION_V1_SCHEMA = "installation-v1"` separate from `visual-v2`:
   - `INSTALLATION_V1_LAYOUTS`
   - `permute_installation_labels`
   - `build_installation_v1_world`
   - `render_installation_v1_layout_png` (800x500 predecessor 2D schematic)
   - `render_installation_v1_thermal_png` (800x500 predecessor 2D thermal frame with
     hotspot on `N4` at `[580, 210, 660, 290]`)
   - `build_installation_v1_public_bundle`
2. Backward-compatible `InstallationLab` and `simulate_installation_case`.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import random
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw

from blackbox.domain import (
    AMBIENT_TEMP_C,
    DEFAULT_HORIZON_S,
    DEFAULT_MAX_TEMP_C,
    DEFAULT_MIGRATION_AMPS,
    DEFAULT_STEP_S,
    PDU,
    RackParams,
    World,
    step_temperature,
)


INSTALLATION_V1_SCHEMA: str = "installation-v1"
INSTALLATION_V1_CANVAS_SIZE: Tuple[int, int] = (800, 500)
INSTALLATION_V1_N4_HOTSPOT_BBOX: Tuple[int, int, int, int] = (580, 210, 660, 290)

INSTALLATION_V1_LAYOUTS: Dict[str, Dict[str, Any]] = {
    "A": {
        "layout_code": "A",
        "pdu_to_racks": {"F1": ("N1", "N2"), "F2": ("N3", "N4")},
        "power_edges": {"N1": "F1", "N2": "F1", "N3": "F2", "N4": "F2"},
        "sensor_edges": {
            "SNS_K1": "N1",
            "SNS_K2": "N4",
            "SNS_K3": "N2",
            "SNS_K4": "N3",
        },
        "safe_action_id": "MIGRATE_N4_TO_N3_8A",
        "unsafe_migration_action_id": "MIGRATE_N4_TO_N2_8A",
    },
    "B": {
        "layout_code": "B",
        "pdu_to_racks": {"F1": ("N1", "N3"), "F2": ("N2", "N4")},
        "power_edges": {"N1": "F1", "N2": "F2", "N3": "F1", "N4": "F2"},
        "sensor_edges": {
            "SNS_K1": "N1",
            "SNS_K2": "N4",
            "SNS_K3": "N2",
            "SNS_K4": "N3",
        },
        "safe_action_id": "MIGRATE_N4_TO_N2_8A",
        "unsafe_migration_action_id": "MIGRATE_N4_TO_N3_8A",
    },
}


def _normalize_v1_layout(layout: str) -> str:
    raw = str(layout).strip().upper().replace(" ", "_")
    if raw in ("A", "CASE_A", "CASE-A", "LAYOUT_A"):
        return "A"
    if raw in ("B", "CASE_B", "CASE-B", "LAYOUT_B"):
        return "B"
    raise ValueError(f"Unsupported installation-v1 layout={layout!r}; expected 'A' or 'B'")


def permute_installation_labels(seed: int = 20261024) -> Dict[str, str]:
    """Return a deterministic seeded mapping from canonical layouts ('A', 'B') to opaque IDs."""
    rng = random.Random(int(seed))
    opaque_ids = ["layout_alpha_v1", "layout_beta_v1"]
    rng.shuffle(opaque_ids)
    return {
        "A": opaque_ids[0],
        "B": opaque_ids[1],
        opaque_ids[0]: "A",
        opaque_ids[1]: "B",
    }


def _build_v1_ramp_telemetry() -> Dict[str, Any]:
    t_s: List[int] = list(range(-120, 1, DEFAULT_STEP_S))
    n4_currents: List[float] = [12.0] * 4 + [18.0] * 4 + [24.0] * 4 + [24.0]
    n4_temps: List[float] = [34.0]
    t_curr = 34.0
    for k in range(12):
        t_curr = step_temperature(
            t_c=t_curr,
            i_a=n4_currents[k],
            ambient_c=AMBIENT_TEMP_C,
            r0_ohm=0.040,
            alpha=0.004,
            h_w_k=0.4,
            heat_capacity_j_k=8.0,
            dt_s=float(DEFAULT_STEP_S),
        )
        n4_temps.append(float(t_curr))

    return {
        "t_s": t_s,
        "SNS_K1": {
            "sensor_id": "SNS_K1",
            "t_s": list(t_s),
            "temps_C": [36.0] * len(t_s),
            "currents_A": [36.0] * len(t_s),
        },
        "SNS_K2": {
            "sensor_id": "SNS_K2",
            "t_s": list(t_s),
            "temps_C": list(n4_temps),
            "currents_A": list(n4_currents),
        },
        "SNS_K3": {
            "sensor_id": "SNS_K3",
            "t_s": list(t_s),
            "temps_C": [25.0] * len(t_s),
            "currents_A": [4.0] * len(t_s),
        },
        "SNS_K4": {
            "sensor_id": "SNS_K4",
            "t_s": list(t_s),
            "temps_C": [25.0] * len(t_s),
            "currents_A": [4.0] * len(t_s),
        },
    }


def build_installation_v1_world(
    layout: str = "A",
    fault_type: str = "electrical",
) -> World:
    """Build the local reference `domain.World` for `installation-v1` Layout A or Layout B."""
    norm_layout = _normalize_v1_layout(layout)
    power_edges = INSTALLATION_V1_LAYOUTS[norm_layout]["power_edges"]

    ft = str(fault_type).strip().lower()
    if ft == "electrical":
        n4_r0, n4_h, n4_c = 0.040, 0.4, 8.0
    elif ft == "cooling":
        n4_r0, n4_h, n4_c = 0.020, 0.2, 4.0
    else:
        raise ValueError(f"Unsupported fault_type={fault_type!r}; expected 'electrical' or 'cooling'")

    ramp = _build_v1_ramp_telemetry()
    n4_t0 = float(ramp["SNS_K2"]["temps_C"][-1])

    racks = (
        RackParams(
            rack_id="N1",
            pdu_id=power_edges["N1"],
            current_A=36.0,
            initial_temp_C=36.0,
            R0_ohm=0.005,
            alpha_per_C=0.004,
            h_W_per_K=0.5,
            C_J_per_K=8.0,
            max_current_A=40.0,
            max_temp_C=DEFAULT_MAX_TEMP_C,
        ),
        RackParams(
            rack_id="N2",
            pdu_id=power_edges["N2"],
            current_A=4.0,
            initial_temp_C=25.0,
            R0_ohm=0.010,
            alpha_per_C=0.004,
            h_W_per_K=0.5,
            C_J_per_K=8.0,
            max_current_A=16.0,
            max_temp_C=DEFAULT_MAX_TEMP_C,
        ),
        RackParams(
            rack_id="N3",
            pdu_id=power_edges["N3"],
            current_A=4.0,
            initial_temp_C=25.0,
            R0_ohm=0.010,
            alpha_per_C=0.004,
            h_W_per_K=0.5,
            C_J_per_K=8.0,
            max_current_A=16.0,
            max_temp_C=DEFAULT_MAX_TEMP_C,
        ),
        RackParams(
            rack_id="N4",
            pdu_id=power_edges["N4"],
            current_A=24.0,
            initial_temp_C=n4_t0,
            R0_ohm=n4_r0,
            alpha_per_C=0.004,
            h_W_per_K=n4_h,
            C_J_per_K=n4_c,
            max_current_A=32.0,
            max_temp_C=DEFAULT_MAX_TEMP_C,
        ),
    )
    pdus = (
        PDU(pdu_id="F1", max_current_A=44.0),
        PDU(pdu_id="F2", max_current_A=50.0),
    )
    return World(
        racks=racks,
        pdus=pdus,
        ambient_C=AMBIENT_TEMP_C,
        horizon_s=DEFAULT_HORIZON_S,
        step_s=DEFAULT_STEP_S,
        allowed_targets=("N2", "N3"),
    )


def render_installation_v1_layout_png(
    layout: str,
    output_path: Path,
) -> Dict[str, Any]:
    """Render an 800x500 predecessor 2D schematic PNG for `installation-v1` Layout A or B."""
    norm_layout = _normalize_v1_layout(layout)
    power_edges = INSTALLATION_V1_LAYOUTS[norm_layout]["power_edges"]
    sensor_edges = INSTALLATION_V1_LAYOUTS[norm_layout]["sensor_edges"]

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    width, height = INSTALLATION_V1_CANVAS_SIZE
    img = Image.new("RGB", (width, height), color=(248, 250, 252))
    draw = ImageDraw.Draw(img)

    # Header bar
    draw.rectangle([0, 0, width, 44], fill=(30, 41, 59))
    draw.text((18, 14), "INSTALLATION-V1 PREDECESSOR 2D LAYOUT", fill=(241, 245, 249))

    # F1 and F2 busbars
    f1_y, f2_y = 95, 145
    draw.line([(70, f1_y), (730, f1_y)], fill=(37, 99, 235), width=4)
    draw.text((22, f1_y - 8), "F1(44A)", fill=(37, 99, 235))
    draw.line([(70, f2_y), (730, f2_y)], fill=(217, 119, 6), width=4)
    draw.text((22, f2_y - 8), "F2(50A)", fill=(217, 119, 6))

    rack_xs = {"N1": 140, "N2": 310, "N3": 480, "N4": 620}
    rack_box_top, rack_box_bottom = 200, 320

    for rk, cx in rack_xs.items():
        pdu = power_edges[rk]
        tap_y = f1_y if pdu == "F1" else f2_y
        color = (37, 99, 235) if pdu == "F1" else (217, 119, 6)
        draw.ellipse([cx - 5, tap_y - 5, cx + 5, tap_y + 5], fill=color)
        draw.line([(cx, tap_y), (cx, rack_box_top)], fill=color, width=3)
        draw.rectangle(
            [cx - 45, rack_box_top, cx + 45, rack_box_bottom],
            fill=(226, 232, 240),
            outline=(71, 85, 105),
            width=2,
        )
        draw.text((cx - 12, rack_box_top + 45), rk, fill=(15, 23, 42))

    # Sensor boxes SNS_K1..SNS_K4 at bottom
    sensor_xs = {"SNS_K1": 140, "SNS_K2": 310, "SNS_K3": 480, "SNS_K4": 620}
    sensor_y = 420
    for sid, sx in sensor_xs.items():
        target_rk = sensor_edges[sid]
        tx = rack_xs[target_rk]
        draw.line([(sx, sensor_y - 20), (tx, rack_box_bottom)], fill=(16, 185, 129), width=2)
        draw.rectangle(
            [sx - 42, sensor_y - 20, sx + 42, sensor_y + 20],
            fill=(209, 250, 229),
            outline=(5, 150, 105),
            width=2,
        )
        draw.text((sx - 28, sensor_y - 6), sid, fill=(6, 78, 59))

    img.save(out, format="PNG", optimize=True)
    raw = out.read_bytes()
    return {
        "path": str(out),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "width": width,
        "height": height,
        "layout": norm_layout,
        "schema_version": INSTALLATION_V1_SCHEMA,
    }


def render_installation_v1_thermal_png(output_path: Path) -> Dict[str, Any]:
    """Render an 800x500 predecessor 2D thermal frame PNG with hotspot on `N4` at `[580, 210, 660, 290]`."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    width, height = INSTALLATION_V1_CANVAS_SIZE
    img = Image.new("RGB", (width, height), color=(18, 12, 48))
    draw = ImageDraw.Draw(img)

    draw.rectangle([0, 0, width, 44], fill=(28, 20, 68))
    draw.text((18, 14), "INSTALLATION-V1 2D THERMAL FRAME (800x500)", fill=(230, 230, 250))

    rack_xs = {"N1": 140, "N2": 310, "N3": 480, "N4": 620}
    for rk, cx in rack_xs.items():
        base_col = (52, 26, 98) if rk == "N1" else (30, 18, 74)
        draw.rectangle([cx - 45, 180, cx + 45, 360], fill=base_col, outline=(85, 60, 140), width=2)
        draw.text((cx - 10, 370), rk, fill=(210, 210, 235))

    # Hotspot on N4 at [580, 210, 660, 290]
    hx0, hy0, hx1, hy1 = INSTALLATION_V1_N4_HOTSPOT_BBOX
    draw.rectangle([hx0, hy0, hx1, hy1], fill=(235, 120, 30), outline=(255, 220, 90), width=2)
    draw.ellipse([hx0 + 18, hy0 + 18, hx1 - 18, hy1 - 18], fill=(255, 245, 190))
    img.putpixel(((hx0 + hx1) // 2, (hy0 + hy1) // 2), (255, 255, 255))

    img.save(out, format="PNG", optimize=True)
    raw = out.read_bytes()
    return {
        "path": str(out),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "width": width,
        "height": height,
        "hotspot_bbox": list(INSTALLATION_V1_N4_HOTSPOT_BBOX),
        "schema_version": INSTALLATION_V1_SCHEMA,
    }


def build_installation_v1_public_bundle(
    layout_or_output_dir: Any = "A",
    output_dir: Optional[Path] = None,
    seed: int = 20261024,
    *,
    layout: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the predecessor `installation-v1` public input bundle and optional 2D PNGs."""
    if layout is not None:
        resolved_layout = layout
        if output_dir is None and layout_or_output_dir != "A":
            output_dir = Path(layout_or_output_dir)
    elif isinstance(layout_or_output_dir, Path):
        output_dir = layout_or_output_dir
        resolved_layout = "A"
    else:
        resolved_layout = str(layout_or_output_dir)
    norm_layout = _normalize_v1_layout(resolved_layout)
    perm = permute_installation_labels(seed=seed)
    opaque_id = perm[norm_layout]
    telemetry = _build_v1_ramp_telemetry()

    bundle: Dict[str, Any] = {
        "schema_version": INSTALLATION_V1_SCHEMA,
        "layout_id": opaque_id,
        "canvas_size_px": list(INSTALLATION_V1_CANVAS_SIZE),
        "ambient_C": AMBIENT_TEMP_C,
        "alpha_per_C": 0.004,
        "max_temp_C": DEFAULT_MAX_TEMP_C,
        "horizon_s": DEFAULT_HORIZON_S,
        "step_s": DEFAULT_STEP_S,
        "migration_quantum_A": DEFAULT_MIGRATION_AMPS,
        "pdu_limits_A": {"F1": 44.0, "F2": 50.0},
        "rack_limits_A": {"N1": 40.0, "N2": 16.0, "N3": 16.0, "N4": 32.0},
        "diagnostic_menu": ["voltage_drop", "temperature_snapshot", "controller_setting"],
        "candidate_actions": [
            "NO_ACTION",
            "MIGRATE_N4_TO_N2_8A",
            "MIGRATE_N4_TO_N3_8A",
        ],
        "telemetry": telemetry,
    }

    if output_dir is not None:
        odir = Path(output_dir)
        odir.mkdir(parents=True, exist_ok=True)
        layout_meta = render_installation_v1_layout_png(norm_layout, odir / "layout_2d.png")
        thermal_meta = render_installation_v1_thermal_png(odir / "thermal_2d.png")
        bundle["visual_files"] = {
            "layout_2d.png": {
                "sha256": layout_meta["sha256"],
                "width": layout_meta["width"],
                "height": layout_meta["height"],
            },
            "thermal_2d.png": {
                "sha256": thermal_meta["sha256"],
                "width": thermal_meta["width"],
                "height": thermal_meta["height"],
            },
        }
        (odir / "public_bundle_v1.json").write_text(
            json.dumps(bundle, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    return bundle


# ---------------------------------------------------------------------------
# Backward-Compatible Phase 00 InstallationLab
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstallationLab:
    """Simulates rack installation commissioning faults for Case A and Case B."""

    nominal_pressure_pa: float = 125.0
    nominal_delta_t_c: float = 11.5

    def simulate_case(self, case_id: str) -> dict[str, Any]:
        normalized = case_id.strip().upper().replace(" ", "_")
        if normalized in {"A", "CASE_A", "CASE-A"}:
            # Case A: Reversed tray baffle causing localized bypass & elevated exhaust delta-T
            return {
                "case_id": "Case A",
                "fault_mode": "reversed_baffle_bypass",
                "plenum_pressure_pa": 118.4,
                "exhaust_delta_t_c": 19.8,
                "bypass_fraction": 0.34,
                "numerical_residual": 1.2e-4,
                "local_numerical_check_passed": True,
            }
        if normalized in {"B", "CASE_B", "CASE-B"}:
            # Case B: Partially obstructed floor damper causing pressure drop
            return {
                "case_id": "Case B",
                "fault_mode": "obstructed_floor_damper",
                "plenum_pressure_pa": 68.2,
                "exhaust_delta_t_c": 17.1,
                "bypass_fraction": 0.06,
                "numerical_residual": 0.9e-4,
                "local_numerical_check_passed": True,
            }
        raise ValueError(f"Unknown installation-v1 case_id: {case_id!r}")


def simulate_installation_case(case_id: str) -> dict[str, Any]:
    """Convenience function to run the deterministic installation-v1 lab for a single case."""
    return InstallationLab().simulate_case(case_id)
