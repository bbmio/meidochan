@echo off
rem ============================================================
rem  Meido-chan v0.1 launcher (desktop)
rem
rem  This file is intentionally ASCII-only.
rem  cmd.exe parses .bat files using the system ANSI codepage (GBK
rem  on Chinese Windows), so UTF-8 Chinese here gets mangled into
rem  bogus commands -- it does not just look ugly, it silently eats
rem  real command lines. All Chinese text lives in
rem  tools\startup_notice.py, which Python prints instead.
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   Meido-chan v0.1 - one-click launch
echo ============================================================
echo.

echo [1/3] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.11+
    pause
    exit /b 1
)
echo        Python OK

echo [2/3] Checking dependencies...
python -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [WARN] Dependency install may be incomplete; launching anyway.
)
echo        Dependencies OK

echo [3/3] Launching...
rem ------------------------------------------------------------
rem Launch with pythonw: no console window is allocated, which is
rem the default (release) look. To get a live debug console, set
rem     [logging] show_console = true
rem in config\bot.toml and restart -- the app allocates the console
rem itself. No need for a separate build or exe.
rem ------------------------------------------------------------
set "MEIDO_PYW=pythonw"
where pythonw >nul 2>&1
if errorlevel 1 (
    set "MEIDO_PYW=python"
    echo        [WARN] pythonw not found; falling back to python ^(a console will show^)
)
start "" %MEIDO_PYW% main.py

rem Chinese notice (see the header comment for why it is not inline here)
python "tools\startup_notice.py"

pause
