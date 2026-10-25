"""Deterministic Single-Line Diagram (SLD) PNG generator using Pillow.

Renders the electrical topology for Case A (straight F2->N2, F3->N3) and
Case B (crossed F2->N3, F3->N2) with high-contrast labels for multimodal VLM
inspection and human review.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, Tuple

from PIL import Image, ImageDraw, ImageFont


SLD_WIDTH = 960
SLD_HEIGHT = 600


def get_case_sld_mapping(case_id: str) -> Dict[str, str]:
    """Return canonical node_id -> feeder_id mapping for the given case."""
    norm = case_id.strip()
    if norm in ("case_A", "A"):
        return {"N1": "F1", "N2": "F2", "N3": "F3", "N4": "F4"}
    if norm in ("case_B", "B"):
        return {"N1": "F1", "N2": "F3", "N3": "F2", "N4": "F4"}
    raise ValueError(f"Unsupported case_id for SLD rendering: {case_id!r}")


def render_sld_png(case_id: str, output_path: Path) -> Dict[str, object]:
    """Render a deterministic Single-Line Diagram PNG for case_A or case_B."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    node_to_feeder = get_case_sld_mapping(case_id)
    canonical_id = "case_A" if case_id.strip() in ("case_A", "A") else "case_B"

    img = Image.new("RGB", (SLD_WIDTH, SLD_HEIGHT), color=(15, 23, 42))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()

    # Header bar and title
    draw.rectangle([20, 20, SLD_WIDTH - 20, 75], fill=(30, 41, 59), outline=(100, 116, 139), width=2)
    title_text = (
        f"BLACKBOX-DC SINGLE-LINE DIAGRAM (SLD) — {canonical_id.upper()} "
        f"| 480V MAIN PDU BUS"
    )
    draw.text((40, 40), title_text, fill=(248, 250, 252), font=font)

    # Main 480V busbar
    bus_y = 115
    draw.line([(80, bus_y), (SLD_WIDTH - 80, bus_y)], fill=(56, 189, 248), width=6)
    draw.text((85, bus_y - 22), "480V AC MAIN DISTRIBUTION BUSBAR", fill=(56, 189, 248), font=font)

    # Column X coordinates for 4 bays
    col_x = {
        1: 160,
        2: 370,
        3: 590,
        4: 800,
    }
    feeder_x = {
        "F1": col_x[1],
        "F2": col_x[2],
        "F3": col_x[3],
        "F4": col_x[4],
    }
    node_x = {
        "N1": col_x[1],
        "N2": col_x[2],
        "N3": col_x[3],
        "N4": col_x[4],
    }

    breaker_top_y = 165
    breaker_bot_y = 235
    crossover_top_y = 275
    crossover_bot_y = 375
    rack_top_y = 430
    rack_bot_y = 525

    # Draw Feeder Breakers F1..F4 at top row
    for fid, fx in feeder_x.items():
        draw.line([(fx, bus_y), (fx, breaker_top_y)], fill=(148, 163, 184), width=3)
        draw.rectangle(
            [fx - 55, breaker_top_y, fx + 55, breaker_bot_y],
            fill=(30, 41, 59),
            outline=(250, 204, 21),
            width=2,
        )
        draw.text((fx - 40, breaker_top_y + 18), f"FEEDER {fid}", fill=(250, 204, 21), font=font)
        draw.text((fx - 40, breaker_top_y + 38), "125A BREAKER", fill=(203, 213, 225), font=font)
        draw.line([(fx, breaker_bot_y), (fx, crossover_top_y)], fill=(250, 204, 21), width=3)

    # Draw routing between Feeders and Nodes (straight in case_A, crossed for F2/F3 in case_B)
    for nid, fid in sorted(node_to_feeder.items()):
        fx = feeder_x[fid]
        nx = node_x[nid]
        is_crossed = fx != nx
        wire_color = (244, 63, 94) if is_crossed else (34, 197, 94)
        draw.line([(fx, crossover_top_y), (nx, crossover_bot_y)], fill=wire_color, width=4)
        draw.line([(nx, crossover_bot_y), (nx, rack_top_y)], fill=wire_color, width=4)

        mid_x = int((fx + nx) / 2)
        mid_y = int((crossover_top_y + crossover_bot_y) / 2)
        badge_offset = -22 if fid == "F2" else 14
        draw.rectangle(
            [mid_x - 46, mid_y + badge_offset - 10, mid_x + 46, mid_y + badge_offset + 12],
            fill=(15, 23, 42),
            outline=wire_color,
            width=1,
        )
        draw.text(
            (mid_x - 38, mid_y + badge_offset - 5),
            f"{fid} -> {nid}",
            fill=(255, 255, 255),
            font=font,
        )

    # Draw Node Rack Terminations N1..N4 at bottom row
    currents = {"N1": 80.0, "N2": 95.0, "N3": 95.0, "N4": 75.0}
    for nid, nx in node_x.items():
        connected_fid = node_to_feeder[nid]
        draw.rectangle(
            [nx - 65, rack_top_y, nx + 65, rack_bot_y],
            fill=(30, 41, 59),
            outline=(56, 189, 248),
            width=2,
        )
        draw.text((nx - 50, rack_top_y + 15), f"RACK NODE {nid}", fill=(248, 250, 252), font=font)
        draw.text(
            (nx - 50, rack_top_y + 38),
            f"FED BY: {connected_fid}",
            fill=(56, 189, 248),
            font=font,
        )
        draw.text(
            (nx - 50, rack_top_y + 60),
            f"LOAD: {currents[nid]:.1f} A",
            fill=(203, 213, 225),
            font=font,
        )

    # Footer summary legend
    routing_summary = ", ".join(f"{n}<={f}" for n, f in sorted(node_to_feeder.items()))
    draw.text(
        (40, SLD_HEIGHT - 35),
        f"VERIFIED SLD ROUTING SCHEDULE ({canonical_id}): {routing_summary}",
        fill=(148, 163, 184),
        font=font,
    )

    img.save(output_path, format="PNG", optimize=False)
    digest = hashlib.sha256(output_path.read_bytes()).hexdigest()
    return {
        "case_id": canonical_id,
        "output_path": str(output_path),
        "sha256": digest,
        "width": SLD_WIDTH,
        "height": SLD_HEIGHT,
        "node_to_feeder": node_to_feeder,
    }
