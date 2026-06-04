<#
.SYNOPSIS
    Install unitree-d1-control on Windows (Python environment only).
.DESCRIPTION
    Creates a Python virtual environment and installs all Python dependencies.

    NOTE: Direct arm control requires Linux. The network configuration module
    (net_config.py) uses Linux 'ip' commands and will not work natively on
    Windows. To control the arm from Windows, use WSL2 with Ubuntu and run
    install.sh inside the WSL2 environment instead.

    This script is useful for:
      - Developing and testing code on Windows before deploying to Linux
      - Running the GUI in simulation/offline mode (no DDS connection)
.PARAMETER Yes
    Non-interactive: skip all prompts and use defaults.
.EXAMPLE
    .\install.ps1
    .\install.ps1 -Yes
.NOTES
    If PowerShell refuses to run this script, use install.bat instead, or run:
        powershell -ExecutionPolicy Bypass -File .\install.ps1
#>

#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$Yes
)

$ErrorActionPreference = "Continue"

$D1_DIR  = $PSScriptRoot
if (-not $D1_DIR) { $D1_DIR = (Get-Location).Path }

$VENV_DIR = Join-Path $D1_DIR "venv"
$VENV_PY  = Join-Path $VENV_DIR "Scripts\python.exe"

function Write-Info { param($msg) Write-Host "[D1] $msg" -ForegroundColor Green }
function Write-Warn { param($msg) Write-Host "[D1] $msg" -ForegroundColor Yellow }
function Write-Fail { param($msg) Write-Host "[D1] ERROR: $msg" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "  +================================================+" -ForegroundColor Cyan
Write-Host "  |   Unitree D1 Arm Control -- Windows Installer  |" -ForegroundColor Cyan
Write-Host "  +================================================+" -ForegroundColor Cyan
Write-Host ""
Write-Host "  NOTE: Arm control requires Linux or WSL2." -ForegroundColor Yellow
Write-Host "        This script sets up the Python environment only." -ForegroundColor Yellow
Write-Host ""
Write-Info "Install directory: $D1_DIR"

if ($D1_DIR -match '[\[\]]') {
    Write-Fail "Install path contains '[' or ']' which PowerShell handles poorly. Move the folder and re-run."
}

# === 1. Find Python 3.10+ ===================================================
Write-Info "Looking for Python 3.10 or newer..."

function Get-PythonVersion {
    param([string]$Exe, [string[]]$PreArgs = @())
    try {
        $output = & $Exe @PreArgs "--version" 2>&1
        if ($null -eq $output) { return $null }
        $text = ($output | Out-String)
        if ($text -match "Python\s+(\d+)\.(\d+)") {
            return [pscustomobject]@{ Major = [int]$Matches[1]; Minor = [int]$Matches[2]; Raw = $text.Trim() }
        }
    } catch { return $null }
    return $null
}

$candidates = @(
    @{ Exe = "py";      Args = @("-3") },
    @{ Exe = "py";      Args = @() },
    @{ Exe = "python";  Args = @() },
    @{ Exe = "python3"; Args = @() }
)

$pythonExe  = $null
$pythonArgs = @()

foreach ($c in $candidates) {
    $cmd = Get-Command $c.Exe -ErrorAction SilentlyContinue
    if (-not $cmd) { continue }
    if ($c.Exe -eq "python" -and $cmd.Source -match "WindowsApps") {
        Write-Warn "Skipping Microsoft Store 'python' stub."
        continue
    }
    $v = Get-PythonVersion -Exe $c.Exe -PreArgs $c.Args
    if ($null -eq $v) { continue }
    if ($v.Major -gt 3 -or ($v.Major -eq 3 -and $v.Minor -ge 10)) {
        $pythonExe  = $c.Exe
        $pythonArgs = $c.Args
        $displayCmd = if ($c.Args.Count -gt 0) { "$($c.Exe) $($c.Args -join ' ')" } else { $c.Exe }
        Write-Info "Using Python $($v.Major).$($v.Minor) via '$displayCmd'."
        break
    } else {
        Write-Warn "'$($c.Exe)' is Python $($v.Major).$($v.Minor) -- need 3.10+; trying next."
    }
}

if (-not $pythonExe) {
    Write-Host ""
    Write-Host "  Python 3.10 or newer was not found." -ForegroundColor Red
    Write-Host "  Install it from https://www.python.org/downloads/" -ForegroundColor White
    Write-Host "  IMPORTANT: tick 'Add python.exe to PATH' during install." -ForegroundColor White
    Write-Host ""
    Write-Fail "Python 3.10+ is required."
}

# === 2. Create virtual environment ==========================================
if (Test-Path -LiteralPath $VENV_PY) {
    Write-Info "Existing venv found -- reusing it."
} else {
    Write-Info "Creating virtual environment in venv\ ..."
    & $pythonExe @pythonArgs -m venv "$VENV_DIR"
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VENV_PY)) {
        Write-Fail "Failed to create virtual environment."
    }
}

# === 3. Upgrade pip =========================================================
Write-Info "Upgrading pip..."
& $VENV_PY -m pip install --disable-pip-version-check --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) {
    Write-Warn "pip upgrade returned non-zero; continuing anyway."
}

# === 4. Install requirements ================================================
$REQS = Join-Path $D1_DIR "requirements.txt"
if (-not (Test-Path -LiteralPath $REQS)) { Write-Fail "requirements.txt not found." }

Write-Info "Installing Python requirements (this can take a few minutes)..."
& $VENV_PY -m pip install --disable-pip-version-check -r "$REQS"
if ($LASTEXITCODE -ne 0) {
    Write-Warn ""
    Write-Warn "Some packages may have failed."
    Write-Warn "cyclonedds and dearpygui have prebuilt wheels for Windows."
    Write-Warn "If you see build errors, ensure Visual C++ Build Tools are installed:"
    Write-Warn "  https://visualstudio.microsoft.com/visual-cpp-build-tools/"
    Write-Fail "Dependency install failed."
}
Write-Info "Python packages installed."

# === 5. Runtime directories =================================================
foreach ($d in @("logs", "recordings")) {
    $path = Join-Path $D1_DIR $d
    if (-not (Test-Path -LiteralPath $path)) {
        New-Item -ItemType Directory -Path $path -Force | Out-Null
    }
}
Write-Info "Runtime directories ready (logs\, recordings\)."

# === Done ===================================================================
Write-Host ""
Write-Host "  Installation complete!" -ForegroundColor Green
Write-Host ""
Write-Host "  Activate the virtual environment:" -ForegroundColor White
Write-Host "    venv\Scripts\activate" -ForegroundColor Cyan
Write-Host ""
Write-Host "  NOTE: For full arm control, use WSL2 + Ubuntu and run install.sh." -ForegroundColor Yellow
Write-Host ""
