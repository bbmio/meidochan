"""AI 对 Live2D 立绘的控制通道。

## 为什么要单独一个模块

工具（插件）跑在 **worker 线程**，而 Live2D 是 Qt 控件、只能在 **GUI 线程**碰 ——
跨线程调用会崩。所以这里不直接操作任何 Qt 对象，只放一个**带锁的请求队列**：
工具把「切哪个表情 / 播哪个动作」写进来，UI 侧用自己的节奏取走并在 GUI 线程执行。

这与 `core/brain.py` 上报对话阶段是同一种做法（core 存状态、UI 来读），
好处是**不引入 core → Qt 的反向依赖** —— `core/` 依旧不需要知道 `ui_qt/` 的存在。

## 「保持期」不在这里

「AI 显式设的表情不要被对话状态机立刻覆盖」属于 UI 的呈现策略
（只有 UI 知道阶段何时变化），放在 `ui_qt/main_window.py`。
本模块只管把请求可靠地传过去。

## 名字从哪来

可用动作 / 表情名由 `core/live2d_assets.py` 扫描模型目录得出 ——
与播放器 manifest、设置界面下拉框**同一份定义**，
所以 AI 能选到的名字，播放器一定认得出。
"""
from __future__ import annotations

import threading
from typing import List, Tuple

#: 切到某个表情
KIND_EXPRESSION = "expression"
#: 播一次某个动作
KIND_MOTION = "motion"
#: 清掉表情、回到模型默认脸
KIND_CLEAR = "clear"

_pending: List[Tuple[str, str]] = []
_lock = threading.Lock()


def request(kind: str, value: str = "") -> None:
    """把一条请求放进队列（工具线程调用，线程安全）。"""
    with _lock:
        _pending.append((str(kind), str(value)))


def request_expression(name: str) -> None:
    """切到指定表情。"""
    request(KIND_EXPRESSION, name)


def request_motion(name: str) -> None:
    """播一次指定动作。"""
    request(KIND_MOTION, name)


def request_clear_expression() -> None:
    """清掉表情、回到模型默认脸。"""
    request(KIND_CLEAR)


def take_pending() -> List[Tuple[str, str]]:
    """取走全部待处理请求（UI 线程调用，取后即清）。

    一次取走**全部**而不是一条：一轮对话里 AI 可能先设表情再播动作，
    分两次取会让中间插入一次状态切换，表情白设。
    """
    with _lock:
        items = list(_pending)
        _pending.clear()
    return items


def pending_count() -> int:
    """当前积压的请求数（排障 / 测试用）。"""
    with _lock:
        return len(_pending)


def reset() -> None:
    """清空队列（测试用；正常运行不需要调）。"""
    with _lock:
        _pending.clear()
