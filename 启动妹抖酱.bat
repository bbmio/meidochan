@echo off
rem ============================================================
rem  Meido-chan v0.1 launcher -- HEADED (with debug console)
rem
rem  Runs `python main.py` in THIS console, so the window doubles
rem  as the live debug console (logger output + print output).
rem
rem  For the console-less launch use the packaged exe instead:
rem      dist\meido\meido.exe      (built with console=False)
rem  The two paths differ only in whether a console exists --
rem  core\console_window.py reuses a console it did not allocate
rem  and never hides it, and core\logging_utils.py attaches a
rem  console StreamHandler whenever a console is present.
rem
rem  This file is intentionally ASCII-only.
rem  cmd.exe parses .bat files using the system ANSI codepage (GBK
rem  on Chinese Windows), so UTF-8 Chinese here gets mangled into
rem  bogus commands -- it does not just look ugly, it silently eats
rem  real command lines (measured: it ate `pip install`). All Chinese
rem  text lives in tools\startup_notice.py, which Python prints instead.
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   Meido-chan v0.1 - launch (debug console)
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
rem Chinese banner (see the header comment for why it is not inline here)
python "tools\startup_notice.py"

rem ------------------------------------------------------------
rem  No `start`, no `pythonw`: running python.exe directly in this
rem  console is exactly what makes the window a live debug console.
rem  Ctrl+C or closing this window stops the app.
rem ------------------------------------------------------------
python main.py

echo.
echo ============================================================
echo   Meido-chan exited.
echo ============================================================
pause
