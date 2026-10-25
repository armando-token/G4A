"""Deterministic Single-Line Diagram (SLD) SVG & PNG generator and geometry parser.

Implements Phase 2 SLD requirements:
- `1280×800` IEC 61082-1 / IEC 60617-inspired technical single-line diagram.
- `F1` bus at `y = 210` (`F1 BUS — 44A LIMIT`), `F2` bus at `y = 290` (`F2 BUS — 50A LIMIT`).
- Rack columns at `x = 200` (`N1`), `x = 460` (`N2`), `x = 720` (`N3`), `x = 980` (`N4`).
- Filled junction dots (`r = 7`) exclusively at connected bus intersections.
- Semicircular crossing bridge arches (`r = 12`, `y = 278..302`) over `F2` (`y = 290`) with
  NO dot at `(x, 290)` whenever a column connects to `F1` (`y = 210`).
- Identical text, labels, title block, and legends between Case A and Case B (zero text
  leakage of case ID or feeder mapping; only branch geometry, dots, and arches change).
- OCR-agnostic SVG and PNG topology parsers (`parse_sld_svg_topology`, `parse_sld_png_topology`).
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional, Set, Tuple
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw, ImageFont

SLD_WIDTH: int = 1280
SLD_HEIGHT: int = 800

F1_BUS_Y: int = 210
F2_BUS_Y: int = 290
BUS_X_MIN: int = 100
BUS_X_MAX: int = 1180

JUNCTION_DOT_RADIUS: int = 7
ARCH_RADIUS: int = 12
ARCH_Y_TOP: int = F2_BUS_Y - ARCH_RADIUS  # 278
ARCH_Y_BOTTOM: int = F2_BUS_Y + ARCH_RADIUS  # 302

SWITCH_Y_TOP: int = 370
SWITCH_Y_CENTER: int = 390
SWITCH_Y_BOTTOM: int = 410

RACK_BOX_Y_TOP: int = 520
RACK_BOX_Y_BOTTOM: int = 632
RACK_BOX_HALF_W: int = 88

SENSOR_BOX_Y_TOP: int = 652
SENSOR_BOX_Y_BOTTOM: int = 702
SENSOR_BOX_HALF_W: int = 68

RACK_COLUMNS: Dict[str, int] = {
    "N1": 200,
    "N2": 460,
    "N3": 720,
    "N4": 980,
}

COLUMN_TO_RACK: Dict[int, str] = {x: rack_id for rack_id, x in RACK_COLUMNS.items()}

RACK_LOAD_LABELS: Dict[str, str] = {
    "N1": "N1 (36A / 40A MAX)",
    "N2": "N2 (4A / 16A MAX)",
    "N3": "N3 (4A / 16A MAX)",
    "N4": "N4 (24A / 32A MAX)",
}

RACK_SENSOR_LABELS: Dict[str, str] = {
    "N1": "SNS_K1",
    "N2": "SNS_K3",
    "N3": "SNS_K4",
    "N4": "SNS_K2",
}

# Palette (24-bit RGB)
BG_COLOR: Tuple[int, int, int] = (248, 250, 252)       # #F8FAFC technical drawing sheet
GRID_BORDER_COLOR: Tuple[int, int, int] = (148, 163, 184)  # #94A3B8
HEADER_BG_COLOR: Tuple[int, int, int] = (226, 232, 240)    # #E2E8F0
BOX_FILL_COLOR: Tuple[int, int, int] = (255, 255, 255)     # #FFFFFF
SENSOR_FILL_COLOR: Tuple[int, int, int] = (241, 245, 249)  # #F1F5F9
INK_COLOR: Tuple[int, int, int] = (15, 23, 42)         # #0F172A primary conductor / dot ink
SUB_INK_COLOR: Tuple[int, int, int] = (51, 65, 85)     # #334155 secondary text / outlines


class RenderedSLDPath(type(Path())):  # type: ignore[misc]
    """Path subclass that also supports dict-style metadata lookup for backward compatibility."""

    _meta: Dict[str, Any]

    def __new__(cls, *args: Any, meta: Optional[Dict[str, Any]] = None) -> "RenderedSLDPath":
        instance = super().__new__(cls, *args)
        instance._meta = dict(meta or {})
        return instance

    def __getitem__(self, key: str) -> Any:
        return self._meta[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._meta.get(key, default)

    def keys(self) -> Any:
        return self._meta.keys()

    def items(self) -> Any:
        return self._meta.items()

    def values(self) -> Any:
        return self._meta.values()

    def __contains__(self, key: object) -> bool:
        return key in self._meta

    def __iter__(self) -> Iterator[str]:
        return iter(self._meta)

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._meta)


def get_case_sld_mapping(case_id: str) -> Dict[str, str]:
    """Return canonical rack_id -> pdu_id mapping for Network A or Network B."""
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError("case_id must be a non-empty string")
    norm = case_id.strip()
    if norm in ("case_A", "A"):
        return {"N1": "F1", "N2": "F1", "N3": "F2", "N4": "F2"}
    if norm in ("case_B", "B"):
        return {"N1": "F1", "N2": "F2", "N3": "F1", "N4": "F2"}
    raise ValueError(f"Unsupported case_id for SLD rendering: {case_id!r}")


def _validate_explicit_mapping(mapping: Mapping[str, str]) -> Dict[str, str]:
    out = {str(k): str(v) for k, v in dict(mapping).items()}
    if sorted(out) != ["N1", "N2", "N3", "N4"] or any(v not in ("F1", "F2") for v in out.values()):
        raise ValueError(f"Explicit SLD mapping must assign N1..N4 to F1/F2, got {out!r}")
    return out


def render_sld_svg(case_id: str, mapping: Optional[Mapping[str, str]] = None) -> str:
    """Render a deterministic 1280x800 IEC-inspired Single-Line Diagram SVG.

    Text, labels, title block, and legends are 100% identical between Case A and Case B;
    only the vertical branch start, junction dot, and crossing arch geometry differ.
    """
    mapping = _validate_explicit_mapping(mapping) if mapping is not None else get_case_sld_mapping(case_id)

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SLD_WIDTH}" height="{SLD_HEIGHT}" '
        f'viewBox="0 0 {SLD_WIDTH} {SLD_HEIGHT}">',
        '  <rect x="0" y="0" width="1280" height="800" fill="#F8FAFC" />',
        '  <rect x="24" y="24" width="1232" height="752" fill="none" stroke="#334155" stroke-width="2" />',
        '  <!-- Title Block -->',
        '  <rect x="24" y="24" width="1232" height="84" fill="#E2E8F0" stroke="#334155" stroke-width="2" />',
        '  <text x="48" y="60" font-family="monospace" font-size="18" font-weight="bold" fill="#0F172A">'
        'BLACKBOX-DC 480V DISTRIBUTION SINGLE-LINE DIAGRAM (IEC 61082-1)'
        '</text>',
        '  <text x="48" y="88" font-family="monospace" font-size="13" fill="#334155">'
        'STATUS: ACTIVE REVISION | AUTHORITATIVE FEEDER SCHEDULE'
        '</text>',
        '  <text x="860" y="60" font-family="monospace" font-size="13" font-weight="bold" fill="#0F172A">'
        'REV: ACTIVE REVISION'
        '</text>',
        '  <text x="860" y="88" font-family="monospace" font-size="12" fill="#334155">'
        'SHEET 1 OF 1 | SCALE: NTS'
        '</text>',
        '  <!-- Horizontal Feeder Buses -->',
        f'  <text x="{BUS_X_MIN}" y="{F1_BUS_Y - 16}" font-family="monospace" font-size="14" font-weight="bold" fill="#0F172A">'
        'F1 BUS — 44A LIMIT'
        '</text>',
        f'  <line id="bus-F1" x1="{BUS_X_MIN}" y1="{F1_BUS_Y}" x2="{BUS_X_MAX}" y2="{F1_BUS_Y}" '
        'stroke="#0F172A" stroke-width="4" />',
        f'  <text x="{BUS_X_MIN}" y="{F2_BUS_Y - 16}" font-family="monospace" font-size="14" font-weight="bold" fill="#0F172A">'
        'F2 BUS — 50A LIMIT'
        '</text>',
        f'  <line id="bus-F2" x1="{BUS_X_MIN}" y1="{F2_BUS_Y}" x2="{BUS_X_MAX}" y2="{F2_BUS_Y}" '
        'stroke="#0F172A" stroke-width="4" />',
    ]

    for rack_id in ("N1", "N2", "N3", "N4"):
        x = RACK_COLUMNS[rack_id]
        connected_bus = mapping[rack_id]
        load_label = RACK_LOAD_LABELS[rack_id]
        sensor_label = RACK_SENSOR_LABELS[rack_id]

        lines.append(f'  <!-- Column {rack_id} (x={x}) -->')
        if connected_bus == "F1":
            # Junction dot on F1 (y=210), vertical drop to y=278, bridge arch over F2 (y=278..302), drop to switch y=370
            lines.append(
                f'  <circle class="junction-dot" cx="{x}" cy="{F1_BUS_Y}" r="{JUNCTION_DOT_RADIUS}" fill="#0F172A" />'
            )
            lines.append(
                f'  <line x1="{x}" y1="{F1_BUS_Y}" x2="{x}" y2="{ARCH_Y_TOP}" stroke="#0F172A" stroke-width="3" />'
            )
            lines.append(
                f'  <path class="crossing-arch" d="M {x} {ARCH_Y_TOP} A {ARCH_RADIUS} {ARCH_RADIUS} 0 0 1 {x} {ARCH_Y_BOTTOM}" '
                'fill="none" stroke="#0F172A" stroke-width="3" />'
            )
            lines.append(
                f'  <line x1="{x}" y1="{ARCH_Y_BOTTOM}" x2="{x}" y2="{SWITCH_Y_TOP}" stroke="#0F172A" stroke-width="3" />'
            )
        else:
            # Junction dot on F2 (y=290), vertical drop directly to switch y=370 (no connection or arch at F1)
            lines.append(
                f'  <circle class="junction-dot" cx="{x}" cy="{F2_BUS_Y}" r="{JUNCTION_DOT_RADIUS}" fill="#0F172A" />'
            )
            lines.append(
                f'  <line x1="{x}" y1="{F2_BUS_Y}" x2="{x}" y2="{SWITCH_Y_TOP}" stroke="#0F172A" stroke-width="3" />'
            )

        # Branch switch / breaker symbol around y=390 (370..410)
        lines.append(
            f'  <circle cx="{x}" cy="{SWITCH_Y_TOP}" r="4" fill="#FFFFFF" stroke="#0F172A" stroke-width="2" />'
        )
        lines.append(
            f'  <circle cx="{x}" cy="{SWITCH_Y_BOTTOM}" r="4" fill="#FFFFFF" stroke="#0F172A" stroke-width="2" />'
        )
        lines.append(
            f'  <line x1="{x}" y1="{SWITCH_Y_BOTTOM}" x2="{x - 14}" y2="{SWITCH_Y_TOP + 4}" stroke="#0F172A" stroke-width="3" />'
        )
        lines.append(
            f'  <text x="{x + 14}" y="{SWITCH_Y_CENTER + 4}" font-family="monospace" font-size="12" fill="#334155">CB-{rack_id}</text>'
        )

        # Conductor from bottom of switch (y=410) to top of rack load box (y=520)
        lines.append(
            f'  <line x1="{x}" y1="{SWITCH_Y_BOTTOM}" x2="{x}" y2="{RACK_BOX_Y_TOP}" stroke="#0F172A" stroke-width="3" />'
        )

        # Rack load box y=520..632
        rx1 = x - RACK_BOX_HALF_W
        rw = 2 * RACK_BOX_HALF_W
        rh = RACK_BOX_Y_BOTTOM - RACK_BOX_Y_TOP
        lines.append(
            f'  <rect x="{rx1}" y="{RACK_BOX_Y_TOP}" width="{rw}" height="{rh}" '
            'fill="#FFFFFF" stroke="#0F172A" stroke-width="2" />'
        )
        lines.append(
            f'  <text x="{x}" y="{RACK_BOX_Y_TOP + 42}" text-anchor="middle" font-family="monospace" '
            f'font-size="14" font-weight="bold" fill="#0F172A">RACK {rack_id}</text>'
        )
        lines.append(
            f'  <text x="{x}" y="{RACK_BOX_Y_TOP + 76}" text-anchor="middle" font-family="monospace" '
            f'font-size="12" fill="#334155">{load_label}</text>'
        )

        # Tie line from rack box (y=632) to sensor box (y=652)
        lines.append(
            f'  <line x1="{x}" y1="{RACK_BOX_Y_BOTTOM}" x2="{x}" y2="{SENSOR_BOX_Y_TOP}" '
            'stroke="#334155" stroke-width="2" stroke-dasharray="4,3" />'
        )

        # Sensor box y=652..702
        sx1 = x - SENSOR_BOX_HALF_W
        sw = 2 * SENSOR_BOX_HALF_W
        sh = SENSOR_BOX_Y_BOTTOM - SENSOR_BOX_Y_TOP
        lines.append(
            f'  <rect x="{sx1}" y="{SENSOR_BOX_Y_TOP}" width="{sw}" height="{sh}" '
            'fill="#F1F5F9" stroke="#334155" stroke-width="2" />'
        )
        lines.append(
            f'  <text x="{x}" y="{SENSOR_BOX_Y_TOP + 30}" text-anchor="middle" font-family="monospace" '
            f'font-size="13" font-weight="bold" fill="#0F172A">SENSOR: {sensor_label}</text>'
        )

    # Legend and Footer (identical across cases)
    lines.extend(
        [
            '  <!-- Legend & Footer -->',
            '  <rect x="24" y="728" width="1232" height="48" fill="#E2E8F0" stroke="#334155" stroke-width="2" />',
            '  <text x="48" y="757" font-family="monospace" font-size="13" font-weight="bold" fill="#0F172A">'
            'LEGEND: FILLED DOT = BUS CONNECTION | BRIDGE ARCH = NON-CONNECTED CROSSING'
            '</text>',
            '  <text x="760" y="757" font-family="monospace" font-size="13" font-weight="bold" fill="#334155">'
            'TRAINING / SIMULATION — NOT FOR CONSTRUCTION'
            '</text>',
            '</svg>',
        ]
    )
    return "\n".join(lines) + "\n"


def render_sld_png(
    case_id: str,
    output_path: Path,
    svg_output_path: Optional[Path] = None,
    mapping: Optional[Mapping[str, str]] = None,
) -> RenderedSLDPath:
    """Render both the deterministic SVG and the 1280x800 24-bit RGB PNG for the given case."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if mapping is not None:
        mapping = _validate_explicit_mapping(mapping)
        canonical_id = case_id.strip()
    else:
        mapping = get_case_sld_mapping(case_id)
        canonical_id = "case_A" if case_id.strip() in ("case_A", "A") else "case_B"

    svg_text = render_sld_svg(case_id, mapping=mapping)
    svg_path = Path(svg_output_path) if svg_output_path is not None else output_path.with_suffix(".svg")
    svg_path.parent.mkdir(parents=True, exist_ok=True)
    svg_path.write_text(svg_text, encoding="utf-8")

    img = Image.new("RGB", (SLD_WIDTH, SLD_HEIGHT), color=BG_COLOR)
    draw = ImageDraw.Draw(img)
    try:
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
        font_bold = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 13)
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
    except OSError:
        font_title = ImageFont.load_default()
        font_bold = font_title
        font = font_title

    # Outer border & title block (identical across Case A and Case B)
    draw.rectangle([24, 24, 1256, 776], outline=SUB_INK_COLOR, width=2)
    draw.rectangle([24, 24, 1256, 108], fill=HEADER_BG_COLOR, outline=SUB_INK_COLOR, width=2)
    draw.text(
        (48, 44),
        "BLACKBOX-DC 480V DISTRIBUTION SINGLE-LINE DIAGRAM (IEC 61082-1)",
        fill=INK_COLOR,
        font=font_title,
    )
    draw.text(
        (48, 72),
        "STATUS: ACTIVE REVISION | AUTHORITATIVE FEEDER SCHEDULE",
        fill=SUB_INK_COLOR,
        font=font,
    )
    draw.text((860, 44), "REV: ACTIVE REVISION", fill=INK_COLOR, font=font_bold)
    draw.text((860, 72), "SHEET 1 OF 1 | SCALE: NTS", fill=SUB_INK_COLOR, font=font)

    # Horizontal Feeder Buses F1 (y=210) and F2 (y=290)
    draw.text((48, F1_BUS_Y - 24), "F1 BUS — 44A LIMIT", fill=INK_COLOR, font=font_bold)
    draw.line([(48, F1_BUS_Y), (BUS_X_MAX, F1_BUS_Y)], fill=INK_COLOR, width=3)

    draw.text((48, F2_BUS_Y - 24), "F2 BUS — 50A LIMIT", fill=INK_COLOR, font=font_bold)
    draw.line([(48, F2_BUS_Y), (BUS_X_MAX, F2_BUS_Y)], fill=INK_COLOR, width=3)

    # Vertical rack drops, connection dots, crossing arches, switches, rack boxes, sensor boxes
    for rack_id in ("N1", "N2", "N3", "N4"):
        x = RACK_COLUMNS[rack_id]
        connected_bus = mapping[rack_id]

        if connected_bus == "F1":
            # Filled junction dot on F1 (y=210)
            r = JUNCTION_DOT_RADIUS
            draw.ellipse([x - r, F1_BUS_Y - r, x + r, F1_BUS_Y + r], fill=INK_COLOR, outline=INK_COLOR)
            # Conductor from F1 (y=210) to top of arch (y=278)
            draw.line([(x, F1_BUS_Y), (x, ARCH_Y_TOP)], fill=INK_COLOR, width=3)
            # Semicircular bridge arch jumping over F2 (y=278..302, bulging right to x + 12)
            # Draw with explicit polyline segments along the right semicircle so pixel locations are exact
            arch_pts = []
            for deg in range(-90, 91, 6):
                rad = math.radians(deg)
                px = int(round(x + ARCH_RADIUS * math.cos(rad)))
                py = int(round(F2_BUS_Y + ARCH_RADIUS * math.sin(rad)))
                arch_pts.append((px, py))
            draw.line(arch_pts, fill=INK_COLOR, width=3)
            # Conductor from bottom of arch (y=302) to switch top (y=370)
            draw.line([(x, ARCH_Y_BOTTOM), (x, SWITCH_Y_TOP)], fill=INK_COLOR, width=3)
        else:
            # Filled junction dot on F2 (y=290), NO dot on F1 and NO conductor above y=290
            r = JUNCTION_DOT_RADIUS
            draw.ellipse([x - r, F2_BUS_Y - r, x + r, F2_BUS_Y + r], fill=INK_COLOR, outline=INK_COLOR)
            draw.line([(x, F2_BUS_Y), (x, SWITCH_Y_TOP)], fill=INK_COLOR, width=3)

        # Branch switch / breaker symbol at y=370..410
        draw.line([(x, SWITCH_Y_BOTTOM), (x - 14, SWITCH_Y_TOP + 4)], fill=INK_COLOR, width=3)
        draw.ellipse(
            [x - 4, SWITCH_Y_TOP - 4, x + 4, SWITCH_Y_TOP + 4],
            fill=BOX_FILL_COLOR,
            outline=INK_COLOR,
            width=2,
        )
        draw.ellipse(
            [x - 4, SWITCH_Y_BOTTOM - 4, x + 4, SWITCH_Y_BOTTOM + 4],
            fill=BOX_FILL_COLOR,
            outline=INK_COLOR,
            width=2,
        )
        draw.text((x + 14, SWITCH_Y_CENTER - 6), f"CB-{rack_id}", fill=SUB_INK_COLOR, font=font)

        # Conductor from switch bottom (y=410) to rack box top (y=520)
        draw.line([(x, SWITCH_Y_BOTTOM + 4), (x, RACK_BOX_Y_TOP)], fill=INK_COLOR, width=3)

        # Rack load box y=520..632
        draw.rectangle(
            [x - RACK_BOX_HALF_W, RACK_BOX_Y_TOP, x + RACK_BOX_HALF_W, RACK_BOX_Y_BOTTOM],
            fill=BOX_FILL_COLOR,
            outline=INK_COLOR,
            width=2,
        )
        draw.text((x, RACK_BOX_Y_TOP + 36), f"RACK {rack_id}", fill=INK_COLOR, font=font_bold, anchor="mm")
        draw.text(
            (x, RACK_BOX_Y_TOP + 74),
            RACK_LOAD_LABELS[rack_id],
            fill=SUB_INK_COLOR,
            font=font,
            anchor="mm",
        )

        # Connector line from rack box (y=632) to sensor box (y=652)
        draw.line([(x, RACK_BOX_Y_BOTTOM), (x, SENSOR_BOX_Y_TOP)], fill=SUB_INK_COLOR, width=2)

        # Sensor box y=652..702
        draw.rectangle(
            [x - SENSOR_BOX_HALF_W, SENSOR_BOX_Y_TOP, x + SENSOR_BOX_HALF_W, SENSOR_BOX_Y_BOTTOM],
            fill=SENSOR_FILL_COLOR,
            outline=SUB_INK_COLOR,
            width=2,
        )
        draw.text(
            (x, SENSOR_BOX_Y_TOP + 25),
            f"SENSOR: {RACK_SENSOR_LABELS[rack_id]}",
            fill=INK_COLOR,
            font=font_bold,
            anchor="mm",
        )

    # Footer block (identical across Case A and Case B)
    draw.rectangle([24, 728, 1256, 776], fill=HEADER_BG_COLOR, outline=SUB_INK_COLOR, width=2)
    draw.text(
        (48, 744),
        "LEGEND: FILLED DOT = BUS CONNECTION | BRIDGE ARCH = NON-CONNECTED CROSSING",
        fill=INK_COLOR,
        font=font_bold,
    )
    draw.text(
        (780, 744),
        "TRAINING / SIMULATION — NOT FOR CONSTRUCTION",
        fill=SUB_INK_COLOR,
        font=font_bold,
    )

    img.save(output_path, format="PNG", optimize=True)
    png_bytes = output_path.read_bytes()
    digest = hashlib.sha256(png_bytes).hexdigest()
    svg_digest = hashlib.sha256(svg_text.encode("utf-8")).hexdigest()

    edges = {f"{bus}-{rack}" for rack, bus in mapping.items()}
    meta: Dict[str, Any] = {
        "case_id": canonical_id,
        "output_path": str(output_path),
        "svg_path": str(svg_path),
        "sha256": digest,
        "svg_sha256": svg_digest,
        "width": SLD_WIDTH,
        "height": SLD_HEIGHT,
        "node_to_feeder": dict(mapping),
        "edges": sorted(edges),
    }
    return RenderedSLDPath(output_path, meta=meta)


def parse_sld_svg_topology(svg_text: str) -> Dict[str, Any]:
    """OCR-agnostic SVG geometry parser verifying junction dots and crossing arches."""
    root = ET.fromstring(svg_text)
    dots_by_rack: Dict[str, str] = {}
    arches_on_f2_by_rack: Set[str] = set()

    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag == "circle":
            r_val = float(elem.attrib.get("r", "0"))
            cx = int(round(float(elem.attrib.get("cx", "0"))))
            cy = int(round(float(elem.attrib.get("cy", "0"))))
            if abs(r_val - JUNCTION_DOT_RADIUS) < 1e-6 and cx in COLUMN_TO_RACK:
                rack_id = COLUMN_TO_RACK[cx]
                if cy == F1_BUS_Y:
                    if rack_id in dots_by_rack:
                        raise ValueError(f"Duplicate junction dot on column {rack_id}")
                    dots_by_rack[rack_id] = "F1"
                elif cy == F2_BUS_Y:
                    if rack_id in dots_by_rack:
                        raise ValueError(f"Duplicate junction dot on column {rack_id}")
                    dots_by_rack[rack_id] = "F2"
        elif tag == "path":
            d_attr = elem.attrib.get("d", "").strip()
            # Format: M {x} 278 A 12 12 0 0 1 {x} 302
            parts = d_attr.split()
            if len(parts) >= 11 and parts[0] == "M" and parts[3] == "A":
                x_start = int(round(float(parts[1])))
                y_start = int(round(float(parts[2])))
                x_end = int(round(float(parts[9])))
                y_end = int(round(float(parts[10])))
                if (
                    x_start == x_end
                    and x_start in COLUMN_TO_RACK
                    and y_start == ARCH_Y_TOP
                    and y_end == ARCH_Y_BOTTOM
                ):
                    arches_on_f2_by_rack.add(COLUMN_TO_RACK[x_start])

    if set(dots_by_rack.keys()) != {"N1", "N2", "N3", "N4"}:
        raise ValueError(f"Incomplete junction dots in SVG: {dots_by_rack}")

    for rack_id, bus_id in dots_by_rack.items():
        if bus_id == "F1" and rack_id not in arches_on_f2_by_rack:
            raise ValueError(f"Column {rack_id} connected to F1 is missing crossing arch over F2")
        if bus_id == "F2" and rack_id in arches_on_f2_by_rack:
            raise ValueError(f"Column {rack_id} connected to F2 must not have a crossing arch over F2")

    edges: Set[str] = {f"{bus}-{rack}" for rack, bus in dots_by_rack.items()}
    return {
        "mapping": dict(dots_by_rack),
        "edges": edges,
        "arches_over_F2": sorted(arches_on_f2_by_rack),
    }


def _is_dark_ink(rgb: Tuple[int, int, int]) -> bool:
    """Return True if pixel RGB is dark conductor/dot ink rather than light sheet background."""
    return (rgb[0] + rgb[1] + rgb[2]) < 300


def parse_sld_png_topology(png_path: Path) -> Dict[str, Any]:
    """OCR-agnostic PNG pixel parser inspecting junction dots and crossing arches at (x, 210) and (x, 290)."""
    img = Image.open(png_path)
    if img.mode != "RGB":
        raise ValueError(f"Expected RGB PNG mode, got {img.mode!r}")
    if img.size != (SLD_WIDTH, SLD_HEIGHT):
        raise ValueError(f"Expected PNG size {(SLD_WIDTH, SLD_HEIGHT)}, got {img.size}")

    mapping: Dict[str, str] = {}
    arches_over_f2: Set[str] = set()

    for rack_id, x in RACK_COLUMNS.items():
        # A filled r=7 junction dot at (x, bus_y) colors diagonal pixels (x - 4, bus_y - 4) and (x + 4, bus_y - 4)
        # whereas a vertical wire or horizontal bus alone leaves (x - 4, bus_y - 4) as background color.
        f1_dot = _is_dark_ink(img.getpixel((x - 4, F1_BUS_Y - 4))) and _is_dark_ink(
            img.getpixel((x + 4, F1_BUS_Y + 4))
        )
        f2_dot = _is_dark_ink(img.getpixel((x - 4, F2_BUS_Y - 4))) and _is_dark_ink(
            img.getpixel((x + 4, F2_BUS_Y + 4))
        )

        # Semicircular arch of radius 12 centered at (x, 290) bulges right through (x + 12, 290) and (x + 10, 284)
        has_arch = _is_dark_ink(img.getpixel((x + 10, F2_BUS_Y - 6))) and _is_dark_ink(
            img.getpixel((x + 10, F2_BUS_Y + 6))
        )

        if f1_dot and not f2_dot:
            if not has_arch:
                raise ValueError(f"PNG column {rack_id} (x={x}) has F1 dot but no crossing arch over F2")
            mapping[rack_id] = "F1"
            arches_over_f2.add(rack_id)
        elif f2_dot and not f1_dot:
            if has_arch:
                raise ValueError(f"PNG column {rack_id} (x={x}) has F2 dot and unexpected arch")
            mapping[rack_id] = "F2"
        else:
            raise ValueError(
                f"Ambiguous PNG junction state at column {rack_id} (x={x}): f1_dot={f1_dot}, f2_dot={f2_dot}"
            )

    edges: Set[str] = {f"{bus}-{rack}" for rack, bus in mapping.items()}
    return {
        "mapping": mapping,
        "edges": edges,
        "arches_over_F2": sorted(arches_over_f2),
    }
