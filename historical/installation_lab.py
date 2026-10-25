"""Predecessor installation-v1 physical fault simulation lab (Case A & Case B)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
