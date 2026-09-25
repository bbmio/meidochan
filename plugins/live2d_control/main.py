"""让 AI 主动控制 Live2D 立绘（表情 / 动作 / 回默认脸）。

## 为什么用「动态工具」而不是 manifest 的 tools 数组

工具说明里要**列出当前模型的实际表情名**（自带模型有 44 个），
AI 才能一次调用就挑到最接近的那个（「笑一个」→ `开心兴奋` / `星星眼`）。
manifest.json 是静态 JSON，写不了运行时才知道的名字，
所以走 `get_dynamic_tools()` + `owns_tool()` + `call_dynamic_tool()` 这条路。

## 名字从哪来

`core/live2d_assets.asset_names()` 扫描 `config/live2d.toml` 里配的模型目录 ——
与设置界面的下拉框、播放器 manifest **同一份定义**，
所以 AI 能选到的名字，播放器一定认得出。

## 为什么只是「塞一条请求」

本模块跑在 **worker 线程**，而 Live2D 是 Qt 控件、只能在 GUI 线程碰。
所以这里只往 `core/live2d_control` 的队列里塞一条请求，
真正切表情的是 UI（见 `ui_qt/main_window.py` 的 `_drain_live2d_requests`）。

## 「保持期」

AI 设的表情会被 UI 保持到本轮回复结束 —— 否则工具一返回、对话阶段变成
`writing`，表情立刻被状态机换成「画笔」，等于白设。这段策略在 UI 侧。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

from core import live2d_control
from core.live2d_assets import asset_names
from core.paths import to_app_path

TOOL_SET_EXPRESSION = "set_live2d_expression"
TOOL_PLAY_MOTION = "play_live2d_motion"
TOOL_CLEAR_EXPRESSION = "clear_live2d_expression"

ALL_TOOLS = (TOOL_SET_EXPRESSION, TOOL_PLAY_MOTION, TOOL_CLEAR_EXPRESSION)

#: 名字列表太长时的截断长度（说明里够用即可，不至于把 prompt 撑爆）
_MAX_NAMES_IN_DESC = 80


# ── 模型资源 ──

def _model_dir() -> Path:
    """读 config/live2d.toml 里配的模型目录，解析成绝对路径。

    读不到就返回空 Path —— 调用方据此判断「这台机器上没有立绘」。
    """
    try:
        from core.config.loader import ConfigLoader
        cfg = ConfigLoader().get_live2d_config()
    except Exception:
        return Path()
    raw = str(((cfg.get("model") or {}).get("dir") or "")).strip()
    if not raw:
        return Path()
    try:
        return to_app_path(raw)
    except Exception:
        return Path(raw)


def _available() -> Tuple[List[str], List[str]]:
    """`(动作名, 表情名)`。模型目录不存在时两个都是空列表。"""
    model_dir = _model_dir()
    if not str(model_dir) or not model_dir.is_dir():
        return [], []
    return asset_names(model_dir)


def _joined(names: List[str]) -> str:
    if not names:
        return "（无）"
    if len(names) > _MAX_NAMES_IN_DESC:
        return "、".join(names[:_MAX_NAMES_IN_DESC]) + f" …（共 {len(names)} 个）"
    return "、".join(names)


# ── 工具定义 ──

def get_dynamic_tools() -> List[Dict[str, Any]]:
    """OpenAI 函数调用格式的工具定义（名字列表运行时生成）。"""
    motions, expressions = _available()
    if not motions and not expressions:
        # 没有模型就不暴露工具 —— 否则 AI 会一直调一个注定失败的接口
        return []

    tools: List[Dict[str, Any]] = []
    if expressions:
        tools.append({
            "type": "function",
            "function": {
                "name": TOOL_SET_EXPRESSION,
                "description": (
                    "切换桌面立绘的表情。用户提出表情 / 情绪类要求时调用，"
                    "例如「笑一个」「害羞一点」「生气」「卖个萌」。\n"
                    "**必须从下面这些名字里挑最接近的一个**（不要自己造名字）：\n"
                    f"{_joined(expressions)}\n"
                    "切换后会保持到本轮回复结束，之后自动恢复。"),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "expression": {
                            "type": "string",
                            "description": "表情名，必须来自上面给出的列表",
                        },
                    },
                    "required": ["expression"],
                },
            },
        })
        tools.append({
            "type": "function",
            "function": {
                "name": TOOL_CLEAR_EXPRESSION,
                "description": (
                    "清掉当前表情、回到模型默认脸。用户说「别笑了」「表情收一收」"
                    "「正常点」时调用。不需要参数。"),
                "parameters": {"type": "object", "properties": {}},
            },
        })
    if motions:
        tools.append({
            "type": "function",
            "function": {
                "name": TOOL_PLAY_MOTION,
                "description": (
                    "让桌面立绘播一次动作（播完自己停）。用户说「跳个舞」"
                    "「转个圈」「拍张照」这类要求时调用。\n"
                    "**必须从下面这些名字里挑最接近的一个**：\n"
                    f"{_joined(motions)}"),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "motion": {
                            "type": "string",
                            "description": "动作名，必须来自上面给出的列表",
                        },
                    },
                    "required": ["motion"],
                },
            },
        })
    return tools


# ── 执行 ──

def owns_tool(tool_name: str) -> bool:
    return tool_name in ALL_TOOLS


def call_dynamic_tool(tool_name: str, arguments: Dict[str, Any]) -> str:
    if tool_name == TOOL_SET_EXPRESSION:
        return set_expression(str((arguments or {}).get("expression", "")))
    if tool_name == TOOL_PLAY_MOTION:
        return play_motion(str((arguments or {}).get("motion", "")))
    if tool_name == TOOL_CLEAR_EXPRESSION:
        return clear_expression()
    return f" 未知工具：{tool_name}"


def set_expression(name: str) -> str:
    """切到指定表情。名字对不上时把可用清单回给 AI，让它自己纠正。"""
    motions, expressions = _available()
    if not expressions:
        return " 这台机器上没有可用的 Live2D 模型，无法切换表情。"
    name = (name or "").strip()
    if name not in expressions:
        return (f" 没有叫「{name}」的表情。可用的有：{_joined(expressions)}\n"
                "请从上面挑一个最接近的重新调用。")
    live2d_control.request_expression(name)
    return f" 已切换到表情「{name}」，会保持到本轮回复结束。"


def play_motion(name: str) -> str:
    """播一次指定动作。"""
    motions, _expressions = _available()
    if not motions:
        return " 这台机器上没有可用的 Live2D 模型，无法播放动作。"
    name = (name or "").strip()
    if name not in motions:
        return (f" 没有叫「{name}」的动作。可用的有：{_joined(motions)}\n"
                "请从上面挑一个最接近的重新调用。")
    live2d_control.request_motion(name)
    return f" 已播放动作「{name}」。"


def clear_expression() -> str:
    """清掉表情、回到模型默认脸。"""
    _motions, expressions = _available()
    if not expressions:
        return " 这台机器上没有可用的 Live2D 模型。"
    live2d_control.request_clear_expression()
    return " 已清掉表情，回到默认脸。"
