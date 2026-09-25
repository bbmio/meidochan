"""控制台输出回归测试：思考链只打印一次（曾被误认为"模型重复思考"）。

背景：core/brain.py::chat_stream 会把思考分片**逐字实时**打印，
旧版在每轮结束后又把完整思考链整段回打一遍 → 控制台出现两份相同内容。
本测试锁死"思考链与正文都只打印一次"这个行为（正文自 2026-09-12 起也实时打印）。
"""
import io
import sys
import types
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.brain import Brain  # noqa: E402
from core.config.models import ModelConfig, PersonaConfig  # noqa: E402


class _Delta:
    def __init__(self, reasoning="", content=""):
        self.reasoning_content = reasoning
        self.content = content
        self.tool_calls = None


class _Chunk:
    def __init__(self, delta):
        self.choices = [types.SimpleNamespace(delta=delta)]


class _Completions:
    def __init__(self, chunks):
        self._chunks = chunks

    def create(self, **_kwargs):
        return list(self._chunks)


class _FakeClient:
    def __init__(self, chunks):
        self.chat = types.SimpleNamespace(completions=_Completions(chunks))


def _make_brain(chunks):
    brain = Brain(
        ModelConfig(provider="deepseek", api_key="test-key", base_url="http://127.0.0.1:1/v1"),
        PersonaConfig(),
        None,
    )
    brain._client = _FakeClient(chunks)
    return brain


def test_reasoning_is_printed_only_once():
    chunks = [
        _Chunk(_Delta(reasoning="AAA")),
        _Chunk(_Delta(reasoning="BBB")),
        _Chunk(_Delta(content="XYZ")),
    ]
    brain = _make_brain(chunks)

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        events = list(brain.chat_stream(
            [{"role": "user", "content": "hi"}],
            lambda _name, _args: "",
            system_prompt="",
            plugin_tools=[],
        ))
    output = buffer.getvalue()

    types_seen = {e["type"] for e in events}
    assert "reasoning" in types_seen and "text" in types_seen

    # 完整思考链 "AAABBB" 只能出现一次（实时逐字打印那一遍）
    assert output.count("AAABBB") == 1, f"思考链被重复打印了：\n{output}"
    # 元信息行仍然保留，便于排查
    assert "[思考链 #1 完整 6 字]" in output
    assert "[正文 #1 完整 3 字]" in output
    # 正文实时打印一次、且不再整段回打
    assert output.count("XYZ") == 1, f"正文应只出现一次：\n{output}"
    # 思考链与正文之间有分隔标记，避免两段粘连
    assert "[正文] " in output


def test_reasoning_events_are_streamed_to_caller():
    chunks = [
        _Chunk(_Delta(reasoning="R1")),
        _Chunk(_Delta(content="C1")),
    ]
    brain = _make_brain(chunks)

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        events = list(brain.chat_stream(
            [{"role": "user", "content": "hi"}],
            lambda _name, _args: "",
            system_prompt="",
            plugin_tools=[],
        ))

    assert [e["content"] for e in events if e["type"] == "reasoning"] == ["R1"]
    assert [e["content"] for e in events if e["type"] == "text"] == ["C1"]
