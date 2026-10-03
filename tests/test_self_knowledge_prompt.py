"""默认提示词末尾的「工具调用说明 / 立绘表情规则」测试。

对应需求：
1. 默认提示词末尾要有工具调用描述（工具调用约定）；
2. 让她每轮回复按自己的语境/心情挑一个 Live2D 表情 —— 但只在表情工具真的可用时才提。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import self_knowledge as sk  # noqa: E402


class _PM:
    """假插件管理器：按需返回给定的工具定义。"""

    def __init__(self, tools):
        self.tools = tools
        self.last_allow = None

    def get_tool_definitions(self, allow_risky_tools=False):
        self.last_allow = allow_risky_tools
        return list(self.tools)


def _tool(name):
    return {"type": "function",
            "function": {"name": name, "description": "说明", "parameters": {}}}


def test_tool_usage_guide_always_present():
    """工具调用约定是默认提示词的一部分，与有没有模型无关。"""
    text = sk.build_self_knowledge_prompt(_PM([_tool("kb_search")]))
    assert "工具调用约定" in text
    assert "`kb_search`" in text


def test_live2d_rule_added_only_when_tool_available():
    """有表情工具才提「每轮自己挑表情」；没有就不提（免得反复调不存在的工具）。"""
    with_tool = sk.build_self_knowledge_prompt(
        _PM([_tool("kb_search"), _tool("set_live2d_expression")]))
    assert "set_live2d_expression" in with_tool
    assert "每一轮回复都自己挑一个表情" in with_tool

    without_tool = sk.build_self_knowledge_prompt(_PM([_tool("kb_search")]))
    assert "每一轮回复都自己挑一个表情" not in without_tool


def test_policy_is_forwarded_to_tool_enumeration():
    """提示词里的工具清单必须与给模型的实际工具一致（风险开关要透传）。"""
    pm = _PM([])
    sk.build_self_knowledge_prompt(pm, allow_risky_tools=True)
    assert pm.last_allow is True
    sk.build_self_knowledge_prompt(pm)
    assert pm.last_allow is False
