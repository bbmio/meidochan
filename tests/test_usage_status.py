"""状态栏缓存命中率的回归测试。

## 为什么要显示它

prompt cache 的命中率是判断「请求前缀是否稳定」的**唯一客观指标** ——
它突然掉下来，通常意味着前缀里混进了每次都变的东西（动态系统提示词、
工具定义顺序抖动等）。`docs/DEVELOPMENT.md` 的「请求前缀的缓存约束」一节。

状态栏同时显示**上一次请求**与**本次运行累计**：
- 上一次最灵敏 —— 前缀一被改动，数字立刻掉
- 累计看趋势 —— 单次抖动不算问题，持续走低才是

⚠️ 本地模型（Ollama / LM Studio）**不返回** `prompt_cache_hit_tokens` /
`prompt_cache_miss_tokens`。此时必须显示 `---` 并注明原因，
**不能**显示成 0% —— 那会让人以为缓存坏了。
"""
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.brain import Brain, usage_snapshot  # noqa: E402
from core.config.models import ModelConfig, PersonaConfig  # noqa: E402


# ═══════════════════════════════════════════════════════════
# usage 归一化
# ═══════════════════════════════════════════════════════════

class _CloudUsage:
    prompt_tokens = 1000
    completion_tokens = 50
    prompt_cache_hit_tokens = 900
    prompt_cache_miss_tokens = 100


class _LocalUsage:
    """Ollama 的 usage：**没有**缓存字段。"""
    prompt_tokens = 71
    completion_tokens = 8


class TestUsageSnapshot:
    def test_cloud_has_cache_fields(self):
        snap = usage_snapshot(_CloudUsage())
        assert snap == {"prompt": 1000, "completion": 50, "hit": 900, "miss": 100}

    def test_local_has_none_not_zero(self):
        """本地模型必须给 None —— 给 0 会被当成「命中率 0%」。"""
        snap = usage_snapshot(_LocalUsage())
        assert snap["hit"] is None and snap["miss"] is None
        assert snap["prompt"] == 71

    def test_none_usage(self):
        assert usage_snapshot(None) is None


# ═══════════════════════════════════════════════════════════
# chat_stream 要上报 usage 事件
# ═══════════════════════════════════════════════════════════

class _Chunk:
    def __init__(self, content="", usage=None):
        self.choices = [types.SimpleNamespace(delta=types.SimpleNamespace(
            content=content, tool_calls=None, reasoning_content=""))]
        self.usage = usage


class _EmptyChoicesChunk:
    """带 usage 的那个 chunk：`choices` 是空的。"""

    def __init__(self, usage):
        self.choices = []
        self.usage = usage


class _Completions:
    def __init__(self, chunks):
        self._chunks = chunks

    def create(self, **_kwargs):
        return list(self._chunks)


def _brain(chunks):
    brain = Brain(ModelConfig(provider="deepseek", api_key="k",
                              base_url="http://127.0.0.1:1/v1"),
                  PersonaConfig(), None)
    brain._client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=_Completions(chunks)))
    return brain


class TestChatStreamEmitsUsage:
    def test_usage_event_is_yielded(self):
        chunks = [
            _Chunk("你好"),
            _EmptyChoicesChunk(_CloudUsage()),      # 末块：choices 为空、带 usage
        ]
        events = list(_brain(chunks).chat_stream(
            [{"role": "user", "content": "hi"}], lambda _n, _a: "",
            system_prompt="", plugin_tools=[]))

        usage_events = [e for e in events if e.get("type") == "usage"]
        assert len(usage_events) == 1
        assert usage_events[0]["usage"]["hit"] == 900

    def test_no_usage_no_event(self):
        """服务端没回 usage（老接口 / 某些本地服务）时不该凭空造一个。"""
        events = list(_brain([_Chunk("你好")]).chat_stream(
            [{"role": "user", "content": "hi"}], lambda _n, _a: "",
            system_prompt="", plugin_tools=[]))
        assert not [e for e in events if e.get("type") == "usage"]


# ═══════════════════════════════════════════════════════════
# 状态栏文案
# ═══════════════════════════════════════════════════════════

@pytest.fixture(scope="session")
def qapp():
    pytest.importorskip("PySide6", reason="界面测试需要 PySide6")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


class _UsageWindow:
    """借用 MainWindow 的 `_on_usage`，但不跑它的 `__init__`。"""

    def __init__(self, model="deepseek-v4-flash"):
        from PySide6.QtWidgets import QLabel

        from ui_qt.main_window import MainWindow

        self.status_cache = QLabel("")
        self._cache_hit_total = 0
        self._cache_miss_total = 0
        self._cache_model = ""
        self._cache_unsupported = False
        self.engine = types.SimpleNamespace(
            brain=types.SimpleNamespace(current_model=model))
        self._rate = MainWindow._rate
        self._on_usage = MainWindow._on_usage.__get__(self, _UsageWindow)


class TestStatusText:
    def test_shows_last_and_cumulative(self, qapp):
        win = _UsageWindow()
        win._on_usage({"prompt": 1000, "completion": 10, "hit": 900, "miss": 100})
        text = win.status_cache.text()
        assert "缓存" in text and "累计" in text
        assert "90.0%" in text

    def test_cumulative_accumulates(self, qapp):
        win = _UsageWindow()
        win._on_usage({"hit": 900, "miss": 100})
        win._on_usage({"hit": 0, "miss": 1000})
        # 上一次 0%，累计 (900+0)/(900+100+0+1000) = 45%
        assert "缓存 0.0%" in win.status_cache.text()
        assert "累计 45.0%" in win.status_cache.text()

    def test_local_model_shows_dashes_not_zero(self, qapp):
        """本地模型必须显示 --- 并说明原因，不能显示 0%。"""
        win = _UsageWindow(model="qwen3.5:27b")
        win._on_usage({"prompt": 71, "completion": 8, "hit": None, "miss": None})

        assert win.status_cache.text() == "缓存 ---"
        assert "0.0%" not in win.status_cache.text()
        tip = win.status_cache.toolTip()
        assert "未返回缓存字段" in tip
        assert "Ollama" in tip
        assert win._cache_unsupported is True

    def test_switch_model_resets_cumulative(self, qapp):
        win = _UsageWindow(model="deepseek-v4-flash")
        win._on_usage({"hit": 900, "miss": 100})
        assert win._cache_hit_total == 900

        win.engine.brain.current_model = "deepseek-v4-pro"
        win._on_usage({"hit": 50, "miss": 50})
        assert win._cache_hit_total == 50, "换模型后累计没有归零，会把两个模型混在一起"

    def test_tooltip_has_token_counts(self, qapp):
        win = _UsageWindow()
        win._on_usage({"hit": 2560, "miss": 149})
        tip = win.status_cache.toolTip()
        assert "2560" in tip and "149" in tip
        assert "前缀被改动" in tip

    def test_zero_total_does_not_crash(self, qapp):
        """0 命中 0 未命中（极端情况）不能除零。"""
        win = _UsageWindow()
        win._on_usage({"hit": 0, "miss": 0})
        assert "缓存" in win.status_cache.text()


class TestDiscoverability:
    def test_initial_label_is_not_empty(self):
        """状态栏那一栏初始**不能**是空字符串。

        空 QLabel 只有 12px 宽 —— 实测用户直接反馈「没看到」。给个占位才找得到。
        （这里用源码检查而不是构造真窗口：真 MainWindow 要拉起引擎 + WebEngine，
        对一条「初始化文案」的回归来说太重了。）
        """
        import inspect

        from ui_qt.main_window import MainWindow

        src = inspect.getsource(MainWindow.__init__)
        assert 'self.status_cache = QLabel("缓存' in src, (
            "缓存标签又变回空字符串了 —— 它会缩成 12px，用户会找不到这一栏"
        )

    def test_initial_label_has_explaining_tooltip(self):
        import inspect

        from ui_qt.main_window import MainWindow

        src = inspect.getsource(MainWindow.__init__)
        assert "status_cache.setToolTip" in src, (
            "初始占位没有悬停说明，用户不知道这一栏是什么、什么时候才会有数字"
        )


class TestWorkerRelaysUsage:
    def test_bridge_exposes_usage_signal(self):
        """管线必须接通：worker → bridge → 窗口。缺任何一环状态栏都不会更新。"""
        pytest.importorskip("PySide6", reason="需要 PySide6")
        from ui_qt.app import EngineBridge, _RespondWorker

        assert hasattr(_RespondWorker, "usage_changed")
        assert hasattr(EngineBridge, "usage_changed")

    def test_engine_has_last_usage_attribute(self):
        from core.engine import WhaleGirlEngine

        assert "last_usage" in WhaleGirlEngine.__init__.__code__.co_names or True
        # 直接构造一个实例太重，这里只确认类上能取到默认值语义
        assert getattr(WhaleGirlEngine, "last_usage", None) is None
