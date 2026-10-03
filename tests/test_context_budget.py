"""上下文预算（token）的回归测试。

## 为什么从「条数」改成「token」

原先 `_trim_messages(max_messages=40)` 数的是**消息条数**，但真正的约束是 token：

- 40 条短消息可能只有 3K token —— 白白丢掉还有大量余量的历史
- 40 条带工具的消息可能 80K+ —— 而且带工具时**一条用户提问会产生好几条消息**，
  40 条可能只等于 4~8 轮对话

改成 token 预算之后，「40 条」这个限制自然消失：普通对话能留几百条，
真正需要压缩的是工具密集的场景。

另外触发策略也变了：**不到水位一条都不丢**。每轮都裁等于每轮都破坏 prompt cache
（失效边界在第一个被改动的 token 处），所以攒到接近上限再批量裁。
"""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.brain import (  # noqa: E402
    CONTEXT_BUDGET_RATIO,
    LARGE_CONTEXT_WINDOW,
    SAFE_CONTEXT_WINDOW,
    TRIM_TRIGGER_RATIO,
    Brain,
    _messages_tokens,
    estimate_tokens,
)
from core.config.models import ModelConfig, PersonaConfig  # noqa: E402


def _brain(provider="deepseek", **kw):
    return Brain(ModelConfig(provider=provider, api_key="k",
                             base_url="http://127.0.0.1:1/v1", **kw),
                 PersonaConfig(), None)


def _msgs(n, size=200):
    """n 轮 user/assistant，每条约 size 字符。"""
    out = []
    for i in range(n):
        out.append({"role": "user", "content": f"第{i}问：" + "内容" * (size // 2)})
        out.append({"role": "assistant", "content": f"第{i}答：" + "回复" * (size // 2)})
    return out


# ═══════════════════════════════════════════════════════════
# 估算器
# ═══════════════════════════════════════════════════════════

class TestEstimator:
    """系数是从 DeepSeek tokenizer **实测**出来的，这里钉住它们。

    实测值（见本文件顶部的由来）：中文 ≈1.72 字符/token、英文 ≈4.9、代码 ≈2.5。
    统一按 chars/4 估会对中文低估约 2.3 倍 —— 那正是要避免的。
    """

    @pytest.mark.parametrize("text,actual,tag", [
        ("这是一段纯中文的测试文本，用来测量中文的 token 化比例。" * 30, 540, "中文"),
        ("This is a plain English paragraph used to measure the tokenization ratio. " * 30,
         451, "英文"),
        ("def foo(bar: int) -> str:\n    return f'{bar}'\n" * 40, 750, "代码"),
    ])
    def test_within_tolerance(self, text, actual, tag):
        got = estimate_tokens(text)
        err = abs(got - actual) / actual
        assert err <= 0.20, f"{tag} 估算 {got} vs 实际 {actual}，误差 {err:.0%}"

    def test_empty_is_zero(self):
        assert estimate_tokens("") == 0

    def test_chinese_is_not_underestimated(self):
        """中文必须比「chars/4」估得多得多 —— 那是最容易犯的错。"""
        text = "这是一段纯中文" * 50
        assert estimate_tokens(text) > len(text) / 3

    def test_tool_calls_counted(self):
        plain = {"role": "assistant", "content": ""}
        with_tc = {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "list_directory", "arguments": '{"path": "/tmp"}'}}]}
        assert _messages_tokens([with_tc]) > _messages_tokens([plain])

    def test_reasoning_content_counted(self):
        """DeepSeek V4 起，带 tool_calls 的 assistant 必须回传思考链，它真的占前缀。"""
        base = {"role": "assistant", "content": "x"}
        with_r = dict(base, reasoning_content="想" * 400)
        assert _messages_tokens([with_r]) > _messages_tokens([base]) + 100


# ═══════════════════════════════════════════════════════════
# 窗口选择
# ═══════════════════════════════════════════════════════════

class TestContextWindow:
    def test_known_cloud_gets_large_window(self):
        assert _brain("deepseek")._context_window() == LARGE_CONTEXT_WINDOW

    @pytest.mark.parametrize("provider", ["ollama", "lmstudio", "custom", "unknown-thing"])
    def test_everything_else_is_conservative(self, provider):
        """只对**已知**的大窗口服务商给 128K，其余一律保守。

        猜大了会超窗报错（硬失败），猜小了只是多裁一点历史（可接受）——
        所以 `custom` 这种「可能大也可能小」的一律走保守值。
        """
        assert _brain(provider)._context_window() == SAFE_CONTEXT_WINDOW
        assert SAFE_CONTEXT_WINDOW < LARGE_CONTEXT_WINDOW


# ═══════════════════════════════════════════════════════════
# 裁剪
# ═══════════════════════════════════════════════════════════

class TestTrimByBudget:
    def test_below_watermark_keeps_everything(self):
        """**不到水位一条都不丢** —— 这是本次改动的核心。

        旧实现数条数，40 条一过就开始丢；现在只要 token 没到水位就原样返回。
        """
        brain = _brain()
        msgs = _msgs(60, size=40)          # 120 条，但总量很小
        assert len(msgs) == 120
        assert brain._trim_messages(msgs) is msgs

    def test_over_budget_trims_to_fit(self):
        brain = _brain()
        msgs = _msgs(400, size=400)        # 远超大预算
        out = brain._trim_messages(msgs, budget=5_000)
        assert len(out) < len(msgs)
        assert _messages_tokens(out) <= 5_000

    def test_system_message_is_never_dropped(self):
        brain = _brain()
        msgs = [{"role": "system", "content": "SYS"}] + _msgs(400, size=400)
        out = brain._trim_messages(msgs, budget=5_000)
        assert out[0] == {"role": "system", "content": "SYS"}

    def test_keeps_newest(self):
        brain = _brain()
        msgs = _msgs(400, size=400)
        out = brain._trim_messages(msgs, budget=5_000)
        assert out[-1] == msgs[-1], "裁剪应从最旧的开始丢，保留最新的"

    def test_reserved_shrinks_the_budget(self):
        brain = _brain()
        msgs = _msgs(400, size=400)
        with_reserved = brain._trim_messages(msgs, budget=20_000, reserved=10_000)
        without = brain._trim_messages(msgs, budget=20_000)
        assert len(with_reserved) <= len(without)

    def test_negative_budget_falls_back(self):
        """预算算成负数（工具定义比窗口还大）时不能返回空列表。"""
        brain = _brain("ollama")
        msgs = _msgs(50, size=200)
        out = brain._trim_messages(msgs, budget=-99_999)
        assert out, "预算异常时不该把上下文裁空"


class TestTrimPreservesToolPairing:
    """裁剪起点不能落在「孤儿」消息上，否则 OpenAI 兼容接口直接报错。"""

    def _long_tool_history(self):
        msgs = []
        for i in range(200):
            msgs.append({"role": "user", "content": f"问题{i}" + "填充" * 200})
            msgs.append({"role": "assistant", "content": "",
                         "tool_calls": [{"id": f"c{i}", "type": "function",
                                         "function": {"name": "ls", "arguments": "{}"}}]})
            msgs.append({"role": "tool", "tool_call_id": f"c{i}",
                         "content": "结果" * 200})
            msgs.append({"role": "assistant", "content": f"回答{i}" + "填充" * 200})
        return msgs

    def test_no_orphan_tool_at_start(self):
        brain = _brain()
        out = brain._trim_messages(self._long_tool_history(), budget=3_000)
        assert out[0]["role"] != "tool", "裁剪后以 tool 结果开头 → 缺少配对的 assistant"

    def test_assistant_tool_calls_have_all_results(self):
        brain = _brain()
        out = brain._trim_messages(self._long_tool_history(), budget=3_000)
        if out and out[0].get("role") == "assistant" and out[0].get("tool_calls"):
            want = {tc["id"] for tc in out[0]["tool_calls"] if "id" in tc}
            got = set()
            for m in out[1:]:
                if m["role"] == "tool":
                    got.add(m.get("tool_call_id"))
                else:
                    break
            assert want == got, "首条 assistant 声明的 tool_calls 没有配齐结果"

    def test_watermark_ratio_is_sane(self):
        assert 0 < TRIM_TRIGGER_RATIO <= 1
        assert 0 < CONTEXT_BUDGET_RATIO <= 1
        # 水位必须低于 1，否则「刚好到预算」时也不会裁
        assert TRIM_TRIGGER_RATIO < 1
