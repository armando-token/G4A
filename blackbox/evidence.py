"""Replay bundle, SHA256SUMS generator, and report formatter for BlackboxDC."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List


def compute_file_sha256(file_path: Path) -> str:
    """Compute hex SHA-256 digest of a file on disk."""
    return hashlib.sha256(Path(file_path).read_bytes()).hexdigest()


def format_campaign_markdown_report(campaign_summary: Dict[str, Any]) -> str:
    """Format a human-readable Markdown audit report for the A/B campaign."""
    case_a = campaign_summary["case_A"]
    case_b = campaign_summary["case_B"]
    lines = [
        "# BlackboxDC A/B Evaluation Campaign Report",
        "",
        f"- **Campaign Overall Pass**: `{campaign_summary['campaign_pass']}`",
        f"- **Feeder Swap Disambiguation (F2 vs F3)**: `{campaign_summary['feeder_disambiguation_pass']}`",
        f"- **Auditor Disambiguation Pass**: `{campaign_summary['auditor_disambiguation_pass']}`",
        f"- **Total Locked Agent Interactions**: `{campaign_summary['total_interactions_locked']}`",
        "",
        "## Case-by-Case Oracle Breakdown",
        "",
        "| Metric | Case A (Straight SLD) | Case B (Swapped N2/N3 SLD) |",
        "| :--- | :--- | :--- |",
        f"| Overall Case Pass | `{case_a['overall_case_pass']}` | `{case_b['overall_case_pass']}` |",
        f"| Zero Leakage Verified | `{case_a['zero_leakage_verified']}` | `{case_b['zero_leakage_verified']}` |",
        f"| Diagnosed Fault Node | `{case_a['safety_details']['node_match']}` (N2) | `{case_b['safety_details']['node_match']}` (N2) |",
        f"| Diagnosed Fault Feeder | `{case_a['safety_details']['feeder_match']}` (F2) | `{case_b['safety_details']['feeder_match']}` (F3) |",
        f"| Proposed Current Cap (A) | `{case_a['safety_details']['proposed_current_cap_a']}` | `{case_b['safety_details']['proposed_current_cap_a']}` |",
        f"| True Max Safe Current (A) | `{case_a['safety_details']['true_max_safe_current_a']}` | `{case_b['safety_details']['true_max_safe_current_a']}` |",
        f"| Resistance Abs Error (Ohm) | `{case_a['i2_resistance_abs_error_ohm']}` | `{case_b['i2_resistance_abs_error_ohm']}` |",
        f"| Auditor A2 Verdict | `{case_a['a2_verdict_correct']}` | `{case_b['a2_verdict_correct']}` |",
        "",
    ]
    return "\n".join(lines)


def generate_sha256sums(artifacts_root: Path) -> Path:
    """Compute SHA256SUMS for all artifact files under artifacts_root."""
    artifacts_root = Path(artifacts_root)
    reports_dir = artifacts_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    sums_path = reports_dir / "SHA256SUMS.txt"

    entries: List[str] = []
    for file_path in sorted(artifacts_root.rglob("*")):
        if not file_path.is_file():
            continue
        if file_path.name == ".gitkeep" or file_path.resolve() == sums_path.resolve():
            continue
        rel = file_path.relative_to(artifacts_root).as_posix()
        digest = compute_file_sha256(file_path)
        entries.append(f"{digest}  {rel}")

    sums_path.write_text("\n".join(entries) + ("\n" if entries else ""), encoding="utf-8")
    return sums_path


def write_campaign_evidence_bundle(
    artifacts_root: Path,
    campaign_summary: Dict[str, Any],
) -> Dict[str, str]:
    """Write campaign JSON bundle, Markdown report, and SHA256SUMS manifest."""
    artifacts_root = Path(artifacts_root)
    reports_dir = artifacts_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    summary_json_path = reports_dir / "campaign_summary.json"
    summary_json_path.write_text(
        json.dumps(campaign_summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    report_md_path = reports_dir / "campaign_report.md"
    report_md_path.write_text(
        format_campaign_markdown_report(campaign_summary),
        encoding="utf-8",
    )

    sums_path = generate_sha256sums(artifacts_root)
    return {
        "summary_json": str(summary_json_path),
        "report_md": str(report_md_path),
        "sha256sums": str(sums_path),
    }
