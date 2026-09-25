@echo off
rem ============================================================
rem  QtWebEngine environment self-check  (for Live2D rendering)
rem
rem  This file is intentionally ASCII-only.
rem  cmd.exe parses .bat files using the system ANSI codepage (GBK
rem  on Chinese Windows), so UTF-8 Chinese here gets mangled into
rem  bogus commands. All Chinese text lives in tools\probe_webengine.py,
rem  which Python prints through the Unicode console API instead.
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   QtWebEngine self-check
echo ============================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.11+
    pause
    exit /b 1
)

python "tools\probe_webengine.py"
set EXITCODE=%errorlevel%

echo.
echo ============================================================
echo   Done. Please send the full output above back to the dev.
echo ============================================================
pause
exit /b %EXITCODE%
