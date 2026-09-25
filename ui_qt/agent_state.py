"""把引擎上报的「阶段」映射成 AgentState（驱动 Live2D 的动作与表情）。

引擎在 `core/brain.py` 里显式上报阶段，UI 直接读 —— **不再从展示文本反推**。

为什么不再反推：展示文本是给人看的，引擎分支一变就失准。实测踩到的坑 ——
`正在生成最终回复` 那个状态行是**死代码**（`brain.py` 在「无工具调用」分支就
`yield done` + `return` 了，只有工具循环跑满 max_iterations 才到得了后面那行），
所以「拿到结果」的表情从来没出现过。实测 81 次 yield 里该状态行 0 次。

引擎上报的阶段（见 `core/brain.py` 的 `_phase_event`）：

    thinking  正在想（含推理流、等首 token）
    tool      正在调工具
    found     这一轮的工具都返回了  ← 「哦，拿到了」的真实时刻
    writing   正文在流

一个阶段可能在一次对话里反复出现（多轮工具调用），所以引擎侧是**边沿触发**：
只在阶段与上一次不同时才上报，UI 侧也只在实际变化时切换状态。
"""
from __future__ import annotations

from typing import Optional

from .media import AgentState

# 阶段 → 状态。阶段名必须与 core/brain.py 的 _phase_event() 保持一致。
PHASE_TO_STATE = {
    "thinking": AgentState.THINKING,
    "tool": AgentState.WORKING,
    "found": AgentState.SURPRISE,
    "writing": AgentState.RESPONDING,
    "idle": AgentState.IDLE,
}


class AgentStateTracker:
    """消费引擎阶段，输出 AgentState 变化。

    **刻意不做「最短停留」**：一开始加了停留来保证「拿到结果」看得见，结果
    紧随其后的 writing 被挡掉 —— 短回复下「输出中」整段轮不到，长回复下
    立绘还会一直卡在星星眼上。

    现在按「到了就切」处理，靠两件事自然成立：
      · 动作（motion）本身是一次性的，播完自己停 —— 触发一次就够，
        即使表情随后被下一个状态换掉，动作还在继续播；
      · 状态会一直保持到下一个阶段到来，所以回复长的时候「输出中」
        自然就持续到结束。
    """

    def __init__(self, **_) -> None:
        self._state = AgentState.IDLE

    @property
    def state(self) -> AgentState:
        return self._state

    # ── 生命周期 ──

    def reset(self) -> Optional[AgentState]:
        """新一轮对话开始 / 本轮结束。"""
        return self._set(AgentState.IDLE)

    def fail(self) -> Optional[AgentState]:
        return self._set(AgentState.ERROR)

    # ── 主入口 ──

    def feed_phase(self, phase: str) -> Optional[AgentState]:
        """喂一个引擎阶段；返回变化后的状态，无变化返回 None。"""
        state = PHASE_TO_STATE.get((phase or "").strip())
        if state is None:
            return None
        return self._set(state)

    def _set(self, state: AgentState) -> Optional[AgentState]:
        if state == self._state:
            return None
        self._state = state
        return state
