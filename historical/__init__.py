"""Historical predecessor modules preserved from earlier BlackboxDC pilot phases.

Includes:
- legacy_thermal: Predecessor datacenter rack thermal & hotspot calculation model.
- pilot / cli_pilot: Predecessor pilot simulation and CLI runner.
- causal_fixture / causal_pilot: Predecessor causal intervention & DAG identification pilot.
- installation_lab / installation_contracts / installation_pilot: Predecessor installation-v1
  paired diagnostic lab, explicitly preserving the historical audit record where local
  numerical checks passed but Case A investigator turn was incomplete (strict pair gate = False).
"""

from historical.legacy_thermal import (
    RackSpec,
    ThermalState,
    compute_rack_hotspot,
    simulate_thermal_step,
)
from historical.pilot import run_legacy_pilot
from historical.causal_fixture import CausalFixture, build_cooling_dag_fixture
from historical.causal_pilot import run_causal_pilot
from historical.installation_contracts import (
    CaseDiagnostics,
    InstallationPairReport,
    evaluate_strict_pair_gate,
)
from historical.installation_lab import InstallationLab, simulate_installation_case
from historical.installation_pilot import (
    HISTORICAL_INSTALLATION_V1_OUTCOME,
    get_historical_installation_v1_record,
    run_installation_pilot,
)

__all__ = [
    "RackSpec",
    "ThermalState",
    "compute_rack_hotspot",
    "simulate_thermal_step",
    "run_legacy_pilot",
    "CausalFixture",
    "build_cooling_dag_fixture",
    "run_causal_pilot",
    "CaseDiagnostics",
    "InstallationPairReport",
    "evaluate_strict_pair_gate",
    "InstallationLab",
    "simulate_installation_case",
    "HISTORICAL_INSTALLATION_V1_OUTCOME",
    "get_historical_installation_v1_record",
    "run_installation_pilot",
]
