"""Phase 00 environment, dependency, and secret-safe credential preflight checks."""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping

from blackbox import DEFAULT_AGENT_NAME, DEFAULT_MODEL_ID

REQUIRED_PYTHON_MAJOR = 3
REQUIRED_PYTHON_MINOR = 11

REQUIRED_PACKAGES: tuple[tuple[str, str], ...] = (
    ("google.genai", "google-genai"),
    ("numpy", "numpy"),
    ("streamlit", "streamlit"),
    ("PIL", "Pillow"),
    ("pandas", "pandas"),
    ("pydantic", "pydantic"),
    ("httpx", "httpx"),
)

REQUIRED_CLI_TOOLS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("node", ("node",)),
    ("npm", ("npm",)),
    ("ffmpeg", ("ffmpeg",)),
    ("chromium", ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")),
)

SENSITIVE_ENV_KEYS: frozenset[str] = frozenset(
    {
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "HF_TOKEN",
    }
)


def redact_secret(value: str | None) -> str:
    """Redact any non-empty secret string so raw credentials never appear in outputs."""
    if not value or not value.strip():
        return "<unset>"
    return "[REDACTED]"


def check_python_version(version_info: tuple[int, int, int] | None = None) -> dict[str, Any]:
    """Verify Python runtime is 3.11 or newer."""
    if version_info is None:
        v = sys.version_info
        major, minor, micro = v.major, v.minor, v.micro
    else:
        major, minor, micro = version_info[:3]

    ok = (major, minor) >= (REQUIRED_PYTHON_MAJOR, REQUIRED_PYTHON_MINOR)
    return {
        "version": f"{major}.{minor}.{micro}",
        "required": f">={REQUIRED_PYTHON_MAJOR}.{REQUIRED_PYTHON_MINOR}",
        "ok": ok,
    }


def check_python_packages() -> dict[str, dict[str, Any]]:
    """Verify required Python packages can be imported without leaking environment state."""
    results: dict[str, dict[str, Any]] = {}
    for import_name, dist_name in REQUIRED_PACKAGES:
        try:
            importlib.import_module(import_name)
            try:
                version = importlib.metadata.version(dist_name)
            except importlib.metadata.PackageNotFoundError:
                version = "unknown"
            results[import_name] = {
                "distribution": dist_name,
                "installed": True,
                "version": version,
                "error": None,
            }
        except Exception as exc:
            results[import_name] = {
                "distribution": dist_name,
                "installed": False,
                "version": None,
                "error": exc.__class__.__name__,
            }
    return results


def check_cli_tools() -> dict[str, dict[str, Any]]:
    """Check presence of external CLI binaries on PATH."""
    results: dict[str, dict[str, Any]] = {}
    for canonical_name, candidates in REQUIRED_CLI_TOOLS:
        resolved_binary: str | None = None
        resolved_path: str | None = None
        for candidate in candidates:
            found = shutil.which(candidate)
            if found:
                resolved_binary = candidate
                resolved_path = found
                break
        results[canonical_name] = {
            "available": resolved_path is not None,
            "binary": resolved_binary,
            "path": resolved_path,
            "candidates": list(candidates),
        }
    return results


def _parse_bool_env(raw: str | None) -> bool:
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _default_gcloud_adc_path() -> Path:
    if os.name == "nt":
        appdata = os.environ.get("APPDATA", "")
        return Path(appdata) / "gcloud" / "application_default_credentials.json"
    return Path.home() / ".config" / "gcloud" / "application_default_credentials.json"


def _load_dotenv_if_present() -> None:
    """Load key=value pairs from repo `.env` into os.environ if not already set."""
    dotenv_path = Path(__file__).resolve().parent.parent / ".env"
    if not dotenv_path.is_file():
        return
    try:
        for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except OSError:
        pass


def check_credentials(
    env: Mapping[str, str] | None = None,
    adc_path: Path | None = None,
) -> dict[str, Any]:
    """Discriminate between Google AI Studio API Key and Vertex AI ADC without leaking secrets.

    Never includes the value of GEMINI_API_KEY, GOOGLE_API_KEY, or credential file paths/contents.
    """
    if env is None:
        _load_dotenv_if_present()
        environ: Mapping[str, str] = os.environ
    else:
        environ = env
    use_vertex = _parse_bool_env(environ.get("GOOGLE_GENAI_USE_VERTEXAI"))

    gemini_key_raw = (environ.get("GEMINI_API_KEY") or "").strip()
    google_key_raw = (environ.get("GOOGLE_API_KEY") or "").strip()
    has_gemini_key = bool(gemini_key_raw)
    has_google_key = bool(google_key_raw)
    has_api_key = has_gemini_key or has_google_key

    project = (environ.get("GOOGLE_CLOUD_PROJECT") or "").strip()
    location = (environ.get("GOOGLE_CLOUD_LOCATION") or "us-central1").strip() or "us-central1"
    gac_raw = (environ.get("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()
    has_gac_env = bool(gac_raw)
    gac_file_exists = Path(gac_raw).is_file() if has_gac_env else False

    default_adc = adc_path if adc_path is not None else _default_gcloud_adc_path()
    gcloud_adc_exists = default_adc.is_file()
    has_adc = (has_gac_env and gac_file_exists) or gcloud_adc_exists

    if use_vertex:
        mode = "vertex_ai"
        ready = bool(project) and has_adc
        adc_source = (
            "GOOGLE_APPLICATION_CREDENTIALS"
            if (has_gac_env and gac_file_exists)
            else ("gcloud_default_adc" if gcloud_adc_exists else "missing")
        )
        summary = (
            "Vertex AI ADC configured"
            if ready
            else "Vertex AI enabled but missing GOOGLE_CLOUD_PROJECT or valid ADC credentials"
        )
    elif has_api_key:
        mode = "ai_studio"
        ready = True
        adc_source = "not_used"
        summary = "Google AI Studio API key configured"
    elif has_gac_env or gcloud_adc_exists:
        # ADC exists on system, but GOOGLE_GENAI_USE_VERTEXAI is not enabled and no API key is set
        mode = "unconfigured"
        ready = False
        adc_source = "GOOGLE_APPLICATION_CREDENTIALS" if has_gac_env else "gcloud_default_adc"
        summary = (
            "No AI Studio API key set and GOOGLE_GENAI_USE_VERTEXAI is false "
            "(set GEMINI_API_KEY or enable GOOGLE_GENAI_USE_VERTEXAI=true)"
        )
    else:
        mode = "unconfigured"
        ready = False
        adc_source = "none"
        summary = "No credentials configured (set GEMINI_API_KEY or configure Vertex AI ADC)"

    return {
        "mode": mode,
        "ready": ready,
        "use_vertexai": use_vertex,
        "ai_studio": {
            "gemini_api_key_set": has_gemini_key,
            "google_api_key_set": has_google_key,
            "gemini_api_key": redact_secret(gemini_key_raw),
            "google_api_key": redact_secret(google_key_raw),
        },
        "vertex_ai": {
            "project_set": bool(project),
            "location": location,
            "google_application_credentials_set": has_gac_env,
            "google_application_credentials": redact_secret(gac_raw),
            "google_application_credentials_file_exists": gac_file_exists,
            "gcloud_adc_file_exists": gcloud_adc_exists,
            "adc_source": adc_source,
        },
        "summary": summary,
    }


def check_agent_config(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Read non-sensitive agent and model identifiers."""
    environ = os.environ if env is None else env
    agent_name = (environ.get("BLACKBOX_AGENT_NAME") or DEFAULT_AGENT_NAME).strip() or DEFAULT_AGENT_NAME
    model_id = (environ.get("BLACKBOX_MODEL_ID") or DEFAULT_MODEL_ID).strip() or DEFAULT_MODEL_ID
    return {
        "agent_name": agent_name,
        "model_id": model_id,
    }


def run_preflight(
    env: Mapping[str, str] | None = None,
    adc_path: Path | None = None,
) -> dict[str, Any]:
    """Execute all Phase 00 preflight checks and return a structured, secret-safe report."""
    python_report = check_python_version()
    packages_report = check_python_packages()
    cli_report = check_cli_tools()
    credentials_report = check_credentials(env=env, adc_path=adc_path)
    agent_config = check_agent_config(env=env)

    packages_ok = all(pkg["installed"] for pkg in packages_report.values())
    cli_ok = all(tool["available"] for tool in cli_report.values())

    return {
        "python": python_report,
        "packages": packages_report,
        "cli_tools": cli_report,
        "credentials": credentials_report,
        "agent_config": agent_config,
        "summary": {
            "python_ok": python_report["ok"],
            "packages_ok": packages_ok,
            "cli_tools_ok": cli_ok,
            "credentials_ready": credentials_report["ready"],
            "overall_ok": bool(python_report["ok"] and packages_ok and cli_ok),
        },
    }


def format_preflight_table(report: Mapping[str, Any]) -> str:
    """Format the preflight report as a clean human-readable ASCII table."""
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("BLACKBOXDC PHASE 00 PREFLIGHT REPORT")
    lines.append("=" * 72)

    agent_cfg = report.get("agent_config", {})
    lines.append(f"Agent Name : {agent_cfg.get('agent_name', DEFAULT_AGENT_NAME)}")
    lines.append(f"Model ID   : {agent_cfg.get('model_id', DEFAULT_MODEL_ID)}")
    lines.append("-" * 72)

    lines.append(f"{'Category':<16} | {'Check':<26} | {'Status':<8} | {'Details'}")
    lines.append("-" * 72)

    py = report["python"]
    py_status = "PASS" if py["ok"] else "FAIL"
    lines.append(
        f"{'Python':<16} | {'runtime':<26} | {py_status:<8} | {py['version']} (req {py['required']})"
    )

    for pkg_name, pkg_info in report["packages"].items():
        status = "PASS" if pkg_info["installed"] else "MISSING"
        detail = f"v{pkg_info['version']}" if pkg_info["installed"] else str(pkg_info.get("error") or "not installed")
        lines.append(f"{'Package':<16} | {pkg_name:<26} | {status:<8} | {detail}")

    for tool_name, tool_info in report["cli_tools"].items():
        status = "PASS" if tool_info["available"] else "MISSING"
        detail = tool_info["binary"] or "not found on PATH"
        lines.append(f"{'CLI Tool':<16} | {tool_name:<26} | {status:<8} | {detail}")

    creds = report["credentials"]
    cred_status = "READY" if creds["ready"] else "WARN"
    lines.append(
        f"{'Credentials':<16} | {'mode=' + creds['mode']:<26} | {cred_status:<8} | {creds['summary']}"
    )
    lines.append("=" * 72)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for `python -m blackbox.preflight`."""
    parser = argparse.ArgumentParser(
        prog="python -m blackbox.preflight",
        description="Run BlackboxDC Phase 00 environment and credential preflight checks.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit only the JSON preflight report.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with non-zero code if packages, CLI tools, or credentials are not ready.",
    )
    args = parser.parse_args(argv)

    report = run_preflight()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_preflight_table(report))
        print("\nJSON Summary:")
        print(json.dumps(report["summary"], indent=2, sort_keys=True))

    if args.strict:
        all_ok = report["summary"]["overall_ok"] and report["summary"]["credentials_ready"]
        return 0 if all_ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
