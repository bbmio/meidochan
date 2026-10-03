"""本轮「细节优化」的行为回归测试。

覆盖六项改动，每项都钉住**改动前会失败**的那个点：

- A1 窗口位置/大小记忆（`clamp_to_screen` 纯函数 + 存取往返）
- A2 生成中仍可输入（不再 `setReadOnly`；提交被挡时给反馈而不是静默吞掉）
- A3 最大化图标随窗口状态切换
- A4 会话列表标出当前会话
- A5 Esc 只在真正生成时才生效
- B1 `_EngineStartThread` 的成功/失败都要如实上报

刻意做到零副作用：`_window_state_path` 一律猴子补丁到 tmp_path，
绝不往真实 `data/` 里写文件。
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# offscreen 必须在导入 QtWidgets 之前设好，否则无显示环境起不来
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="界面测试需要 PySide6")

from PySide6.QtCore import QEvent, QRect, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton, QWidget  # noqa: E402

import ui_qt.main_window as mw  # noqa: E402
from ui_qt.chat_view import ChatView  # noqa: E402
from ui_qt.main_window import MainWindow, clamp_to_screen  # noqa: E402
from ui_qt.pet_input_bar import PetInputBar  # noqa: E402
from ui_qt.sidebar import Sidebar  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    """整个会话共用一个 QApplication —— Qt 不允许创建第二个。"""
    yield QApplication.instance() or QApplication([])


class _BridgeStub:
    def __init__(self, busy: bool) -> None:
        self._busy = busy

    def busy(self) -> bool:
        return self._busy


class _TestWindow(MainWindow):
    """绕过 `MainWindow.__init__`（它需要一个完整 engine），只补被测方法要用的状态。

    必须**真的继承** MainWindow：`keyPressEvent` 末尾调 `super().keyPressEvent()`，
    若把一个非 MainWindow 的对象硬绑上这个方法，`super()` 会直接抛 TypeError。
    """

    def __init__(self, busy: bool = False) -> None:
        QWidget.__init__(self)
        self.bridge = _BridgeStub(busy)
        self.stopped = 0
        self._max_btn = QPushButton("▢")

    def _on_stop(self) -> None:      # 覆盖掉真实实现，只记次数
        self.stopped += 1


def _esc_event() -> QKeyEvent:
    return QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                     Qt.KeyboardModifier.NoModifier)


# ═══════════════════════════════════════════════════════════
# A1 窗口位置 / 大小记忆
# ═══════════════════════════════════════════════════════════

class TestClampToScreen:
    """坐标越界要夹回屏内，否则换分辨率后窗口会"消失"在屏幕外。"""

    SINGLE = [QRect(0, 0, 1920, 1080)]

    def test_inside_is_untouched(self):
        assert clamp_to_screen(100, 100, 1000, 700, self.SINGLE) == (100, 100)

    def test_beyond_right_edge_is_clamped(self):
        x, y = clamp_to_screen(1500, 100, 1000, 700, self.SINGLE)
        assert x == 1920 - 1000        # 右边缘正好贴住屏幕右边
        assert y == 100

    def test_far_outside_snaps_to_nearest_screen(self):
        x, y = clamp_to_screen(3000, 2000, 1000, 700, self.SINGLE)
        assert x == 1920 - 1000
        assert y == 1080 - 700

    def test_nearest_screen_wins(self):
        areas = [QRect(0, 0, 1920, 1080), QRect(3000, 0, 1920, 1080)]
        # 2500 落在两块屏幕之间：离第二块更近（500 < 581），应贴到第二块左边
        assert clamp_to_screen(2500, 500, 1000, 700, areas)[0] == 3000

    def test_no_screens_is_a_noop(self):
        assert clamp_to_screen(100, 200, 800, 600, []) == (100, 200)


class TestWindowStatePersistence:
    def test_roundtrip(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setattr(mw, "_window_state_path",
                            lambda: tmp_path / "window_state.json")
        # 夹取会受真实屏幕尺寸影响，这里单独测过了，往返测试只关心存取一致
        monkeypatch.setattr(mw, "clamp_to_screen", lambda x, y, w, h, areas=None: (x, y))

        win = _TestWindow()
        win.resize(1000, 700)
        win.move(120, 90)
        win._save_window_state()

        data = json.loads((tmp_path / "window_state.json").read_text(encoding="utf-8"))
        assert (data["w"], data["h"]) == (1000, 700)
        assert (data["x"], data["y"]) == (120, 90)
        assert data["maximized"] is False

        restored = _TestWindow()
        restored._restore_window_state()
        assert (restored.width(), restored.height()) == (1000, 700)
        assert (restored.x(), restored.y()) == (120, 90)

    def test_corrupt_state_is_ignored(self, qapp, tmp_path, monkeypatch):
        """窗口状态文件坏了不该让程序起不来 —— 回落默认几何即可。"""
        path = tmp_path / "window_state.json"
        path.write_text("这不是 json {{{", encoding="utf-8")
        monkeypatch.setattr(mw, "_window_state_path", lambda: path)

        win = _TestWindow()
        win.resize(1280, 800)
        win._restore_window_state()          # 不应抛异常
        assert (win.width(), win.height()) == (1280, 800)

    def test_missing_state_file_is_ignored(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setattr(mw, "_window_state_path",
                            lambda: tmp_path / "nope.json")
        _TestWindow()._restore_window_state()   # 不应抛异常


# ═══════════════════════════════════════════════════════════
# A2 生成中仍可输入
# ═══════════════════════════════════════════════════════════

class TestTypingWhileGenerating:
    def test_input_stays_editable(self, qapp):
        view = ChatView()
        view.set_busy(True)
        assert view.input.isReadOnly() is False
        assert view.input.isEnabled() is True
        assert view.send_btn.isEnabled() is False
        assert view.stop_btn.isEnabled() is True

    def test_submit_is_blocked_but_gives_feedback(self, qapp):
        view = ChatView()
        sent = []
        view.send_requested.connect(sent.append)
        view.input.setPlainText("下一条消息")
        view.set_busy(True)

        view.submit()

        assert sent == []                          # 没有真的发出去
        assert "正在生成中" in view.status.text()   # 但给了可读反馈
        assert view.input.toPlainText() == "下一条消息"   # 草稿没被清掉

    def test_startup_lock_does_not_light_up_stop(self, qapp):
        """启动阶段没有可停止的生成，不能把「停止」按钮点亮。"""
        view = ChatView()
        view.set_startup_lock(True)
        assert view.send_btn.isEnabled() is False
        assert view.stop_btn.isEnabled() is False
        assert view.input.isEnabled() is False

        view.set_startup_lock(False)
        assert view.send_btn.isEnabled() is True
        assert view.input.isEnabled() is True

    def test_startup_lock_takes_priority_over_idle(self, qapp):
        view = ChatView()
        view.set_startup_lock(True)
        view.set_busy(False)               # 不应因此解锁发送
        assert view.send_btn.isEnabled() is False

    def test_pet_input_bar_keeps_draft(self, qapp):
        bar = PetInputBar()
        sent = []
        bar.send_requested.connect(sent.append)
        bar.set_enabled_input(False)

        assert bar.edit.isEnabled() is True        # 仍可打字
        bar.edit.setText("桌宠这边也想说话")
        bar.submit()

        assert sent == []
        assert bar.edit.text() == "桌宠这边也想说话"
        assert "生成中" in bar.edit.placeholderText()   # placeholder 当反馈位


# ═══════════════════════════════════════════════════════════
# A3 最大化图标
# ═══════════════════════════════════════════════════════════

class TestMaximizeButton:
    def test_icon_follows_state(self, qapp):
        win = _TestWindow()
        win.isMaximized = lambda: False
        win._sync_maximize_button()
        assert win._max_btn.text() == "▢"

        win.isMaximized = lambda: True
        win._sync_maximize_button()
        assert win._max_btn.text() == "⧉"

    def test_window_state_change_event_updates_icon(self, qapp):
        """双击标题栏 / 系统快捷键走的是 changeEvent，图标必须跟着变。"""
        win = _TestWindow()
        win.isMaximized = lambda: True
        win.changeEvent(QEvent(QEvent.Type.WindowStateChange))
        assert win._max_btn.text() == "⧉"


# ═══════════════════════════════════════════════════════════
# A4 当前会话高亮
# ═══════════════════════════════════════════════════════════

class TestSessionHighlight:
    @staticmethod
    def _rows(sidebar):
        rows = []
        for i in range(sidebar._session_layout.count()):
            widget = sidebar._session_layout.itemAt(i).widget()
            if widget is not None:
                rows.append(widget)
        return rows

    def test_only_current_row_is_flagged(self, qapp):
        sidebar = Sidebar()
        sessions = [
            {"file": "a.json", "title": "会话A", "count": 3, "summary": ""},
            {"file": "b.json", "title": "会话B", "count": 5, "summary": ""},
        ]
        sidebar.set_sessions(sessions, 30, current_file="b.json")
        assert [r.property("current") for r in self._rows(sidebar)] == ["false", "true"]

    def test_no_current_file_flags_nothing(self, qapp):
        sidebar = Sidebar()
        sessions = [{"file": "a.json", "title": "会话A", "count": 3, "summary": ""}]
        sidebar.set_sessions(sessions, 30)
        assert [r.property("current") for r in self._rows(sidebar)] == ["false"]

    def test_unknown_current_file_flags_nothing(self, qapp):
        sidebar = Sidebar()
        sessions = [{"file": "a.json", "title": "会话A", "count": 3, "summary": ""}]
        sidebar.set_sessions(sessions, 30, current_file="不存在.json")
        assert [r.property("current") for r in self._rows(sidebar)] == ["false"]


# ═══════════════════════════════════════════════════════════
# A5 Esc 只在生成时生效
# ═══════════════════════════════════════════════════════════

class TestEscapeKey:
    def test_idle_escape_has_no_side_effect(self, qapp):
        win = _TestWindow(busy=False)
        win.keyPressEvent(_esc_event())
        assert win.stopped == 0

    def test_busy_escape_stops_generation(self, qapp):
        win = _TestWindow(busy=True)
        win.keyPressEvent(_esc_event())
        assert win.stopped == 1


# ═══════════════════════════════════════════════════════════
# B1 引擎启动线程
# ═══════════════════════════════════════════════════════════

class TestEngineStartThread:
    def test_success_emits_started_ok(self, qapp):
        from ui_qt.app import _EngineStartThread

        class _Engine:
            def __init__(self):
                self.called = False

            def start(self):
                self.called = True

        engine = _Engine()
        thread = _EngineStartThread(engine)
        got = []
        thread.started_ok.connect(lambda: got.append("ok"))
        thread.failed.connect(got.append)

        thread.run()                      # 直接跑，不起真线程，避免时序抖动

        assert engine.called is True
        assert got == ["ok"]

    def test_failure_emits_traceback(self, qapp):
        from ui_qt.app import _EngineStartThread

        class _Engine:
            def start(self):
                raise RuntimeError("插件加载炸了")

        thread = _EngineStartThread(_Engine())
        got = []
        thread.started_ok.connect(lambda: got.append("ok"))
        thread.failed.connect(got.append)

        thread.run()                      # 异常必须被接住，不能漏到线程外

        assert len(got) == 1
        assert "插件加载炸了" in got[0]
