param(
    [ValidateSet("setup", "demo", "api", "worker", "test", "eval", "crash", "doctor", "live", "down")]
    [string]$Task = "demo"
)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$env:PYTHONUTF8 = "1"
$PythonExe = Join-Path (Get-Location) ".venv\Scripts\python.exe"

function Run-Python {
    & $PythonExe @args
    if ($LASTEXITCODE -ne 0) { throw "Python command failed with exit code $LASTEXITCODE" }
}
if ($Task -eq "setup") {
    if (-not (Test-Path $PythonExe)) {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            & py -3.12 -m venv .venv
        } elseif (Get-Command python -ErrorAction SilentlyContinue) {
            & python -m venv .venv
        } else {
            throw "Install Python 3.12 x64 from python.org, then reopen PowerShell."
        }
        if ($LASTEXITCODE -ne 0) { throw "Could not create venv. Install Python 3.12 x64." }
    }
    Run-Python -c "import sys; assert sys.version_info >= (3, 12), 'Python 3.12+ required'"
    & $PythonExe -c "import sys, struct, platform; sys.exit(0 if sys.version_info[:2] == (3, 12) and struct.calcsize('P') == 8 and platform.machine().lower() in ('amd64', 'x86_64') else 1)"
    $UseOffline = ($LASTEXITCODE -eq 0) -and (Test-Path "wheelhouse")
    if ($UseOffline) {
        Run-Python -m pip install --no-index --find-links wheelhouse -r requirements-dev.txt
    } else {
        Run-Python -m pip install -r requirements-dev.txt
    }
    Run-Python -m pip install --no-deps --no-build-isolation -e .
    Run-Python -m aegisops init
    Run-Python -m aegisops doctor
    exit 0
}
if ($Task -eq "live") {
    if (-not (Test-Path $PythonExe)) { throw "Run setup first." }
    Run-Python scripts/configure_docker.py --live
    & docker compose --profile live up -d --build
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose failed. Start Docker Desktop (Linux containers)." }
    exit 0
}
if ($Task -eq "down") {
    & docker compose --profile live down
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose down failed" }
    exit 0
}
if (-not (Test-Path $PythonExe)) { throw "Run: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\windows.ps1 setup" }
switch ($Task) {
    "demo" { Run-Python -m aegisops demo-replay }
    "api" { Run-Python -m aegisops api }
    "worker" { Run-Python -m aegisops worker }
    "test" {
        Run-Python -m ruff check .
        Run-Python -m mypy src
        Run-Python -m pytest -q
    }
    "eval" { Run-Python -m aegisops eval }
    "crash" { Run-Python -m aegisops demo-crash-recovery }
    "doctor" { Run-Python -m aegisops doctor }
}

