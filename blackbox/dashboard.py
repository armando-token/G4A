"""Streamlit web application with offline replay and 3D/SLD inspection for BlackboxDC."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from blackbox.campaign import run_ab_campaign


def load_or_generate_replay_state(artifacts_root: Path) -> Dict[str, Any]:
    """Load existing campaign replay bundle from disk or generate a fresh deterministic bundle."""
    artifacts_root = Path(artifacts_root)
    summary_file = artifacts_root / "reports" / "campaign_summary.json"
    if not summary_file.exists():
        run_ab_campaign(artifacts_root=artifacts_root)
    summary_data = json.loads(summary_file.read_text(encoding="utf-8"))

    cases: Dict[str, Dict[str, Any]] = {}
    for cid in ("case_A", "case_B"):
        case_dir = artifacts_root / cid
        cases[cid] = {
            "i1": json.loads((case_dir / "i1_contract.json").read_text(encoding="utf-8")),
            "probe": json.loads((case_dir / "probe_response.json").read_text(encoding="utf-8")),
            "i2": json.loads((case_dir / "i2_contract.json").read_text(encoding="utf-8")),
            "a1": json.loads((case_dir / "a1_contract.json").read_text(encoding="utf-8")),
            "a2": json.loads((case_dir / "a2_contract.json").read_text(encoding="utf-8")),
            "oracle": json.loads((case_dir / "oracle_grade.json").read_text(encoding="utf-8")),
            "sld_path": str(case_dir / f"sld_{cid}.png"),
        }

    return {
        "summary": summary_data,
        "cases": cases,
        "scene_3d_path": str(artifacts_root / "public" / "scene_3d_shared.png"),
        "sha256sums_path": str(artifacts_root / "reports" / "SHA256SUMS.txt"),
    }


def render_streamlit_dashboard(artifacts_root: Optional[Path] = None) -> Dict[str, Any]:
    """Render the BlackboxDC Streamlit dashboard for interactive or headless inspection."""
    import streamlit as st

    root = Path(artifacts_root) if artifacts_root else Path("artifacts")
    state = load_or_generate_replay_state(root)
    summary = state["summary"]

    st.set_page_config(page_title="BlackboxDC A/B Evaluation Console", layout="wide")
    st.title("BlackboxDC — Multimodal Causal & Safety Evaluation Console")
    st.caption(
        "Paired World A/B Evaluation | Investigator (I1 -> Probe -> I2) & Independent Auditor (A1 -> A2)"
    )

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Campaign Pass", str(summary["campaign_pass"]))
    col2.metric("Feeder Swap Disambiguated", str(summary["feeder_disambiguation_pass"]))
    col3.metric("Auditor Disambiguated", str(summary["auditor_disambiguation_pass"]))
    col4.metric("Locked Interactions", str(summary["total_interactions_locked"]))

    st.subheader("Shared 3D IR Thermography Scene (`scene_3d_shared.png`)")
    if Path(state["scene_3d_path"]).exists():
        st.image(state["scene_3d_path"], caption="Shared 3D Thermal Hotspot on Rack Node N2")

    tab_a, tab_b = st.tabs(["Case A (Straight SLD: F2 -> N2)", "Case B (Swapped SLD: F3 -> N2)"])
    for tab, cid in ((tab_a, "case_A"), (tab_b, "case_B")):
        with tab:
            c_data = state["cases"][cid]
            st.markdown(f"### {cid} Single-Line Diagram & Locked Contracts")
            if Path(c_data["sld_path"]).exists():
                st.image(c_data["sld_path"], caption=f"SLD for {cid}")
            st.json(
                {
                    "I1": c_data["i1"],
                    "Probe": c_data["probe"],
                    "I2": c_data["i2"],
                    "A1": c_data["a1"],
                    "A2": c_data["a2"],
                    "OracleGrade": c_data["oracle"],
                }
            )

    return state


if __name__ == "__main__":
    render_streamlit_dashboard()
