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
try:
    from historical.pilot import run_legacy_pilot
except ImportError:
    def run_legacy_pilot(*args, **kwargs):  # type: ignore[misc]
        from historical.pilot import run_legacy_pilot as _fn
        return _fn(*args, **kwargs)
from historical.causal_fixture import CausalFixture, build_cooling_dag_fixture
try:
    from historical.causal_pilot import run_causal_pilot
except ImportError:
    def run_causal_pilot(*args, **kwargs):  # type: ignore[misc]
        from historical.causal_pilot import run_causal_pilot as _fn
        return _fn(*args, **kwargs)
from historical.installation_contracts import (
    CANONICAL_V1_ACTIONS,
    CaseDiagnostics,
    InstallationPairReport,
    build_verified_spanish_summary,
    check_critical_dependency_gate,
    classify_case_outcome,
    compute_v1_bbox_iou,
    evaluate_strict_pair_gate,
    execute_safe_forecast_function,
    validate_forecast_source_ast,
    validate_v1_audit,
    validate_v1_bbox,
    validate_v1_plan,
    validate_v1_proposal,
)
from historical.installation_lab import (
    INSTALLATION_V1_CANVAS_SIZE,
    INSTALLATION_V1_LAYOUTS,
    INSTALLATION_V1_N4_HOTSPOT_BBOX,
    INSTALLATION_V1_SCHEMA,
    InstallationLab,
    build_installation_v1_public_bundle,
    build_installation_v1_world,
    permute_installation_labels,
    render_installation_v1_layout_png,
    render_installation_v1_thermal_png,
    simulate_installation_case,
)
from historical.installation_pilot import (
    HISTORICAL_INSTALLATION_V1_OUTCOME,
    SimulatedInstallationV1Client,
    build_v1_revision_dossier,
    build_v1_state_transfer_packet,
    get_historical_installation_v1_record,
    recover_interrupted_v1_run,
    run_installation_pilot,
    run_installation_v1_campaign,
    verify_v1_public_boundary,
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
    "CANONICAL_V1_ACTIONS",
    "CaseDiagnostics",
    "InstallationPairReport",
    "build_verified_spanish_summary",
    "check_critical_dependency_gate",
    "classify_case_outcome",
    "compute_v1_bbox_iou",
    "evaluate_strict_pair_gate",
    "execute_safe_forecast_function",
    "validate_forecast_source_ast",
    "validate_v1_audit",
    "validate_v1_bbox",
    "validate_v1_plan",
    "validate_v1_proposal",
    "INSTALLATION_V1_CANVAS_SIZE",
    "INSTALLATION_V1_LAYOUTS",
    "INSTALLATION_V1_N4_HOTSPOT_BBOX",
    "INSTALLATION_V1_SCHEMA",
    "InstallationLab",
    "build_installation_v1_public_bundle",
    "build_installation_v1_world",
    "permute_installation_labels",
    "render_installation_v1_layout_png",
    "render_installation_v1_thermal_png",
    "simulate_installation_case",
    "HISTORICAL_INSTALLATION_V1_OUTCOME",
    "SimulatedInstallationV1Client",
    "build_v1_revision_dossier",
    "build_v1_state_transfer_packet",
    "get_historical_installation_v1_record",
    "recover_interrupted_v1_run",
    "run_installation_pilot",
    "run_installation_v1_campaign",
    "verify_v1_public_boundary",
]
