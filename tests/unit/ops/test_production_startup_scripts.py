from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "start_customer_service_production.ps1"
BAT_WRAPPER = ROOT / "start_customer_service_production.bat"


def _startup_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PYTHON_EXE": sys.executable,
            "MYSQL_HOST": "127.0.0.1",
            "MYSQL_PORT": "3306",
            "MYSQL_DATABASE": "ecs",
            "MYSQL_USER": "service_user",
            "MYSQL_PASSWORD": "placeholder-secret",
            "REDIS_URL": "redis://127.0.0.1:6379/15",
            "NEO4J_URI": "bolt://127.0.0.1:7687",
            "NEO4J_USER": "neo4j",
            "NEO4J_PASSWORD": "neo4j-placeholder-secret",
            "DEEPSEEK_API_KEY": "deepseek-placeholder-secret",
            "AUTH_PUBLIC_BASE_URL": "http://127.0.0.1:8099/auth",
            "SMTP_HOST": "localhost",
            "SMTP_PORT": "1025",
            "SMTP_USERNAME": "",
            "SMTP_PASSWORD": "smtp-placeholder-secret",
            "SMTP_FROM_ADDRESS": "no-reply@example.test",
            "SMTP_USE_TLS": "false",
            "PRODUCTION_CHAT_ENABLED": "true",
            "PRODUCTION_AGENT_PATH": "ecs_demo",
            "PRODUCTION_ENABLE_INSPECT": "false",
        }
    )
    return env


def _run_script(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            *args,
        ],
        cwd=ROOT,
        env=env or _startup_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_production_startup_check_only_constructs_app_without_printing_secrets() -> None:
    result = _run_script(
        "-CheckOnly",
        "-SkipExternalServiceChecks",
        "-NoBrowser",
        "-Port",
        "8099",
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "PRODUCTION_STARTUP_CHECK_OK" in combined
    assert "http://127.0.0.1:8099" in combined
    assert "placeholder-secret" not in combined
    assert "smtp-placeholder-secret" not in combined
    assert "neo4j-placeholder-secret" not in combined
    assert "deepseek-placeholder-secret" not in combined


def test_production_startup_check_fails_fast_for_missing_required_config_without_secret_leakage() -> None:
    env = _startup_env()
    env.pop("AUTH_PUBLIC_BASE_URL")

    result = _run_script(
        "-CheckOnly",
        "-SkipExternalServiceChecks",
        "-NoBrowser",
        "-Port",
        "8099",
        env=env,
    )

    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "AUTH_PUBLIC_BASE_URL" in combined
    assert "placeholder-secret" not in combined
    assert "smtp-placeholder-secret" not in combined
    assert "neo4j-placeholder-secret" not in combined
    assert "deepseek-placeholder-secret" not in combined


def test_production_startup_bat_wrapper_delegates_to_powershell_script() -> None:
    wrapper = BAT_WRAPPER.read_text(encoding="utf-8")

    assert "start_customer_service_production.ps1" in wrapper
    assert "-ExecutionPolicy Bypass" in wrapper
