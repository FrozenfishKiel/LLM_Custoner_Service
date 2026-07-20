from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest


pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "start_customer_service_production.ps1"


def test_production_startup_script_runs_server_and_exposes_user_http_flow() -> None:
    if os.environ.get("RUN_PRODUCTION_STARTUP_SERVER_TEST") != "1":
        pytest.skip("set RUN_PRODUCTION_STARTUP_SERVER_TEST=1 to run real startup server test")

    port = _free_port()
    env = os.environ.copy()
    env.setdefault("PYTHON_EXE", sys.executable)
    env.setdefault("PRODUCTION_AGENT_PATH", "ecs_demo")
    env.setdefault("PRODUCTION_CHAT_ENABLED", "true")
    env.setdefault("PRODUCTION_ENABLE_INSPECT", "false")

    process = subprocess.Popen(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SCRIPT),
            "-NoBrowser",
            "-Port",
            str(port),
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    try:
        base_url = f"http://127.0.0.1:{port}"
        ready = _wait_for_http_ok(f"{base_url}/health/ready", process, timeout_seconds=90)
        assert '"ready":true' in ready.replace(" ", "")

        assert _http_status(f"{base_url}/") == 200
        assert _http_status(f"{base_url}/login") == 200
        assert _http_text(f"{base_url}/health/live") == '{"status":"alive"}'
        metrics = _http_text(f"{base_url}/internal/metrics")
        assert "auth_configured" in metrics
        assert "MYSQL_PASSWORD" not in metrics
        assert "DEEPSEEK_API_KEY" not in metrics
        assert _http_status(
            f"{base_url}/api/chat/messages",
            method="POST",
            body=b'{"message":"hello"}',
            headers={"Content-Type": "application/json"},
        ) == 401
    finally:
        _terminate_listener_on_port(port)
        _terminate_process_tree(process)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_http_ok(
    url: str,
    process: subprocess.Popen[str],
    *,
    timeout_seconds: int,
) -> str:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                f"production startup process exited early with code {process.returncode}"
            )
        try:
            return _http_text(url)
        except OSError:
            time.sleep(0.5)
    raise AssertionError("production startup server did not become ready")


def _http_status(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> int:
    request = Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urlopen(request, timeout=10) as response:
            return int(response.status)
    except HTTPError as exc:
        return int(exc.code)


def _http_text(url: str) -> str:
    with urlopen(url, timeout=10) as response:
        return response.read().decode("utf-8")


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _terminate_listener_on_port(port: int) -> None:
    if os.name != "nt":
        return
    command = (
        "$connection = Get-NetTCPConnection "
        f"-LocalAddress 127.0.0.1 -LocalPort {port} "
        "-State Listen -ErrorAction SilentlyContinue; "
        "if ($connection) { "
        "Stop-Process -Id $connection.OwningProcess -Force "
        "}"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
