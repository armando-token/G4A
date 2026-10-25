"""Three.js 3D Twin render bridge and pixel-corridor verification for `twin_repair`."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np
from PIL import Image


REPO_ROOT = Path(__file__).resolve().parent.parent
CAPTURE_MJS_PATH = REPO_ROOT / "blackbox" / "static3d" / "capture.mjs"
EXPECTED_WIDTH = 1280
EXPECTED_HEIGHT = 800
MAX_PNG_BYTES = 512 * 1024


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _find_thermal_peak_xy_in_bbox(
    thermal_png_path: Path,
    bbox_xyxy: Sequence[int],
) -> Tuple[float, float]:
    """Locate the brightest thermal core centroid inside `bbox_xyxy`."""
    with Image.open(thermal_png_path) as img:
        arr = np.asarray(img.convert("RGB"), dtype=np.float64)
    x0, y0, x1, y1 = [int(v) for v in bbox_xyxy]
    x0 = max(0, min(arr.shape[1] - 1, x0))
    x1 = max(x0 + 1, min(arr.shape[1], x1))
    y0 = max(0, min(arr.shape[0] - 1, y0))
    y1 = max(y0 + 1, min(arr.shape[0], y1))

    sub = arr[y0:y1, x0:x1]
    luma = 0.299 * sub[:, :, 0] + 0.587 * sub[:, :, 1] + 0.114 * sub[:, :, 2]
    max_val = float(np.max(luma))
    mask = luma >= (max_val - 3.0)
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return float((x0 + x1) / 2.0), float((y0 + y1) / 2.0)
    cx = float(x0 + np.mean(xs))
    cy = float(y0 + np.mean(ys))
    return cx, cy


def render_scene_spec(
    spec_path: Union[Path, str, Mapping[str, Any]],
    out_dir: Union[Path, str],
    *,
    timeout_s: float = 45.0,
) -> Dict[str, Any]:
    """Render a validated `SceneSpec v1` via headless Three.js (`capture.mjs`) and verify assets."""
    out_path = Path(out_dir).resolve()
    out_path.mkdir(parents=True, exist_ok=True)

    if isinstance(spec_path, Mapping):
        spec_obj = dict(spec_path)
        spec_file = out_path / "scene_spec_input.json"
        spec_file.write_text(
            json.dumps(spec_obj, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    else:
        spec_file = Path(spec_path).resolve()
        if not spec_file.is_file():
            raise FileNotFoundError(f"SceneSpec file not found: {spec_file}")
        spec_obj = json.loads(spec_file.read_text(encoding="utf-8"))

    if str(spec_obj.get("schema_version", "")) not in ("scene_spec.v1", "SceneSpec.v1"):
        raise ValueError(
            f"Expected schema_version='scene_spec.v1', got {spec_obj.get('schema_version')!r}"
        )

    node_bin = shutil.which("node") or "node"
    cmd = [
        node_bin,
        str(CAPTURE_MJS_PATH),
        "scene",
        str(spec_file),
        str(out_path),
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=float(timeout_s),
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"3D capture failed (rc={proc.returncode}):\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
        )

    meta_path = out_path / "render_meta.json"
    if not meta_path.is_file():
        raise FileNotFoundError(f"Expected render_meta.json not produced in {out_path}")

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    required_pngs = ("render_wiring.png", "render_rgb_rear.png", "render_thermal_rear.png")
    files_summary: Dict[str, Dict[str, Any]] = {}

    for fname in required_pngs:
        fpath = out_path / fname
        if not fpath.is_file():
            raise FileNotFoundError(f"Missing required rendered PNG: {fpath}")
        raw_bytes = fpath.read_bytes()
        if len(raw_bytes) >= MAX_PNG_BYTES:
            raise ValueError(f"{fname} exceeds 512 KiB budget ({len(raw_bytes)} bytes)")
        with Image.open(fpath) as img:
            w, h = img.size
            if (w, h) != (EXPECTED_WIDTH, EXPECTED_HEIGHT):
                raise ValueError(
                    f"{fname} dimensions {(w, h)} != {(EXPECTED_WIDTH, EXPECTED_HEIGHT)}"
                )
            arr = np.asarray(img.convert("RGB"), dtype=np.float64)
            std_val = float(np.std(arr))
            if std_val < 1.0:
                raise ValueError(f"{fname} appears blank (pixel std={std_val:.4f})")

        files_summary[fname] = {
            "path": str(fpath),
            "sha256": _sha256_bytes(raw_bytes),
            "bytes": len(raw_bytes),
            "width": w,
            "height": h,
            "pixel_std": round(std_val, 3),
        }

    # Verify RGB/thermal hotspot registration <= 3 px in rear_inspection pose
    hotspot_roi = meta.get("hotspot_roi") or {}
    proj_center = hotspot_roi.get("projectedPixelCenter", [1106.0, 359.0])
    proj_bbox = hotspot_roi.get("projectedPixelBBox", [1079, 325, 1143, 393])
    peak_x, peak_y = _find_thermal_peak_xy_in_bbox(out_path / "render_thermal_rear.png", proj_bbox)
    reg_err_px = math.hypot(peak_x - float(proj_center[0]), peak_y - float(proj_center[1]))
    if reg_err_px > 3.0:
        raise ValueError(
            f"Hotspot RGB/thermal registration error {reg_err_px:.2f} px exceeds 3.0 px limit"
        )

    meta["out_dir"] = str(out_path)
    meta["files"] = files_summary
    meta["thermal_peak_xy_px"] = [round(peak_x, 2), round(peak_y, 2)]
    meta["registration_error_px"] = round(reg_err_px, 4)
    meta["registration_ok"] = bool(reg_err_px <= 3.0)
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return meta


def _union_bbox(box_a: Sequence[int], box_b: Sequence[int]) -> Tuple[int, int, int, int]:
    return (
        min(int(box_a[0]), int(box_b[0])),
        min(int(box_a[1]), int(box_b[1])),
        max(int(box_a[2]), int(box_b[2])),
        max(int(box_a[3]), int(box_b[3])),
    )


def compare_wiring_renders(
    render0_dir: Union[Path, str],
    render1_dir: Union[Path, str],
    meta0_override: Optional[Union[Path, str, Sequence[str]]] = None,
    meta1_override: Optional[Union[Path, str, Sequence[str]]] = None,
    *,
    changed_racks: Sequence[str] = ("N2", "N3"),
    stable_racks: Sequence[str] = ("N1", "N4"),
    pixel_diff_threshold: int = 15,
    min_changed_corridor_pixels: int = 100,
    max_stable_corridor_pixels: int = 10,
) -> Dict[str, Any]:
    """Compare two `SceneSpec v1` render directories or PNG paths to verify graph-coupled 3D cable changes."""
    p0 = Path(render0_dir).resolve()
    p1 = Path(render1_dir).resolve()

    if p0.is_file():
        wiring0_path = p0
        dir0 = p0.parent
    else:
        dir0 = p0
        wiring0_path = dir0 / "render_wiring.png"

    if p1.is_file():
        wiring1_path = p1
        dir1 = p1.parent
    else:
        dir1 = p1
        wiring1_path = dir1 / "render_wiring.png"

    if isinstance(meta0_override, (Path, str)):
        meta0_path = Path(meta0_override).resolve()
    else:
        if isinstance(meta0_override, Sequence):
            changed_racks = meta0_override
        meta0_path = dir0 / "render_meta.json"

    if isinstance(meta1_override, (Path, str)):
        meta1_path = Path(meta1_override).resolve()
    else:
        if isinstance(meta1_override, Sequence):
            stable_racks = meta1_override
        meta1_path = dir1 / "render_meta.json"

    raw0 = wiring0_path.read_bytes()
    raw1 = wiring1_path.read_bytes()
    sha0 = _sha256_bytes(raw0)
    sha1 = _sha256_bytes(raw1)
    sha_differs = sha0 != sha1

    meta0 = json.loads(meta0_path.read_text(encoding="utf-8"))
    meta1 = json.loads(meta1_path.read_text(encoding="utf-8"))

    with Image.open(wiring0_path) as img0:
        arr0 = np.asarray(img0.convert("RGB"), dtype=np.int16)
    with Image.open(wiring1_path) as img1:
        arr1 = np.asarray(img1.convert("RGB"), dtype=np.int16)

    if arr0.shape != arr1.shape:
        raise ValueError(f"Render shape mismatch: {arr0.shape} vs {arr1.shape}")

    max_diff = np.max(np.abs(arr1 - arr0), axis=2)
    diff_mask = max_diff > int(pixel_diff_threshold)

    # Mask out HUD exclusion rectangles so text badge changes never count as cable geometry changes
    exclusions = list(meta0.get("hud_exclusion_bboxes_px") or [[0, 0, 360, 64]]) + list(
        meta1.get("hud_exclusion_bboxes_px") or []
    )
    for box in exclusions:
        if len(box) == 4:
            x0, y0, x1, y1 = [int(v) for v in box]
            diff_mask[max(0, y0) : min(diff_mask.shape[0], y1), max(0, x0) : min(diff_mask.shape[1], x1)] = False

    corridors0 = meta0.get("projected_cable_corridors") or {}
    corridors1 = meta1.get("projected_cable_corridors") or {}
    changed_pixels_by_rack: Dict[str, int] = {}
    corridor_union_bboxes: Dict[str, List[int]] = {}

    for rk in ("N1", "N2", "N3", "N4"):
        b0 = (corridors0.get(rk) or {}).get("bbox_xyxy_px", [0, 0, 0, 0])
        b1 = (corridors1.get(rk) or {}).get("bbox_xyxy_px", [0, 0, 0, 0])
        ux0, uy0, ux1, uy1 = _union_bbox(b0, b1)
        corridor_union_bboxes[rk] = [ux0, uy0, ux1, uy1]
        sub_mask = diff_mask[
            max(0, uy0) : min(diff_mask.shape[0], uy1),
            max(0, ux0) : min(diff_mask.shape[1], ux1),
        ]
        changed_pixels_by_rack[rk] = int(np.count_nonzero(sub_mask))

    total_changed_excl_hud = int(np.count_nonzero(diff_mask))

    changed_racks_ok = all(
        changed_pixels_by_rack.get(rk, 0) >= int(min_changed_corridor_pixels)
        for rk in changed_racks
    )
    stable_racks_ok = all(
        changed_pixels_by_rack.get(rk, 0) <= int(max_stable_corridor_pixels)
        for rk in stable_racks
    )

    # Check thermal hotspot stability across render0 and render1
    c0 = (meta0.get("hotspot_roi") or {}).get("projectedPixelCenter", [1106.0, 359.0])
    c1 = (meta1.get("hotspot_roi") or {}).get("projectedPixelCenter", [1106.0, 359.0])
    hotspot_shift_px = math.hypot(float(c1[0]) - float(c0[0]), float(c1[1]) - float(c0[1]))
    hotspot_stable = hotspot_shift_px <= 1.0

    cable_pixels_changed = bool(
        sha_differs and changed_racks_ok and stable_racks_ok and hotspot_stable
    )

    return {
        "sha256_render0": sha0,
        "sha256_render1": sha1,
        "sha256_differs": sha_differs,
        "total_changed_pixels_excluding_hud": total_changed_excl_hud,
        "changed_pixels_by_rack": changed_pixels_by_rack,
        "corridor_union_bboxes_px": corridor_union_bboxes,
        "changed_racks_verified": changed_racks_ok,
        "stable_racks_verified": stable_racks_ok,
        "hotspot_shift_px": round(hotspot_shift_px, 4),
        "hotspot_stable": hotspot_stable,
        "cable_pixels_changed": cable_pixels_changed,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m twin_repair.render_bridge",
        description="Render a SceneSpec v1 JSON into 3D wiring/RGB/thermal PNGs.",
    )
    parser.add_argument("--spec", required=True, help="Path to SceneSpec v1 JSON file.")
    parser.add_argument("--out", "--out-dir", dest="out", required=True, help="Output directory for rendered PNGs and metadata.")
    parser.add_argument("--compare-with", default=None, help="Optional second render dir to compare against.")
    args = parser.parse_args(list(argv) if argv is not None else None)

    meta = render_scene_spec(args.spec, args.out)
    output: Dict[str, Any] = {"render": meta}
    if args.compare_with:
        output["comparison"] = compare_wiring_renders(args.compare_with, args.out)
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
