"""工具结果落盘的回归测试（书里的「工具结果预算控制」）。

## 守的核心是「不丢信息」

单次读文件可以吐 100,000 字符（`file_explorer.MAX_FULL_CONTENT`），一次就吃掉
大半个上下文预算。但**直接截断会丢信息** —— 模型后面要用到某个细节时原文已经没了。

所以 `page_out()` 做的是「换位置」而不是「删掉」：
原文**逐字节落盘**，上下文里只放路径 + 行数 + 首尾预览。
这里最重要的一条测试就是 **落盘内容与原文逐字节相同**。
"""
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import tool_outputs  # noqa: E402
from core.tool_outputs import (  # noqa: E402
    MAX_INLINE_CHARS,
    PREVIEW_HEAD_CHARS,
    PREVIEW_TAIL_CHARS,
    page_out,
    path_for,
    prune,
)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """把落盘目录指到 tmp，别往真实 data/ 里写。"""
    out = tmp_path / "tool_outputs"
    monkeypatch.setattr(tool_outputs, "outputs_dir", lambda: out)
    tool_outputs._reset_prune_flag()
    return out


def _big(n=5000):
    return "\n".join(f"第 {i} 行：这是用来撑大体积的内容填充" for i in range(n))


def _stored(isolated) -> Path:
    """落盘目录里那唯一一个文件。

    不去复刻文件名规则（它带内容哈希），直接看目录 —— 这样测试不会和实现
    的命名细节绑死。
    """
    files = [f for f in isolated.iterdir() if f.is_file()]
    assert len(files) == 1, f"期望恰好一个落盘文件，实际 {len(files)} 个"
    return files[0]


# ═══════════════════════════════════════════════════════════
# 基本行为
# ═══════════════════════════════════════════════════════════

class TestPageOut:
    def test_small_result_untouched(self, isolated):
        small = "core/\nui_qt/\nplugins/"
        assert page_out("call_1", small) == small
        assert not list(isolated.iterdir()) if isolated.exists() else True

    def test_boundary_is_inclusive(self, isolated):
        """正好等于阈值时不落盘（阈值是「超过才处理」）。"""
        exact = "x" * MAX_INLINE_CHARS
        assert page_out("call_exact", exact) == exact

    def test_big_result_is_replaced(self, isolated):
        big = _big()
        out = page_out("call_big", big)
        assert len(out) < len(big)
        assert out != big

    def test_original_is_stored_byte_identical(self, isolated):
        """**最关键的一条**：原文必须逐字节存下来，否则就是变相截断。"""
        big = _big()
        page_out("call_big", big)
        assert _stored(isolated).read_text(encoding="utf-8") == big

    def test_preview_has_head_and_tail(self, isolated):
        """报错往往在末尾，所以首尾都要给 —— 只给开头等于把栈信息扔了。"""
        big = "HEAD" + ("中" * 20000) + "TAIL_END"
        out = page_out("call_t", big)
        assert "HEAD" in out
        assert "TAIL_END" in out, "结尾没给 → 报错信息会看不到"

    def test_preview_has_path_and_counts(self, isolated):
        big = _big(1000)
        out = page_out("call_p", big)
        assert str(_stored(isolated)) in out, "没给路径，模型无法读回"
        assert "1,000 行" in out
        assert f"{len(big):,} 字符" in out

    def test_preview_is_bounded(self, isolated):
        """预览必须远小于原文，否则落盘就没意义了。"""
        big = _big(20000)
        out = page_out("call_b", big)
        assert len(out) < PREVIEW_HEAD_CHARS + PREVIEW_TAIL_CHARS + 600
        assert len(out) < len(big) / 10

    def test_preview_points_at_a_real_tool(self, isolated):
        """预览里指路的工具名必须**真实存在**。

        实测踩过：文案一开始写的是 `read_file`，但本项目实际工具叫 `view` ——
        写错等于让模型去调一个不存在的工具，比不给指路还糟。
        """
        import json
        import re as _re

        out = page_out("call_x", _big())
        manifest = json.loads(
            (ROOT / "plugins" / "file_explorer" / "manifest.json").read_text(encoding="utf-8"))
        names = {(t.get("function", t)).get("name") for t in manifest.get("tools", [])}

        mentioned = set(_re.findall(r"用 ([a-z_]+) 按行范围", out))
        assert mentioned, "预览里没有指路怎么把内容读回来"
        assert mentioned <= names, f"预览指向了不存在的工具：{mentioned - names}"

    def test_deterministic(self, isolated):
        """同一个 id + 同一段文本 → 逐字节相同的输出（替换串冻结）。

        书里明确要求：替换决策一旦做出就被冻结，否则会话恢复后消息序列
        与缓存里的字节流对不上，prompt cache 全废。
        """
        big = _big()
        first = page_out("call_same", big)
        second = page_out("call_same", big)
        assert first == second

    def test_empty_and_none(self, isolated):
        assert page_out("call_e", "") == ""
        assert page_out("call_e", None) == ""


class TestSafeName:
    @pytest.mark.parametrize("bad", [
        "../../etc/passwd", "..\\..\\win.ini", "a/b/c", "call:1", "x" * 300,
    ])
    def test_no_path_traversal(self, isolated, bad):
        """tool_call_id 由服务端生成，但**不能**直接当路径用。

        判据是「解析后仍落在落盘目录内」，不是「文件名里没有 `..` 字样」——
        `.._.._etc_passwd.txt` 里的 `..` 只是普通字符，没有分隔符就不是穿越。
        """
        path = path_for(bad)
        assert "/" not in path.name and "\\" not in path.name
        assert len(path.name) <= 84
        # 真正要紧的：解析后必须还在落盘目录里
        assert path.resolve().parent == isolated.resolve()

    def test_empty_id(self, isolated):
        assert path_for("").name == "unknown.txt"

    def test_same_id_different_content_does_not_overwrite(self, isolated):
        """文件名带内容哈希：同一个 id 给出不同内容时不能互相覆盖。

        文件要留 7 天、跨会话。万一 id 被复用而没有哈希，旧文件会被覆盖，
        而**旧消息里记着的那个路径**还在引用它 —— 模型读回来的就是张冠李戴的内容。
        """
        a = _big(4000)
        b = _big(4000).replace("填充", "内容")
        page_out("call_same_id", a)
        page_out("call_same_id", b)

        files = sorted(f for f in isolated.iterdir() if f.is_file())
        assert len(files) == 2, "同 id 不同内容被覆盖了"
        assert {f.read_text(encoding="utf-8") for f in files} == {a, b}

    def test_same_id_same_content_reuses_one_file(self, isolated):
        """同样的内容重复落盘不该产生垃圾文件。"""
        big = _big()
        page_out("call_x", big)
        page_out("call_x", big)
        assert len([f for f in isolated.iterdir() if f.is_file()]) == 1


# ═══════════════════════════════════════════════════════════
# 兜底与清理
# ═══════════════════════════════════════════════════════════

class TestFailSafe:
    def test_disk_failure_returns_original(self, isolated, monkeypatch):
        """落盘失败时必须**原样返回** —— 宁可多占上下文，也不能把内容弄丢。"""
        big = _big()

        def boom(*_a, **_k):
            raise OSError("磁盘满了")

        monkeypatch.setattr(Path, "write_text", boom)
        assert page_out("call_fail", big) == big


class TestPrune:
    def test_removes_old_keeps_fresh(self, isolated):
        isolated.mkdir(parents=True, exist_ok=True)
        old = isolated / "old.txt"
        new = isolated / "new.txt"
        old.write_text("旧", encoding="utf-8")
        new.write_text("新", encoding="utf-8")
        stale = time.time() - 30 * 86400
        import os
        os.utime(old, (stale, stale))

        removed = prune(max_age_days=7)

        assert removed == 1
        assert not old.exists()
        assert new.exists()

    def test_missing_dir_is_graceful(self, isolated):
        assert prune() == 0

    def test_prune_runs_once_per_process(self, isolated):
        """清理只在第一次落盘时跑一遍 —— 别每次落盘都遍历目录。"""
        isolated.mkdir(parents=True, exist_ok=True)
        stale = isolated / "stale.txt"
        stale.write_text("x", encoding="utf-8")
        import os
        old = time.time() - 30 * 86400
        os.utime(stale, (old, old))

        tool_outputs._reset_prune_flag()
        page_out("call_a", _big())          # 第一次 → 触发清理
        assert not stale.exists()

        stale.write_text("x", encoding="utf-8")
        os.utime(stale, (old, old))
        page_out("call_b", _big())          # 第二次 → 不再清理
        assert stale.exists()


# ═══════════════════════════════════════════════════════════
# 接入 brain.chat_stream
# ═══════════════════════════════════════════════════════════

class _Fn:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _TC:
    def __init__(self, index, id=None, name=None, arguments=None):
        self.index = index
        self.id = id
        self.function = _Fn(name, arguments)


class _Delta:
    def __init__(self, content="", tool_calls=None):
        self.reasoning_content = ""
        self.content = content
        self.tool_calls = tool_calls


class _Chunk:
    def __init__(self, delta=None):
        self.choices = [__import__("types").SimpleNamespace(delta=delta)] if delta else []
        self.usage = None


class _Completions:
    def __init__(self, rounds):
        self._rounds = [list(r) for r in rounds]

    def create(self, **_kw):
        return list(self._rounds.pop(0)) if self._rounds else []


def _brain_with_tool(rounds):
    import types as _t
    from core.brain import Brain
    from core.config.models import ModelConfig, PersonaConfig

    brain = Brain(ModelConfig(provider="deepseek", api_key="k",
                              base_url="http://127.0.0.1:1/v1"),
                  PersonaConfig(), None)
    brain._client = _t.SimpleNamespace(
        chat=_t.SimpleNamespace(completions=_Completions(rounds)))
    return brain


class TestChatStreamIntegration:
    def _run(self, tool_output):
        rounds = [
            [_Chunk(_Delta(tool_calls=[_TC(0, id="call_big", name="read_file",
                                           arguments='{"path": "big.py"}')]))],
            [_Chunk(_Delta(content="读完了。"))],
        ]
        brain = _brain_with_tool(rounds)
        events = list(brain.chat_stream(
            [{"role": "user", "content": "读一下 big.py"}],
            lambda _n, _a: tool_output,
            system_prompt="", plugin_tools=[]))
        reported = [e for e in events if e.get("type") == "tool_messages"]
        return reported[0]["messages"] if reported else []

    def test_big_result_replaced_in_context(self, isolated):
        big = _big()
        msgs = self._run(big)
        tool_msgs = [m for m in msgs if m["role"] == "tool"]
        assert tool_msgs, "没有 tool 消息"
        assert len(tool_msgs[0]["content"]) < len(big) / 10
        assert "已落盘" in tool_msgs[0]["content"]

    def test_original_still_on_disk(self, isolated):
        big = _big()
        self._run(big)
        assert _stored(isolated).read_text(encoding="utf-8") == big

    def test_small_result_goes_through_untouched(self, isolated):
        small = "def foo():\n    return 1\n"
        msgs = self._run(small)
        tool_msgs = [m for m in msgs if m["role"] == "tool"]
        assert tool_msgs[0]["content"] == small
