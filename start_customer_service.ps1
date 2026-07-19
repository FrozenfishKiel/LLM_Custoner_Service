param(
    [switch]$PrepareOnly,
    [switch]$ForceResetData
)

$ErrorActionPreference = "Stop"

$RootDir = $PSScriptRoot
$DemoDir = Join-Path $RootDir "ecs_demo"
$PythonExe = if ($env:PYTHON_EXE) { $env:PYTHON_EXE } else { "python" }
$CliExe = if ($env:ATGUIGU_CLI_EXE) { $env:ATGUIGU_CLI_EXE } else { "atguigu" }
$ServicePort = 8012
$MysqlContainer = "llm-cs-mysql"
$Neo4jContainer = "llm-cs-neo4j"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Assert-PathExists {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Missing required path: $Path"
    }
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

function Get-DockerDesktopExe {
    $candidates = @(
        "C:\Program Files\Docker\Docker\Docker Desktop.exe",
        "D:\Docker\Docker\Docker Desktop.exe",
        "C:\Program Files\Docker\Docker\resources\Docker desktop.exe",
        "D:\Docker\Docker\resources\Docker desktop.exe"
    )

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }

    throw "Docker Desktop executable was not found in the expected locations."
}

function Test-DockerReady {
    cmd /c "docker version >nul 2>nul"
    return ($LASTEXITCODE -eq 0)
}

function Wait-ForDocker {
    $maxAttempts = 90
    for ($i = 1; $i -le $maxAttempts; $i++) {
        if (Test-DockerReady) {
            return
        }
        Start-Sleep -Seconds 2
    }
    throw "Docker Desktop did not become ready in time."
}

function Ensure-DockerDesktop {
    Write-Step "Checking Docker Desktop"
    if (Test-DockerReady) {
        Write-Host "Docker is already ready."
        return
    }

    $dockerDesktopExe = Get-DockerDesktopExe
    Write-Host "Docker is not ready. Starting Docker Desktop..."
    Start-Process -FilePath $dockerDesktopExe -WindowStyle Hidden
    Wait-ForDocker
    Write-Host "Docker is ready."
}

function Ensure-Containers {
    Write-Step "Starting MySQL and Neo4j containers"
    docker start $MysqlContainer $Neo4jContainer | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to start containers. Expected containers: $MysqlContainer, $Neo4jContainer"
    }
    Write-Host "Containers are running."
}

function Wait-ForMySql {
    Write-Step "Waiting for MySQL"
    $maxAttempts = 60
    for ($i = 1; $i -le $maxAttempts; $i++) {
        $tcp = Test-NetConnection -ComputerName 127.0.0.1 -Port 3306 -WarningAction SilentlyContinue
        if ($tcp.TcpTestSucceeded) {
            Write-Host "MySQL is reachable."
            return
        }
        Start-Sleep -Seconds 2
    }
    throw "MySQL did not become reachable on port 3306."
}

function Get-OrderCount {
    $script = @'
from actions.db import SessionLocal
from sqlalchemy import text

session = SessionLocal()
try:
    print(session.execute(text("select count(*) from order_info")).scalar_one())
finally:
    session.close()
'@
    Push-Location $DemoDir
    try {
        $count = $script | & $PythonExe -
    } finally {
        Pop-Location
    }
    return [int]($count | Select-Object -Last 1)
}

function Ensure-SeedData {
    Write-Step "Checking demo data"
    $orderCount = Get-OrderCount
    if ($ForceResetData -or $orderCount -eq 0) {
        if ($ForceResetData) {
            Write-Host "Force reset requested. Regenerating demo data..."
        } else {
            Write-Host "No order data found. Generating demo data..."
        }

        Push-Location $DemoDir
        try {
            & $PythonExe "gen_data.py"
            if ($LASTEXITCODE -ne 0) {
                throw "gen_data.py failed."
            }
        } finally {
            Pop-Location
        }

        $orderCount = Get-OrderCount
    }

    Write-Host "Current order_info row count: $orderCount"
}

function Get-PortListener {
    $listener = Get-NetTCPConnection -LocalPort $ServicePort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $listener) {
        return $null
    }

    $process = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
    if ($process) {
        return $process
    }
    return $listener
}

function Start-Service {
    Write-Step "Starting customer service app"
    $existing = Get-PortListener
    if ($existing) {
        $procName = if ($existing.ProcessName) { $existing.ProcessName } else { "PID $($existing.OwningProcess)" }
        Write-Host "Port $ServicePort is already in use by $procName"
        Write-Host "If that is this project, open: http://127.0.0.1:$ServicePort/inspect"
        return
    }

    Push-Location $DemoDir
    try {
        Write-Host "Keep this window open while the service is running."
        & $CliExe inspect --model . --host 127.0.0.1 --port $ServicePort
    } finally {
        Pop-Location
    }
}

Assert-PathExists $DemoDir
$PythonExe = Resolve-Executable $PythonExe "Create a Python environment, run 'python -m pip install -r requirements-atguigu.txt', or set PYTHON_EXE to the full python.exe path."
$CliExe = Resolve-Executable $CliExe "Install this project into the active Python environment with 'python -m pip install -e .' or set ATGUIGU_CLI_EXE to the full atguigu executable path."

Ensure-DockerDesktop
Ensure-Containers
Wait-ForMySql
Ensure-SeedData

if ($PrepareOnly) {
    Write-Step "Environment is ready"
    Write-Host "Inspect URL: http://127.0.0.1:$ServicePort/inspect"
    Write-Host "API docs: http://127.0.0.1:$ServicePort/docs"
    exit 0
}

Start-Service
