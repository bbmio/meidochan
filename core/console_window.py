"""后台调试窗口（控制台）的可选开启。

由 `config/bot.toml` 的 `[logging] show_console` 控制，**默认关闭**。

## 为什么用 AllocConsole 而不是 console=True 构建 + ShowWindow(SW_HIDE)

`console=True` 构建出来的 exe 由 Windows 分配控制台，窗口在进程启动时就出现了；
就算我们在 Python 里第一时间隐藏，用户仍会看到一次黑窗闪烁。
反过来用 GUI 子系统（打包版 `console=False`、源码版 `pythonw`）时，
系统**根本不会**分配控制台，需要时再 `AllocConsole()` 新建一个 —— 启动路径上零闪烁。

## 只管理「自己分配的」窗口

如果程序本来就跑在用户的终端里（源码模式 `python main.py`），
这里**不会**去隐藏或改动那个终端 —— 那是用户的窗口，不是我们的。
所以 `show_console = false` 在那种情况下表现为「保持原样」，而不是「把终端藏了」。

## 与日志的关系

`show_console = true` 时，控制台与 `data/logs/*.log` **同时**有输出
（见 `core/logging_utils.py` 的 `_LogStream.mirror`）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from core.paths import app_path

_IS_WINDOWS = sys.platform == "win32"

# 是否由本模块分配了控制台（区别于「用户本来就有终端」）
_allocated: bool = False
# 已建立的输出流，避免重复 open 泄漏句柄
_out_stream = None
_err_stream = None
_in_stream = None


# ── 配置 ──

def _read_show_console() -> bool:
    """读 `config/bot.toml` 的 `[logging] show_console`。

    读不到 / 解析失败 / 类型不对一律按 **False**（默认关闭）处理：
    配置写坏时不应该意外弹出一个控制台窗口。
    """
    path = app_path("config", "bot.toml")
    try:
        import tomllib
    except ImportError:                      # Python < 3.11 的兜底
        try:
            import tomli as tomllib          # type: ignore
        except ImportError:
            return False
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError):
        return False
    logging_section = data.get("logging")
    if not isinstance(logging_section, dict):
        return False
    return logging_section.get("show_console") is True


def console_enabled() -> bool:
    """配置是否要求开启后台调试窗口。"""
    return _read_show_console()


def allocated_by_us() -> bool:
    """当前控制台是否由本模块分配（而非用户原有的终端）。"""
    return _allocated


# ── 控制台分配 ──

def _kernel32():
    import ctypes
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _console_hwnd() -> int:
    try:
        return int(_kernel32().GetConsoleWindow() or 0)
    except Exception:
        return 0


def _redirect_streams() -> bool:
    """把 Python 的三条标准流接到新分配的控制台上。"""
    global _out_stream, _err_stream, _in_stream
    try:
        # CONOUT$ / CONIN$ 是控制台的设备名，必须用行缓冲
        _out_stream = open("CONOUT$", "w", encoding="utf-8", buffering=1,
                           errors="replace")
        _err_stream = open("CONOUT$", "w", encoding="utf-8", buffering=1,
                           errors="replace")
        _in_stream = open("CONIN$", "r", encoding="utf-8", errors="replace")
    except OSError:
        return False
    sys.stdout = _out_stream
    sys.stderr = _err_stream
    sys.stdin = _in_stream
    return True


def _set_title(title: str) -> None:
    try:
        _kernel32().SetConsoleTitleW(title)
    except Exception:
        pass


def attach_console(title: str = "妹抖酱 · 后台调试窗口") -> bool:
    """确保当前进程有控制台可用；已有则复用，没有则分配一个。

    返回 True 表示「现在有控制台可写」。**不要用它去判断是否由我们分配** ——
    用户从终端直接运行时返回也是 True，那种情况下我们并没有动过窗口。
    """
    global _allocated

    if not _IS_WINDOWS:
        return False

    # 已经有控制台（用户从终端启动）→ 直接复用，绝不改动用户的窗口
    if _console_hwnd():
        return True

    try:
        kernel32 = _kernel32()
        if not kernel32.AllocConsole():
            # 分配失败但此时已有窗口（竞态）→ 当作已有
            return bool(_console_hwnd())
    except Exception:
        return False

    if not _redirect_streams():
        return False

    _allocated = True
    _set_title(title)
    return True


def ensure_console() -> bool:
    """按配置决定是否分配控制台。返回「最终是否有控制台可写」。

    这是入口（`main.py`）该调用的函数：配置关闭时它什么都不做。
    """
    if not console_enabled():
        return bool(_console_hwnd())
    return attach_console()


def force_console(title: str = "妹抖酱 · 启动失败") -> bool:
    """无视配置，确保有控制台可写。

    专用于「启动期致命异常」：此时如果什么都不显示，用户只会看到程序一闪而过，
    完全无从排查。崩溃时弹一个窗口出来，比安静地死掉有用得多。
    """
    if not _IS_WINDOWS:
        return False
    return attach_console(title)


def log_file_hint() -> Optional[Path]:
    """日志文件路径（供启动失败时提示用户）。"""
    try:
        from core.app_info import LOG_FILE_PREFIX
    except Exception:
        return None
    return app_path("data", "logs", f"{LOG_FILE_PREFIX}.log")


def describe_state() -> str:
    """一行状态描述，写进日志便于事后确认当时的控制台形态。"""
    if not _IS_WINDOWS:
        return f"控制台：非 Windows 平台（{sys.platform}）"
    if _allocated:
        return "控制台：由程序分配（show_console = true）"
    if _console_hwnd():
        return "控制台：用户终端（未改动）"
    return "控制台：无（show_console = false）"
