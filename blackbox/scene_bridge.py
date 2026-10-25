"""Python bridge to static3d scene capture and deterministic 3D fallback renderer.

Provides visual scene generation and verification for the shared 3D data-center
thermal overlay image (`scene_3d_shared.png`), depicting racks N1..N4 with the
localized terminal thermal hotspot on Node N2.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, Optional

from PIL import Image, ImageDraw, ImageFont


SCENE_WIDTH = 960
SCENE_HEIGHT = 600


def get_static3d_dir() -> Path:
    """Return the path to blackbox/static3d directory."""
    return Path(__file__).resolve().parent / "static3d"


def render_deterministic_3d_scene(output_path: Path) -> Dict[str, object]:
    """Render a deterministic 2.5D/3D perspective thermal view of racks N1..N4.

    Shows racks N1, N2, N3, N4 in an isometric data-center aisle with infrared
    thermography overlay highlighting the high-temperature lug hotspot on N2
    (135.2 C) while N3 remains at nominal operating temperature (37.8 C).
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    img = Image.new("RGB", (SCENE_WIDTH, SCENE_HEIGHT), color=(10, 15, 28))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()

    # Header
    draw.rectangle([20, 20, SCENE_WIDTH - 20, 72], fill=(23, 37, 84), outline=(59, 130, 246), width=2)
    draw.text(
        (38, 38),
        "BLACKBOX-DC 3D IR THERMOGRAPHY OVERLAY — SHARED SENSORY EVIDENCE (HALL 4)",
        fill=(248, 250, 252),
        font=font,
    )

    # Isometric floor grid lines
    for y in range(470, 575, 25):
        draw.line([(60, y), (SCENE_WIDTH - 60, y)], fill=(30, 41, 59), width=1)

    racks = [
        {"node": "N1", "x": 110, "temp_c": 32.2, "hotspot": False, "load_a": 80.0},
        {"node": "N2", "x": 315, "temp_c": 135.2, "hotspot": True, "load_a": 95.0},
        {"node": "N3", "x": 520, "temp_c": 37.8, "hotspot": False, "load_a": 95.0},
        {"node": "N4", "x": 725, "temp_c": 31.7, "hotspot": False, "load_a": 75.0},
    ]

    for r in racks:
        rx = int(r["x"])
        ry_top = 150
        ry_bot = 465
        w = 135
        depth_dx = 28
        depth_dy = -24

        # 3D side/top faces
        top_poly = [
            (rx, ry_top),
            (rx + depth_dx, ry_top + depth_dy),
            (rx + w + depth_dx, ry_top + depth_dy),
            (rx + w, ry_top),
        ]
        side_poly = [
            (rx + w, ry_top),
            (rx + w + depth_dx, ry_top + depth_dy),
            (rx + w + depth_dx, ry_bot + depth_dy),
            (rx + w, ry_bot),
        ]
        outline_col = (244, 63, 94) if r["hotspot"] else (71, 85, 105)
        draw.polygon(top_poly, fill=(30, 41, 59), outline=outline_col)
        draw.polygon(side_poly, fill=(15, 23, 42), outline=outline_col)

        # Front rack face
        draw.rectangle(
            [rx, ry_top, rx + w, ry_bot],
            fill=(24, 33, 52),
            outline=outline_col,
            width=3 if r["hotspot"] else 2,
        )

        # Thermal lug glow at top termination block
        if r["hotspot"]:
            draw.ellipse([rx + 25, ry_top + 20, rx + w - 25, ry_top + 105], fill=(220, 38, 38))
            draw.ellipse([rx + 42, ry_top + 38, rx + w - 42, ry_top + 88], fill=(254, 240, 138))
            draw.text((rx + 14, ry_top + 115), "IR HOTSPOT: 135.2 C", fill=(254, 202, 202), font=font)
            draw.text((rx + 14, ry_top + 133), "HIGH-R LUG ANOMALY", fill=(253, 224, 71), font=font)
        else:
            draw.ellipse([rx + 42, ry_top + 40, rx + w - 42, ry_top + 85], fill=(14, 116, 144))
            draw.text(
                (rx + 18, ry_top + 115),
                f"IR NORMAL: {r['temp_c']:.1f} C",
                fill=(125, 211, 252),
                font=font,
            )

        # Rack label plate
        draw.rectangle([rx + 12, ry_bot - 85, rx + w - 12, ry_bot - 15], fill=(15, 23, 42), outline=outline_col)
        draw.text((rx + 24, ry_bot - 72), f"RACK BAY {r['node']}", fill=(255, 255, 255), font=font)
        draw.text((rx + 24, ry_bot - 50), f"LOAD: {r['load_a']:.1f} A", fill=(148, 163, 184), font=font)
        draw.text((rx + 24, ry_bot - 32), "FEEDER: SEE SLD", fill=(56, 189, 248), font=font)

    # Footer note
    draw.text(
        (38, SCENE_HEIGHT - 36),
        "VISUAL EVIDENCE SUMMARY: Thermal hotspot localized to Rack Node N2 top lug (135.2 C). "
        "Consult SLD for feeder ID.",
        fill=(226, 232, 240),
        font=font,
    )

    img.save(output_path, format="PNG", optimize=False)
    digest = hashlib.sha256(output_path.read_bytes()).hexdigest()
    return {
        "output_path": str(output_path),
        "sha256": digest,
        "width": SCENE_WIDTH,
        "height": SCENE_HEIGHT,
        "hotspot_node": "N2",
        "static3d_dir_present": get_static3d_dir().exists(),
    }


def verify_visual_artifact(image_path: Path) -> Dict[str, object]:
    """Verify that a generated PNG artifact exists, is readable, and has valid dimensions."""
    image_path = Path(image_path)
    if not image_path.exists():
        raise FileNotFoundError(f"Visual artifact missing: {image_path}")
    data = image_path.read_bytes()
    if len(data) < 128:
        raise ValueError(f"Visual artifact too small ({len(data)} bytes): {image_path}")
    with Image.open(image_path) as im:
        width, height = im.size
        fmt = im.format
    return {
        "path": str(image_path),
        "exists": True,
        "size_bytes": len(data),
        "width": width,
        "height": height,
        "format": fmt,
        "sha256": hashlib.sha256(data).hexdigest(),
    }
