"""A/B campaign runner executing the 8 interactions across Case A and Case B.

Coordinates fixture generation, visual SLD and 3D scene rendering, protocol
state machine execution (4 interactions per case = 8 total interactions),
oracle grading, and artifact persistence.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Optional

from blackbox.evidence import write_campaign_evidence_bundle
from blackbox.fixture import write_case_fixtures
from blackbox.managed import ManagedAgentTransport
from blackbox.oracle import grade_ab_campaign, grade_case_run
from blackbox.protocol import BlackboxProtocolStateMachine, CaseProtocolRunResult
from blackbox.render_sld import render_sld_png
from blackbox.scene_bridge import render_deterministic_3d_scene


def run_ab_campaign(
    artifacts_root: Path,
    transport: Optional[ManagedAgentTransport] = None,
) -> Dict[str, Any]:
    """Run the full 8-interaction BlackboxDC A/B evaluation campaign."""
    artifacts_root = Path(artifacts_root)
    active_transport = transport or ManagedAgentTransport(mode="deterministic")

    # 1. Materialize public & private fixtures for Case A and Case B
    bundles = write_case_fixtures(artifacts_root=artifacts_root)

    # 2. Render shared 3D visual scene and case-specific Single-Line Diagrams
    scene_meta = render_deterministic_3d_scene(
        artifacts_root / "public" / "scene_3d_shared.png"
    )
    sld_meta: Dict[str, Any] = {}
    for cid in ("case_A", "case_B"):
        sld_meta[cid] = render_sld_png(
            case_id=cid,
            output_path=artifacts_root / cid / f"sld_{cid}.png",
        )

    # 3. Execute 4 interactions per case (I1, A1, Probe, I2, A2) -> 8 interactions total
    case_runs: Dict[str, CaseProtocolRunResult] = {}
    case_grades: Dict[str, Any] = {}

    for cid in ("case_A", "case_B"):
        pub_ev, sealed_truth = bundles[cid]
        sm = BlackboxProtocolStateMachine(
            public_evidence=pub_ev,
            sealed_truth=sealed_truth,
            transport=active_transport,
        )
        run_res = sm.run_case_protocol()
        unsealed = sm.unseal_truth_for_oracle()
        grade = grade_case_run(run_res, unsealed)
        case_runs[cid] = run_res
        case_grades[cid] = grade

        # Persist case interaction artifacts
        case_dir = artifacts_root / cid
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "i1_contract.json").write_text(
            json.dumps(run_res.i1.model_dump(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (case_dir / "probe_response.json").write_text(
            json.dumps(run_res.probe_response.model_dump(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (case_dir / "i2_contract.json").write_text(
            json.dumps(run_res.i2.model_dump(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (case_dir / "a1_contract.json").write_text(
            json.dumps(run_res.a1.model_dump(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (case_dir / "a2_contract.json").write_text(
            json.dumps(run_res.a2.model_dump(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (case_dir / "oracle_grade.json").write_text(
            json.dumps(asdict(grade), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    # 4. Aggregate A/B campaign oracle summary
    ab_summary = grade_ab_campaign(
        grade_a=case_grades["case_A"],
        grade_b=case_grades["case_B"],
        run_a=case_runs["case_A"],
        run_b=case_runs["case_B"],
    )
    ab_summary["visual_artifacts"] = {
        "scene_3d_shared": scene_meta,
        "sld": sld_meta,
    }
    ab_summary["accounting"] = [
        asdict(acc)
        for cid in ("case_A", "case_B")
        for acc in case_runs[cid].accounting
    ]

    # 5. Write replay bundle, markdown report, and SHA256SUMS
    evidence_meta = write_campaign_evidence_bundle(
        artifacts_root=artifacts_root,
        campaign_summary=ab_summary,
    )
    ab_summary["evidence_bundle"] = evidence_meta
    return ab_summary
