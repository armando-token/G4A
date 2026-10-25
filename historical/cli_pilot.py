"""CLI entrypoint and Phase 7B local coding CLI pilot (`historical/cli_pilot.py`).

Provides:
1. Backward-compatible `main(argv)` CLI runner for `run_legacy_pilot`.
2. Phase 7B local CLI discovery, environment sanitization, bounded subprocess execution,
   and scenario separation lab:
   - `discover_coding_cli()`
   - `sanitize_cli_env(env)`
   - `invoke_cli_bounded(prompt, timeout_s=5.0, command=None, env=None)`
   - `compute_prediction_separation(baseline_traj, shifted_traj, min_separation_c=1.0)`
   - `ScenarioLab`
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

from historical.legacy_thermal import (
    ConnectorThermalParams,
    simulate_connector_series,
)
from historical.pilot import run_legacy_pilot, sha256_hex

SENSITIVE_ENV_KEYS: tuple[str, ...] = (
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "AUTHORIZATION",
)

FORBIDDEN_CLI_SECRET_PATTERNS: tuple[str, ...] = (
    "AIza" + "Sy",
    "Bearer ",
    "ya29.",
    "sk-proj-",
    "GOOGLE_API_KEY=",
    "GEMINI_API_KEY=",
)


def discover_coding_cli() -> Dict[str, Any]:
    """Discover installed local coding CLI executable and version metadata."""
    candidates = ("gemini", "agy", "antigravity")
    found_name: Optional[str] = None
    found_path: Optional[str] = None
    for name in candidates:
        resolved = shutil.which(name)
        if resolved:
            found_name = name
            found_path = resolved
            break

    if found_path is None:
        found_name = "python3"
        found_path = sys.executable

    return {
        "cli_name": found_name,
        "executable_path": found_path,
        "python_version": sys.version.split()[0],
        "is_managed_agent_evidence": False,
        "classification": "local_cli_subprocess_only",
    }


def sanitize_cli_env(env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Return a copy of `env` with all cloud/API credentials stripped."""
    source = dict(os.environ) if env is None else dict(env)
    sanitized: Dict[str, str] = {}
    for k, v in source.items():
        ku = str(k).upper()
        if ku in SENSITIVE_ENV_KEYS:
            continue
        if ku.endswith("_API_KEY") or ku.endswith("_SECRET") or "SECRET_KEY" in ku:
            continue
        sanitized[str(k)] = str(v)
    return sanitized


def _assert_no_secrets_in_cli_args(text: str, label: str) -> None:
    for pat in FORBIDDEN_CLI_SECRET_PATTERNS:
        if pat in text:
            raise PermissionError(f"Secret leak blocked in {label}: pattern {pat!r} detected")


def invoke_cli_bounded(
    prompt: str,
    *,
    timeout_s: float = 5.0,
    command: Optional[Sequence[str]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Execute a local CLI command with a sanitized environment, secret guard, and hard timeout."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or float(timeout_s) <= 0.0:
        raise ValueError(f"timeout_s must be > 0, got {timeout_s!r}")

    _assert_no_secrets_in_cli_args(prompt, "prompt")
    cmd_list: List[str] = (
        [str(x) for x in command]
        if command is not None
        else [
            sys.executable,
            "-c",
            "import hashlib, sys; p = sys.argv[-1]; print(hashlib.sha256(p.encode('utf-8')).hexdigest()[:16])",
            prompt,
        ]
    )
    for idx, arg in enumerate(cmd_list):
        _assert_no_secrets_in_cli_args(arg, f"command[{idx}]")

    clean_env = sanitize_cli_env(env)
    for k in SENSITIVE_ENV_KEYS:
        if k in clean_env:
            raise PermissionError(f"Sanitized environment still contains {k}")

    prompt_hash = sha256_hex(prompt)
    t0 = time.monotonic()
    timed_out = False
    try:
        proc = subprocess.run(
            cmd_list,
            env=clean_env,
            capture_output=True,
            text=True,
            timeout=float(timeout_s),
            check=False,
        )
        returncode = int(proc.returncode)
        stdout_str = proc.stdout.strip()
        stderr_str = proc.stderr.strip()
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = -1
        stdout_str = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr_str = "TimeoutExpired"

    elapsed = round(max(0.0, time.monotonic() - t0), 4)
    return {
        "prompt_sha256": prompt_hash,
        "command_redacted": [os.path.basename(cmd_list[0]), *cmd_list[1:-1], "<PROMPT>"],
        "returncode": returncode,
        "stdout": stdout_str,
        "stderr": stderr_str,
        "timed_out": timed_out,
        "elapsed_s": elapsed,
        "sanitized_env_verified": True,
        "is_managed_agent_evidence": False,
    }


def compute_prediction_separation(
    baseline_traj: Sequence[float],
    shifted_traj: Sequence[float],
    min_separation_c: float = 1.0,
) -> Dict[str, Any]:
    """Compute trajectory separation metrics between baseline and parameter-shifted scenarios."""
    b_list = [float(x) for x in baseline_traj]
    s_list = [float(x) for x in shifted_traj]
    if len(b_list) == 0 or len(b_list) != len(s_list):
        raise ValueError("baseline_traj and shifted_traj must be non-empty sequences of equal length")

    diffs = [abs(a - b) for a, b in zip(b_list, s_list)]
    max_diff = max(diffs)
    rmse = math.sqrt(sum(d * d for d in diffs) / len(diffs))
    return {
        "max_abs_diff_c": round(float(max_diff), 6),
        "rmse_diff_c": round(float(rmse), 6),
        "separated": bool(max_diff >= float(min_separation_c)),
    }


class ScenarioLab:
    """Scenario comparison lab evaluating baseline vs shifted connector thermal trajectories."""

    def __init__(
        self,
        baseline_params: Optional[ConnectorThermalParams] = None,
        shifted_params: Optional[ConnectorThermalParams] = None,
        dt_s: float = 10.0,
    ) -> None:
        self.baseline_params = baseline_params or ConnectorThermalParams(
            r_ohm=0.020, h_w_per_k=0.40, c_j_per_k=8.0, ambient_c=25.0
        )
        self.shifted_params = shifted_params or ConnectorThermalParams(
            r_ohm=0.040, h_w_per_k=0.40, c_j_per_k=8.0, ambient_c=25.0
        )
        self.dt_s = float(dt_s)

    def evaluate_scenarios(
        self,
        currents_a: Sequence[float] = (24.0, 24.0, 24.0, 24.0, 24.0, 24.0),
        initial_temp_c: float = 25.0,
    ) -> Dict[str, Any]:
        base_traj = simulate_connector_series(
            initial_temp_c=initial_temp_c,
            currents_a=currents_a,
            params=self.baseline_params,
            dt_s=self.dt_s,
        )
        shift_traj = simulate_connector_series(
            initial_temp_c=initial_temp_c,
            currents_a=currents_a,
            params=self.shifted_params,
            dt_s=self.dt_s,
        )
        sep = compute_prediction_separation(base_traj.temps_c, shift_traj.temps_c)
        payload = {
            "baseline_temps_c": list(base_traj.temps_c),
            "shifted_temps_c": list(shift_traj.temps_c),
            "separation": sep,
            "is_managed_agent_evidence": False,
        }
        payload["lock_sha256"] = sha256_hex(payload)
        return payload


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
