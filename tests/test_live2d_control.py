"""AI 主动控制 Live2D 立绘的测试。

## 为什么有这组用例

这个功能的链路很长，而且有两个**不做测试就一定会踩**的坑：

1. **线程**：工具跑在 worker 线程，Live2D 是 Qt 控件。所以插件只往
   `core/live2d_control` 的队列里塞请求，由 UI 的定时器搬到 GUI 线程执行。
   队列必须线程安全、取走即清。

2. **状态机覆盖**：`_sync_agent_state()` 在每次阶段变化时用 `state_map` 覆盖表情。
   AI 刚设完「开心兴奋」，工具一返回、阶段变成 `writing`，表情立刻被换成「画笔」——
   这个功能会被当场吃掉。所以有「保持期」。

   保持期里还藏着一个更隐蔽的坑：**回合结束时不能提前调 `tracker.reset()`**。
   它会把状态置为 IDLE，之后定时器里再 `reset()` 就返回 `None`（状态无变化），
   `_sync_agent_state(None)` 直接 early-return —— 表情**永远回不去**。
   `TestHoldRelease` 里专门有一条钉住这个顺序。
"""
import json
import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core import live2d_control  # noqa: E402
#: 从 base 而不是 ui_qt.media 导入 —— 后者的 __init__ 会拉起 QtWebEngine，
#: 而 base.py 是纯 Python，没有 GUI 依赖也能拿到 AgentState
from ui_qt.media.base import AgentState  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_MODEL_DIR = REPO_ROOT / "assets" / "live2d" / "DS鲸鱼娘"


@pytest.fixture(autouse=True)
def _clean_queue():
    live2d_control.reset()
    yield
    live2d_control.reset()


# ── 1. 请求队列（跨线程）──

class TestRequestQueue:
    def test_empty_initially(self):
        assert live2d_control.take_pending() == []

    def test_expression_round_trip(self):
        live2d_control.request_expression("开心兴奋")
        assert live2d_control.take_pending() == [("expression", "开心兴奋")]

    def test_motion_round_trip(self):
        live2d_control.request_motion("喷水")
        assert live2d_control.take_pending() == [("motion", "喷水")]

    def test_clear_round_trip(self):
        live2d_control.request_clear_expression()
        assert live2d_control.take_pending() == [("clear", "")]

    def test_take_clears(self):
        live2d_control.request_expression("a")
        live2d_control.take_pending()
        assert live2d_control.take_pending() == []
        assert live2d_control.pending_count() == 0

    def test_order_is_preserved(self):
        """一轮里 AI 可能先设表情再播动作，顺序不能乱。"""
        live2d_control.request_expression("脸红")
        live2d_control.request_motion("自拍")
        assert live2d_control.take_pending() == [
            ("expression", "脸红"), ("motion", "自拍")]

    def test_concurrent_requests_are_not_lost(self):
        """工具线程写入、UI 线程读取 —— 队列必须真的线程安全。"""
        def worker(tag: str):
            for i in range(50):
                live2d_control.request_expression(f"{tag}{i}")

        threads = [threading.Thread(target=worker, args=(t,)) for t in "abcd"]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert live2d_control.pending_count() == 200

    def test_reset_empties(self):
        live2d_control.request_expression("x")
        live2d_control.reset()
        assert live2d_control.pending_count() == 0


# ── 2. 插件 ──

class TestPlugin:
    @pytest.fixture()
    def plugin(self):
        from plugins.live2d_control import main as mod
        return mod

    @pytest.fixture()
    def model_dir(self, tmp_path, monkeypatch, plugin):
        """指到一个可预测的假模型目录。"""
        root = tmp_path / "模型"
        (root / "motions").mkdir(parents=True)
        (root / "motions" / "idle.motion3.json").write_text("{}", encoding="utf-8")
        (root / "吐舌.exp3.json").write_text("{}", encoding="utf-8")
        (root / "开心兴奋.exp3.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(plugin, "_model_dir", lambda: root)
        return root

    def test_owns_tool(self, plugin):
        assert plugin.owns_tool("set_live2d_expression")
        assert plugin.owns_tool("play_live2d_motion")
        assert plugin.owns_tool("clear_live2d_expression")
        assert not plugin.owns_tool("ls")
        assert not plugin.owns_tool("")

    def test_tools_have_expected_shape(self, plugin, model_dir):
        names = {t["function"]["name"] for t in plugin.get_dynamic_tools()}
        assert names == {"set_live2d_expression", "play_live2d_motion",
                         "clear_live2d_expression"}
        for tool in plugin.get_dynamic_tools():
            assert tool["type"] == "function"
            assert tool["function"]["description"].strip()
            assert tool["function"]["parameters"]["type"] == "object"

    def test_description_lists_actual_names(self, plugin, model_dir):
        """说明里必须列出**当前模型的实际名字**，否则 AI 没法挑。"""
        desc = {t["function"]["name"]: t["function"]["description"]
                for t in plugin.get_dynamic_tools()}
        assert "开心兴奋" in desc["set_live2d_expression"]
        assert "吐舌" in desc["set_live2d_expression"]
        assert "idle" in desc["play_live2d_motion"]

    def test_no_model_means_no_tools(self, plugin, monkeypatch, tmp_path):
        """没有模型就别暴露工具 —— 否则 AI 会一直调一个注定失败的接口。"""
        monkeypatch.setattr(plugin, "_model_dir", lambda: tmp_path / "没有这个")
        assert plugin.get_dynamic_tools() == []

    def test_set_expression_queues(self, plugin, model_dir):
        assert "已切换" in plugin.set_expression("开心兴奋")
        assert live2d_control.take_pending() == [("expression", "开心兴奋")]

    def test_play_motion_queues(self, plugin, model_dir):
        assert "已播放" in plugin.play_motion("idle")
        assert live2d_control.take_pending() == [("motion", "idle")]

    def test_clear_queues(self, plugin, model_dir):
        assert "已清掉" in plugin.clear_expression()
        assert live2d_control.take_pending() == [("clear", "")]

    def test_unknown_expression_returns_full_list(self, plugin, model_dir):
        """名字对不上时把完整清单回给 AI，让它自己纠正 —— 而不是静默失败。"""
        result = plugin.set_expression("笑")
        assert "没有叫「笑」的表情" in result
        assert "开心兴奋" in result and "吐舌" in result
        assert "重新调用" in result
        assert live2d_control.take_pending() == []      # 不能塞进队列

    def test_unknown_motion_returns_full_list(self, plugin, model_dir):
        result = plugin.play_motion("跳个舞")
        assert "没有叫「跳个舞」的动作" in result
        assert "idle" in result
        assert live2d_control.take_pending() == []

    def test_no_model_reports_clearly(self, plugin, monkeypatch, tmp_path):
        monkeypatch.setattr(plugin, "_model_dir", lambda: tmp_path / "没有")
        assert "没有可用的 Live2D 模型" in plugin.set_expression("开心兴奋")
        assert "没有可用的 Live2D 模型" in plugin.play_motion("idle")

    def test_call_dynamic_tool_dispatch(self, plugin, model_dir):
        assert "已切换" in plugin.call_dynamic_tool(
            "set_live2d_expression", {"expression": "吐舌"})
        assert "未知工具" in plugin.call_dynamic_tool("不存在", {})

    def test_call_dynamic_tool_tolerates_missing_args(self, plugin, model_dir):
        """AI 少传参数时不该抛异常，要返回可读的错误。"""
        assert "没有叫" in plugin.call_dynamic_tool("set_live2d_expression", {})
        assert "已清掉" in plugin.call_dynamic_tool("clear_live2d_expression", {})

    @pytest.mark.skipif(not REAL_MODEL_DIR.is_dir(), reason="仓库里没有模型")
    def test_real_model_is_wired(self, plugin):
        """对真实模型跑一遍：名字清单要能读出来，且真的能排进队列。"""
        motions, expressions = plugin._available()
        assert motions and expressions
        assert "已切换" in plugin.set_expression(expressions[0])
        assert live2d_control.take_pending() == [("expression", expressions[0])]


# ── 3. 发给页面的 JS ──

class TestSourceJs:
    @pytest.fixture()
    def source(self):
        pytest.importorskip("PySide6", reason="Live2DSource 依赖 QtWebEngine")
        from ui_qt.media.sources.live2d_source import Live2DSource

        src = Live2DSource.__new__(Live2DSource)
        src._ready = False          # 未就绪 → _js 会排队，正好用来检查
        src._view = None
        src._queue = []
        src._last_state = None
        return src

    def _queue(self, source):
        return list(source._queue)

    def test_set_state_default_keeps_expression(self, source):
        source.set_state(AgentState.IDLE)
        assert 'MeidoLive2D.setState("idle", false);' in self._queue(source)

    def test_set_state_skip_expression(self, source):
        """保持期里必须传 true —— 只播动作、不动表情。"""
        source.set_state(AgentState.RESPONDING, skip_expression=True)
        assert 'MeidoLive2D.setState("responding", true);' in self._queue(source)

    def test_set_state_dedups(self, source):
        source.set_state(AgentState.IDLE)
        source.set_state(AgentState.IDLE)
        assert len(self._queue(source)) == 1

    def test_set_state_skip_bypasses_dedup(self, source):
        """显式要求 skip 时必须真的发出去，不能被去重挡掉。"""
        source.set_state(AgentState.IDLE)
        source.set_state(AgentState.IDLE, skip_expression=True)
        assert len(self._queue(source)) == 2

    def test_play_motion_js(self, source):
        source.play_motion("喷水")
        assert 'MeidoLive2D.playMotion("喷水");' in self._queue(source)

    def test_set_expression_js(self, source):
        source.set_expression("开心兴奋")
        assert 'MeidoLive2D.setExpression("开心兴奋");' in self._queue(source)

    def test_clear_expression_js(self, source):
        source.clear_expression()
        assert "MeidoLive2D.clearExpression();" in self._queue(source)

    def test_names_are_json_escaped(self, source):
        """名字里带引号 / 反斜杠时不能拼出坏 JS（模型文件名是不可控输入）。"""
        nasty = 'a"b\\c'
        source.set_expression(nasty)
        code = self._queue(source)[0]
        assert code == f"MeidoLive2D.setExpression({json.dumps(nasty)});"
        # 把参数抠出来必须还能被 JSON 解析 —— 说明转义是完整的
        payload = code[len("MeidoLive2D.setExpression("):-2]
        assert json.loads(payload) == nasty

    def test_queued_until_ready(self, source):
        """页面还没就绪时排队，就绪后补发 —— 不能丢指令。"""
        source.set_expression("吐舌")
        assert source._ready is False
        assert len(source._queue) == 1


# ── 4. 保持期（main_window 的胶水逻辑）──

class _ChatStub:
    def end_response(self):
        pass


class _HoldWin:
    """把真实方法挂到最小宿主上 —— 构造整个 MainWindow 要拉起 QtWebEngine，太重。

    下面三个属性由 `hold_win` fixture 从 MainWindow 上取真实方法填进来。
    """

    _drain_live2d_requests = None
    _on_response_finished = None
    _release_live2d_hold = None

    def __init__(self):
        from ui_qt.agent_state import AgentStateTracker
        self._live2d_hold = False
        self._live2d_hold_release = _TimerStub()
        self._applied = []
        self._agent_tracker = AgentStateTracker()
        self._synced = []
        self.chat = _ChatStub()
        self._desktop_chat = None
        self._bubble = None
        self._stand_window = None
        self.api_state = []

    def _apply_live2d_command(self, kind, value):
        self._applied.append((kind, value))

    def _sync_agent_state(self, state, skip_expression=False):
        self._synced.append((state, skip_expression))

    def refresh_sessions(self):
        pass


class _TimerStub:
    def __init__(self):
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


@pytest.fixture()
def hold_win():
    from ui_qt.main_window import MainWindow

    _HoldWin._drain_live2d_requests = MainWindow._drain_live2d_requests
    _HoldWin._on_response_finished = MainWindow._on_response_finished
    _HoldWin._release_live2d_hold = MainWindow._release_live2d_hold
    return _HoldWin()


class TestHoldLogic:
    def test_expression_enters_hold(self, hold_win):
        live2d_control.request_expression("开心兴奋")
        hold_win._drain_live2d_requests()
        assert hold_win._applied == [("expression", "开心兴奋")]
        assert hold_win._live2d_hold is True

    def test_clear_enters_hold(self, hold_win):
        live2d_control.request_clear_expression()
        hold_win._drain_live2d_requests()
        assert hold_win._live2d_hold is True

    def test_motion_does_not_enter_hold(self, hold_win):
        """动作是一次性的，和表情不冲突，不该拖住状态机。"""
        live2d_control.request_motion("喷水")
        hold_win._drain_live2d_requests()
        assert hold_win._applied == [("motion", "喷水")]
        assert hold_win._live2d_hold is False

    def test_empty_queue_is_noop(self, hold_win):
        hold_win._drain_live2d_requests()
        assert hold_win._applied == []
        assert hold_win._live2d_hold is False

    def test_both_requests_applied_in_order(self, hold_win):
        live2d_control.request_expression("脸红")
        live2d_control.request_motion("自拍")
        hold_win._drain_live2d_requests()
        assert hold_win._applied == [("expression", "脸红"), ("motion", "自拍")]


class TestHoldRelease:
    def test_finished_defers_when_holding(self, hold_win):
        """保持期内回合结束 → 推迟恢复，而不是立刻清掉表情。"""
        hold_win._live2d_hold = True
        hold_win._on_response_finished([])
        assert hold_win._live2d_hold_release.started == 1
        assert hold_win._synced == []          # 没有立刻同步状态

    def test_finished_syncs_normally_when_not_holding(self, hold_win):
        hold_win._on_response_finished([])
        assert hold_win._live2d_hold_release.started == 0
        assert len(hold_win._synced) == 1

    def test_finished_does_not_reset_tracker_while_holding(self, hold_win):
        """⚠️ 关键顺序：保持期内**不能**提前 reset。

        reset() 会把 tracker 置为 IDLE；之后 _release_live2d_hold 里再 reset()
        就返回 None（状态无变化），_sync_agent_state(None) 直接 early-return，
        AI 设的表情**永远回不去**。
        """
        hold_win._agent_tracker.feed_phase("writing")
        hold_win._live2d_hold = True
        hold_win._on_response_finished([])
        assert hold_win._agent_tracker.state is AgentState.RESPONDING, \
            "回合结束时提前 reset 了 —— 保持期结束后表情将无法恢复"

    def test_release_restores_idle(self, hold_win):
        """保持期结束后必须真的把状态推下去（表情才会回到默认脸）。"""
        hold_win._agent_tracker.feed_phase("writing")
        hold_win._live2d_hold = True
        hold_win._release_live2d_hold()
        assert hold_win._live2d_hold is False
        assert hold_win._synced and hold_win._synced[-1][0] is AgentState.IDLE

    def test_release_is_idempotent_safe(self, hold_win):
        """重复释放不该炸（定时器与失败路径都可能触发）。"""
        hold_win._live2d_hold = True
        hold_win._release_live2d_hold()
        hold_win._release_live2d_hold()
        assert hold_win._live2d_hold is False
