# -*- mode: python ; coding: utf-8 -*-
"""妹抖酱 v0.1 PyInstaller 打包配置（ARCHITECTURE_V3 §6-M2 / §7）。

用法：
    python -m PyInstaller build/meido.spec --noconfirm                # 发布版（无控制台）
    set MEIDO_CONSOLE=1 && python -m PyInstaller build/meido.spec     # 排障版（带控制台）

产物：
    dist/meido/meido.exe             （发布：onedir、--noconsole）
    dist/meido-debug/meido-debug.exe （排障：同样的 onedir，但保留控制台，§7.3）
    config/ plugins/ workspaces/ assets/ roles/ skills/ data/ 一律**外置**在 exe 同级（§7.4）

关键点：
1. onedir（不用 --onefile）、无控制台（console=False）
2. 插件是动态 import，第三方依赖必须显式写进 hiddenimports（§7.5 / R2）
3. `plugins` 整个包**排除**，运行期从 exe 同级的 plugins/ 目录加载（§7.4）

## Live2D 必须打包 QtWebEngine（别再把它们排除掉）

`ui_qt/media/sources/live2d_source.py` 在**模块顶层**就 import 了
`PySide6.QtWebEngineCore` / `QtWebEngineWidgets`，而这条导入链属于**启动路径**：

    main.py → ui_qt.app → main_window → agent_state → ui_qt.media → live2d_source

所以 WebEngine 必须打进产物（DLL + `resources/` 下的 icudtl.dat / *.pak / v8 快照）。
漏了的表现是**进程静默退出**：日志文件里只有一行 `[启动] 控制台：…`，
真正的 traceback 被 `main.py` 的崩溃兜底重定向进了临时控制台窗口 —— 日志里看不到，
排查时先按「打包缺模块」想。

体积代价：`Qt6WebEngineCore.dll` 约 195MB（产物 360MB → 约 580MB）。

## 发布版怎么开后台调试窗口（与上面的 debug 变体有何不同）

发布版保持 `console=False`，**不**用 `console=True` 构建 —— 那样每次启动都会
先由系统分配控制台、再被隐藏，用户会看到一次黑窗闪烁。
需要调试时改 `config/bot.toml`：

    [logging]
    show_console = true

重启后 `core/console_window.py` 会用 `AllocConsole()` 动态分配一个控制台，
并把 stdout/stderr 重定向过去（启动路径上零闪烁）。两条链路共用这一套逻辑。

`MEIDO_CONSOLE=1` 的 debug 变体仍然保留，用途不同：
它保留的是 **PyInstaller bootloader 阶段**的控制台 —— 也就是「Python 还没跑起来
就崩了」（解包失败、_internal 缺文件、缺 VC 运行库）时唯一能看到输出的形态。
`AllocConsole()` 只能覆盖 Python 代码开始执行之后的阶段。
"""
import os

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

PROJECT_ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))

# 发布版无控制台；必要时（排障）用环境变量切出带控制台的版本（§7.3）
WITH_CONSOLE = os.environ.get("MEIDO_CONSOLE") == "1"
APP_NAME = "meido-debug" if WITH_CONSOLE else "meido"

# ── 插件/引擎的第三方依赖（动态 import，静态分析看不到） ──
hiddenimports = [
    "openai", "httpx", "httpcore", "anyio", "sniffio", "distro", "jiter", "tqdm",
    "requests", "urllib3", "charset_normalizer", "idna", "certifi",
    "bs4", "yaml", "ddgs", "lxml", "readability", "markdownify",
    "rank_bm25", "jieba", "PyPDF2", "docx", "PIL", "PIL.Image", "pytesseract",
    "numpy", "tomli_w",
    "core.paths", "core.logging_utils", "core.selfcheck", "core.console_window",
    # Live2D 靠 QtWebEngine 播放（ui_qt/media/sources/live2d_source.py 顶层 import）。
    # PySide6 的子模块静态分析抓不到，必须显式列出 —— 漏了就是「启动即崩」
    # （ModuleNotFoundError: No module named 'PySide6.QtWebEngineCore'）。
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
]

datas = []
binaries = []

# jieba 词典（缺了就分词失效）
for package in ("jieba",):
    _d, _b, _h = collect_all(package)
    datas += _d
    binaries += _b
    hiddenimports += _h

# 向量库：ChromaDB 数据文件 + 内嵌 sqlite + 子模块
_d, _b, _h = collect_all("chromadb")
datas += _d
binaries += _b
hiddenimports += _h

# lxml（BeautifulSoup 解析器）
hiddenimports += collect_submodules("lxml")

# Qt 图像格式插件：webp/jpeg/gif 必须打包（§7.5）
datas += collect_data_files("PySide6", includes=["plugins/imageformats/*"])

# QtWebEngine 运行时资源：icudtl.dat / *.pak / v8 快照 / 本地化。
# 只打 DLL 不打这些，WebEngine 同样起不来（表现为进程静默退出，日志里什么都没有）。
datas += collect_data_files("PySide6", includes=["resources/*", "resources/locales/*"])
datas += collect_data_files("PySide6", includes=["translations/qtwebengine_locales/*"])

# ── 明确排除（体积 / 稳定性，§2.1 不引入清单） ──
excludes = [
    "gradio",
    "torch",
    "sentence_transformers",
    "transformers",
    "tkinter",
    "matplotlib",
    "pandas",
    "IPython",
    "notebook",
    "pytest",
    # ⚠️ QtWebEngineCore / QtWebEngineWidgets **不能排除** —— Live2D 靠它播放。
    #    2026-09-23 实现 Live2D 时漏改这里（live2d_source.py 顶层就 import 了它），
    #    结果打包版启动即崩，直到 09-24 重建产物才暴露（旧 dist 是 09-12 的，没这问题）。
    #    代价：Qt6WebEngineCore.dll 约 195MB，产物 360MB → 约 580MB。
    #    Quick 是 QML 版，本项目只用 Widgets，继续排除。
    "PySide6.QtWebEngineQuick",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    # 插件不进 exe：运行期从 exe 同级 plugins/ 目录动态加载（§7.4）
    "plugins",
]

a = Analysis(
    [os.path.join(PROJECT_ROOT, "main.py")],
    pathex=[PROJECT_ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,              # 不压缩壳，降低杀软误报（§7.8）
    console=WITH_CONSOLE,   # 发布版无控制台；MEIDO_CONSOLE=1 时保留控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)

# ── 构建后清理 ──
# M1b：QQ 适配器已移除，`websockets` 不再是本项目依赖（已从 requirements.txt 与
# hiddenimports 移除）。实测 PyInstaller 仍会把它的 **dist-info（仅元数据，不含包代码）**
# 拷进产物，这里清掉，保证交付目录零残留（§6-M1B 验收：产物不含 websockets）。
import glob
import shutil

for _leftover in glob.glob(os.path.join(
        DISTPATH, APP_NAME, "_internal", "websockets-*.dist-info")):
    shutil.rmtree(_leftover, ignore_errors=True)

