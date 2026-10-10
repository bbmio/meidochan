"""主窗口 flags 的守卫：无边框窗口必须带 `WindowMinimizeButtonHint`。

Windows 上无边框（`FramelessWindowHint`）窗口原生是 `WS_POPUP` 且**没有 `WS_MINIMIZEBOX`**，
任务栏按钮因此不会「最小化 / 还原」—— 表现为「单击任务栏图标没反应」（用户实测报的）。
实测加上 `WindowMinimizeButtonHint` 后原生样式变成 `SYSMENU=True, MINIMIZEBOX=True`，
而因为是 Frameless，外观完全不变。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="界面测试需要 PySide6")

from PySide6.QtCore import Qt  # noqa: E402

from ui_qt.main_window import MainWindow  # noqa: E402


class TestWindowFlags:
    def test_has_minimize_button_hint(self):
        """核心：少了它，任务栏按钮点不动（WS_MINIMIZEBOX 不会被设上）。"""
        assert MainWindow.WINDOW_FLAGS & Qt.WindowType.WindowMinimizeButtonHint

    def test_still_frameless_and_toplevel(self):
        assert MainWindow.WINDOW_FLAGS & Qt.WindowType.FramelessWindowHint
        assert MainWindow.WINDOW_FLAGS & Qt.WindowType.Window
