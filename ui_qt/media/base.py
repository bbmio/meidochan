"""媒体抽象层（ARCHITECTURE_V3 §4.1）。

将来加动效 / Live2D / 粒子，只需新增 MediaSource 实现并注册到 MediaRegistry，
UI（StageView）与引擎零改动。M1 只实现 MediaKind.IMAGE。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Dict


class MediaKind(str, Enum):
    IMAGE = "image"          # ✅ M1 实现（png/jpg/webp 静态）
    ANIM_IMAGE = "anim"      # ⏳ 预留（gif / 动图 webp）
    LOTTIE = "lottie"        # ⏳ 预留
    LIVE2D = "live2d"        # ⏳ 预留
    PROCEDURAL = "proc"      # ⏳ 预留（粒子 / 代码特效）


class AgentState(str, Enum):
    IDLE = "idle"
    THINKING = "thinking"
    WORKING = "working"
    RESPONDING = "responding"
    ERROR = "error"
    SURPRISE = "surprise"


@dataclass
class LayerStyle:
    """虚化 / 透明 / 布局统一描述，与媒体类型无关。

    虚化与透明**必须**通过本对象传递（禁止在具体 Source 里写死效果参数）。
    """
    visible: bool = True
    opacity: float = 1.0          # 0..1，层透明度
    blur_radius: float = 0.0      # >=0，层虚化（景深）
    scale: float = 1.0
    anchor: str = "bottom-center"  # fill | top/bottom/center - left/center/right
    offset: tuple = (0, 0)
    z: int = 0


@dataclass
class AssetSpec:
    id: str
    kind: MediaKind
    path: Path
    style: LayerStyle = field(default_factory=LayerStyle)
    params: dict = field(default_factory=dict)   # 各类型私有参数


class MediaSource(ABC):
    """一个可挂到舞台某一层的媒体源。"""

    kind: MediaKind

    @abstractmethod
    def attach(self, host) -> None:
        """挂到宿主（StageView）上：创建并显示自身控件。"""

    @abstractmethod
    def detach(self) -> None:
        """从宿主上摘除并释放自身控件。"""

    @abstractmethod
    def apply_style(self, style: LayerStyle) -> None:
        """应用层样式（虚化 / 透明 / 缩放 / 位置）。"""

    def apply_style_fast(self, style: LayerStyle) -> None:
        """拖动滑杆时的低开销近似渲染；默认回退到 apply_style（精算）。

        §4.1 契约未定义该扩展点，这里是 M1 为实现「拖动近似 / 松手精算」新增的
        可选钩子：不实现也能跑，只是拖动时按精算路径渲染。
        """
        self.apply_style(style)

    @abstractmethod
    def set_state(self, state: AgentState, skip_expression: bool = False) -> None:
        """响应 Agent 状态；静态图可忽略。

        `skip_expression` 只对 Live2D 层有意义（AI 显式设的表情正在保持期，
        不该被状态机的映射覆盖）。基类收下这个参数只是为了统一签名 ——
        否则调用方得按类型分支，很容易漏。
        """

    def start(self) -> None:
        """开始播放（静态图忽略）。"""

    def stop(self) -> None:
        """停止播放（静态图忽略）。"""


class MediaRegistry:
    """kind -> factory。新增媒体类型 = register 一个新的工厂。"""

    def __init__(self) -> None:
        self._factories: Dict[MediaKind, Callable[[AssetSpec], MediaSource]] = {}

    def register(self, kind: MediaKind,
                 factory: Callable[[AssetSpec], MediaSource]) -> None:
        self._factories[kind] = factory

    def has(self, kind: MediaKind) -> bool:
        return kind in self._factories

    def kinds(self) -> list:
        return list(self._factories.keys())

    def create(self, spec: AssetSpec) -> MediaSource:
        factory = self._factories.get(spec.kind)
        if factory is None:
            raise NotImplementedError(f"媒体类型 {spec.kind.value!r} 尚未实现（M1 仅支持 image）")
        return factory(spec)
