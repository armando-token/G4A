"""Predecessor thermal pilot simulation, append-only Store, 2-experiment Lab, and ManagedRunner.

Provides:
1. Predecessor 3-rack thermal pilot (`run_legacy_pilot`).
2. Phase 7A (`pilot.py`) reconstruction:
   - `canonical_json` and `sha256_hex`
   - `Store`: append-only hash-chained event log (in-memory + optional JSONL file)
   - `validate_load_profile` and `build_experiment_schedule`
   - `Lab`: private truth encapsulation, model registration, pre-experiment prediction
     commitment, 2-experiment cap enforcement, auditor packet builder, and blind numerical audit
   - `ManagedRunner`: adapter around `blackbox.managed` (`resolve_auth_config`,
     `normalize_usage`, `PairBudgetTracker`, `UNKNOWN_TURN_TOKEN_RESERVE`) with explicit
     `mock_counts_as_real = False`, `live_direct_function_calling_supported = False`, and
     cancelled/interrupted interaction token reconciliation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from blackbox.managed import (
    UNKNOWN_USAGE_RESERVE as UNKNOWN_TURN_TOKEN_RESERVE,
    PairBudgetTracker,
    normalize_usage_snapshot as normalize_usage,
    resolve_auth_config,
)
from historical.legacy_thermal import (
    ConnectorThermalParams,
    RackSpec,
    ThermalState,
    ThermalTrajectory,
    compute_rack_hotspot,
    simulate_connector_series,
    simulate_thermal_step,
)


def canonical_json(payload: Any) -> str:
    """Serialize `payload` to canonical deterministic JSON (`sort_keys=True`, compact separators)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def sha256_hex(payload: Any) -> str:
    """Compute the 64-char lowercase SHA-256 hex digest of `payload` (canonical JSON or UTF-8 string)."""
    if isinstance(payload, (bytes, bytearray)):
        raw = bytes(payload)
    elif isinstance(payload, str):
        raw = payload.encode("utf-8")
    else:
        raw = canonical_json(payload).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class Store:
    """Append-only hash-chained event store for pilot lab experiments and commitments."""

    def __init__(self, path: Optional[Path | str] = None) -> None:
        self.path: Optional[Path] = Path(path) if path is not None else None
        self.events: List[Dict[str, Any]] = []
        if self.path is not None and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s:
                    self.events.append(json.loads(s))

    def append(self, event_type: str, payload: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("event_type must be a non-empty string")
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be a mapping")

        idx = len(self.events)
        prev_hash = self.events[-1]["event_hash"] if self.events else "0" * 64
        payload_copy = copy.deepcopy(dict(payload))
        payload_sha = sha256_hex(payload_copy)
        event_hash = sha256_hex(
            {
                "index": idx,
                "event_type": event_type.strip(),
                "prev_hash": prev_hash,
                "payload_sha256": payload_sha,
            }
        )
        record: Dict[str, Any] = {
            "index": idx,
            "event_type": event_type.strip(),
            "prev_hash": prev_hash,
            "payload": payload_copy,
            "payload_sha256": payload_sha,
            "event_hash": event_hash,
        }
        self.events.append(record)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(canonical_json(record) + "\n")
        return copy.deepcopy(record)

    def verify_chain(self) -> bool:
        records = list(self.events)
        if self.path is not None and self.path.exists():
            disk_records = [
                json.loads(line)
                for line in self.path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if disk_records != records:
                return False

        prev_hash = "0" * 64
        for idx, rec in enumerate(records):
            if rec.get("index") != idx:
                return False
            if rec.get("prev_hash") != prev_hash:
                return False
            expected_payload_sha = sha256_hex(rec.get("payload", {}))
            if rec.get("payload_sha256") != expected_payload_sha:
                return False
            expected_event_hash = sha256_hex(
                {
                    "index": idx,
                    "event_type": rec.get("event_type", ""),
                    "prev_hash": prev_hash,
                    "payload_sha256": expected_payload_sha,
                }
            )
            if rec.get("event_hash") != expected_event_hash:
                return False
            prev_hash = expected_event_hash
        return True


def validate_load_profile(
    profile: Sequence[Mapping[str, Any]],
    dt_s: float = 10.0,
) -> List[Dict[str, float]]:
    """Validate a multi-segment load profile `[{'duration_s': ..., 'current_a': ...}, ...]`."""
    if isinstance(dt_s, bool) or not isinstance(dt_s, (int, float)) or not math.isfinite(float(dt_s)) or float(dt_s) <= 0.0:
        raise ValueError(f"dt_s must be a positive finite number, got {dt_s!r}")
    if not isinstance(profile, (list, tuple)) or len(profile) == 0:
        raise ValueError("profile must be a non-empty sequence of segment mappings")

    dt = float(dt_s)
    validated: List[Dict[str, float]] = []
    for idx, seg in enumerate(profile):
        if not isinstance(seg, Mapping):
            raise ValueError(f"profile[{idx}] must be a mapping")
        if "duration_s" not in seg or "current_a" not in seg:
            raise ValueError(f"profile[{idx}] must contain 'duration_s' and 'current_a'")
        raw_dur = seg["duration_s"]
        raw_cur = seg["current_a"]
        if isinstance(raw_dur, bool) or not isinstance(raw_dur, (int, float)) or not math.isfinite(float(raw_dur)):
            raise ValueError(f"profile[{idx}]['duration_s'] must be finite")
        if isinstance(raw_cur, bool) or not isinstance(raw_cur, (int, float)) or not math.isfinite(float(raw_cur)):
            raise ValueError(f"profile[{idx}]['current_a'] must be finite")
        dur = float(raw_dur)
        cur = float(raw_cur)
        if dur <= 0.0:
            raise ValueError(f"profile[{idx}]['duration_s'] must be > 0, got {dur}")
        if cur < 0.0:
            raise ValueError(f"profile[{idx}]['current_a'] must be >= 0, got {cur}")
        steps_float = dur / dt
        n_steps = int(round(steps_float))
        if n_steps <= 0 or abs(steps_float - n_steps) > 1e-6:
            raise ValueError(
                f"profile[{idx}]['duration_s'] ({dur}) must be a positive integer multiple of dt_s ({dt})"
            )
        validated.append({"duration_s": dur, "current_a": cur, "steps": float(n_steps)})
    return validated


def build_experiment_schedule(
    profile: Sequence[Mapping[str, Any]],
    dt_s: float = 10.0,
) -> List[Dict[str, float]]:
    """Expand a validated load profile into discrete step records."""
    validated = validate_load_profile(profile, dt_s=dt_s)
    dt = float(dt_s)
    schedule: List[Dict[str, float]] = []
    step_idx = 0
    for seg_idx, seg in enumerate(validated):
        n_steps = int(seg["steps"])
        for _ in range(n_steps):
            schedule.append(
                {
                    "step_index": float(step_idx),
                    "segment_index": float(seg_idx),
                    "time_s": round((step_idx + 1) * dt, 6),
                    "dt_s": dt,
                    "current_a": seg["current_a"],
                }
            )
            step_idx += 1
    return schedule


class Lab:
    """Controlled thermal experiment lab enforcing precommitment, 2-experiment cap, and blind audit."""

    MAX_EXPERIMENTS_PER_MODEL: int = 2

    def __init__(
        self,
        private_params: Optional[ConnectorThermalParams] = None,
        store: Optional[Store] = None,
        dt_s: float = 10.0,
    ) -> None:
        self._private_params: ConnectorThermalParams = private_params or ConnectorThermalParams(
            r_ohm=0.040,
            h_w_per_k=0.40,
            c_j_per_k=8.0,
            ambient_c=25.0,
            alpha_per_c=0.0,
        )
        self.store: Store = store if store is not None else Store()
        self.dt_s: float = float(dt_s)
        self._models: Dict[str, Dict[str, Any]] = {}

    def publish_public_packet(self) -> Dict[str, Any]:
        """Return the public experiment specification without leaking private parameters."""
        return {
            "schema_version": "legacy-lab-v1",
            "ambient_c": self._private_params.ambient_c,
            "dt_s": self.dt_s,
            "max_experiments_per_model": self.MAX_EXPERIMENTS_PER_MODEL,
            "allowed_current_range_a": [0.0, 40.0],
            "instructions": (
                "Precommit your temperature trajectory predictions before running up to 2 load-profile "
                "experiments. Private connector parameters are sealed."
            ),
        }

    def register_model(
        self,
        model_id: str,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("model_id must be a non-empty string")
        mid = model_id.strip()
        if mid not in self._models:
            self._models[mid] = {
                "model_id": mid,
                "metadata": dict(metadata) if metadata is not None else {},
                "commit_hash": None,
                "committed_predictions": None,
                "experiments": [],
            }
            self.store.append(
                "model_registered",
                {"model_id": mid, "metadata": self._models[mid]["metadata"]},
            )
        return copy.deepcopy(self._models[mid])

    def commit_predictions(self, model_id: str, predictions: Mapping[str, Any]) -> str:
        """Lock precommitted predictions into the append-only Store BEFORE any experiment runs."""
        if not isinstance(predictions, Mapping) or len(predictions) == 0:
            raise ValueError("predictions must be a non-empty mapping")
        mid = model_id.strip() if isinstance(model_id, str) else ""
        if not mid:
            raise ValueError("model_id must be a non-empty string")
        if mid not in self._models:
            self.register_model(mid)

        state = self._models[mid]
        if len(state["experiments"]) > 0:
            raise RuntimeError(
                f"Cannot commit or mutate predictions for {mid!r} after experiments have started"
            )
        if state["commit_hash"] is not None:
            raise RuntimeError(f"Predictions already committed for {mid!r}; mutation forbidden")

        pred_copy = copy.deepcopy(dict(predictions))
        commit_hash = sha256_hex(pred_copy)
        state["commit_hash"] = commit_hash
        state["committed_predictions"] = pred_copy
        self.store.append(
            "predictions_committed",
            {
                "model_id": mid,
                "commit_hash": commit_hash,
                "predictions": pred_copy,
            },
        )
        return commit_hash

    def run_experiment(
        self,
        model_id: str,
        profile: Sequence[Mapping[str, Any]],
        *,
        initial_temp_c: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Run one thermal load-profile experiment (requiring prior commitment and `<= 2` experiments)."""
        mid = model_id.strip() if isinstance(model_id, str) else ""
        if mid not in self._models:
            raise RuntimeError(f"Model {model_id!r} must be registered and precommitted before running experiments")

        state = self._models[mid]
        if state["commit_hash"] is None:
            raise RuntimeError(f"Model {mid!r} must call commit_predictions before run_experiment")
        if len(state["experiments"]) >= self.MAX_EXPERIMENTS_PER_MODEL:
            raise RuntimeError(
                f"Experiment limit ({self.MAX_EXPERIMENTS_PER_MODEL}) exceeded for model {mid!r}"
            )

        schedule = build_experiment_schedule(profile, dt_s=self.dt_s)
        currents = [step["current_a"] for step in schedule]
        t0 = float(initial_temp_c) if initial_temp_c is not None else self._private_params.ambient_c
        traj: ThermalTrajectory = simulate_connector_series(
            initial_temp_c=t0,
            currents_a=currents,
            params=self._private_params,
            dt_s=self.dt_s,
        )
        exp_idx = len(state["experiments"]) + 1
        exp_record: Dict[str, Any] = {
            "experiment_index": exp_idx,
            "model_id": mid,
            "commit_hash": state["commit_hash"],
            "initial_temp_c": t0,
            "profile": [dict(seg) for seg in profile],
            "schedule": schedule,
            "trajectory": {
                "time_s": list(traj.time_s),
                "temps_c": list(traj.temps_c),
                "currents_a": list(traj.currents_a),
            },
        }
        exp_record["measurement_sha256"] = sha256_hex(exp_record["trajectory"])
        state["experiments"].append(exp_record)
        self.store.append("experiment_executed", exp_record)
        return copy.deepcopy(exp_record)

    def build_auditor_packet(self, model_id: str) -> Dict[str, Any]:
        """Build a zero-leakage auditor packet containing only commitments and observed trajectories."""
        mid = model_id.strip()
        if mid not in self._models:
            raise KeyError(f"Unknown model_id {model_id!r}")
        state = self._models[mid]
        return {
            "model_id": mid,
            "commit_hash": state["commit_hash"],
            "committed_predictions": copy.deepcopy(state["committed_predictions"]),
            "experiment_count": len(state["experiments"]),
            "experiments": copy.deepcopy(state["experiments"]),
            "store_chain_valid": self.store.verify_chain(),
        }

    def check_blind_numerical_audit(
        self,
        model_id: str,
        *,
        claimed_predictions: Optional[Mapping[str, Any]] = None,
        tolerance_c: float = 0.25,
    ) -> Dict[str, Any]:
        """Audit precommitment integrity, experiment cap (`<= 2`), and numerical forecast accuracy."""
        pkt = self.build_auditor_packet(model_id)
        commit_hash = pkt["commit_hash"]
        locked_preds = pkt["committed_predictions"]
        exps = pkt["experiments"]
        chain_valid = bool(pkt["store_chain_valid"])

        if commit_hash is None or locked_preds is None:
            return {
                "model_id": model_id,
                "passed": False,
                "commitment_valid": False,
                "commitment_mutated": True,
                "experiment_count": len(exps),
                "max_abs_error_c": float("inf"),
                "store_chain_valid": chain_valid,
                "reason": "missing_precommitment",
            }

        active_preds = dict(claimed_predictions) if claimed_predictions is not None else dict(locked_preds)
        claimed_hash = sha256_hex(active_preds)
        commitment_mutated = claimed_hash != commit_hash
        commitment_valid = not commitment_mutated

        if len(exps) == 0 or len(exps) > self.MAX_EXPERIMENTS_PER_MODEL:
            return {
                "model_id": model_id,
                "passed": False,
                "commitment_valid": commitment_valid,
                "commitment_mutated": commitment_mutated,
                "experiment_count": len(exps),
                "max_abs_error_c": float("inf"),
                "store_chain_valid": chain_valid,
                "reason": "invalid_experiment_count",
            }

        errors: List[float] = []
        if "temps_c" in active_preds and isinstance(active_preds["temps_c"], (list, tuple)):
            obs_temps = exps[0]["trajectory"]["temps_c"]
            pred_temps = [float(x) for x in active_preds["temps_c"]]
            if len(pred_temps) != len(obs_temps):
                errors.append(float("inf"))
            else:
                for a, b in zip(pred_temps, obs_temps):
                    errors.append(abs(float(a) - float(b)))
        elif "experiments" in active_preds and isinstance(active_preds["experiments"], (list, tuple)):
            pred_exps = active_preds["experiments"]
            if len(pred_exps) != len(exps):
                errors.append(float("inf"))
            else:
                for p_exp, o_exp in zip(pred_exps, exps):
                    p_series = p_exp.get("temps_c", []) if isinstance(p_exp, Mapping) else []
                    o_series = o_exp["trajectory"]["temps_c"]
                    if len(p_series) != len(o_series):
                        errors.append(float("inf"))
                    else:
                        for a, b in zip(p_series, o_series):
                            errors.append(abs(float(a) - float(b)))
        elif "final_temp_c" in active_preds:
            obs_final = float(exps[-1]["trajectory"]["temps_c"][-1])
            errors.append(abs(float(active_preds["final_temp_c"]) - obs_final))
        else:
            errors.append(float("inf"))

        max_err = max(errors) if errors else float("inf")
        numerical_ok = math.isfinite(max_err) and max_err <= float(tolerance_c)
        passed = bool(
            chain_valid
            and commitment_valid
            and (1 <= len(exps) <= self.MAX_EXPERIMENTS_PER_MODEL)
            and numerical_ok
        )
        return {
            "model_id": model_id,
            "passed": passed,
            "commitment_valid": commitment_valid,
            "commitment_mutated": commitment_mutated,
            "experiment_count": len(exps),
            "max_abs_error_c": round(float(max_err), 6) if math.isfinite(max_err) else float("inf"),
            "store_chain_valid": chain_valid,
            "reason": "ok" if passed else ("commitment_mutated" if commitment_mutated else "numerical_mismatch"),
        }


class ManagedRunner:
    """Adapter around `blackbox.managed` for historical pilot accounting and reconciliation."""

    def __init__(
        self,
        mode: str = "mock",
        env: Optional[Mapping[str, str]] = None,
        journal_path: Optional[Path | str] = None,
        max_pair_noncached_tokens: int = 280_000,
    ) -> None:
        norm_mode = str(mode).strip().lower()
        if norm_mode not in {"mock", "ai_studio", "adc", "vertex"}:
            raise ValueError(f"Unsupported mode {mode!r}; expected 'mock', 'ai_studio', or 'adc'")
        self.mode: str = "adc" if norm_mode == "vertex" else norm_mode
        if self.mode == "mock":
            self.auth_config: Dict[str, Any] = {
                "mode": "mock",
                "ready": False,
                "reason": "Offline deterministic mock mode",
            }
        else:
            self.auth_config = resolve_auth_config(mode=self.mode, env=env)
        self.is_live: bool = bool(self.mode in {"ai_studio", "adc"} and self.auth_config.get("ready", False))
        self.mock_counts_as_real: bool = False
        self.live_direct_function_calling_supported: bool = False
        self.journal_path: Optional[Path] = Path(journal_path) if journal_path is not None else None
        self.max_pair_noncached_tokens: int = int(max_pair_noncached_tokens)
        self.budget_tracker = PairBudgetTracker()

    def record_turn_usage(
        self,
        case_id: str,
        interaction_id: str,
        raw_usage: Optional[Mapping[str, Any]],
        *,
        status: str = "completed",
        turn_id: str = "I1",
        role: str = "investigator",
    ) -> Dict[str, Any]:
        snap = self.budget_tracker.update_interaction(
            case_id=case_id,
            interaction_id=interaction_id,
            raw_usage=raw_usage,
            role=role,
            turn_id=turn_id,
            status=status,
        )
        return snap.to_dict()

    def reconcile_prior_cancelled_usage(
        self,
        journal_events: Sequence[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        """Account for noncached tokens (or 30k reserve) from cancelled/interrupted prior turns."""
        created_by_id: Dict[str, Mapping[str, Any]] = {}
        terminal_by_id: Dict[str, Mapping[str, Any]] = {}
        for ev in journal_events:
            iid = str(ev.get("interaction_id", "")).strip()
            if not iid:
                continue
            stage = str(ev.get("event", "")).strip().lower()
            if stage == "created":
                created_by_id[iid] = ev
            elif stage in {"cancelled", "terminal", "locked", "reconciled_unknown"}:
                terminal_by_id[iid] = ev

        reconciled_ids: List[str] = []
        for iid, meta in created_by_id.items():
            term = terminal_by_id.get(iid)
            case_id = str(meta.get("case_id", "case_A"))
            turn_id = str(meta.get("turn_id", "I1"))
            role = str(meta.get("role", "investigator"))
            if term is None:
                # Interrupted before terminal event: hold conservative 30k reserve
                self.budget_tracker.update_interaction(
                    case_id=case_id,
                    interaction_id=iid,
                    raw_usage=None,
                    role=role,
                    turn_id=turn_id,
                    status="unknown",
                )
                reconciled_ids.append(iid)
            else:
                status = str(term.get("status", "cancelled")).lower()
                raw_usage = term.get("usage")
                if isinstance(raw_usage, Mapping) and raw_usage.get("unknown_usage") is True:
                    raw_usage = None
                self.budget_tracker.update_interaction(
                    case_id=case_id,
                    interaction_id=iid,
                    raw_usage=raw_usage if isinstance(raw_usage, Mapping) else None,
                    role=role,
                    turn_id=turn_id,
                    status=status,
                )
                reconciled_ids.append(iid)

        return {
            "reconciled_interaction_ids": reconciled_ids,
            "reconciled_count": len(reconciled_ids),
            "unknown_turn_reserve_tokens": UNKNOWN_TURN_TOKEN_RESERVE,
            "pair_noncached_tokens": self.budget_tracker.pair_noncached_tokens,
            "pair_cached_inclusive_tokens": self.budget_tracker.pair_total_tokens,
        }

    reconcile_Prior_cancelled_usage = reconcile_prior_cancelled_usage

    def compact_report(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "is_live": self.is_live,
            "mock_counts_as_real": self.mock_counts_as_real,
            "live_direct_function_calling_supported": self.live_direct_function_calling_supported,
            "execution_architecture_note": (
                "Historical direct function-calling is simulated locally; central visual-v2 "
                "campaign uses remote Managed Agent code_execution workspaces."
            ),
            "budget_summary": self.budget_tracker.to_dict(),
        }


# ---------------------------------------------------------------------------
# Predecessor 3-Rack Thermal Pilot (Retained for Backward Compatibility)
# ---------------------------------------------------------------------------


def run_legacy_pilot(
    steps: int = 8,
    supply_temp_c: float = 18.5,
    fan_duty: float = 0.65,
) -> dict[str, Any]:
    """Run the deterministic predecessor 3-rack thermal pilot trajectory."""
    racks = [
        RackSpec(rack_id="RACK-A1", base_power_kw=11.0, recirculation_factor=0.08),
        RackSpec(rack_id="RACK-A2", base_power_kw=15.5, recirculation_factor=0.16),
        RackSpec(rack_id="RACK-A3", base_power_kw=13.0, recirculation_factor=0.11),
    ]

    states: dict[str, ThermalState] = {
        spec.rack_id: compute_rack_hotspot(
            spec=spec,
            supply_temp_c=supply_temp_c,
            it_load_kw=spec.base_power_kw,
            fan_duty=fan_duty,
        )
        for spec in racks
    }

    history: list[dict[str, Any]] = []
    for step_idx in range(steps):
        # Deterministic workload surge on RACK-A2 mid-trajectory
        step_snapshot: dict[str, Any] = {"step": step_idx, "racks": {}}
        for spec in racks:
            surge = 3.5 if (spec.rack_id == "RACK-A2" and step_idx >= steps // 2) else 0.0
            next_state = simulate_thermal_step(
                prev_state=states[spec.rack_id],
                spec=spec,
                supply_temp_c=supply_temp_c,
                it_load_kw=spec.base_power_kw + surge,
                fan_duty=fan_duty,
            )
            states[spec.rack_id] = next_state
            step_snapshot["racks"][spec.rack_id] = next_state.to_dict()
        history.append(step_snapshot)

    peak_rack = max(states.values(), key=lambda s: s.hotspot_temp_c)
    return {
        "pilot_id": "legacy-thermal-pilot-v0",
        "steps": steps,
        "peak_rack_id": peak_rack.rack_id,
        "peak_hotspot_temp_c": peak_rack.hotspot_temp_c,
        "any_throttled": any(s.throttled for s in states.values()),
        "final_states": {k: v.to_dict() for k, v in states.items()},
        "history": history,
    }
