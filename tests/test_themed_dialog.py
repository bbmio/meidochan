"""主题化弹窗 / 关闭确认 / 应用图标的回归测试。

对应 2026-10-03 的三项改动：

- **关闭确认**：✕ 不再直接最小化到托盘，而是问一句「最小化 / 直接退出 / 取消」。
  这里守的是三个分支的行为 —— 尤其「取消」绝不能当成同意退出。
- **主题化弹窗**：不再用带系统标题栏的裸 `QMessageBox`，改用无边框 + 主题令牌。
  这里守按钮 → key 的映射与样式套用（映射错了会让确认框返回错的选项）。
- **应用图标**：`assets/app.ico` 必须是从立绘裁出的多尺寸图标，且 `_app_icon()`
  优先用它（拿 1100x1800 的全身立绘当图标，托盘里只会是一团糊）。

⚠️ 弹窗函数是模态阻塞的，测试里一律 monkeypatch，不真的弹。
"""
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="界面测试需要 PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QWidget  # noqa: E402

import ui_qt.main_window as mw  # noqa: E402
from ui_qt.main_window import MainWindow  # noqa: E402
from ui_qt.themed_dialog import Choice, ThemedMessageBox, build_box  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    yield QApplication.instance() or QApplication([])


# ═══════════════════════════════════════════════════════════
# 主题化弹窗
# ═══════════════════════════════════════════════════════════

class TestThemedBox:
    CHOICES = [Choice("tray", "最小化到托盘", primary=True),
               Choice("quit", "直接退出", danger=True),
               Choice("cancel", "取消", role=QMessageBox.ButtonRole.RejectRole)]

    def test_button_key_mapping(self, qapp):
        """按钮 → key 的映射必须一一对应，错了会让调用方拿到错的选项。"""
        box, mapping = build_box(None, "标题", "正文", self.CHOICES)
        labels = [b.text() for b in box.buttons()]
        assert labels == ["最小化到托盘", "直接退出", "取消"]
        assert [mapping[b] for b in box.buttons()] == ["tray", "quit", "cancel"]
        box.close()

    def test_style_object_names(self, qapp):
        """primary / danger 要套上对应 objectName，QSS 才命中。"""
        box, _ = build_box(None, "标题", "正文", self.CHOICES)
        by_text = {b.text(): b.objectName() for b in box.buttons()}
        assert by_text["最小化到托盘"] == "primary"
        assert by_text["直接退出"] == "danger"
        assert by_text["取消"] == ""
        box.close()

    def test_default_button_follows_key(self, qapp):
        box, _ = build_box(None, "标题", "正文", self.CHOICES, default="cancel")
        assert box.defaultButton().text() == "取消"
        box.close()

    def test_is_frameless_and_themed(self, qapp):
        """去掉系统标题栏是这次改动的核心，别被改回默认。"""
        from PySide6.QtCore import Qt

        box, _ = build_box(None, "标题", "正文", self.CHOICES)
        assert box.windowFlags() & Qt.WindowType.FramelessWindowHint
        assert box.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        assert isinstance(box, ThemedMessageBox)
        box.close()

    def test_background_is_actually_painted(self, qapp):
        """背景必须真的画出来。

        这条是防回归的关键：窗口设了 `WA_TranslucentBackground`，Qt 会**跳过**
        `paintBackground`，QSS 里的 `background` 根本不会被绘制 —— 实测那时整个
        弹窗 alpha=0，完全透明，只有文字和按钮看得见。所以背景是 `paintEvent`
        自绘的，这里用像素断言把它钉住。
        """
        from ui_qt import theme

        theme.set_active_theme(theme.DARK_TOKENS)
        qapp.setStyleSheet(theme.build_qss())
        box, _ = build_box(None, "标题", "正文", self.CHOICES)
        box.show()
        for _ in range(8):
            qapp.processEvents()
        image = box.grab().toImage()
        center = image.pixelColor(image.width() // 2, image.height() // 2)
        assert center.alpha() == 255, "弹窗背景没被绘制（整块是透明的）"
        # 深色主题下应当接近 bg 令牌，而不是白色
        assert center.red() < 80, f"深色主题下背景仍偏亮：{center.name()}"
        # 四角应当透明，才说明圆角真的生效了
        corner = image.pixelColor(0, 0)
        assert corner.alpha() == 0, "圆角没有生效（角落不是透明的）"
        box.close()


# ═══════════════════════════════════════════════════════════
# 关闭确认
# ═══════════════════════════════════════════════════════════

class _EventStub:
    """用真的 `QCloseEvent`：`super().closeEvent()` 只接受这个类型。

    `ignore()` 会把 `isAccepted()` 置 False，正好用来断言「窗口没被关掉」。
    """

    @staticmethod
    def new():
        from PySide6.QtGui import QCloseEvent

        return QCloseEvent()


class _TrayStub:
    def __init__(self):
        self.messages = []

    def showMessage(self, *args):
        self.messages.append(args)


class _CloseWindow(MainWindow):
    """绕过 `MainWindow.__init__`，只补 closeEvent 会用到的状态。"""

    def __init__(self, action):
        QWidget.__init__(self)
        self._action = action
        self._force_quit = False
        self.tray = _TrayStub()
        self.hidden = 0
        self._desktop_chat = None
        self._bubble = None
        self._stand_window = None
        self.engine = types.SimpleNamespace(retrieval=None)

    def _ask_close_action(self):
        return self._action

    def _save_window_state(self):
        pass

    def hide(self):
        self.hidden += 1


@pytest.fixture
def quiet_close(monkeypatch):
    """拦掉 closeEvent 尾部的落盘与退出，只留分支行为可观测。"""
    import core.memory.profile_cards as cards

    monkeypatch.setattr(cards, "flush_to_disk", lambda: True)
    quits = []
    monkeypatch.setattr(mw.QApplication, "quit", lambda: quits.append(1))
    return quits


class TestCloseDialog:
    def test_choices_have_expected_keys(self):
        """分支逻辑按 key 走，key 改了会静默走错分支。"""
        keys = [c.key for c in MainWindow.CLOSE_CHOICES]
        assert keys == ["tray", "quit", "cancel"]

    def test_tray_hides_and_balloons(self, qapp, quiet_close):
        win = _CloseWindow("tray")
        event = _EventStub.new()
        win.closeEvent(event)
        assert event.isAccepted() is False   # 不真的关闭
        assert win.hidden == 1
        assert len(win.tray.messages) == 1
        assert quiet_close == []             # 没退出进程

    def test_cancel_changes_nothing(self, qapp, quiet_close):
        """取消 = 什么都不做。绝不能当成「同意退出」。"""
        win = _CloseWindow("cancel")
        event = _EventStub.new()
        win.closeEvent(event)
        assert event.isAccepted() is False
        assert win.hidden == 0
        assert win.tray.messages == []
        assert quiet_close == []

    def test_none_means_cancel(self, qapp, quiet_close):
        """直接关掉对话框（Esc / ✕）返回 None，也必须当作取消。"""
        win = _CloseWindow(None)
        event = _EventStub.new()
        win.closeEvent(event)
        assert event.isAccepted() is False
        assert win.hidden == 0
        assert quiet_close == []

    def test_quit_actually_exits(self, qapp, quiet_close):
        win = _CloseWindow("quit")
        event = _EventStub.new()
        win.closeEvent(event)
        assert event.isAccepted() is True    # 放行，真的关
        assert win.hidden == 0
        assert win._force_quit is True
        assert quiet_close == [1]            # 进程结束

    def test_force_quit_skips_dialog(self, qapp, quiet_close):
        """托盘菜单的「退出」不该再问一遍。"""
        win = _CloseWindow("cancel")         # 万一被问到就会取消
        win._force_quit = True
        event = _EventStub.new()
        win.closeEvent(event)
        assert event.isAccepted() is True
        assert quiet_close == [1]


# ═══════════════════════════════════════════════════════════
# 应用图标
# ═══════════════════════════════════════════════════════════

class TestAppIcon:
    ICO = ROOT / "assets" / "app.ico"
    SOURCE = ROOT / "plugins" / "static_stand" / "stand.png"

    def test_ico_exists_and_is_multi_size(self):
        pytest.importorskip("PIL", reason="图标检查需要 Pillow")
        from PIL import Image

        assert self.ICO.exists(), "assets/app.ico 缺失，跑 tools/make_app_icon.py 生成"
        with Image.open(self.ICO) as im:
            sizes = set(im.ico.sizes())
        # 任务栏/标题栏要 16、资源管理器要 32、大图标要 256
        for need in ((16, 16), (32, 32), (48, 48), (256, 256)):
            assert need in sizes, f"图标缺少 {need} 档，缩到那个尺寸会糊"

    def test_ico_is_square(self):
        pytest.importorskip("PIL", reason="图标检查需要 Pillow")
        from PIL import Image

        with Image.open(self.ICO) as im:
            assert im.width == im.height

    def test_app_icon_prefers_ico(self, qapp):
        """`_app_icon()` 必须优先用裁好的 ico，而不是全身立绘。"""
        icon = mw._app_icon()
        assert not icon.isNull()
        # 用 ico 时 Qt 能给出多个尺寸；拿全身立绘当图标只会有一档
        assert len(icon.availableSizes()) > 1, (
            "窗口/托盘图标仍在使用单尺寸图（多半是回退到了全身立绘）"
        )

    def test_icon_source_crop_matches_tool(self):
        """`tools/make_app_icon.py` 的裁切框必须在源图范围内。"""
        pytest.importorskip("PIL", reason="图标检查需要 Pillow")
        from PIL import Image

        sys.path.insert(0, str(ROOT / "tools"))
        import importlib

        mod = importlib.import_module("make_app_icon")
        left, top, right, bottom = mod.CROP
        with Image.open(self.SOURCE) as im:
            assert 0 <= left < right <= im.width
            assert 0 <= top < bottom <= im.height
            assert right - left == bottom - top, "裁切框必须是正方形，否则图标会被拉伸"
