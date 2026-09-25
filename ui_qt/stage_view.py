"""素材舞台（三层合成）+ 舞台侧控制条（ARCHITECTURE_V3 §4.1 / §5.3 / §5.5）。

三层：backdrop(背景) / stand(立绘) / overlay(特效)。
- 每层是一个 MediaSource；虚化与透明一律走 LayerStyle → apply_style()。
- 拖动滑杆时 fast=True（降采样近似），松手后 fast=False（精算）。
"""
from __future__ import annotations

from typing import Dict, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
    QCheckBox,
)

from .media import AgentState, AssetSpec, LayerStyle, MediaRegistry, MediaSource

SLOTS = ("backdrop", "stand", "overlay")
SLOT_LABELS = {"backdrop": "背景", "stand": "立绘", "overlay": "特效"}


class StageView(QWidget):
    """三层合成舞台：统一管理 LayerStyle 与 Agent 状态分发。"""

    def __init__(self, registry: MediaRegistry, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("stage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._registry = registry
        self._layers: Dict[str, Optional[MediaSource]] = {s: None for s in SLOTS}
        self._styles: Dict[str, LayerStyle] = {
            "backdrop": LayerStyle(anchor="fill", z=0),
            "stand": LayerStyle(anchor="bottom-center", z=10),
            "overlay": LayerStyle(anchor="fill", z=20),
        }
        # 当前 Agent 状态。**必须放实例上** —— 之前写成类属性，
        # 多个 StageView 实例会共享同一份状态（set_state 会串台）。
        self._state: AgentState = AgentState.IDLE
        # 缩放结束后再做一次精算模糊，避免拖动窗口时每帧全分辨率处理
        self._precise_timer = QTimer(self)
        self._precise_timer.setSingleShot(True)
        self._precise_timer.setInterval(120)
        self._precise_timer.timeout.connect(lambda: self._refresh_all(fast=False))

    # ── 对外接口（§4.1 舞台契约） ──

    def set_layer(self, slot: str, source: Optional[MediaSource]) -> None:
        self._require_slot(slot)
        old = self._layers.get(slot)
        if old is source:
            return
        if old is not None:
            old.stop()
            old.detach()
        self._layers[slot] = source
        if source is not None:
            source.attach(self)
            source.apply_style(self._styles[slot])
            source.start()
            source.set_state(self._state)
        self._raise_by_z()

    def set_style(self, slot: str, style: LayerStyle, fast: bool = False) -> None:
        """fast=True 走近似渲染（拖滑杆），fast=False 走精算（松手/预设切换）。"""
        self._require_slot(slot)
        self._styles[slot] = style
        src = self._layers.get(slot)
        if src is not None:
            if fast:
                src.apply_style_fast(style)
            else:
                src.apply_style(style)
        self._raise_by_z()

    def style(self, slot: str) -> LayerStyle:
        self._require_slot(slot)
        return self._styles[slot]

    def has_layer(self, slot: str) -> bool:
        return self._layers.get(slot) is not None

    def take_layer(self, slot: str) -> Optional[MediaSource]:
        """摘下一层但**不销毁它的控件**，返回该媒体源。

        用于把立绘从舞台挪到独立窗口：set_layer(slot, None) 会调 detach()
        把 QWebEngineView 删掉，模型得重新加载；这里只从舞台的登记表里移除，
        控件还挂在原来的父级上，交给 reparent() 换个宿主即可。
        """
        self._require_slot(slot)
        source = self._layers.get(slot)
        self._layers[slot] = None
        return source

    def on_agent_state(self, state: AgentState,
                       skip_expression: bool = False) -> None:
        """引擎 AGENT_* 事件 → 分发到所有层（M1 静态图忽略，链路必须打通）。

        `skip_expression=True` 时透传给各层（只有 Live2D 层在意）：AI 显式设的
        表情正处于保持期，不该被状态机的映射覆盖。
        """
        self._state = state
        for src in self._layers.values():
            if src is not None:
                try:
                    src.set_state(state, skip_expression=skip_expression)
                except Exception:
                    pass

    # ── 内部 ──

    @staticmethod
    def _require_slot(slot: str) -> None:
        if slot not in SLOTS:
            raise KeyError(f"未知舞台层：{slot}")

    def _refresh_all(self, fast: bool) -> None:
        for slot in SLOTS:
            src = self._layers.get(slot)
            if src is not None:
                src.refresh(fast=fast)
        self._raise_by_z()

    def _raise_by_z(self) -> None:
        ordered = sorted(SLOTS, key=lambda s: self._styles[s].z)
        for slot in ordered:
            src = self._layers.get(slot)
            widget = getattr(src, "_label", None) if src is not None else None
            if widget is not None:
                widget.raise_()

    def resizeEvent(self, event):  # noqa: N802 (Qt 命名)
        super().resizeEvent(event)
        self._refresh_all(fast=True)
        self._precise_timer.start()


class StagePanel(QWidget):
    """右侧舞台控制条：素材 / 动效开关(置灰预留) / 虚化滑杆 / 透明滑杆 / 层显隐。"""

    blur_changed = Signal(str, float, bool)   # slot, radius, fast
    window_opacity_changed = Signal(float)    # 0.80 ~ 1.00
    visible_changed = Signal(str, bool)
    stand_window_toggled = Signal(bool)       # True = 独立到桌面

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("stagepanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        title = QLabel("素材舞台")
        title.setProperty("class", "section")
        layout.addWidget(title)

        # ── 素材选择 ──
        self.asset_combo = QComboBox()
        self.asset_combo.addItem("默认立绘", "stand")
        self.asset_combo.setEnabled(False)   # M1 只有一张立绘，M1.5 支持多素材
        self.asset_combo.setToolTip("多素材导入在 M1.5 提供")
        layout.addWidget(self.asset_combo)

        # ── 外观预设 ──
        # 这里原本还有一个「外观预设」下拉，与标题栏那个重复。
        # 已按用户要求移除，只保留标题栏的切换入口（信号 preset_changed 随之删除）。

        # ── 动效开关（M1 置灰预留） ──
        self.motion_check = QCheckBox("动效")
        self.motion_check.setEnabled(False)
        self.motion_check.setToolTip("动效能力在后续里程碑接入（本阶段仅预留接口）")
        layout.addWidget(self.motion_check)

        # ── 虚化滑杆 ──
        group = QGroupBox("虚化 / 透明")
        form = QFormLayout(group)
        form.setContentsMargins(8, 10, 8, 8)
        form.setSpacing(6)

        self.backdrop_blur = QSlider(Qt.Orientation.Horizontal)
        self.backdrop_blur.setRange(0, 20)
        self.stand_depth = QSlider(Qt.Orientation.Horizontal)
        self.stand_depth.setRange(0, 20)
        self.window_opacity = QSlider(Qt.Orientation.Horizontal)
        self.window_opacity.setRange(80, 100)
        self.window_opacity.setValue(98)

        self.backdrop_blur.valueChanged.connect(
            lambda v: self.blur_changed.emit("backdrop", float(v), True))
        self.backdrop_blur.sliderReleased.connect(
            lambda: self.blur_changed.emit("backdrop", float(self.backdrop_blur.value()), False))
        self.stand_depth.valueChanged.connect(
            lambda v: self.blur_changed.emit("stand", float(v), True))
        self.stand_depth.sliderReleased.connect(
            lambda: self.blur_changed.emit("stand", float(self.stand_depth.value()), False))
        self.window_opacity.valueChanged.connect(
            lambda v: self.window_opacity_changed.emit(v / 100.0))

        form.addRow("背景虚化", self.backdrop_blur)
        form.addRow("立绘景深", self.stand_depth)
        form.addRow("窗口透明", self.window_opacity)
        layout.addWidget(group)

        # ── 层显隐 ──
        layer_group = QGroupBox("图层")
        layer_layout = QVBoxLayout(layer_group)
        layer_layout.setContentsMargins(8, 10, 8, 8)
        layer_layout.setSpacing(4)
        self.layer_checks: Dict[str, QCheckBox] = {}
        for slot in SLOTS:
            box = QCheckBox(SLOT_LABELS[slot])
            box.setChecked(True)
            box.setEnabled(slot != "overlay")   # M1 无特效素材
            box.toggled.connect(
                lambda checked, s=slot: self.visible_changed.emit(s, checked))
            self.layer_checks[slot] = box
            layer_layout.addWidget(box)
        layout.addWidget(layer_group)

        # ── 立绘独立到桌面 ──
        self._stand_detached = False
        self.stand_window_btn = QPushButton("独立到桌面")
        self.stand_window_btn.setObjectName("primary")
        self.stand_window_btn.clicked.connect(
            lambda: self.stand_window_toggled.emit(not self._stand_detached))
        layout.addWidget(self.stand_window_btn)
        self.set_stand_detached(False)

        layout.addStretch(1)

    def set_stand_detached(self, detached: bool) -> None:
        """按钮文案随状态切换（独立 ↔ 收回）。"""
        self._stand_detached = bool(detached)
        if detached:
            self.stand_window_btn.setText("收回舞台")
            self.stand_window_btn.setToolTip("把立绘收回右侧舞台")
        else:
            self.stand_window_btn.setText("独立到桌面")
            self.stand_window_btn.setToolTip(
                "把立绘变成一个独立的透明窗口，放在桌面任意位置（按住即可拖动）")

    @property
    def stand_detached(self) -> bool:
        return self._stand_detached

    def set_backdrop_available(self, available: bool, hint: str = "") -> None:
        """没有背景素材时禁用背景虚化滑杆，避免给出无效交互。"""
        self.backdrop_blur.setEnabled(available)
        if available:
            self.backdrop_blur.setToolTip("")
        else:
            self.backdrop_blur.setToolTip(
                hint or "暂无背景素材：放入 assets/default/backdrop.png 后生效")

    # ── 由预设同步滑杆（不回环触发信号） ──

    def apply_appearance(self, window_opacity: float, backdrop_blur: float, stand_depth: float) -> None:
        for widget, value in (
            (self.window_opacity, int(round(window_opacity * 100))),
            (self.backdrop_blur, int(round(backdrop_blur))),
            (self.stand_depth, int(round(stand_depth))),
        ):
            widget.blockSignals(True)
            widget.setValue(value)
            widget.blockSignals(False)
