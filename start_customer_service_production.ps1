param(
    [switch]$CheckOnly,
    [switch]$SkipExternalServiceChecks,
    [switch]$NoBrowser,
    [switch]$EnableInspect,
    [string]$BindAddress = "127.0.0.1",
    [int]$Port = 8012
)

$ErrorActionPreference = "Stop"

$RootDir = $PSScriptRoot
$PythonExe = if ($env:PYTHON_EXE) { $env:PYTHON_EXE } else { "python" }

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Resolve-Executable {
    param(
        [string]$Command,
        [string]$InstallHint
    )

    if (Test-Path -LiteralPath $Command) {
        return (Resolve-Path -LiteralPath $Command).Path
    }

    $resolved = Get-Command $Command -ErrorAction SilentlyContinue
    if ($resolved) {
        return $resolved.Source
    }

    throw "Executable '$Command' was not found. $InstallHint"
}

function Import-DotEnvFile {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    Get-Content -LiteralPath $Path -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            return
        }
        $separatorIndex = $line.IndexOf("=")
        if ($separatorIndex -le 0) {
            return
        }

        $name = $line.Substring(0, $separatorIndex).Trim()
        $value = $line.Substring($separatorIndex + 1).Trim()
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }

        if (-not [Environment]::GetEnvironmentVariable($name, "Process")) {
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

function Set-ProductionDefaults {
    if (-not $env:PRODUCTION_CHAT_ENABLED) {
        $env:PRODUCTION_CHAT_ENABLED = "true"
    }
    if (-not $env:PRODUCTION_AGENT_PATH) {
        $env:PRODUCTION_AGENT_PATH = "ecs_demo"
    }
    if (-not $env:PRODUCTION_ENABLE_INSPECT) {
        $env:PRODUCTION_ENABLE_INSPECT = if ($EnableInspect) { "true" } else { "false" }
    }
    if (-not $env:REDIS_URL) {
        $env:REDIS_URL = "redis://127.0.0.1:6379/15"
    }
}

function Assert-RequiredEnvironment {
    $requiredNames = @(
        "MYSQL_PASSWORD",
        "NEO4J_URI",
        "NEO4J_USER",
        "NEO4J_PASSWORD",
        "DEEPSEEK_API_KEY",
        "AUTH_PUBLIC_BASE_URL",
        "SMTP_HOST",
        "SMTP_FROM_ADDRESS"
    )

    $missing = @()
    foreach ($name in $requiredNames) {
        if (-not [Environment]::GetEnvironmentVariable($name, "Process")) {
            $missing += $name
        }
    }

    if ($missing.Count -gt 0) {
        throw "Missing required production environment variables: $($missing -join ', ')"
    }
}

function Test-TcpPort {
    param(
        [string]$ComputerName,
        [int]$RemotePort,
        [string]$Label
    )

    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($ComputerName, $RemotePort, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(2000, $false)) {
            throw "$Label is not reachable at ${ComputerName}:${RemotePort}"
        }
        $client.EndConnect($async)
    } finally {
        $client.Close()
    }
}

function Test-ExternalServices {
    if ($SkipExternalServiceChecks) {
        Write-Host "External service checks skipped by flag."
        return
    }

    Write-Step "Checking external services"
    $mysqlHost = if ($env:MYSQL_HOST) { $env:MYSQL_HOST } else { "127.0.0.1" }
    $mysqlPort = if ($env:MYSQL_PORT) { [int]$env:MYSQL_PORT } else { 3306 }
    Test-TcpPort -ComputerName $mysqlHost -RemotePort $mysqlPort -Label "MySQL"

    $redisUri = [Uri]$env:REDIS_URL
    $redisPort = if ($redisUri.Port -gt 0) { $redisUri.Port } else { 6379 }
    Test-TcpPort -ComputerName $redisUri.Host -RemotePort $redisPort -Label "Redis"
    Write-Host "External service checks passed."
}

function Test-PythonRuntime {
    Write-Step "Checking Python runtime dependencies"
    $script = @'
import importlib.util

required_modules = [
    "dotenv",
    "fastapi",
    "uvicorn",
    "jieba",
    "neo4j",
    "neo4j_graphrag",
    "langchain_community",
    "langchain_openai",
]
missing = [name for name in required_modules if importlib.util.find_spec(name) is None]
if missing:
    raise RuntimeError(
        "Missing Python modules: "
        + ", ".join(missing)
        + ". Install project dependencies with: python -m pip install -r requirements-atguigu.txt"
    )
print("PYTHON_RUNTIME_OK")
'@
    $script | & $PythonExe -
    if ($LASTEXITCODE -ne 0) {
        throw "Python runtime dependency check failed."
    }
}

function Test-ProductionAppFactory {
    Write-Step "Checking production FastAPI app factory"
    $script = @'
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path.cwd() / ".env", override=False)

from fastapi.testclient import TestClient

from atguigu_ai.api.production import create_production_app

app = create_production_app(enable_inspect=False)
client = TestClient(app, base_url="https://testserver")

checks = {
    "/": client.get("/").status_code == 200,
    "/login": client.get("/login").status_code == 200,
    "/health/live": client.get("/health/live").status_code == 200,
    "/health/ready": client.get("/health/ready").status_code == 200,
    "/internal/metrics": client.get("/internal/metrics").status_code == 200,
    "/api/chat/messages": client.post("/api/chat/messages", json={"message": "hello"}).status_code == 401,
}
failed = sorted(path for path, ok in checks.items() if not ok)
if failed:
    raise RuntimeError(f"production app route check failed: {', '.join(failed)}")
if not hasattr(app.state, "auth_deps"):
    raise RuntimeError("production app missing auth dependencies")
if not hasattr(app.state, "chat_deps"):
    raise RuntimeError("production app missing chat dependencies")
print(f"PRODUCTION_APP_FACTORY_OK route_count={len(app.routes)}")
'@
    $script | & $PythonExe -
    if ($LASTEXITCODE -ne 0) {
        throw "Production FastAPI app factory check failed."
    }
}

function Start-ProductionServer {
    Write-Step "Starting production customer service"
    $env:ATGUIGU_PRODUCTION_BIND_ADDRESS = $BindAddress
    $env:ATGUIGU_PRODUCTION_PORT = [string]$Port
    if ($EnableInspect) {
        $env:PRODUCTION_ENABLE_INSPECT = "true"
    }

    $url = "http://${BindAddress}:$Port"
    Write-Host "Frontend: $url"
    Write-Host "Readiness: $url/health/ready"
    Write-Host "Metrics: $url/internal/metrics"
    Write-Host "Keep this window open while the service is running."

    if (-not $NoBrowser) {
        Start-Job -ScriptBlock {
            param([string]$TargetUrl)
            Start-Sleep -Seconds 2
            Start-Process $TargetUrl
        } -ArgumentList $url | Out-Null
    }

    $script = @'
import os
from pathlib import Path

from dotenv import load_dotenv
import uvicorn

from atguigu_ai.api.production import create_production_app

load_dotenv(Path.cwd() / ".env", override=False)
app = create_production_app(
    enable_inspect=os.environ.get("PRODUCTION_ENABLE_INSPECT", "").strip().lower()
    in {"1", "true", "yes", "on"}
)
uvicorn.run(
    app,
    host=os.environ["ATGUIGU_PRODUCTION_BIND_ADDRESS"],
    port=int(os.environ["ATGUIGU_PRODUCTION_PORT"]),
    log_level="info",
)
'@
    $script | & $PythonExe -
    if ($LASTEXITCODE -ne 0) {
        throw "Production server exited with code $LASTEXITCODE."
    }
}

Set-Location -LiteralPath $RootDir
Import-DotEnvFile -Path (Join-Path $RootDir ".env")
Set-ProductionDefaults
$PythonExe = Resolve-Executable $PythonExe "Install Python dependencies with 'python -m pip install -r requirements-atguigu.txt', or set PYTHON_EXE to the full python.exe path."

Write-Step "Checking required production configuration"
Assert-RequiredEnvironment
Write-Host "Required production configuration is present."

Test-PythonRuntime
Test-ExternalServices
Test-ProductionAppFactory

$url = "http://${BindAddress}:$Port"
if ($CheckOnly) {
    Write-Host "PRODUCTION_STARTUP_CHECK_OK $url"
    exit 0
}

Start-ProductionServer
