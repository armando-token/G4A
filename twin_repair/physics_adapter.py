"""Electrothermal physics adapter for BlackboxDC Twin Repair.

Reuses `blackbox.domain` (`RackParams`, `PDU`, `World`, `Migration`, `simulate_world`) and
implements independent feeder current summation and candidate action evaluation:
- `compute_feeder_currents(graph, rack_currents_a)`
- `build_world_from_graph(graph, public_state)`
- `evaluate_actions(graph, public_state)`
- `recommended_action(outcomes)`
- `compare_stale_and_repaired_actions(stale_graph, repaired_graph, public_state)`
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

from blackbox.domain import (
    AMBIENT_TEMP_C,
    DEFAULT_HORIZON_S,
    DEFAULT_MAX_TEMP_C,
    DEFAULT_MIGRATION_AMPS,
    DEFAULT_STEP_S,
     TOTAL_WORLD_CURRENT_A,
    Migration,
    PDU,
    RackParams,
    World,
    simulate_world,
)
from blackbox.fixture import DEFAULT_ALPHA_PER_C, N4_T0_TEMP_C
from twin_repair.contracts import (
    ActionOutcome,
    CANONICAL_FEEDER_IDS,
    CANONICAL_FEEDER_LIMITS_A,
    CANONICAL_RACK_IDS,
    SceneSpec,
    extract_feeder_graph,
)


DEFAULT_RACK_CURRENTS_A: Dict[str, float] = {
    "N1": 36.0,
    "N2": 4.0,
    "N3": 4.0,
    "N4": 24.0,
}

DEFAULT_RACK_MAX_CURRENTS_A: Dict[str, float] = {
    "N1": 40.0,
    "N2": 16.0,
    "N3": 16.0,
    "N4": 32.0,
}


def _normalize_graph(graph: Union[SceneSpec, Mapping[str, Any]]) -> Dict[str, str]:
    if isinstance(graph, SceneSpec):
        return extract_feeder_graph(graph)
    if not isinstance(graph, Mapping):
        raise ValueError("graph must be a SceneSpec or rack_id -> feeder_id mapping")
    if "edges" in graph and isinstance(graph["edges"], (list, tuple)):
        out: Dict[str, str] = {}
        for item in graph["edges"]:
            if not isinstance(item, Mapping) or "rack_id" not in item or "feeder_id" not in item:
                raise ValueError(f"Malformed edge entry: {item!r}")
            out[str(item["rack_id"])] = str(item["feeder_id"])
    else:
        out = {str(k): str(v) for k, v in graph.items()}

    if set(out.keys()) != set(CANONICAL_RACK_IDS):
        raise ValueError(f"Graph must map exactly {CANONICAL_RACK_IDS}, got {sorted(out.keys())}")
    for rk, fd in out.items():
        if fd not in CANONICAL_FEEDER_IDS:
            raise ValueError(f"Rack {rk} maps to unknown feeder {fd!r}; expected {CANONICAL_FEEDER_IDS}")
    return {rk: out[rk] for rk in CANONICAL_RACK_IDS}


def _extract_rack_currents(public_state: Optional[Mapping[str, Any]] = None) -> Dict[str, float]:
    if public_state is None:
        return dict(DEFAULT_RACK_CURRENTS_A)
    raw_racks = public_state.get("racks", public_state.get("rack_currents_a"))
    if raw_racks is None:
        return dict(DEFAULT_RACK_CURRENTS_A)

    currents: Dict[str, float] = {}
    if isinstance(raw_racks, Mapping):
        for rk, val in raw_racks.items():
            if isinstance(val, Mapping):
                c = float(val.get("current_a", val.get("current_A", DEFAULT_RACK_CURRENTS_A[str(rk)])))
            else:
                c = float(val)
            currents[str(rk)] = c
    elif isinstance(raw_racks, (list, tuple)):
        for item in raw_racks:
            if isinstance(item, Mapping) and "rack_id" in item:
                rk = str(item["rack_id"])
                c = float(item.get("current_a", item.get("current_A", DEFAULT_RACK_CURRENTS_A[rk])))
                currents[rk] = c

    if set(currents.keys()) != set(CANONICAL_RACK_IDS):
        raise ValueError(f"rack_currents_a must contain exactly {CANONICAL_RACK_IDS}, got {sorted(currents.keys())}")
    return {rk: float(currents[rk]) for rk in CANONICAL_RACK_IDS}


def _extract_feeder_limits(public_state: Optional[Mapping[str, Any]] = None) -> Dict[str, float]:
    limits = dict(CANONICAL_FEEDER_LIMITS_A)
    if public_state is None:
        return limits
    raw_feeders = public_state.get("feeders", public_state.get("feeder_limits_a"))
    if isinstance(raw_feeders, Mapping):
        for fid, val in raw_feeders.items():
            if str(fid) in limits:
                if isinstance(val, Mapping):
                    limits[str(fid)] = float(val.get("limit_a", val.get("max_current_A", limits[str(fid)])))
                else:
                    limits[str(fid)] = float(val)
    elif isinstance(raw_feeders, (list, tuple)):
        for item in raw_feeders:
            if isinstance(item, Mapping) and "feeder_id" in item:
                fid = str(item["feeder_id"])
                if fid in limits:
                    limits[fid] = float(item.get("limit_a", item.get("max_current_A", limits[fid])))
    return limits


def compute_feeder_currents(
    graph: Union[SceneSpec, Mapping[str, Any]],
    rack_currents_a: Optional[Mapping[str, float]] = None,
) -> Dict[str, float]:
    """Independently sum rack currents per feeder and verify 68.0 A total current conservation."""
    norm_graph = _normalize_graph(graph)
    currents = (
        {rk: float(rack_currents_a[rk]) for rk in CANONICAL_RACK_IDS}
        if rack_currents_a is not None
        else dict(DEFAULT_RACK_CURRENTS_A)
    )
    for rk, val in currents.items():
        if isinstance(val, bool) or not math.isfinite(val) or val < 0.0:
            raise ValueError(f"Invalid current for rack {rk}: {val!r}")

    total_rack_a = sum(currents.values())
    if abs(total_rack_a - TOTAL_WORLD_CURRENT_A) > 1e-6:
        raise ValueError(
            f"Current conservation violated: sum(rack_currents_a)={total_rack_a} A != {TOTAL_WORLD_CURRENT_A} A"
        )

    feeder_totals: Dict[str, float] = {fid: 0.0 for fid in CANONICAL_FEEDER_IDS}
    for rk in CANONICAL_RACK_IDS:
        fid = norm_graph[rk]
        feeder_totals[fid] = float(feeder_totals[fid] + currents[rk])

    total_feeder_a = sum(feeder_totals.values())
    if abs(total_feeder_a - total_rack_a) > 1e-9:
        raise ValueError(
            f"Feeder current conservation violated: sum(feeder_currents)={total_feeder_a} A != {total_rack_a} A"
        )
    return feeder_totals


def build_world_from_graph(
    graph: Union[SceneSpec, Mapping[str, Any]],
    public_state: Optional[Mapping[str, Any]] = None,
) -> World:
    """Construct a `blackbox.domain.World` from a rack->feeder graph and public state."""
    norm_graph = _normalize_graph(graph)
    currents = _extract_rack_currents(public_state)
    limits = _extract_feeder_limits(public_state)
    # Verify conservation before constructing World
    compute_feeder_currents(norm_graph, currents)

    ambient_c = float(
        (public_state or {}).get("ambient_c", AMBIENT_TEMP_C)
    )
    max_temp_c = float(
        (public_state or {}).get("max_temp_c", DEFAULT_MAX_TEMP_C)
    )
    n4_initial_temp_c = float(
        (public_state or {}).get("n4_initial_temp_c", N4_T0_TEMP_C)
    )

    racks = (
        RackParams(
            rack_id="N1",
            pdu_id=norm_graph["N1"],
            current_A=currents["N1"],
            initial_temp_C=36.0,
            R0_ohm=0.005,
            alpha_per_C=DEFAULT_ALPHA_PER_C,
            h_W_per_K=0.5,
            C_J_per_K=8.0,
            max_current_A=DEFAULT_RACK_MAX_CURRENTS_A["N1"],
            max_temp_C=max_temp_c,
        ),
        RackParams(
            rack_id="N2",
            pdu_id=norm_graph["N2"],
            current_A=currents["N2"],
            initial_temp_C=25.0,
            R0_ohm=0.010,
            alpha_per_C=DEFAULT_ALPHA_PER_C,
            h_W_per_K=0.5,
            C_J_per_K=8.0,
            max_current_A=DEFAULT_RACK_MAX_CURRENTS_A["N2"],
            max_temp_C=max_temp_c,
        ),
        RackParams(
            rack_id="N3",
            pdu_id=norm_graph["N3"],
            current_A=currents["N3"],
            initial_temp_C=25.0,
            R0_ohm=0.010,
            alpha_per_C=DEFAULT_ALPHA_PER_C,
            h_W_per_K=0.5,
            C_J_per_K=8.0,
            max_current_A=DEFAULT_RACK_MAX_CURRENTS_A["N3"],
            max_temp_C=max_temp_c,
        ),
        RackParams(
            rack_id="N4",
            pdu_id=norm_graph["N4"],
            current_A=currents["N4"],
            initial_temp_C=n4_initial_temp_c,
            R0_ohm=0.040,
            alpha_per_C=DEFAULT_ALPHA_PER_C,
            h_W_per_K=0.4,
            C_J_per_K=8.0,
            max_current_A=DEFAULT_RACK_MAX_CURRENTS_A["N4"],
            max_temp_C=max_temp_c,
        ),
    )
    pdus = (
        PDU(pdu_id="F1", max_current_A=limits["F1"]),
        PDU(pdu_id="F2", max_current_A=limits["F2"]),
    )
    return World(
        racks=racks,
        pdus=pdus,
        ambient_C=ambient_c,
        horizon_s=DEFAULT_HORIZON_S,
        step_s=DEFAULT_STEP_S,
        allowed_targets=("N2", "N3"),
    )


def evaluate_actions(
    graph: Union[SceneSpec, Mapping[str, Any]],
    public_state: Optional[Mapping[str, Any]] = None,
) -> List[ActionOutcome]:
    """Evaluate `NO_ACTION`, `MIGRATE_N4_TO_N2_8A`, and `MIGRATE_N4_TO_N3_8A` on `graph`."""
    norm_graph = _normalize_graph(graph)
    world = build_world_from_graph(norm_graph, public_state)
    base_currents = _extract_rack_currents(public_state)
    limits = _extract_feeder_limits(public_state)
    max_temp_c = float((public_state or {}).get("max_temp_c", DEFAULT_MAX_TEMP_C))
    action_amps = float((public_state or {}).get("action_size_a", DEFAULT_MIGRATION_AMPS))

    candidates = [
        ("NO_ACTION", None, 0.0, None),
        ("MIGRATE_N4_TO_N2_8A", "N2", action_amps, Migration(source="N4", target="N2", amps=action_amps)),
        ("MIGRATE_N4_TO_N3_8A", "N3", action_amps, Migration(source="N4", target="N3", amps=action_amps)),
    ]

    outcomes: List[ActionOutcome] = []
    for action_id, target_rack, amps, migration in candidates:
        post_currents = dict(base_currents)
        if target_rack is not None:
            post_currents["N4"] = float(post_currents["N4"] - amps)
            post_currents[target_rack] = float(post_currents[target_rack] + amps)

        # Independent feeder summation
        feeder_currents = compute_feeder_currents(norm_graph, post_currents)
        f1_a = float(feeder_currents["F1"])
        f2_a = float(feeder_currents["F2"])
        f1_lim = float(limits["F1"])
        f2_lim = float(limits["F2"])
        feeder_safe = (f1_a <= f1_lim + 1e-9) and (f2_a <= f2_lim + 1e-9)

        # Thermal trajectory simulation via blackbox.domain.simulate_world
        traj = simulate_world(world, migration=migration)
        n4_temp_180s = float(traj.temps_C["N4"][-1])
        thermal_safe = not bool(traj.safety_flags["thermal_violation"]) and (n4_temp_180s < max_temp_c)
        safe = bool(feeder_safe and thermal_safe and bool(traj.safety_flags["safe"]))

        reasons: List[str] = []
        if not feeder_safe:
            if f1_a > f1_lim + 1e-9:
                reasons.append(f"F1 overloaded ({f1_a:.1f} A > {f1_lim:.1f} A limit)")
            if f2_a > f2_lim + 1e-9:
                reasons.append(f"F2 overloaded ({f2_a:.1f} A > {f2_lim:.1f} A limit)")
        if not thermal_safe:
            reasons.append(f"N4 rear connector reaches {n4_temp_180s:.2f} C >= {max_temp_c:.1f} C limit")
        if safe:
            reason_str = (
                f"Safe: F1={f1_a:.1f}/{f1_lim:.1f} A, F2={f2_a:.1f}/{f2_lim:.1f} A, "
                f"N4(180s)={n4_temp_180s:.2f} C < {max_temp_c:.1f} C"
            )
        else:
            reason_str = "Unsafe: " + "; ".join(reasons)

        outcomes.append(
            ActionOutcome(
                action_id=action_id,
                target_rack=target_rack,
                amps=float(amps),
                f1_current_a=round(f1_a, 6),
                f2_current_a=round(f2_a, 6),
                f1_limit_a=round(f1_lim, 6),
                f2_limit_a=round(f2_lim, 6),
                n4_temp_180s_c=round(n4_temp_180s, 6),
                max_temp_limit_c=round(max_temp_c, 6),
                feeder_safe=feeder_safe,
                thermal_safe=thermal_safe,
                safe=safe,
                reason=reason_str,
                rack_currents_a=post_currents,
            )
        )

    return outcomes


def recommended_action(outcomes: Sequence[ActionOutcome]) -> Optional[ActionOutcome]:
    """Return the unique safe offload action if one exists, otherwise None."""
    safe_actions = [o for o in outcomes if o.safe and o.target_rack is not None]
    if len(safe_actions) == 1:
        return safe_actions[0]
    if len(safe_actions) > 1:
        return sorted(safe_actions, key=lambda o: (max(o.f1_current_a / o.f1_limit_a, o.f2_current_a / o.f2_limit_a), o.target_rack or ""))[0]
    return None


def compare_stale_and_repaired_actions(
    stale_graph: Union[SceneSpec, Mapping[str, Any]],
    repaired_graph: Union[SceneSpec, Mapping[str, Any]],
    public_state: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Compare action outcomes on the stale graph versus the post-repair graph."""
    stale_outcomes = evaluate_actions(stale_graph, public_state)
    repaired_outcomes = evaluate_actions(repaired_graph, public_state)
    stale_rec = recommended_action(stale_outcomes)
    repaired_rec = recommended_action(repaired_outcomes)

    repaired_by_target = {o.target_rack: o for o in repaired_outcomes if o.target_rack is not None}
    no_action_outcome = next((o for o in repaired_outcomes if o.target_rack is None), None)

    stale_choice_on_repaired: Optional[ActionOutcome] = None
    if stale_rec is not None and stale_rec.target_rack in repaired_by_target:
        stale_choice_on_repaired = repaired_by_target[stale_rec.target_rack]

    return {
        "stale_recommended_receiver": stale_rec.target_rack if stale_rec is not None else None,
        "stale_recommended_action_id": stale_rec.action_id if stale_rec is not None else None,
        "repaired_recommended_receiver": repaired_rec.target_rack if repaired_rec is not None else None,
        "repaired_recommended_action_id": repaired_rec.action_id if repaired_rec is not None else None,
        "recommendation_flipped": (
            stale_rec is not None
            and repaired_rec is not None
            and stale_rec.target_rack != repaired_rec.target_rack
        ),
        "stale_choice_evaluated_on_repaired_graph": (
            stale_choice_on_repaired.to_dict() if stale_choice_on_repaired is not None else None
        ),
        "repaired_choice_evaluated_on_repaired_graph": (
            repaired_rec.to_dict() if repaired_rec is not None else None
        ),
        "no_action_outcome": no_action_outcome.to_dict() if no_action_outcome is not None else None,
        "stale_outcomes": [o.to_dict() for o in stale_outcomes],
        "repaired_outcomes": [o.to_dict() for o in repaired_outcomes],
    }
