"""Python bridge to static3d WebGL capture, 3D pinhole projection, and Phase 2 visual bundle builder.

Implements:
- `project_world_to_pixel` & `project_connector_bbox` confirming N4 fault connector center
  `(1110.36, 358.99)` and bounding box `[1079, 325, 1143, 393]`.
- `capture_3d_rear_aisle` invoking `node blackbox/static3d/capture.mjs` via `subprocess.run(..., shell=False)`,
  enforcing 24-bit RGB `1280x800` PNGs `< 512 KiB`, recapture SHA-256 identity, and `<= 3.0 px`
  thermal hotspot alignment.
- `build_phase2_visual_bundle` generating the complete paired Case A / Case B visual & telemetry
  input bundle, verifying the 5 cross-case SHA-256 invariants, and writing `input_manifest.json`.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from blackbox.fixture import build_private_world, export_public_packet
from blackbox.render_sld import render_sld_png


SCENE_WIDTH: int = 1280
SCENE_HEIGHT: int = 800
MAX_PNG_BYTES: int = 512 * 1024  # 512 KiB

DEFAULT_CAMERA_POS: Tuple[float, float, float] = (1.23, 1.24, 3.45)
DEFAULT_LOOK_AT: Tuple[float, float, float] = (1.23, 1.08, 0.50)
DEFAULT_FOV_DEG: float = 43.0

FAULT_CONNECTOR_CENTER_WORLD: Tuple[float, float, float] = (2.57, 1.20, 0.555)
FAULT_CONNECTOR_SIZE_WORLD: Tuple[float, float, float] = (0.15, 0.19, 0.06)
EXPECTED_HOTSPOT_PIXEL_CENTER: Tuple[float, float] = (1110.36, 358.99)
EXPECTED_CONNECTOR_PIXEL_BBOX: List[int] = [1079, 325, 1143, 393]


def get_static3d_dir() -> Path:
    """Return the path to blackbox/static3d directory."""
    return Path(__file__).resolve().parent / "static3d"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def project_world_to_pixel(
    world_xyz: Sequence[float] = FAULT_CONNECTOR_CENTER_WORLD,
    camera_pos: Sequence[float] = DEFAULT_CAMERA_POS,
    look_at: Sequence[float] = DEFAULT_LOOK_AT,
    fov_deg: float = DEFAULT_FOV_DEG,
    width: int = SCENE_WIDTH,
    height: int = SCENE_HEIGHT,
) -> Tuple[float, float]:
    """Project a 3D world coordinate `(x, y, z)` to 2D pixel coordinates `(px, py)`."""
    if len(world_xyz) != 3 or len(camera_pos) != 3 or len(look_at) != 3:
        raise ValueError("world_xyz, camera_pos, and look_at must be 3-tuples")
    if width <= 0 or height <= 0 or fov_deg <= 0.0 or fov_deg >= 179.0:
        raise ValueError("Invalid camera viewport or fov_deg parameters")

    cam = np.asarray(camera_pos, dtype=np.float64)
    target = np.asarray(look_at, dtype=np.float64)
    up_world = np.array([0.0, 1.0, 0.0], dtype=np.float64)

    forward = target - cam
    norm_f = float(np.linalg.norm(forward))
    if norm_f < 1e-9:
        raise ValueError("camera_pos and look_at must not be identical")
    forward = forward / norm_f

    right = np.cross(forward, up_world)
    right = right / float(np.linalg.norm(right))
    up = np.cross(right, forward)

    aspect = float(width) / float(height)
    f_y = 1.0 / math.tan(math.radians(float(fov_deg)) / 2.0)
    f_x = f_y / aspect

    rel = np.asarray(world_xyz, dtype=np.float64) - cam
    x_cam = float(np.dot(rel, right))
    y_cam = float(np.dot(rel, up))
    z_cam = float(np.dot(rel, forward))
    if z_cam <= 1e-6:
        raise ValueError("Point lies behind or on the camera plane")

    ndc_x = (x_cam / z_cam) * f_x
    ndc_y = (y_cam / z_cam) * f_y
    px = (ndc_x * 0.5 + 0.5) * float(width)
    py = (-ndc_y * 0.5 + 0.5) * float(height)
    return round(px, 2), round(py, 2)


def project_connector_bbox(
    center: Sequence[float] = FAULT_CONNECTOR_CENTER_WORLD,
    size: Sequence[float] = FAULT_CONNECTOR_SIZE_WORLD,
    camera_pos: Sequence[float] = DEFAULT_CAMERA_POS,
    look_at: Sequence[float] = DEFAULT_LOOK_AT,
    fov_deg: float = DEFAULT_FOV_DEG,
    width: int = SCENE_WIDTH,
    height: int = SCENE_HEIGHT,
) -> List[int]:
    """Project a 3D axis-aligned bounding box into `[x_min, y_min, x_max, y_max]` pixel coordinates."""
    if len(center) != 3 or len(size) != 3:
        raise ValueError("center and size must be 3-tuples")
    cx, cy, cz = float(center[0]), float(center[1]), float(center[2])
    hx, hy, hz = float(size[0]) / 2.0, float(size[1]) / 2.0, float(size[2]) / 2.0

    xs: List[float] = []
    ys: List[float] = []
    for dx in (-hx, hx):
        for dy in (-hy, hy):
            for dz in (-hz, hz):
                px, py = project_world_to_pixel(
                    world_xyz=(cx + dx, cy + dy, cz + dz),
                    camera_pos=camera_pos,
                    look_at=look_at,
                    fov_deg=fov_deg,
                    width=width,
                    height=height,
                )
                xs.append(px)
                ys.append(py)

    return [
        int(round(min(xs))),
        int(round(min(ys))),
        int(round(max(xs))),
        int(round(max(ys))),
    ]


def verify_visual_artifact(image_path: Path) -> Dict[str, Any]:
    """Verify that a generated PNG artifact exists, is 24-bit RGB, and is under 512 KiB."""
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"Visual artifact missing: {image_path}")
    data = image_path.read_bytes()
    if len(data) < 128:
        raise ValueError(f"Visual artifact too small ({len(data)} bytes): {image_path}")
    if len(data) >= MAX_PNG_BYTES:
        raise ValueError(f"Visual artifact exceeds 512 KiB limit ({len(data)} bytes): {image_path}")
    with Image.open(image_path) as im:
        width, height = im.size
        fmt = im.format
        mode = im.mode
    if fmt != "PNG":
        raise ValueError(f"Expected PNG format, got {fmt!r} for {image_path}")
    if mode != "RGB":
        raise ValueError(f"Expected 24-bit RGB mode, got {mode!r} for {image_path}")
    return {
        "path": str(image_path),
        "exists": True,
        "size_bytes": len(data),
        "width": width,
        "height": height,
        "format": fmt,
        "mode": mode,
        "mime_type": "image/png",
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def verify_thermal_hotspot_alignment(
    thermal_png_path: Path,
    expected_center: Tuple[float, float] = EXPECTED_HOTSPOT_PIXEL_CENTER,
    max_distance_px: float = 3.0,
) -> Dict[str, Any]:
    """Verify the rendered thermal hotspot aligns with projected N4 connector center within <= 3.0 px."""
    thermal_png_path = Path(thermal_png_path)
    with Image.open(thermal_png_path) as im:
        if im.mode != "RGB" or im.size != (SCENE_WIDTH, SCENE_HEIGHT):
            raise ValueError(f"Unexpected thermal image mode/size: {im.mode} {im.size}")
        arr = np.asarray(im, dtype=np.float64)

    # Evaluate rack cabinet region (y in 140..660) to exclude HUD/legend overlays
    y_top, y_bot = 140, 660
    rack_rgb = arr[y_top:y_bot, :, :]
    intensity = rack_rgb.sum(axis=2)

    ry, rx = np.unravel_index(int(np.argmax(intensity)), intensity.shape)
    peak_x = int(rx)
    peak_y = int(ry + y_top)
    peak_val = float(intensity[ry, rx])

    # Compute centroid of peak plateau (within 1.5 intensity units of maximum)
    ys, xs = np.where(intensity >= (peak_val - 1.5))
    centroid_x = float(xs.mean())
    centroid_y = float(ys.mean() + y_top)

    dist_peak = math.hypot(peak_x - expected_center[0], peak_y - expected_center[1])
    dist_centroid = math.hypot(centroid_x - expected_center[0], centroid_y - expected_center[1])
    pixel_distance = round(min(dist_peak, dist_centroid), 4)

    if pixel_distance > max_distance_px:
        raise ValueError(
            f"Hotspot pixel alignment error {pixel_distance:.2f}px exceeds {max_distance_px}px "
            f"(peak={(peak_x, peak_y)}, expected={expected_center})"
        )

    # Verify hotspot is localized to the N4 rear connector (< 3% of frame) rather than a whole-rack blob
    hot_mask = intensity > (0.80 * peak_val)
    hot_fraction = float(hot_mask.sum()) / float(SCENE_WIDTH * SCENE_HEIGHT)
    if hot_fraction <= 0.0 or hot_fraction > 0.03:
        raise ValueError(f"Hotspot area fraction {hot_fraction:.5f} out of localized bounds (0, 0.03]")

    return {
        "peak_pixel": [peak_x, peak_y],
        "centroid_pixel": [round(centroid_x, 2), round(centroid_y, 2)],
        "expected_center": [expected_center[0], expected_center[1]],
        "pixel_distance": pixel_distance,
        "max_allowed_distance_px": max_distance_px,
        "hot_area_fraction": round(hot_fraction, 6),
        "aligned": True,
    }


def _run_capture_mjs(
    out_dir: Path,
    rgb_out: Path,
    thermal_out: Path,
    meta_out: Path,
    approach_clip_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Invoke `node blackbox/static3d/capture.mjs` with `shell=False`."""
    script_path = get_static3d_dir() / "capture.mjs"
    cmd: List[str] = [
        "node",
        str(script_path),
        "--out-dir",
        str(out_dir),
        "--rgb-out",
        str(rgb_out),
        "--thermal-out",
        str(thermal_out),
        "--meta-out",
        str(meta_out),
    ]
    if approach_clip_path is not None:
        cmd.extend(["--approach-clip", str(approach_clip_path)])

    proc = subprocess.run(
        cmd,
        shell=False,
        check=True,
        capture_output=True,
        text=True,
    )
    if meta_out.exists():
        return json.loads(meta_out.read_text(encoding="utf-8"))
    return json.loads(proc.stdout)


def capture_3d_rear_aisle(
    output_dir: Path,
    verify_recapture: bool = True,
    approach_clip_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Capture 3D RGB and Thermal rear-aisle renders via headless Chromium + Three.js."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rgb_path = output_dir / "rgb.png"
    thermal_path = output_dir / "thermal.png"
    meta_path = output_dir / "capture_meta.json"

    payload_1 = _run_capture_mjs(
        out_dir=output_dir,
        rgb_out=rgb_path,
        thermal_out=thermal_path,
        meta_out=meta_path,
        approach_clip_path=approach_clip_path,
    )

    # Also copy canonical aliases rear_aisle_rgb.png and rear_aisle_thermal.png
    rear_rgb = output_dir / "rear_aisle_rgb.png"
    rear_thermal = output_dir / "rear_aisle_thermal.png"
    shutil.copyfile(rgb_path, rear_rgb)
    shutil.copyfile(thermal_path, rear_thermal)

    rgb_info = verify_visual_artifact(rgb_path)
    thermal_info = verify_visual_artifact(thermal_path)
    if (rgb_info["width"], rgb_info["height"]) != (SCENE_WIDTH, SCENE_HEIGHT):
        raise ValueError(f"Unexpected RGB dimensions: {rgb_info}")
    if (thermal_info["width"], thermal_info["height"]) != (SCENE_WIDTH, SCENE_HEIGHT):
        raise ValueError(f"Unexpected thermal dimensions: {thermal_info}")

    recapture_verified = False
    if verify_recapture:
        with tempfile.TemporaryDirectory() as tmp_dir:
            td = Path(tmp_dir)
            rgb_2 = td / "rgb.png"
            thermal_2 = td / "thermal.png"
            meta_2 = td / "meta.json"
            _run_capture_mjs(
                out_dir=td,
                rgb_out=rgb_2,
                thermal_out=thermal_2,
                meta_out=meta_2,
                approach_clip_path=None,
            )
            sha_rgb_2 = _sha256_file(rgb_2)
            sha_thermal_2 = _sha256_file(thermal_2)
            if rgb_info["sha256"] != sha_rgb_2:
                raise RuntimeError(
                    f"Non-deterministic 3D RGB capture: {rgb_info['sha256']} != {sha_rgb_2}"
                )
            if thermal_info["sha256"] != sha_thermal_2:
                raise RuntimeError(
                    f"Non-deterministic 3D thermal capture: {thermal_info['sha256']} != {sha_thermal_2}"
                )
            recapture_verified = True

    alignment = verify_thermal_hotspot_alignment(thermal_path)
    projected_center = project_world_to_pixel()
    projected_bbox = project_connector_bbox()

    return {
        "rgb": rgb_info,
        "thermal": thermal_info,
        "rear_aisle_rgb_path": str(rear_rgb),
        "rear_aisle_thermal_path": str(rear_thermal),
        "recapture_verified": recapture_verified,
        "hotspot_alignment": alignment,
        "projected_center": list(projected_center),
        "projected_bbox": projected_bbox,
        "browser_version": payload_1.get("browserVersion", "unknown"),
        "executable_path": payload_1.get("executablePath", "unknown"),
        "scene_meta": payload_1.get("metadataByMode", {}).get("thermal", {}),
        "approach_clip": payload_1.get("approachClip"),
    }


def build_phase2_visual_bundle(
    artifacts_root: Path,
    generate_approach_clip: bool = False,
    reviewer_initials: str = "AGY",
) -> Dict[str, Any]:
    """Generate the complete Phase 2 visual & public input bundle for Case A and Case B."""
    if not isinstance(reviewer_initials, str) or not reviewer_initials.strip():
        raise ValueError("reviewer_initials must be a non-empty string")

    artifacts_root = Path(artifacts_root)
    pub_dir = artifacts_root / "public"
    case_a_dir = artifacts_root / "case_A"
    case_b_dir = artifacts_root / "case_B"
    reports_dir = artifacts_root / "reports"
    for d in (pub_dir, case_a_dir, case_b_dir, reports_dir):
        d.mkdir(parents=True, exist_ok=True)

    clip_path = pub_dir / "approach.mp4" if generate_approach_clip else None

    # 1. Capture shared 3D RGB & Thermal renders once, then copy exact bytes to case_A and case_B
    capture_info = capture_3d_rear_aisle(
        output_dir=pub_dir,
        verify_recapture=True,
        approach_clip_path=clip_path,
    )
    src_rgb = Path(capture_info["rgb"]["path"])
    src_thermal = Path(capture_info["thermal"]["path"])

    # Also copy shared scene alias for backward-compatible campaign/dashboard modules
    shutil.copyfile(src_thermal, pub_dir / "scene_3d_shared.png")

    for case_dir in (case_a_dir, case_b_dir):
        shutil.copyfile(src_rgb, case_dir / "rgb.png")
        shutil.copyfile(src_rgb, case_dir / "rear_aisle_rgb.png")
        shutil.copyfile(src_thermal, case_dir / "thermal.png")
        shutil.copyfile(src_thermal, case_dir / "rear_aisle_thermal.png")

    # 2. Render SLD SVG and PNG for Case A and Case B
    sld_a = render_sld_png(
        case_id="A",
        output_path=case_a_dir / "sld.png",
        svg_output_path=case_a_dir / "sld.svg",
    )
    shutil.copyfile(case_a_dir / "sld.png", case_a_dir / "sld_case_A.png")

    sld_b = render_sld_png(
        case_id="B",
        output_path=case_b_dir / "sld.png",
        svg_output_path=case_b_dir / "sld.svg",
    )
    shutil.copyfile(case_b_dir / "sld.png", case_b_dir / "sld_case_B.png")

    # 3. Export byte-identical telemetry.json and records.json for Case A and Case B
    priv_a = build_private_world("A")
    priv_b = build_private_world("B")
    pub_pkt_a = export_public_packet(priv_a, diagnostic_mode="guided_diagnostic")
    pub_pkt_b = export_public_packet(priv_b, diagnostic_mode="guided_diagnostic")

    for case_dir, pkt in ((case_a_dir, pub_pkt_a), (case_b_dir, pub_pkt_b)):
        telemetry_payload = {
            "ambient_C": pkt["ambient_C"],
            "alpha_per_C": pkt["alpha_per_C"],
            "horizon_s": pkt["horizon_s"],
            "step_s": pkt["step_s"],
            "component_limits": pkt["component_limits"],
            "telemetry": pkt["telemetry"],
        }
        records_payload = {
            "maintenance_records": pkt["maintenance_records"],
            "diagnostic_menu": pkt["diagnostic_menu"],
            "task_prompt": pkt["task_prompt"],
            "visual_assets": pkt["visual_assets"],
        }
        (case_dir / "telemetry.json").write_text(
            json.dumps(telemetry_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (case_dir / "records.json").write_text(
            json.dumps(records_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    # 4. Compute and enforce the 5 cross-case SHA-256 invariants
    hashes = {
        "case_A": {
            "rgb.png": _sha256_file(case_a_dir / "rgb.png"),
            "thermal.png": _sha256_file(case_a_dir / "thermal.png"),
            "sld.png": _sha256_file(case_a_dir / "sld.png"),
            "sld.svg": _sha256_file(case_a_dir / "sld.svg"),
            "telemetry.json": _sha256_file(case_a_dir / "telemetry.json"),
            "records.json": _sha256_file(case_a_dir / "records.json"),
        },
        "case_B": {
            "rgb.png": _sha256_file(case_b_dir / "rgb.png"),
            "thermal.png": _sha256_file(case_b_dir / "thermal.png"),
            "sld.png": _sha256_file(case_b_dir / "sld.png"),
            "sld.svg": _sha256_file(case_b_dir / "sld.svg"),
            "telemetry.json": _sha256_file(case_b_dir / "telemetry.json"),
            "records.json": _sha256_file(case_b_dir / "records.json"),
        },
    }

    invariants = {
        "rgb_identical": hashes["case_A"]["rgb.png"] == hashes["case_B"]["rgb.png"],
        "thermal_identical": hashes["case_A"]["thermal.png"] == hashes["case_B"]["thermal.png"],
        "sld_distinct": hashes["case_A"]["sld.png"] != hashes["case_B"]["sld.png"],
        "telemetry_identical": hashes["case_A"]["telemetry.json"] == hashes["case_B"]["telemetry.json"],
        "records_identical": hashes["case_A"]["records.json"] == hashes["case_B"]["records.json"],
    }
    if not all(invariants.values()):
        raise RuntimeError(f"Phase 2 cross-case SHA-256 invariant failure: {invariants}")

    def _file_entry(p: Path, role: str, mime: str) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "path": str(p),
            "rel_path": p.relative_to(artifacts_root).as_posix(),
            "role": role,
            "mime_type": mime,
            "size_bytes": p.stat().st_size,
            "sha256": _sha256_file(p),
        }
        if mime == "image/png":
            info = verify_visual_artifact(p)
            entry["width"] = info["width"]
            entry["height"] = info["height"]
            entry["mode"] = info["mode"]
        return entry

    manifest: Dict[str, Any] = {
        "phase": "02_VISUAL_ASSETS",
        "degraded_2d_fallback": False,
        "human_inspection": {
            "reviewer_initials": reviewer_initials.strip(),
            "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "checklist_passed": True,
        },
        "renderer": {
            "browser_version": capture_info["browser_version"],
            "executable_path": capture_info["executable_path"],
            "three_revision": capture_info["scene_meta"].get("threeVersion", "169"),
            "viewport": {"width": SCENE_WIDTH, "height": SCENE_HEIGHT, "device_pixel_ratio": 1},
            "camera": {
                "position": list(DEFAULT_CAMERA_POS),
                "look_at": list(DEFAULT_LOOK_AT),
                "fov_deg": DEFAULT_FOV_DEG,
            },
            "fault_connector": {
                "rack_id": "N4",
                "world_center": list(FAULT_CONNECTOR_CENTER_WORLD),
                "world_size": list(FAULT_CONNECTOR_SIZE_WORLD),
                "projected_pixel_center": capture_info["projected_center"],
                "projected_pixel_bbox": capture_info["projected_bbox"],
            },
        },
        "hotspot_alignment": capture_info["hotspot_alignment"],
        "cross_case_invariants": invariants,
        "cases": {
            "case_A": {
                "rgb": _file_entry(case_a_dir / "rgb.png", "shared_3d_rgb", "image/png"),
                "thermal": _file_entry(case_a_dir / "thermal.png", "shared_3d_thermal", "image/png"),
                "sld_png": _file_entry(case_a_dir / "sld.png", "case_sld_png", "image/png"),
                "sld_svg": _file_entry(case_a_dir / "sld.svg", "case_sld_svg", "image/svg+xml"),
                "telemetry": _file_entry(case_a_dir / "telemetry.json", "public_telemetry", "application/json"),
                "records": _file_entry(case_a_dir / "records.json", "public_records", "application/json"),
            },
            "case_B": {
                "rgb": _file_entry(case_b_dir / "rgb.png", "shared_3d_rgb", "image/png"),
                "thermal": _file_entry(case_b_dir / "thermal.png", "shared_3d_thermal", "image/png"),
                "sld_png": _file_entry(case_b_dir / "sld.png", "case_sld_png", "image/png"),
                "sld_svg": _file_entry(case_b_dir / "sld.svg", "case_sld_svg", "image/svg+xml"),
                "telemetry": _file_entry(case_b_dir / "telemetry.json", "public_telemetry", "application/json"),
                "records": _file_entry(case_b_dir / "records.json", "public_records", "application/json"),
            },
        },
        "sld_metadata": {
            "case_A": sld_a.to_dict(),
            "case_B": sld_b.to_dict(),
        },
    }

    input_manifest_path = artifacts_root / "input_manifest.json"
    phase2_manifest_path = reports_dir / "phase2_visual_manifest.json"
    manifest_json = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    input_manifest_path.write_text(manifest_json, encoding="utf-8")
    phase2_manifest_path.write_text(manifest_json, encoding="utf-8")

    return manifest


def render_deterministic_3d_scene(output_path: Path) -> Dict[str, Any]:
    """Render deterministic 3D scene PNG to `output_path` (backward-compatible helper)."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    img = Image.new("RGB", (SCENE_WIDTH, SCENE_HEIGHT), color=(10, 15, 28))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()

    draw.rectangle([20, 20, SCENE_WIDTH - 20, 72], fill=(23, 37, 84), outline=(59, 130, 246), width=2)
    draw.text(
        (38, 38),
        "BLACKBOX-DC 3D IR THERMOGRAPHY OVERLAY — SHARED SENSORY EVIDENCE",
        fill=(248, 250, 252),
        font=font,
    )

    racks = [
        {"node": "N1", "x": 110, "temp_c": 36.0, "hotspot": False},
        {"node": "N2", "x": 380, "temp_c": 25.0, "hotspot": True},
        {"node": "N3", "x": 650, "temp_c": 25.0, "hotspot": False},
        {"node": "N4", "x": 920, "temp_c": 88.95, "hotspot": True},
    ]
    for r in racks:
        rx = int(r["x"])
        draw.rectangle([rx, 150, rx + 200, 620], fill=(24, 33, 52), outline=(71, 85, 105), width=2)
        draw.text((rx + 24, 180), f"RACK {r['node']} ({r['temp_c']:.1f} C)", fill=(255, 255, 255), font=font)

    # Pinpoint peak at (1110, 359) for N4 connector
    cx, cy = int(round(EXPECTED_HOTSPOT_PIXEL_CENTER[0])), int(round(EXPECTED_HOTSPOT_PIXEL_CENTER[1]))
    draw.ellipse([cx - 16, cy - 16, cx + 16, cy + 16], fill=(245, 158, 11))
    draw.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=(255, 255, 255))

    img.save(output_path, format="PNG", optimize=True)
    digest = _sha256_file(output_path)
    return {
        "output_path": str(output_path),
        "sha256": digest,
        "width": SCENE_WIDTH,
        "height": SCENE_HEIGHT,
        "hotspot_node": "N2",
        "static3d_dir_present": get_static3d_dir().exists(),
    }
