@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0\.."

echo ============================================
echo   妹抖酱 v0.1 打包（PyInstaller onedir）
echo ============================================
echo.
echo   用法：build.bat          发布版（无控制台，dist\meido）
echo         build.bat debug    排障版（带控制台，dist\meido-debug）
echo.
echo   发布版的后台调试窗口：改 config\bot.toml 的
echo     [logging] show_console = true
echo   重启后程序会自己分配一个控制台（无需换 exe）。
echo   debug 变体留给「Python 还没跑起来就崩」的场景。
echo.

set MEIDO_CONSOLE=
if /i "%1"=="debug" (
    set MEIDO_CONSOLE=1
    echo   [模式] 排障版（保留控制台）
) else (
    echo   [模式] 发布版（无控制台）
)
echo.

echo [1/5] 检查 PyInstaller...
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo        未安装，正在安装 PyInstaller==6.22.2 ...
    python -m pip install "pyinstaller==6.22.2"
    if errorlevel 1 (
        echo [错误] PyInstaller 安装失败
        pause
        exit /b 1
    )
)
echo        OK

echo [2/5] 清理旧产物...
if exist "dist\meido" rmdir /s /q "dist\meido"
if exist "dist\meido-debug" rmdir /s /q "dist\meido-debug"
if exist "build\meido-work" rmdir /s /q "build\meido-work"

echo [3/5] 打包（首次约需 3-10 分钟）...
python -m PyInstaller build\meido.spec --noconfirm --distpath dist --workpath build\meido-work
if errorlevel 1 (
    echo [错误] 打包失败，请查看上面的报错
    pause
    exit /b 1
)

echo [4/5] 复制外置目录（不进 exe，必须与 exe 同级）...
set TARGET_NAME=meido
if defined MEIDO_CONSOLE set TARGET_NAME=meido-debug
for %%D in (config plugins workspaces assets roles skills data) do (
    if exist "%%D" (
        xcopy /e /i /y /q "%%D" "dist\%TARGET_NAME%\%%D" >nul
        echo        已复制 %%D\
    )
)

echo [5/5] 完成
echo.
echo ============================================
echo   产物：dist\%TARGET_NAME%\%TARGET_NAME%.exe
echo   自检：双击该 exe，窗口应正常打开
echo   排障：dist\%TARGET_NAME%\data\logs\meido.log
echo   交付：将 dist\%TARGET_NAME% 整个目录打成 zip，或把 exe
echo         改名为「妹抖酱v0.1.exe」（建议 exe 路径保持纯 ASCII，见 §7.6）
echo ============================================
pause
