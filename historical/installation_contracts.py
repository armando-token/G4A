"""Contracts, AST safety checker, and strict pair-gate evaluation for predecessor `installation-v1`.

Provides:
1. Phase 7C (`07_HISTORICAL_RECONSTRUCTION_AND_FULL_PARITY.md`) `installation-v1` contracts:
   - `CANONICAL_V1_ACTIONS`
   - `validate_v1_bbox` and `compute_v1_bbox_iou`
   - `validate_forecast_source_ast` and `execute_safe_forecast_function` (strict AST allowlist
     blocking imports, attributes, loops, lambdas, comprehensions, dunders, and dangerous builtins)
   - `check_critical_dependency_gate`
   - `validate_v1_proposal`, `validate_v1_plan` (recomputing all 3 actions locally via
     `domain.simulate_world` and detecting forged `safe=True` flags), and `validate_v1_audit`
   - `classify_case_outcome` (4-cell outcome classification matrix)
   - `build_verified_spanish_summary` (strictly interpolated Spanish summary with verbatim check)
2. Backward-compatible `CaseDiagnostics`, `InstallationPairReport`, and `evaluate_strict_pair_gate`.
"""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass
import inspect
import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from blackbox.domain import (
    DEFAULT_MIGRATION_AMPS,
    Migration,
    simulate_world,
    step_temperature,
)
from historical.installation_lab import (
    INSTALLATION_V1_CANVAS_SIZE,
    INSTALLATION_V1_LAYOUTS,
    INSTALLATION_V1_N4_HOTSPOT_BBOX,
    INSTALLATION_V1_SCHEMA,
    _normalize_v1_layout,
    build_installation_v1_world,
)


CANONICAL_V1_ACTIONS: Dict[str, Tuple[Optional[str], Optional[str], float]] = {
    "NO_ACTION": (None, None, 0.0),
    "MIGRATE_N4_TO_N2_8A": ("N4", "N2", DEFAULT_MIGRATION_AMPS),
    "MIGRATE_N4_TO_N3_8A": ("N4", "N3", DEFAULT_MIGRATION_AMPS),
}

ALLOWED_AST_CALL_NAMES: frozenset[str] = frozenset(
    {
        "min",
        "max",
        "abs",
        "round",
        "float",
        "int",
        "exp",
        "step_temperature",
        "forecast_step",
    }
)

ALLOWED_AST_NODE_TYPES: Tuple[type, ...] = (
    ast.Module,
    ast.FunctionDef,
    ast.arguments,
    ast.arg,
    ast.Return,
    ast.Assign,
    ast.AnnAssign,
    ast.AugAssign,
    ast.Expr,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.If,
    ast.IfExp,
    ast.Name,
    ast.Load,
    ast.Store,
    ast.Constant,
    ast.Tuple,
    ast.List,
    ast.Dict,
    ast.Subscript,
    ast.Call,
    # Safe arithmetic, boolean, and comparison operators
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.And,
    ast.Or,
    ast.Not,
)


# ---------------------------------------------------------------------------
# Phase 7C: 2D Bounding Box Validation & IoU
# ---------------------------------------------------------------------------


def validate_v1_bbox(
    bbox: Sequence[int | float],
    width: int = INSTALLATION_V1_CANVAS_SIZE[0],
    height: int = INSTALLATION_V1_CANVAS_SIZE[1],
) -> Tuple[int, int, int, int]:
    """Validate a 4-tuple `[x0, y0, x1, y1]` within the 800x500 `installation-v1` canvas."""
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise ValueError(f"Expected 4-element bbox [x0, y0, x1, y1], got {bbox!r}")
    coords: List[int] = []
    for c in bbox:
        if isinstance(c, bool) or not isinstance(c, (int, float)) or not math.isfinite(float(c)):
            raise ValueError(f"Invalid coordinate in bbox: {c!r}")
        coords.append(int(round(float(c))))
    x0, y0, x1, y1 = coords
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError(
            f"bbox {coords} violates {width}x{height} bounds (0 <= x0 < x1 <= {width}, 0 <= y0 < y1 <= {height})"
        )
    return x0, y0, x1, y1


def compute_v1_bbox_iou(
    box_a: Sequence[int | float],
    box_b: Sequence[int | float],
) -> float:
    """Compute exact Intersection-over-Union (IoU) between two 2D bounding boxes."""
    ax0, ay0, ax1, ay1 = validate_v1_bbox(box_a)
    bx0, by0, bx1, by1 = validate_v1_bbox(box_b)

    ix0 = max(ax0, bx0)
    iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1)
    iy1 = min(ay1, by1)
    iw = max(0, ix1 - ix0)
    ih = max(0, iy1 - iy0)
    inter = float(iw * ih)
    if inter <= 0.0:
        return 0.0
    area_a = float((ax1 - ax0) * (ay1 - ay0))
    area_b = float((bx1 - bx0) * (by1 - by0))
    union = area_a + area_b - inter
    return float(inter / union) if union > 0.0 else 0.0


# ---------------------------------------------------------------------------
# Phase 7C: Restricted Forecast-Source AST Checker & Safe Execution
# ---------------------------------------------------------------------------


def validate_forecast_source_ast(source_code: str) -> ast.FunctionDef:
    """Validate that `source_code` is a single pure numerical `step_temperature` function.

    Rejects imports, attribute access (`.`), loops, comprehensions, lambdas, classes,
    try/with/global/nonlocal statements, dunder identifiers, and non-allowlisted calls.
    """
    if not isinstance(source_code, str) or not source_code.strip():
        raise ValueError("Disallowed AST construct in forecast_source: empty source_code")

    try:
        tree = ast.parse(source_code, mode="exec")
    except SyntaxError as exc:
        raise ValueError(f"Disallowed AST construct in forecast_source: syntax error: {exc}") from exc

    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise ValueError(
            "Disallowed AST construct in forecast_source: expected a single top-level FunctionDef"
        )

    fn_def = tree.body[0]
    if fn_def.name not in {"step_temperature", "forecast_step"}:
        raise ValueError(
            f"Disallowed AST construct in forecast_source: function name {fn_def.name!r} "
            "must be 'step_temperature' or 'forecast_step'"
        )
    if fn_def.decorator_list:
        raise ValueError("Disallowed AST construct in forecast_source: decorators are forbidden")

    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED_AST_NODE_TYPES):
            raise ValueError(
                f"Disallowed AST construct in forecast_source: {type(node).__name__} is forbidden"
            )
        if isinstance(node, ast.Name):
            if node.id.startswith("__") or node.id in {
                "eval",
                "exec",
                "open",
                "compile",
                "getattr",
                "setattr",
                "delattr",
                "globals",
                "locals",
                "vars",
                "dir",
                "type",
                "object",
                "input",
                "breakpoint",
            }:
                raise ValueError(
                    f"Disallowed AST construct in forecast_source: forbidden identifier {node.id!r}"
                )
        elif isinstance(node, ast.arg):
            if node.arg.startswith("__"):
                raise ValueError(
                    f"Disallowed AST construct in forecast_source: forbidden dunder arg {node.arg!r}"
                )
            # Strip type annotations before compilation so annotation expressions cannot smuggle calls
            node.annotation = None
        elif isinstance(node, ast.FunctionDef):
            node.returns = None
        elif isinstance(node, ast.AnnAssign):
            node.annotation = ast.Name(id="float", ctx=ast.Load())
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in ALLOWED_AST_CALL_NAMES:
                callee = getattr(node.func, "id", type(node.func).__name__)
                raise ValueError(
                    f"Disallowed AST construct in forecast_source: call to {callee!r} is forbidden"
                )

    return fn_def


def execute_safe_forecast_function(
    source_code: str,
    *,
    t_c: float = 88.95030198217694,
    i_a: float = 24.0,
    ambient_c: float = 25.0,
    r0_ohm: float = 0.04,
    alpha: float = 0.004,
    h_w_k: float = 0.4,
    heat_capacity_j_k: float = 8.0,
    dt_s: float = 10.0,
) -> float:
    """Validate `source_code` AST, execute it in a locked-down namespace, and verify vs `domain.step_temperature`."""
    fn_def = validate_forecast_source_ast(source_code)
    clean_module = ast.Module(body=[fn_def], type_ignores=[])
    ast.fix_missing_locations(clean_module)
    code_obj = compile(clean_module, filename="<safe_forecast_ast>", mode="exec")

    safe_globals: Dict[str, Any] = {
        "__builtins__": {
            "min": min,
            "max": max,
            "abs": abs,
            "round": round,
            "float": float,
            "int": int,
        },
        "exp": math.exp,
        "step_temperature": step_temperature,
    }
    local_ns: Dict[str, Any] = {}
    exec(code_obj, safe_globals, local_ns)
    fn = local_ns[fn_def.name]

    result = float(
        fn(
            t_c=t_c,
            i_a=i_a,
            ambient_c=ambient_c,
            r0_ohm=r0_ohm,
            alpha=alpha,
            h_w_k=h_w_k,
            heat_capacity_j_k=heat_capacity_j_k,
            dt_s=dt_s,
        )
    )
    if not math.isfinite(result):
        raise ValueError(f"Non-finite forecast output: {result!r}")

    expected = step_temperature(
        t_c=t_c,
        i_a=i_a,
        ambient_c=ambient_c,
        r0_ohm=r0_ohm,
        alpha=alpha,
        h_w_k=h_w_k,
        heat_capacity_j_k=heat_capacity_j_k,
        dt_s=dt_s,
    )
    if abs(result - expected) > 1.5:
        raise ValueError(
            f"Forecast function output {result:.8f} diverges from domain.step_temperature {expected:.8f}"
        )
    return result


# ---------------------------------------------------------------------------
# Phase 7C: Critical Dependency Gate & V1 Contract Validators
# ---------------------------------------------------------------------------


def check_critical_dependency_gate(
    unknown_critical_edges: Sequence[str],
    selected_action_id: str,
    decision: str,
) -> Dict[str, Any]:
    """Enforce abstention (`NO_ACTION`, `abstain`) when critical receiver edges (`N2` or `N3`) are unknown."""
    norm_unknown = sorted({str(x).strip().upper() for x in (unknown_critical_edges or []) if str(x).strip()})
    critical_unknown = [rk for rk in norm_unknown if rk in {"N2", "N3", "N4", "F1", "F2"}]
    blocking = len(critical_unknown) > 0

    act = str(selected_action_id).strip().upper()
    dec = str(decision).strip().lower()

    if blocking:
        passed = act == "NO_ACTION" and dec == "abstain"
        reason = (
            "Critical edges unknown; safely abstained with NO_ACTION."
            if passed
            else f"Violation: unknown_critical_edges={critical_unknown} requires selected_action_id='NO_ACTION' and decision='abstain', got ({act!r}, {dec!r})"
        )
    else:
        passed = (
            (dec == "intervene" and act in {"MIGRATE_N4_TO_N2_8A", "MIGRATE_N4_TO_N3_8A"})
            or (dec == "abstain" and act == "NO_ACTION")
        )
        reason = "No blocking critical edge uncertainty." if passed else f"Inconsistent (action={act!r}, decision={dec!r})"

    return {
        "passed": bool(passed),
        "blocking_uncertainty": bool(blocking),
        "unknown_critical_edges": norm_unknown,
        "selected_action_id": act,
        "decision": dec,
        "reason": reason,
    }


def validate_v1_proposal(
    payload: Mapping[str, Any],
    *,
    layout: str = "A",
) -> Dict[str, Any]:
    """Validate an `installation-v1` Turn 1 (`proposal`) payload."""
    norm_layout = _normalize_v1_layout(layout)
    expected_power = INSTALLATION_V1_LAYOUTS[norm_layout]["power_edges"]
    expected_sensors = INSTALLATION_V1_LAYOUTS[norm_layout]["sensor_edges"]
    errors: List[str] = []

    if str(payload.get("schema_version")) != INSTALLATION_V1_SCHEMA:
        errors.append(f"Expected schema_version={INSTALLATION_V1_SCHEMA!r}, got {payload.get('schema_version')!r}")

    hotspot_rack = str(payload.get("hotspot_rack", "")).strip().upper()
    if hotspot_rack != "N4":
        errors.append(f"Expected hotspot_rack='N4', got {hotspot_rack!r}")

    try:
        bbox = validate_v1_bbox(payload.get("hotspot_bbox", []))
        iou = compute_v1_bbox_iou(bbox, INSTALLATION_V1_N4_HOTSPOT_BBOX)
        if iou < 0.30:
            errors.append(f"Hotspot bbox IoU {iou:.3f} < 0.30 against {INSTALLATION_V1_N4_HOTSPOT_BBOX}")
    except Exception as exc:
        iou = 0.0
        errors.append(f"Invalid hotspot_bbox: {exc}")

    power_edges = dict(payload.get("power_edges") or {})
    if power_edges != expected_power:
        errors.append(f"power_edges {power_edges} != expected {expected_power}")

    sensor_edges = dict(payload.get("sensor_edges") or {})
    if sensor_edges != expected_sensors:
        errors.append(f"sensor_edges {sensor_edges} != expected {expected_sensors}")

    meas = dict(payload.get("selected_measurement") or {})
    if meas.get("type") != "voltage_drop" or meas.get("target") != "N4":
        errors.append(f"selected_measurement must be ('voltage_drop', 'N4'), got {meas}")

    return {
        "valid": len(errors) == 0,
        "layout": norm_layout,
        "bbox_iou": round(float(iou), 4),
        "errors": errors,
    }


def validate_v1_plan(
    payload: Mapping[str, Any],
    *,
    layout: str = "A",
) -> Dict[str, Any]:
    """Validate an `installation-v1` Turn 2 (`plan`) payload by locally recomputing all 3 actions.

    Never trusts the model's boolean `safe` flag: recomputes every candidate action on
    `build_installation_v1_world(layout)` and flags `safety_flag_forged=True` if any
    claimed `safe` flag contradicts `domain.simulate_world`.
    """
    norm_layout = _normalize_v1_layout(layout)
    layout_spec = INSTALLATION_V1_LAYOUTS[norm_layout]
    world = build_installation_v1_world(norm_layout, fault_type="electrical")
    errors: List[str] = []

    if str(payload.get("schema_version")) != INSTALLATION_V1_SCHEMA:
        errors.append(f"Expected schema_version={INSTALLATION_V1_SCHEMA!r}")

    # 1. Validate forecast_source AST & execution if provided
    forecast_src = payload.get("forecast_source")
    if forecast_src is not None:
        try:
            execute_safe_forecast_function(str(forecast_src))
        except Exception as exc:
            errors.append(f"forecast_source validation failed: {exc}")
    else:
        errors.append("Missing forecast_source")

    # 2. Check critical dependency gate
    unknown_edges = list(payload.get("unknown_critical_edges") or [])
    selected_action_id = str(payload.get("selected_action_id", "")).strip().upper()
    decision = str(payload.get("decision", "")).strip().lower()
    dep_gate = check_critical_dependency_gate(unknown_edges, selected_action_id, decision)
    if not dep_gate["passed"]:
        errors.append(dep_gate["reason"])

    # 3. Independently simulate all 3 candidate actions & detect forged safety flags
    preds_by_action = dict(payload.get("predictions_by_action") or {})
    recomputed_by_action: Dict[str, Dict[str, Any]] = {}
    safety_flag_forged = False
    max_temp_rmse_c = 0.0

    for aid, (src, dst, amps) in CANONICAL_V1_ACTIONS.items():
        mig = None if src is None or dst is None else Migration(src, dst, amps)
        sim = simulate_world(world, migration=mig)
        true_safe = bool(sim.safety_flags["safe"])
        true_n4_t180 = float(sim.temps_C["N4"][-1])
        true_f1 = float(sim.pdu_currents_A["F1"][-1])
        true_f2 = float(sim.pdu_currents_A["F2"][-1])

        recomputed_by_action[aid] = {
            "safe": true_safe,
            "thermal_violation": bool(sim.safety_flags["thermal_violation"]),
            "pdu_overload": bool(sim.safety_flags["pdu_overload"]),
            "n4_temp_t180_c": round(true_n4_t180, 4),
            "f1_current_a": round(true_f1, 2),
            "f2_current_a": round(true_f2, 2),
        }

        if aid not in preds_by_action:
            errors.append(f"Missing prediction for action {aid}")
            continue

        claimed = dict(preds_by_action[aid])
        claimed_safe = bool(claimed.get("safe", False))
        if claimed_safe != true_safe:
            safety_flag_forged = True
            errors.append(
                f"Forged or incorrect safety flag for {aid}: claimed safe={claimed_safe}, true safe={true_safe}"
            )

        if "n4_temp_t180_c" in claimed:
            err_t = abs(float(claimed["n4_temp_t180_c"]) - true_n4_t180)
            if err_t > max_temp_rmse_c:
                max_temp_rmse_c = err_t
            if err_t > 0.35:
                errors.append(f"{aid}: n4_temp_t180_c error {err_t:.3f} C > 0.35 C")

    # 4. Verify selected action is safe when not abstaining
    if not dep_gate["blocking_uncertainty"]:
        expected_safe_action = layout_spec["safe_action_id"]
        if selected_action_id != expected_safe_action or decision != "intervene":
            errors.append(
                f"Expected selected_action_id={expected_safe_action!r} and decision='intervene', "
                f"got ({selected_action_id!r}, {decision!r})"
            )

    return {
        "valid": len(errors) == 0,
        "layout": norm_layout,
        "safety_flag_forged": safety_flag_forged,
        "dependency_gate": dep_gate,
        "recomputed_by_action": recomputed_by_action,
        "max_temp_error_c": round(max_temp_rmse_c, 6),
        "errors": errors,
    }


def validate_v1_audit(
    payload: Mapping[str, Any],
    plan_validation: Mapping[str, Any],
) -> Dict[str, Any]:
    """Validate an `installation-v1` Turn 3 (`auditor`) payload against `plan_validation`."""
    errors: List[str] = []
    verdict = str(payload.get("verdict", "")).strip().lower()
    plan_valid = bool(plan_validation.get("valid", False))
    forged = bool(plan_validation.get("safety_flag_forged", False))
    blocking = bool((plan_validation.get("dependency_gate") or {}).get("blocking_uncertainty", False))

    if forged or not plan_valid:
        if verdict not in {"reject", "abstain"}:
            errors.append(f"Auditor must reject/abstain on invalid or forged plan, got verdict={verdict!r}")
    elif blocking:
        if verdict != "abstain":
            errors.append(f"Auditor must abstain when critical edges are unknown, got verdict={verdict!r}")
    else:
        if verdict != "accept":
            errors.append(f"Auditor should accept valid safe plan, got verdict={verdict!r}")

    return {
        "valid": len(errors) == 0,
        "verdict": verdict,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Phase 7C: 4-Cell Outcome Classification Matrix & Verified Spanish Summary
# ---------------------------------------------------------------------------


def classify_case_outcome(
    numeric_visual_valid: bool,
    turns_completed: bool,
) -> str:
    """Classify a single case run into the 4-cell Phase 7C outcome matrix."""
    nv = bool(numeric_visual_valid)
    tc = bool(turns_completed)
    if nv and tc:
        return "strict case pass"
    if nv and not tc:
        return "numerical evidence only; operational incomplete"
    if not nv and tc:
        return "completed but scientific fail"
    return "incomplete and invalid"


def build_verified_spanish_summary(
    case_id: str,
    selected_action_id: str,
    n4_temp_180s_c: float,
    f1_amps: float,
    f2_amps: float,
    classification_label: str,
) -> str:
    """Build a Spanish diagnostic summary strictly interpolated from verified numbers."""
    t_str = f"{float(n4_temp_180s_c):.2f}"
    f1_str = f"{float(f1_amps):.1f}"
    f2_str = f"{float(f2_amps):.1f}"
    act_str = str(selected_action_id).strip()
    cid_str = str(case_id).strip()
    cls_str = str(classification_label).strip()

    summary = (
        f"Caso {cid_str}: acción seleccionada {act_str} con temperatura final en N4 de "
        f"{t_str} °C a t=180s, corrientes de barra F1={f1_str} A (límite 44.0 A) y "
        f"F2={f2_str} A (límite 50.0 A); clasificación: {cls_str}."
    )

    for required_token in (cid_str, act_str, t_str, f1_str, f2_str, cls_str):
        if required_token not in summary:
            raise ValueError(f"Spanish summary interpolation check failed: missing {required_token!r}")
    return summary


# ---------------------------------------------------------------------------
# Backward-Compatible Phase 00 Contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseDiagnostics:
    """Single-case outcome contract for installation-v1 (Case A or Case B)."""

    case_id: str
    fault_mode: str
    local_numerical_check_passed: bool
    investigator_turn_complete: bool
    root_cause_identified: bool
    notes: str = ""

    @property
    def case_passed(self) -> bool:
        """A single case passes only if both numerical checks and the investigator turn succeed."""
        return (
            self.local_numerical_check_passed
            and self.investigator_turn_complete
            and self.root_cause_identified
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["case_passed"] = self.case_passed
        return payload


@dataclass(frozen=True)
class InstallationPairReport:
    """Paired evaluation contract for installation-v1 (Case A + Case B)."""

    benchmark_id: str
    case_a: CaseDiagnostics
    case_b: CaseDiagnostics
    local_numerical_checks_succeeded: bool
    strict_pair_gate_passed: bool
    outcome_label: str
    audit_summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "benchmark_id": self.benchmark_id,
            "case_a": self.case_a.to_dict(),
            "case_b": self.case_b.to_dict(),
            "local_numerical_checks_succeeded": self.local_numerical_checks_succeeded,
            "strict_pair_gate_passed": self.strict_pair_gate_passed,
            "outcome_label": self.outcome_label,
            "audit_summary": self.audit_summary,
        }


def evaluate_strict_pair_gate(
    case_a: CaseDiagnostics,
    case_b: CaseDiagnostics,
    benchmark_id: str = "installation-v1",
) -> InstallationPairReport:
    """Evaluate the strict paired gate across Case A and Case B.

    The strict pair gate requires BOTH Case A and Case B to have:
    - `local_numerical_check_passed == True`
    - `investigator_turn_complete == True`
    - `root_cause_identified == True`
    """
    numerical_ok = case_a.local_numerical_check_passed and case_b.local_numerical_check_passed
    strict_gate = case_a.case_passed and case_b.case_passed

    if strict_gate:
        outcome_label = "STRICT_PAIR_PASS"
        summary = "Both Case A and Case B completed investigator turns and passed numerical checks."
    elif numerical_ok and not (case_a.investigator_turn_complete and case_b.investigator_turn_complete):
        incomplete_cases = [
            c.case_id for c in (case_a, case_b) if not c.investigator_turn_complete
        ]
        outcome_label = "INCOMPLETE_INVESTIGATOR_TURN_PAIR_FAIL"
        summary = (
            f"Local numerical checks succeeded for both cases, but {', '.join(incomplete_cases)} "
            "had an incomplete investigator turn; strict pair gate did NOT pass."
        )
    else:
        outcome_label = "PAIR_FAIL"
        summary = "One or more cases failed numerical verification or root-cause identification."

    return InstallationPairReport(
        benchmark_id=benchmark_id,
        case_a=case_a,
        case_b=case_b,
        local_numerical_checks_succeeded=numerical_ok,
        strict_pair_gate_passed=strict_gate,
        outcome_label=outcome_label,
        audit_summary=summary,
    )
