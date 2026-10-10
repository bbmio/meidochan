"""托盘图标单击「弹出 / 收回」的回归测试。

改动前：托盘 `activated` 只接 `Trigger`，且 `_restore_from_tray()` **只显示、不隐藏** ——
窗口已经可见时点图标毫无反应（用户报的「没反应」）。
改动后：单击在「可见 ↔ 隐藏」之间切换。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="界面测试需要 PySide6")

from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QSystemTrayIcon, QWidget,
)

from ui_qt.main_window import MainWindow  # noqa: E402

REASON = QSystemTrayIcon.ActivationReason


@pytest.fixture(scope="session")
def qapp():
    """整个会话共用一个 QApplication —— Qt 不允许创建第二个。"""
    yield QApplication.instance() or QApplication([])


class _TrayWindow(MainWindow):
    """绕过 `MainWindow.__init__`（它要完整 engine），只补被测方法要用的状态。"""

    def __init__(self) -> None:
        QWidget.__init__(self)
        self.restores = 0

    def _restore_from_tray(self) -> None:
        self.restores += 1
        MainWindow._restore_from_tray(self)


class TestToggle:
    def test_visible_window_is_hidden(self, qapp):
        win = _TrayWindow()
        win.show()
        assert win.isVisible() is True

        win._toggle_from_tray()
        assert win.isVisible() is False          # 收回
        assert win.restores == 0

    def test_hidden_window_is_restored(self, qapp):
        win = _TrayWindow()
        win.show()
        win.hide()
        assert win.isVisible() is False

        win._toggle_from_tray()
        assert win.isVisible() is True           # 弹出
        assert win.restores == 1

    def test_minimized_window_is_restored_not_hidden(self, qapp):
        """最小化时窗口仍算 isVisible()，必须恢复而不是「再收起」。"""
        win = _TrayWindow()
        win.show()
        win.showMinimized()

        win._toggle_from_tray()
        assert win.restores == 1
        assert win.isMinimized() is False


class TestActivationReason:
    def test_trigger_toggles(self, qapp):
        win = _TrayWindow()
        win.show()
        win._on_tray_activated(REASON.Trigger)
        assert win.isVisible() is False          # 单击 = 切换

    @pytest.mark.parametrize("reason", [
        REASON.Context, REASON.MiddleClick, REASON.Unknown,
    ])
    def test_other_reasons_do_nothing(self, qapp, reason):
        """右键（Context）等不能顺手把窗口收起 —— 那是弹菜单。"""
        win = _TrayWindow()
        win.show()
        win._on_tray_activated(reason)
        assert win.isVisible() is True
        assert win.restores == 0

    def test_double_click_does_not_double_toggle(self, qapp):
        """双击会先给 Trigger 再给 DoubleClick；只认 Trigger，净效果 = 切换一次。"""
        win = _TrayWindow()
        win.show()
        win._on_tray_activated(REASON.Trigger)
        win._on_tray_activated(REASON.DoubleClick)
        assert win.isVisible() is False


class TestRestore:
    def test_restore_clears_minimized(self, qapp):
        win = _TrayWindow()
        win.show()
        win.showMinimized()
        win._restore_from_tray()
        assert win.isMinimized() is False
        assert win.isVisible() is True
