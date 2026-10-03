"""工具结果跨轮保留的回归测试。

## 修的什么

原先 `engine.respond()` 只把 assistant 的**最终文本**写回 `api_state`，而工具调用的
中间消息（`assistant(带 tool_calls)` 与 `tool` 结果）只活在 `brain.chat_stream` 内部的
临时副本里（engine 传进去的是 `list(api_state)` 的**浅拷贝**）。

后果实测过：第二轮模型会说「我上下文里没留着之前那次列目录的记录」，然后
**重新调一次同样的工具**。这正是《AI Agent》第二章警告的失效模式 ——
「使用滑动窗口的 Agent 经常陷入循环，反复执行相同的工具调用，
因为它'忘记了'之前已经获得的结果」。本项目是一轮就丢。

修法：`chat_stream` 把本轮的工具交互作为 `tool_messages` 事件 yield 出去，
由 `engine.respond()` 写进 `api_state`。

## 这里守什么

1. `chat_stream` 必须上报工具交互（否则调用方无从持久化）
2. `respond()` 必须接住并写进 `api_state`，且**不能**把概览卡 / 检索片段
   那两个临时注入一起写进去
3. 持久化之后，UI 回放与检索配对**不能**被工具消息带坏
"""
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.brain import Brain  # noqa: E402
from core.config.models import ModelConfig, PersonaConfig  # noqa: E402
from core.history import HistoryManager  # noqa: E402


# ═══════════════════════════════════════════════════════════
# 伪造的流式响应
# ═══════════════════════════════════════════════════════════

class _Function:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, index, id=None, name=None, arguments=None):
        self.index = index
        self.id = id
        self.function = _Function(name, arguments)


class _Delta:
    def __init__(self, reasoning="", content="", tool_calls=None):
        self.reasoning_content = reasoning
        self.content = content
        self.tool_calls = tool_calls


class _Chunk:
    def __init__(self, delta=None, usage=None):
        self.choices = [types.SimpleNamespace(delta=delta)] if delta is not None else []
        self.usage = usage


class _Completions:
    """按「轮」返回预置的 chunk 序列 —— 工具循环每轮调一次 create()。"""

    def __init__(self, rounds):
        self._rounds = [list(r) for r in rounds]
        self.calls = 0

    def create(self, **_kwargs):
        self.calls += 1
        if not self._rounds:
            return []
        return list(self._rounds.pop(0))


class _FakeClient:
    def __init__(self, rounds):
        self.chat = types.SimpleNamespace(completions=_Completions(rounds))


def _make_brain(rounds):
    brain = Brain(
        ModelConfig(provider="deepseek", api_key="test-key",
                    base_url="http://127.0.0.1:1/v1"),
        PersonaConfig(),
        None,
    )
    brain._client = _FakeClient(rounds)
    return brain


def _tool_round():
    """第 1 轮：模型要调一次 ls；第 2 轮：给最终回复。"""
    return [
        [_Chunk(_Delta(tool_calls=[_ToolCall(0, id="call_1", name="ls", arguments="{}")]))],
        [_Chunk(_Delta(content="目录里有 3 个文件夹。"))],
    ]


# ═══════════════════════════════════════════════════════════
# 1. chat_stream 要上报工具交互
# ═══════════════════════════════════════════════════════════

class TestChatStreamEmitsToolMessages:
    def test_tool_round_is_reported(self):
        brain = _make_brain(_tool_round())
        events = list(brain.chat_stream(
            [{"role": "user", "content": "列一下目录"}],
            lambda _name, _args: "core/\nui_qt/\nplugins/",
            system_prompt="", plugin_tools=[],
        ))

        reported = [e for e in events if e.get("type") == "tool_messages"]
        assert len(reported) == 1, "工具交互没有被上报，调用方无从持久化"

        msgs = reported[0]["messages"]
        assert [m["role"] for m in msgs] == ["assistant", "tool"]
        assert msgs[0]["tool_calls"][0]["id"] == "call_1"
        assert msgs[1]["tool_call_id"] == "call_1"
        assert "core/" in msgs[1]["content"]

    def test_plain_reply_reports_nothing(self):
        """没有工具调用时不该凭空冒出这个事件。"""
        brain = _make_brain([[_Chunk(_Delta(content="你好"))]])
        events = list(brain.chat_stream(
            [{"role": "user", "content": "hi"}],
            lambda _n, _a: "", system_prompt="", plugin_tools=[],
        ))
        assert not [e for e in events if e.get("type") == "tool_messages"]


# ═══════════════════════════════════════════════════════════
# 2. respond() 要写进 api_state
# ═══════════════════════════════════════════════════════════

class _StubEngine:
    """只补 respond() 真正会碰到的属性。"""

    def __init__(self, brain):
        self.brain = brain
        self.retrieval = None
        self.history = types.SimpleNamespace(
            current_file=types.SimpleNamespace(name="s.json"),
            count_messages=lambda: 0,
            save_api_state=lambda *a, **k: None,
            schedule_session_summary=lambda *a, **k: None,
            load_recent=lambda *a, **k: [],
            count_user_turns=lambda: 0,
        )
        self.context_engine = types.SimpleNamespace(
            build_system_prompt=lambda *a, **k: "SYS")
        self.plugin_manager = types.SimpleNamespace()
        self.current_phase = ""

    def _allow_risky_tools(self):
        return False

    def _build_system_prompt(self):
        return "SYS"

    @staticmethod
    def _build_profile_context():
        return "PROFILE_CTX"

    def _plugin_tools_for_model(self):
        return []

    def tool_executor(self, tool_name, arguments):
        return "core/\nui_qt/"


def _drive(engine, user_msg, state):
    """驱动 respond()，接住它 yield 出来的最新 api_state。

    ⚠️ `respond()` 里写的是 `api_state = api_state or []` —— 传空列表进去会被
    **替换成新对象**，所以必须接 yield 出来的那个，不能指望原地修改。
    """
    from core.engine import WhaleGirlEngine

    latest = state
    gen = WhaleGirlEngine.respond.__get__(engine, _StubEngine)
    for _a, _b, st in gen(user_msg, [], state):
        latest = st
    return latest


class TestRespondPersistsToolMessages:
    def _engine(self):
        class _Brain:
            def __init__(self):
                self.seen = []

            def chat_stream(self, messages, executor, system_prompt=None,
                            plugin_tools=None):
                self.seen.append(list(messages))
                yield {"type": "tool_messages", "messages": [
                    {"role": "assistant", "content": "",
                     "tool_calls": [{"id": "c1", "type": "function",
                                     "function": {"name": "ls", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "c1", "content": "core/"},
                ]}
                yield {"type": "done", "content": "有 1 个文件夹。"}

        return _StubEngine(_Brain())

    def test_tool_messages_reach_api_state(self):
        engine = self._engine()
        state = _drive(engine, "列目录", [])
        roles = [m["role"] for m in state]
        assert "tool" in roles, "工具结果没有写进 api_state，下一轮模型就看不到"
        assert roles[-1] == "assistant"          # 最终回复在最后

    def test_injections_are_not_persisted(self):
        """概览卡与检索片段是每轮重算的临时注入，写进历史会污染上下文。"""
        engine = self._engine()
        state = _drive(engine, "列目录", [])
        blob = " ".join(str(m.get("content") or "") for m in state)
        assert "PROFILE_CTX" not in blob

    def test_second_turn_sees_previous_tool_result(self):
        """第二轮必须能看到上一轮的工具结果 —— 这是本次修复的核心目的。"""
        engine = self._engine()
        state = _drive(engine, "列目录", [])
        _drive(engine, "第一个文件夹叫什么", state)

        second_call = engine.brain.seen[-1]
        assert any(m["role"] == "tool" for m in second_call), (
            "第二轮看不到工具结果 → 模型会重新调一次同样的工具"
        )


# ═══════════════════════════════════════════════════════════
# 3. 工具调用配对必须完整
# ═══════════════════════════════════════════════════════════

def _assistant(*ids):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"id": i, "type": "function",
                            "function": {"name": "ls", "arguments": "{}"}} for i in ids]}


def _tool(cid, text="结果"):
    return {"role": "tool", "tool_call_id": cid, "content": text}


class TestToolCallPairing:
    """`_ensure_complete_tool_calls` 必须保证「每个 tool_calls 都配齐结果」。

    ⚠️ 这里守的是一个真实事故：旧实现只检查「紧邻的前一条是不是带 tool_calls 的
    assistant」，于是一个 assistant 声明了 **2 个以上** tool_calls 时，第 2 条起
    全被丢掉，发出去直接 400：

        An assistant message with 'tool_calls' must be followed by tool messages
        responding to each 'tool_call_id'. (insufficient tool messages …)

    它在工具结果**不跨轮保留**的年代碰不到（api_state 里没有 tool 消息），
    2026-10-03 让工具结果跨轮保留之后才暴露出来。
    """

    def _brain(self):
        from core.brain import Brain
        from core.config.models import ModelConfig, PersonaConfig
        return Brain(ModelConfig(provider="deepseek", api_key="k",
                                 base_url="http://127.0.0.1:1/v1"),
                     PersonaConfig(), None)

    def _pairs_ok(self, msgs) -> bool:
        i = 0
        while i < len(msgs):
            m = msgs[i]
            if m.get("role") == "assistant" and m.get("tool_calls"):
                want = [tc["id"] for tc in m["tool_calls"] if "id" in tc]
                got = []
                j = i + 1
                while j < len(msgs) and msgs[j].get("role") == "tool":
                    got.append(msgs[j].get("tool_call_id"))
                    j += 1
                if not want or len(got) != len(want) or set(got) != set(want):
                    return False
                i = j
                continue
            if m.get("role") == "tool":
                return False          # 游离的 tool 结果
            i += 1
        return True

    @pytest.mark.parametrize("n", [1, 2, 3, 5])
    def test_multi_tool_calls_are_kept(self, n):
        """一个 assistant 声明 n 个 tool_calls → n 条结果**一条都不能少**。"""
        ids = [f"c{i}" for i in range(n)]
        msgs = [{"role": "user", "content": "干活"},
                _assistant(*ids)] + [_tool(i) for i in ids] + \
               [{"role": "assistant", "content": "干完了"}]

        out = self._brain()._ensure_complete_tool_calls(msgs)

        assert len(out) == len(msgs), f"{n} 个 tool_calls 时有消息被丢了"
        assert self._pairs_ok(out)

    def test_incomplete_group_is_dropped_whole(self):
        """配不齐时整组丢 —— 留下「有 tool_calls 没人应答」的 assistant 同样 400。"""
        msgs = [{"role": "user", "content": "干活"},
                _assistant("c1", "c2"), _tool("c1"),
                {"role": "assistant", "content": "干完了"}]

        out = self._brain()._ensure_complete_tool_calls(msgs)

        assert [m["role"] for m in out] == ["user", "assistant"]
        assert self._pairs_ok(out)

    def test_orphan_tool_is_dropped(self):
        msgs = [{"role": "user", "content": "hi"}, _tool("c1", "孤儿结果")]
        out = self._brain()._ensure_complete_tool_calls(msgs)
        assert [m["role"] for m in out] == ["user"]

    def test_plain_conversation_untouched(self):
        msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        assert self._brain()._ensure_complete_tool_calls(msgs) == msgs

    def test_realistic_session_unchanged(self):
        """一个完整合法的会话（含两轮工具调用）必须**原样通过**。"""
        msgs = [
            {"role": "user", "content": "看看目录"},
            _assistant("a1", "a2"), _tool("a1"), _tool("a2"),
            _assistant("a3"), _tool("a3"),
            {"role": "assistant", "content": "看完了"},
            {"role": "user", "content": "再改白名单"},
        ]
        out = self._brain()._ensure_complete_tool_calls(msgs)
        assert [m["role"] for m in out] == [m["role"] for m in msgs]
        assert self._pairs_ok(out)


# ═══════════════════════════════════════════════════════════
# 4. 持久化不能带坏 UI 回放与检索配对
# ═══════════════════════════════════════════════════════════

TOOL_BEARING = [
    {"role": "user", "content": "列目录"},
    {"role": "assistant", "content": "",
     "tool_calls": [{"id": "c1", "type": "function",
                     "function": {"name": "ls", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "core/\nui_qt/"},
    {"role": "assistant", "content": "有 2 个文件夹。"},
    {"role": "user", "content": "第一个叫什么"},
    {"role": "assistant", "content": "core"},
]


class TestLoadRecentSkipsToolMessages:
    def _history(self, tmp_path, messages):
        h = HistoryManager(str(tmp_path))
        h.save_api_state(messages)
        return h

    def test_pairs_contain_only_text(self, tmp_path):
        h = self._history(tmp_path, TOOL_BEARING)
        pairs = h.load_recent(10)
        assert pairs == [["列目录", "有 2 个文件夹。"], ["第一个叫什么", "core"]]

    def test_no_empty_assistant_pairs(self, tmp_path):
        """工具轮次的 assistant 正文为空，不能配成「user + 空回复」的假轮次。"""
        h = self._history(tmp_path, TOOL_BEARING)
        for user_msg, bot_msg in h.load_recent(10):
            assert user_msg.strip()
            assert bot_msg is None or bot_msg.strip()


class TestChatViewSkipsToolRounds:
    def test_empty_tool_assistant_makes_no_bubble(self):
        import os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        pytest.importorskip("PySide6", reason="需要 PySide6")
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        from ui_qt.chat_view import ChatView

        view = ChatView()
        view.load_messages(TOOL_BEARING)
        # 工具轮次的空 assistant 不该占一个气泡 → 只剩 4 条（2 问 2 答）
        assert len(view._bubbles) == 4, (
            f"气泡数 {len(view._bubbles)}，工具轮次被渲染成了空气泡"
        )
