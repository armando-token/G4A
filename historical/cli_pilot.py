"""CLI entrypoint for the predecessor legacy thermal pilot."""

from __future__ import annotations

import argparse
import json
from typing import Any

from historical.pilot import run_legacy_pilot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m historical.cli_pilot",
        description="Run the predecessor BlackboxDC legacy thermal pilot.",
    )
    parser.add_argument("--steps", type=int, default=8, help="Number of simulation steps.")
    parser.add_argument("--supply-temp", type=float, default=18.5, help="Chilled air supply temperature (C).")
    parser.add_argument("--fan-duty", type=float, default=0.65, help="Rack fan duty cycle [0.1..1.0].")
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    args = parser.parse_args(argv)

    result: dict[str, Any] = run_legacy_pilot(
        steps=args.steps,
        supply_temp_c=args.supply_temp,
        fan_duty=args.fan_duty,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            f"[{result['pilot_id']}] steps={result['steps']} "
            f"peak_rack={result['peak_rack_id']} "
            f"peak_hotspot_c={result['peak_hotspot_temp_c']:.2f} "
            f"throttled={result['any_throttled']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
