"""请求前缀布局的回归测试（prompt cache 约束）。

## 为什么要守这个

Prompt cache 的失效边界在「**第一个被改动的 token**」处 —— 那之后的所有内容都要
重算。所以请求里的内容必须按「变动频率」排：静态的放前面，动态的放末尾。

本项目实测：静态人设只有 533 字符，而它后面跟着自我认知（4382）+ 工具定义（6304）。
一旦把动态内容插进前半段，代价就是上万字符整段重算（缓存读取只要首次计算的 1/10）。

这里守住 2026-10-03 的三项改动，防止它们被无意改回去：

1. **概览卡不再拼进 system prompt** —— 它随对话演进（每 4 轮重抽），
   原先坐在第 5 个位置，一改就作废其后 10,000+ 字符
2. **流式请求带 `stream_options`** —— 不加就完全拿不到 usage，缓存命中率无从观测
3. **插件扫描顺序固定** —— 工具定义占 6000+ 字符，`iterdir()` 顺序一变整段作废
"""
import json
import logging
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# ═══════════════════════════════════════════════════════════
# 1. 概览卡的位置
# ═══════════════════════════════════════════════════════════

class TestProfileCardsPosition:
    SENTINEL = "SENTINEL_CARD_TEXT_勿删"

    def test_engine_system_prompt_excludes_profile_cards(self, monkeypatch):
        """概览卡绝不能出现在 system prompt 里。

        它是动态内容：放进去就等于把「自我认知 + 工具定义」整段绑在它身上，
        它一变（每 4 轮一次）全部重算。
        """
        import core.memory.profile_cards as pc
        monkeypatch.setattr(pc, "get_profile_prompt", lambda: self.SENTINEL)

        from core.engine import WhaleGirlEngine
        engine = WhaleGirlEngine()

        assert self.SENTINEL not in engine._build_system_prompt()

    def test_profile_context_carries_the_cards(self, monkeypatch):
        import core.memory.profile_cards as pc
        monkeypatch.setattr(pc, "get_profile_prompt", lambda: self.SENTINEL)

        from core.engine import WhaleGirlEngine
        engine = WhaleGirlEngine()
        text = engine._build_profile_context()

        assert self.SENTINEL in text
        # 必须写明「不是用户说的话」—— 它是以 user 角色注入的
        assert "不是用户" in text

    def test_profile_context_empty_when_no_cards(self, monkeypatch):
        """没有卡片时返回空串，调用方据此跳过注入（不要塞一条空消息）。"""
        import core.memory.profile_cards as pc
        monkeypatch.setattr(pc, "get_profile_prompt", lambda: "")

        from core.engine import WhaleGirlEngine
        assert WhaleGirlEngine()._build_profile_context() == ""

    def test_context_engine_puts_cards_last(self, tmp_path):
        """`build_system_prompt` 自己也要把概览卡垫底（兼容旧调用路径）。"""
        from core.context_engine import ContextEngine

        cfg = tmp_path / "cfg"
        cfg.mkdir()
        ce = ContextEngine(str(cfg))
        ce.set_workspace_persona("PERSONA_MARK")
        ce.add_pin("PIN_MARK")

        prompt = ce.build_system_prompt(profile_cards="CARD_MARK")

        assert prompt.index("PERSONA_MARK") < prompt.index("PIN_MARK")
        assert prompt.index("PIN_MARK") < prompt.index("CARD_MARK")
        assert prompt.rstrip().endswith("CARD_MARK")

    def test_respond_injects_cards_as_trailing_message(self, monkeypatch):
        """端到端：`respond()` 把概览卡作为**尾部消息**注入，而不是塞进 system prompt。

        这条把「构建」和「接线」连起来验证 —— 只测 `_build_profile_context()`
        是测不出「到底有没有被用上」的。
        """
        import core.memory.profile_cards as pc
        monkeypatch.setattr(pc, "get_profile_prompt", lambda: "CARD_MARK")
        monkeypatch.setattr(pc, "schedule_extraction", lambda *a, **k: None)

        from core.engine import WhaleGirlEngine
        engine = WhaleGirlEngine()

        captured = {}

        def fake_chat_stream(messages, executor, system_prompt=None, plugin_tools=None):
            captured["messages"] = list(messages)
            captured["system"] = system_prompt
            return iter(())              # 不产生任何事件，直接结束

        monkeypatch.setattr(engine.brain, "chat_stream", fake_chat_stream)
        # 隔离所有写盘副作用，别碰用户真实会话
        monkeypatch.setattr(engine.history, "count_messages", lambda: 0)
        monkeypatch.setattr(engine.history, "save_api_state", lambda *a, **k: None)
        monkeypatch.setattr(engine.history, "schedule_session_summary", lambda *a, **k: None)
        monkeypatch.setattr(engine.history, "load_recent", lambda *a, **k: [])
        engine.retrieval = None

        for _ in engine.respond("你好", [], []):
            pass

        assert captured, "respond() 没有走到 chat_stream"
        # 1) system prompt 里不该有概览卡
        assert "CARD_MARK" not in captured["system"]
        # 2) 它应当作为**最后一条消息**出现（检索片段被隔离掉了，所以就是末条）
        messages = captured["messages"]
        assert messages, "没有任何消息"
        assert "CARD_MARK" in messages[-1]["content"]
        assert messages[-1]["role"] == "user"


# ═══════════════════════════════════════════════════════════
# 2. usage 埋点
# ═══════════════════════════════════════════════════════════

class _Usage:
    """模拟 openai 的 CompletionUsage（含 DeepSeek 的缓存字段）。"""

    def __init__(self, prompt=1000, completion=50, hit=None, miss=None):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        if hit is not None:
            self.prompt_cache_hit_tokens = hit
        if miss is not None:
            self.prompt_cache_miss_tokens = miss


class TestUsageLogging:
    def test_reports_cache_hit_rate(self, caplog):
        from core.brain import _log_usage

        with caplog.at_level(logging.INFO):
            _log_usage(_Usage(prompt=1000, hit=900, miss=100), "主对话")

        text = caplog.text
        assert "缓存命中=900" in text
        assert "未命中=100" in text
        assert "命中率=90.0%" in text

    def test_reports_zero_hit_rate(self, caplog):
        """全未命中也要如实打出来 —— 这正是「前缀被改动」的信号。"""
        from core.brain import _log_usage

        with caplog.at_level(logging.INFO):
            _log_usage(_Usage(hit=0, miss=1000), "主对话")

        assert "命中率=0.0%" in caplog.text

    def test_local_model_without_cache_fields(self, caplog):
        """Ollama 不返回那两个字段，此时只记 prompt / completion，不能崩。"""
        from core.brain import _log_usage

        with caplog.at_level(logging.INFO):
            _log_usage(_Usage(prompt=71, completion=8), "主对话")

        text = caplog.text
        assert "prompt=71" in text
        assert "completion=8" in text
        assert "命中率" not in text

    def test_none_usage_is_silent(self, caplog):
        from core.brain import _log_usage

        with caplog.at_level(logging.INFO):
            _log_usage(None, "主对话")

        assert "用量" not in caplog.text


class TestStreamOptions:
    def test_stream_requests_ask_for_usage(self):
        """不加 stream_options，流式响应里**完全没有** usage。"""
        from core.engine import WhaleGirlEngine

        kwargs = WhaleGirlEngine().brain._build_api_kwargs(stream=True)
        assert kwargs.get("stream_options") == {"include_usage": True}

    def test_non_stream_requests_do_not(self):
        """非流式本来就有 usage，多传这个参数没意义。"""
        from core.engine import WhaleGirlEngine

        kwargs = WhaleGirlEngine().brain._build_api_kwargs(stream=False)
        assert "stream_options" not in kwargs

    def test_usage_is_read_before_the_empty_choices_guard(self):
        """回归守卫：带 usage 的那个 chunk，`choices` 是空的。

        取值必须写在 `if not chunk.choices: continue` **之前**，否则永远拿不到。
        """
        import inspect

        from core import brain

        for func in (brain.Brain._generate_text, brain.Brain.chat_stream):
            src = inspect.getsource(func)
            assert "chunk.usage" in src, f"{func.__name__} 没有取 usage"
            usage_at = src.index("chunk.usage")
            guard_at = src.index("if not chunk.choices")
            assert usage_at < guard_at, (
                f"{func.__name__}: usage 取在 choices 守卫之后，会永远拿不到"
            )


# ═══════════════════════════════════════════════════════════
# 3. 插件扫描顺序
# ═══════════════════════════════════════════════════════════

class TestPluginScanOrder:
    def _make_plugin(self, root: Path, name: str) -> None:
        d = root / name
        d.mkdir()
        (d / "manifest.json").write_text(json.dumps({
            "name": name, "version": "0.0.1", "type": "tool",
            "entry": "main.py", "description": "测试用",
        }), encoding="utf-8")
        (d / "main.py").write_text(
            "def register_commands():\n    return {}\n", encoding="utf-8")

    def test_order_is_deterministic_not_filesystem_order(self, tmp_path):
        """`iterdir()` 的顺序取决于文件系统；必须排序后再注册。

        工具定义在请求前缀里占 6000+ 字符，顺序一变整段 prompt cache 作废。
        """
        from core.plugins.manager import PluginManager

        # 故意按非字典序创建，模拟文件系统返回顺序与名字顺序不一致
        for name in ("zeta", "alpha", "mid"):
            self._make_plugin(tmp_path, name)

        pm = PluginManager(str(tmp_path), disabled=[])
        pm.discover_and_load()

        assert list(pm._plugins.keys()) == ["alpha", "mid", "zeta"]

    def test_order_is_stable_across_two_scans(self, tmp_path):
        from core.plugins.manager import PluginManager

        for name in ("zeta", "alpha", "mid"):
            self._make_plugin(tmp_path, name)

        first = PluginManager(str(tmp_path), disabled=[])
        first.discover_and_load()
        second = PluginManager(str(tmp_path), disabled=[])
        second.discover_and_load()

        assert list(first._plugins.keys()) == list(second._plugins.keys())
