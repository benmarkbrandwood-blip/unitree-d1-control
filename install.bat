@echo off
rem ============================================================================
rem  Unitree D1 Arm Control -- Windows installer (batch wrapper)
rem
rem  This double-clickable wrapper launches install.ps1 with
rem  -ExecutionPolicy Bypass for THIS process only -- no permanent system change.
rem
rem  NOTE: Arm control requires Linux. This script sets up the Python
rem        environment only. For full arm control use WSL2 + Ubuntu and
rem        run install.sh inside the WSL2 environment.
rem
rem  Usage:
rem     install.bat             (interactive)
rem     install.bat /yes        (skip prompts)
rem ============================================================================
setlocal EnableExtensions EnableDelayedExpansion
title Unitree D1 Arm Control -- Installer

set "D1_DIR=%~dp0"
set "PS_SCRIPT=%D1_DIR%install.ps1"

if not exist "%PS_SCRIPT%" (
    echo [D1] ERROR: install.ps1 not found next to install.bat.
    echo [D1] Expected at: %PS_SCRIPT%
    pause
    exit /b 1
)

rem -- Translate batch flags into PowerShell parameters ----------------------
set "PS_ARGS="
:parse
if "%~1"=="" goto runps
if /I "%~1"=="/yes"  set "PS_ARGS=!PS_ARGS! -Yes" & shift & goto parse
if /I "%~1"=="-yes"  set "PS_ARGS=!PS_ARGS! -Yes" & shift & goto parse
if /I "%~1"=="/y"    set "PS_ARGS=!PS_ARGS! -Yes" & shift & goto parse
set "PS_ARGS=!PS_ARGS! %~1"
shift
goto parse

:runps
rem -- Prefer pwsh (PS 7+), fall back to powershell --------------------------
set "PS_EXE="
where pwsh >nul 2>&1 && set "PS_EXE=pwsh"
if "%PS_EXE%"=="" (
    where powershell >nul 2>&1 && set "PS_EXE=powershell"
)
if "%PS_EXE%"=="" (
    echo [D1] ERROR: PowerShell not found on PATH.
    pause
    exit /b 1
)

echo [D1] Using %PS_EXE% to run install.ps1 ...
echo.

"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%" %PS_ARGS%
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo [D1] Installer exited with code %RC%.
) else (
    echo [D1] Installer finished successfully.
)
echo.
echo Press any key to close this window...
pause >nul
exit /b %RC%
