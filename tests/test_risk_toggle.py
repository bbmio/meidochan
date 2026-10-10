"""输入框旁「权限等级」按钮（高风险工具总开关）的回归测试。

对应改动：把 `config/plugins.toml` 的 `allow_risky_tools` 从设置界面搬到输入框旁，
默认开启，点一下即切换并立即持久化（`engine._allow_risky_tools()` 每轮实时读配置，
所以下一轮对话就生效，不需要重载插件）。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="界面测试需要 PySide6")

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from ui_qt.chat_view import ChatView  # noqa: E402
from ui_qt.main_window import MainWindow  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    """整个会话共用一个 QApplication —— Qt 不允许创建第二个。"""
    yield QApplication.instance() or QApplication([])


class _PluginsCfg:
    def __init__(self, allow: bool) -> None:
        self.allow_risky_tools = allow


class _ConfigStub:
    def __init__(self, allow: bool) -> None:
        self._allow = allow
        self.saved = None
        self.fail = False

    def get_plugins_config(self):
        return _PluginsCfg(self._allow)

    def save_plugins_config(self, cfg) -> str:
        if self.fail:
            raise OSError("磁盘只读")
        self._allow = bool(cfg.allow_risky_tools)
        self.saved = self._allow
        return "plugins.toml"


class _EngineStub:
    def __init__(self, allow: bool) -> None:
        self.config = _ConfigStub(allow)


class _SidebarStub:
    def __init__(self) -> None:
        self.last = None

    def set_status(self, text: str) -> None:
        self.last = text


class _RiskWindow(MainWindow):
    """绕过 `MainWindow.__init__`（它要完整 engine），只补被测方法要用的状态。"""

    def __init__(self, allow: bool = False) -> None:
        QWidget.__init__(self)
        self.engine = _EngineStub(allow)
        self.chat = ChatView()
        self.sidebar = _SidebarStub()


# ═══════════════════════════════════════════════════════════
# ChatView：按钮本身
# ═══════════════════════════════════════════════════════════

class TestChatViewRiskButton:
    def test_defaults_to_normal(self, qapp):
        view = ChatView()
        assert view.risk_policy() is False
        assert "普通" in view.risk_btn.text()

    def test_set_risk_policy_switches_label(self, qapp):
        view = ChatView()
        view.set_risk_policy(True)
        assert view.risk_policy() is True
        assert "高风险" in view.risk_btn.text()
        assert view.risk_btn.objectName() == "danger"     # 套危险色样式

        view.set_risk_policy(False)
        assert view.risk_policy() is False
        assert "普通" in view.risk_btn.text()
        assert view.risk_btn.objectName() == ""

    def test_click_emits_the_requested_value(self, qapp):
        view = ChatView()
        got = []
        view.risk_policy_changed.connect(got.append)

        view.risk_btn.click()                 # 普通 → 请求升为高风险
        assert got == [True]

        view.set_risk_policy(True)            # 界面已在高风险
        view.risk_btn.click()                 # → 请求降回普通
        assert got == [True, False]

    def test_button_sits_in_the_input_row(self, qapp):
        """必须在输入框那一行（用户要求的「对话框旁边」）。"""
        view = ChatView()
        # 与发送/附件按钮同属输入行（同一父控件）
        assert view.risk_btn.parent() is view.send_btn.parent()
        assert view.risk_btn.parent() is view.attach_btn.parent()


# ═══════════════════════════════════════════════════════════
# MainWindow：保存 / 回滚 / 同步
# ═══════════════════════════════════════════════════════════

class TestRiskToggleHandler:
    def test_enable_saves_and_updates(self, qapp):
        win = _RiskWindow(allow=False)
        win._on_risk_policy_changed(True)
        assert win.engine.config.saved is True
        assert win.chat.risk_policy() is True
        assert "已启用" in win.sidebar.last

    def test_disable_saves(self, qapp):
        win = _RiskWindow(allow=True)
        win._on_risk_policy_changed(False)
        assert win.engine.config.saved is False
        assert win.chat.risk_policy() is False
        assert "已禁用" in win.sidebar.last

    def test_save_failure_rolls_back_button(self, qapp):
        """保存失败时按钮必须回滚，否则界面与磁盘不一致。"""
        win = _RiskWindow(allow=False)
        win.engine.config.fail = True
        win._on_risk_policy_changed(True)
        assert win.chat.risk_policy() is False
        assert "失败" in win.sidebar.last

    def test_sync_reads_config(self, qapp):
        win = _RiskWindow(allow=True)
        win._sync_risk_button()
        assert win.chat.risk_policy() is True

    def test_sync_survives_broken_config(self, qapp):
        win = _RiskWindow(allow=True)

        def boom():
            raise RuntimeError("配置读不了")
        win.engine.config.get_plugins_config = boom
        win._sync_risk_button()
        assert win.chat.risk_policy() is False      # fail-closed
