"""Unit tests for blackbox.preflight verifying mode detection and zero secret leakage."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from blackbox.preflight import (
    check_credentials,
    check_python_version,
    format_preflight_table,
    main,
    redact_secret,
    run_preflight,
)


def test_redact_secret_never_returns_raw_value() -> None:
    secret = "AIzaSyD-SUPER-SECRET-KEY-999999999"
    redacted = redact_secret(secret)
    assert secret not in redacted
    assert redacted == "[REDACTED]"
    assert redact_secret("") == "<unset>"
    assert redact_secret("   ") == "<unset>"
    assert redact_secret(None) == "<unset>"


def test_mode_detection_ai_studio(tmp_path: Path) -> None:
    fake_secret = "AIzaSy_TEST_FAKE_SECRET_TOKEN_1234567890"
    env = {
        "GEMINI_API_KEY": fake_secret,
        "GOOGLE_GENAI_USE_VERTEXAI": "false",
    }
    non_existent_adc = tmp_path / "no_adc.json"
    creds = check_credentials(env=env, adc_path=non_existent_adc)

    assert creds["mode"] == "ai_studio"
    assert creds["ready"] is True
    assert creds["use_vertexai"] is False
    assert creds["ai_studio"]["gemini_api_key_set"] is True
    assert creds["ai_studio"]["gemini_api_key"] == "[REDACTED]"
    assert fake_secret not in json.dumps(creds)


def test_mode_detection_vertex_ai_with_gac(tmp_path: Path) -> None:
    adc_file = tmp_path / "service-account-secret.json"
    adc_file.write_text('{"type": "service_account", "private_key": "SUPER_SECRET_PRIVATE_KEY"}')

    env = {
        "GOOGLE_GENAI_USE_VERTEXAI": "true",
        "GOOGLE_CLOUD_PROJECT": "my-secret-gcp-project",
        "GOOGLE_CLOUD_LOCATION": "us-east5",
        "GOOGLE_APPLICATION_CREDENTIALS": str(adc_file),
    }
    creds = check_credentials(env=env, adc_path=tmp_path / "unused_default_adc.json")

    assert creds["mode"] == "vertex_ai"
    assert creds["ready"] is True
    assert creds["use_vertexai"] is True
    assert creds["vertex_ai"]["project_set"] is True
    assert creds["vertex_ai"]["location"] == "us-east5"
    assert creds["vertex_ai"]["google_application_credentials_set"] is True
    assert creds["vertex_ai"]["google_application_credentials_file_exists"] is True
    assert creds["vertex_ai"]["adc_source"] == "GOOGLE_APPLICATION_CREDENTIALS"
    # Verify neither the path nor file contents nor project ID is leaked as a raw credential
    serialized = json.dumps(creds)
    assert str(adc_file) not in serialized
    assert "SUPER_SECRET_PRIVATE_KEY" not in serialized


def test_mode_detection_vertex_ai_missing_adc_or_project(tmp_path: Path) -> None:
    env = {
        "GOOGLE_GENAI_USE_VERTEXAI": "true",
        "GOOGLE_CLOUD_PROJECT": "",
    }
    creds = check_credentials(env=env, adc_path=tmp_path / "missing_adc.json")

    assert creds["mode"] == "vertex_ai"
    assert creds["ready"] is False
    assert creds["vertex_ai"]["project_set"] is False
    assert creds["vertex_ai"]["adc_source"] == "missing"


def test_mode_detection_unconfigured(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    creds = check_credentials(env=env, adc_path=tmp_path / "missing_adc.json")

    assert creds["mode"] == "unconfigured"
    assert creds["ready"] is False
    assert creds["ai_studio"]["gemini_api_key_set"] is False
    assert creds["ai_studio"]["google_api_key_set"] is False


def test_secrets_never_leaked_in_preflight_output_or_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    gemini_secret = "AIzaSy_ULTRA_SENSITIVE_GEMINI_KEY_88888"
    google_secret = "AIzaSy_ULTRA_SENSITIVE_GOOGLE_KEY_77777"
    gac_secret_path = tmp_path / "secret_creds_99999.json"
    gac_secret_path.write_text('{"private_key": "PRIVATE_KEY_PAYLOAD_55555"}')

    monkeypatch.setenv("GEMINI_API_KEY", gemini_secret)
    monkeypatch.setenv("GOOGLE_API_KEY", google_secret)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(gac_secret_path))
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "false")

    with caplog.at_level(logging.DEBUG):
        report = run_preflight(adc_path=tmp_path / "no_default_adc.json")
        table_output = format_preflight_table(report)
        exit_code = main(["--json"])
        captured_json = capsys.readouterr()
        exit_code_table = main([])
        captured_table = capsys.readouterr()

    assert exit_code == 0
    assert exit_code_table == 0

    combined_outputs = "\n".join(
        [
            json.dumps(report),
            table_output,
            captured_json.out,
            captured_json.err,
            captured_table.out,
            captured_table.err,
            caplog.text,
        ]
    )

    for forbidden in (
        gemini_secret,
        google_secret,
        "secret_creds_99999.json",
        "PRIVATE_KEY_PAYLOAD_55555",
    ):
        assert forbidden not in combined_outputs


def test_python_version_check() -> None:
    assert check_python_version((3, 11, 0))["ok"] is True
    assert check_python_version((3, 12, 2))["ok"] is True
    assert check_python_version((3, 10, 9))["ok"] is False
