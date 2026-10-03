"""生成期间「上下文重绑定」守卫的回归测试。

对应设计规格 §6：bridge busy 时，切换工作空间 / 新建会话 / 打开其他会话
一律不执行（提示「请先停止回复」），避免旧回复把副作用写进新目标。
"""
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class _SidebarStub:
    def __init__(self):
        self.msgs = []

    def set_status(self, text):
        self.msgs.append(text)


class _LabelStub:
    def __init__(self):
        self.texts = []

    def setText(self, text):
        self.texts.append(text)


class _BridgeStub:
    def __init__(self, busy):
        self._busy = busy

    def busy(self):
        return self._busy


class _EngineRecorder:
    """记录一切被调用的方法；守卫生效时它应当完全安静。"""

    def __init__(self):
        self.calls = []
        self.workspace_mgr = self
        self.history = self
        self.brain = types.SimpleNamespace(current_model="测试模型")

    # ── 工作空间 ──
    @property
    def current(self):
        return None

    def switch_workspace(self, wid):
        self.calls.append(("switch_workspace", wid))
        return " 已切换"

    def create_workspace(self, name):
        self.calls.append(("create_workspace", name))
        return " 已创建"

    def delete_workspace(self, wid):
        self.calls.append(("delete_workspace", wid))

    # ── 会话 ──
    @property
    def current_file(self):
        return types.SimpleNamespace(name="cur.json")

    def count_messages(self):
        return 1

    def switch_to_session(self, name):
        self.calls.append(("switch_to_session", name))

    def load_api_state(self):
        return []

    def get_messages(self):
        return []

    def new_session(self):
        self.calls.append(("new_session",))
        return " 已开启新会话"


def _win(busy: bool):
    from ui_qt.main_window import MainWindow

    class _Win:
        def __init__(self):
            self.bridge = _BridgeStub(busy)
            self.sidebar = _SidebarStub()
            self.status_model = _LabelStub()
            self.engine = _EngineRecorder()
            self.refreshes = 0
            self.api_state = []
            self.chat = self
            # 真实方法在实例化时才挂（类体里挂会在方法还没实现时变成收集错误，
            # 把同一文件里其它已经能跑的用例也一起挡住）
            for name in ("_guard_busy", "_switch_workspace", "_on_workspace_create",
                         "_on_workspace_delete", "_on_new_session", "_on_session_open"):
                setattr(self, name, getattr(MainWindow, name).__get__(self, _Win))

        def refresh_workspaces(self):
            self.refreshes += 1

        def refresh_sessions(self):
            pass

        def clear(self):
            pass

        def load_messages(self, messages):
            pass

    return _Win()


class TestBusyGuards:
    def test_switch_blocked_while_busy(self):
        win = _win(busy=True)
        win._switch_workspace("b")
        assert win.refreshes == 1                       # 下拉被拉回当前空间
        assert "请先停止回复" in win.sidebar.msgs[-1]
        assert win.engine.calls == []                   # 引擎完全没被碰

    def test_create_blocked_while_busy(self):
        win = _win(busy=True)
        win._on_workspace_create("新空间")
        assert win.engine.calls == []
        assert "请先停止回复" in win.sidebar.msgs[-1]

    def test_delete_blocked_while_busy(self):
        win = _win(busy=True)
        win._on_workspace_delete()
        assert win.engine.calls == []
        assert "请先停止回复" in win.sidebar.msgs[-1]

    def test_new_session_blocked_while_busy(self):
        win = _win(busy=True)
        win._on_new_session()
        assert win.engine.calls == []
        assert "请先停止回复" in win.sidebar.msgs[-1]

    def test_session_open_blocked_while_busy(self):
        win = _win(busy=True)
        win._on_session_open("hist.json")
        assert win.engine.calls == []
        assert "请先停止回复" in win.sidebar.msgs[-1]

    def test_switch_refused_keeps_ui(self):
        """引擎拒绝切换（如概览卡保存失败）时：只提示，不假装已切换。"""
        win = _win(busy=False)
        win._switch_workspace("b")
        assert win.engine.calls == [("switch_workspace", "b")]
        assert win.refreshes == 1                       # 只是把下拉拉回
        assert win.sidebar.msgs[-1] == " 已切换"

    def test_idle_new_session_proceeds(self):
        win = _win(busy=False)
        win._on_new_session()
        assert ("new_session",) in win.engine.calls
