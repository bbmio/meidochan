"""色轮自定义面板（设置 → 外观 → 「自定义…」）。

## 为什么不用 QColorDialog

Qt 自带的 QColorDialog（含 `DontUseNativeDialog` 版本）提供的是**方块取色器**
（HSV 光谱 + 色板 + 屏幕取色器），**没有色轮**。本模块自绘一个真色轮：
角度 = 色相、半径 = 饱和度、明度另配滑杆。

## 为什么把 18 个色值放在同一个面板里

若每个色块都弹一次模态对话框，改一套主题要开关 18 次窗口。这里改成
「左边选色位 → 右边色轮调」：选中即编辑，右侧实时预览，一次都不用关窗。

## 预览为什么可信

预览用的 QSS 就是主界面用的那一份（`theme.build_qss`），控件也按真实的
objectName / class 搭建 —— 不另写一套"近似样式"，所以不会出现
「预览好看、应用后变样」。
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QConicalGradient,
    QFont,
    QPainter,
    QPainterPath,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from . import theme

WHEEL_SIZE = 168          # 色轮直径
SWATCH_W = 30             # 左侧色块宽
SWATCH_H = 18


class ColorWheel(QWidget):
    """HSV 色轮：角度 = 色相，半径 = 饱和度。明度由外部滑杆给。

    绘制用 QConicalGradient 铺色相 + QRadialGradient 由中心向外叠白 ——
    逐像素算 HSV 也能画，但那是 O(r²)，拖动时会掉帧。
    """

    picked = Signal(int, int)      # (hue 0-359, sat 0-255)

    #: 纯色相的锥形渐变停靠点（QConicalGradient 的角度按逆时针增大）
    _HUE_STOPS = [
        (0.0 / 360, 255, 0, 0), (60.0 / 360, 255, 255, 0),
        (120.0 / 360, 0, 255, 0), (180.0 / 360, 0, 255, 255),
        (240.0 / 360, 0, 0, 255), (300.0 / 360, 255, 0, 255),
        (1.0, 255, 0, 0),
    ]

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedSize(WHEEL_SIZE, WHEEL_SIZE)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._hue = 0
        self._sat = 255
        self._val = 255

    # ── 取值 / 赋值 ──

    def hsv(self) -> tuple:
        return self._hue, self._sat, self._val

    def set_hsv(self, hue: int, sat: int, val: int) -> None:
        self._hue = max(0, min(359, int(hue)))
        self._sat = max(0, min(255, int(sat)))
        self._val = max(0, min(255, int(val)))
        self.update()

    def set_value_only(self, val: int) -> None:
        """只改明度（色轮本身按满明度绘制，这里影响的是取色结果）。"""
        self._val = max(0, min(255, int(val)))

    # ── 绘制 ──

    def paintEvent(self, _event) -> None:      # noqa: N802 (Qt 命名)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        size = float(min(self.width(), self.height()))
        rect = QRectF(0.0, 0.0, size, size)
        center = QPointF(size / 2.0, size / 2.0)
        radius = size / 2.0 - 1.0

        path = QPainterPath()
        path.addEllipse(rect)

        painter.save()
        painter.setClipPath(path)

        # 1) 色相：锥形渐变（角度逆时针增大）
        conical = QConicalGradient(center, 0.0)
        for stop, r, g, b in self._HUE_STOPS:
            conical.setColorAt(stop, QColor(r, g, b))
        painter.fillRect(rect, conical)

        # 2) 饱和度：中心向外的白色渐变
        radial = QRadialGradient(center, radius)
        radial.setColorAt(0.0, QColor(255, 255, 255, 255))
        radial.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillRect(rect, radial)

        # ⚠️ 刻意**不**按明度压黑色轮。
        # 本项目的令牌里近黑（#111113）和近白（#FFFFFF）占了相当比例，
        # 若按明度压黑，选中这些色位时色轮会整片黑掉/白掉，完全没法取色相。
        # 所以色轮恒以满明度显示，只表达「色相 + 饱和度」；
        # 明度交给旁边的滑杆，结果由色块与预览呈现。
        painter.restore()

        # 外圈
        painter.setPen(QColor(0, 0, 0, 40))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(rect)

        # 当前色游标
        angle = math.radians(self._hue)
        r = radius * (self._sat / 255.0)
        cx = center.x() + r * math.cos(angle)
        cy = center.y() - r * math.sin(angle)
        painter.setPen(QColor(255, 255, 255, 230))
        painter.drawEllipse(QPointF(cx, cy), 5.0, 5.0)
        painter.setPen(QColor(0, 0, 0, 160))
        painter.drawEllipse(QPointF(cx, cy), 6.0, 6.0)
        painter.end()

    # ── 交互 ──

    def mousePressEvent(self, event) -> None:      # noqa: N802
        self._handle(event.position())

    def mouseMoveEvent(self, event) -> None:       # noqa: N802
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._handle(event.position())

    def _handle(self, pos: QPointF) -> None:
        size = float(min(self.width(), self.height()))
        cx = cy = size / 2.0
        dx, dy = pos.x() - cx, cy - pos.y()      # 屏幕 y 向下，取反让角度逆时针为正
        radius = max(1.0, size / 2.0 - 1.0)
        dist = min(radius, math.hypot(dx, dy))
        hue = int(round(math.degrees(math.atan2(dy, dx)))) % 360
        sat = int(round(255 * dist / radius))
        self.set_hsv(hue, sat, self._val)
        self.picked.emit(self._hue, self._sat)


class _TokenRow(QFrame):
    """一行语义色：色块 + 名称 + 色值。点色块选中它去右侧色轮调。"""

    selected = Signal(str)

    def __init__(self, key: str, label: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.key = key
        self._selected = False
        self.setObjectName("token-row")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        row = QHBoxLayout(self)
        row.setContentsMargins(6, 3, 8, 3)
        row.setSpacing(8)

        self.swatch = QLabel()
        self.swatch.setFixedSize(SWATCH_W, SWATCH_H)
        row.addWidget(self.swatch)

        self.name = QLabel(label)
        self.name.setProperty("class", "status")
        row.addWidget(self.name, 1)

        self.value = QLabel()
        self.value.setProperty("class", "status")
        self.value.setFont(_mono_font())
        row.addWidget(self.value)

    def set_color(self, color: str) -> None:
        self.swatch.setStyleSheet(
            f"background: {color}; border: 1px solid rgba(128,128,128,0.55);"
            f" border-radius: 3px;")
        self.value.setText(color)

    def set_selected(self, selected: bool) -> None:
        if selected == self._selected:
            return
        self._selected = selected
        self.setProperty("class", "selected" if selected else "")
        # 属性选择器改了要重新 polish，否则 QSS 不会重算
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, _event) -> None:     # noqa: N802
        self.selected.emit(self.key)


class ThemePreview(QWidget):
    """用真实控件 + 真实 QSS 渲染的小预览。

    控件都按主界面的 objectName / class 命名，这样 `build_qss` 出来的规则
    能原样命中 —— 预览与真实界面共用同一条渲染路径。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("root")
        self.setMinimumHeight(196)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        header = QLabel("预览")
        header.setProperty("class", "section")
        root.addWidget(header)

        # 侧栏底 + 正文底：一眼看出层次是否分得开
        strips = QHBoxLayout()
        strips.setSpacing(6)
        for object_name, text in (("sidebar", "侧栏"), ("chatpanel", "正文")):
            box = QWidget()
            box.setObjectName(object_name)
            lay = QVBoxLayout(box)
            lay.setContentsMargins(8, 6, 8, 6)
            lay.addWidget(QLabel(text))
            strips.addWidget(box, 1)
        root.addLayout(strips)

        # 气泡 + 时间戳
        bubble_row = QHBoxLayout()
        bubble_row.setSpacing(6)
        self.user_bubble = QLabel("用户气泡")
        self.user_bubble.setProperty("class", "bubble")
        self.user_bubble.setObjectName("bubble-user")
        bubble_row.addWidget(self.user_bubble)
        bubble_row.addStretch(1)
        stamp = QLabel("14:32")
        stamp.setObjectName("msg-time")
        bubble_row.addWidget(stamp)
        root.addLayout(bubble_row)

        # 控件行：输入框 + 主按钮 + 危险按钮
        controls = QHBoxLayout()
        controls.setSpacing(6)
        edit = QLineEdit()
        edit.setPlaceholderText("输入框")
        controls.addWidget(edit, 1)
        primary = QPushButton("主按钮")
        primary.setObjectName("primary")
        controls.addWidget(primary)
        danger = QPushButton("删除")
        danger.setObjectName("danger")
        controls.addWidget(danger)
        root.addLayout(controls)

        # 代码块 + 反色提示条
        code = QPlainTextEditStub()
        root.addWidget(code)

    def apply_tokens(self, tokens: Dict[str, str]) -> None:
        self.setStyleSheet(theme.build_qss(tokens))


class QPlainTextEditStub(QFrame):
    """预览里的代码块（用 QFrame 顶替，省一个重量级控件）。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setProperty("class", "codeblock")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(4)
        title = QLabel("code.py")
        title.setProperty("class", "status")
        lay.addWidget(title)
        body = QLabel("print('hello')")
        body.setFont(_mono_font())
        lay.addWidget(body)


def _mono_font() -> QFont:
    font = QFont("Consolas")
    font.setPointSize(9)
    return font


class ThemeEditorDialog(QDialog):
    """18 个语义色的编辑面板：左侧选色位，右侧色轮，下方实时预览与对比度校验。"""

    theme_applied = Signal(dict)      # 用户点「应用」→ 完整令牌表

    def __init__(self, tokens: Dict[str, str],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("主题自定义")
        self.setMinimumSize(880, 640)

        self._rows: Dict[str, _TokenRow] = {}
        self._tokens: Dict[str, str] = {}
        self._current: str = theme.ALL_TOKEN_KEYS[0]
        self._syncing = False

        body = QHBoxLayout()
        body.setContentsMargins(14, 14, 14, 8)
        body.setSpacing(14)
        body.addWidget(self._build_token_list(), 0)
        body.addLayout(self._build_right_side(), 1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addLayout(body, 1)
        outer.addWidget(self._build_footer())

        self.load_tokens(tokens)

    # ── 左侧：色位列表 ──

    def _build_token_list(self) -> QWidget:
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(268)
        inner = QWidget()
        inner_lay = QVBoxLayout(inner)
        inner_lay.setContentsMargins(0, 0, 6, 0)
        inner_lay.setSpacing(2)

        for group, keys in theme.TOKEN_GROUPS:
            title = QLabel(group)
            title.setProperty("class", "section")
            inner_lay.addWidget(title)
            for key in keys:
                row = _TokenRow(key, theme.TOKEN_LABELS.get(key, key))
                row.selected.connect(self._select_token)
                self._rows[key] = row
                inner_lay.addWidget(row)
        inner_lay.addStretch(1)
        scroll.setWidget(inner)
        lay.addWidget(scroll)
        return holder

    # ── 右侧：色轮 + 预览 ──

    def _build_right_side(self) -> QVBoxLayout:
        side = QVBoxLayout()
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(10)

        self.current_label = QLabel()
        self.current_label.setProperty("class", "section")
        side.addWidget(self.current_label)

        wheel_row = QHBoxLayout()
        wheel_row.setSpacing(14)

        self.wheel = ColorWheel()
        self.wheel.picked.connect(self._on_wheel_picked)
        wheel_row.addWidget(self.wheel, 0, Qt.AlignmentFlag.AlignTop)

        right = QVBoxLayout()
        right.setSpacing(8)

        self.preview_swatch = QLabel()
        self.preview_swatch.setFixedHeight(26)
        self.preview_swatch.setSizePolicy(QSizePolicy.Policy.Expanding,
                                         QSizePolicy.Policy.Fixed)
        right.addWidget(self.preview_swatch)

        value_row = QHBoxLayout()
        value_row.addWidget(QLabel("明度"))
        self.value_slider = QSlider(Qt.Orientation.Horizontal)
        self.value_slider.setRange(0, 100)
        self.value_slider.valueChanged.connect(self._on_value_changed)
        value_row.addWidget(self.value_slider, 1)
        right.addLayout(value_row)

        hex_row = QHBoxLayout()
        hex_row.addWidget(QLabel("色值"))
        self.hex_edit = QLineEdit()
        self.hex_edit.setPlaceholderText("#RRGGBB")
        self.hex_edit.setMaximumWidth(120)
        self.hex_edit.editingFinished.connect(self._on_hex_edited)
        hex_row.addWidget(self.hex_edit)
        hex_row.addStretch(1)
        right.addLayout(hex_row)

        hint = QLabel("拖动色轮选色相与饱和度，明度用滑杆；也可直接输入 #RRGGBB。")
        hint.setProperty("class", "status")
        hint.setWordWrap(True)
        right.addWidget(hint)
        right.addStretch(1)

        wheel_row.addLayout(right, 1)
        side.addLayout(wheel_row)

        side.addWidget(self._build_preview_block(), 1)
        return side

    def _build_preview_block(self) -> QWidget:
        # 外面套一层"相框"：框底用 sidebar-bg、里面是应用背景，
        # 两者差一档，预览区才有边界感（否则深色主题下整块糊成一片）。
        box = QWidget()
        box.setObjectName("preview-frame")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(0)
        self.preview = ThemePreview()
        lay.addWidget(self.preview, 1)
        return box

    # ── 底部：对比度 + 按钮 ──

    def _build_footer(self) -> QWidget:
        footer = QWidget()
        footer.setObjectName("theme-footer")
        lay = QVBoxLayout(footer)
        lay.setContentsMargins(14, 6, 14, 12)
        lay.setSpacing(8)

        self.contrast_label = QLabel()
        self.contrast_label.setWordWrap(True)
        lay.addWidget(self.contrast_label)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("从预设重置"))
        self.preset_combo = QComboBox()
        for name in theme.THEMES:
            self.preset_combo.addItem(name)
        self.preset_combo.currentTextChanged.connect(self._reset_from_preset)
        row.addWidget(self.preset_combo)
        row.addStretch(1)

        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        apply_btn = QPushButton("应用")
        apply_btn.setObjectName("primary")
        apply_btn.clicked.connect(self._apply)
        row.addWidget(apply_btn)
        lay.addLayout(row)
        return footer

    # ── 数据流 ──

    def load_tokens(self, tokens: Dict[str, str]) -> None:
        """载入一份令牌（缺键用亮色补，非法值丢弃）。"""
        self._tokens = dict(theme.LIGHT_TOKENS)
        for key in theme.ALL_TOKEN_KEYS:
            value = (tokens or {}).get(key)
            if theme.is_hex_color(value):
                self._tokens[key] = value.upper()
        for key, row in self._rows.items():
            row.set_color(self._tokens[key])
        self._select_token(self._current)
        self._refresh_preview()

    def _select_token(self, key: str) -> None:
        if key not in self._rows:
            return
        self._current = key
        for other, row in self._rows.items():
            row.set_selected(other == key)
        self.current_label.setText(f"正在编辑：{theme.TOKEN_LABELS.get(key, key)}")
        self._sync_editor_to_current()

    def _sync_editor_to_current(self) -> None:
        """把当前色位的值灌进色轮 / 滑杆 / 输入框（不回触发信号）。"""
        self._syncing = True
        color = QColor(self._tokens[self._current])
        hue = max(0, color.hue())
        self.wheel.set_hsv(hue, color.saturation(), color.value())
        self.value_slider.setValue(int(round(color.value() * 100 / 255)))
        self.hex_edit.setText(self._tokens[self._current])
        # 虚线框是必要的：当前色常常正好等于背景色（比如选中「应用背景」本身），
        # 只有实色填充时那块控件看着就是个空白输入框。
        self.preview_swatch.setStyleSheet(
            f"background: {self._tokens[self._current]};"
            f" border: 1px dashed rgba(128,128,128,0.95); border-radius: 6px;")
        self._syncing = False

    def _on_wheel_picked(self, hue: int, sat: int) -> None:
        if self._syncing:
            return
        value = self.wheel.hsv()[2]
        self._set_current_color(QColor.fromHsv(hue, sat, value).name().upper())

    def _on_value_changed(self, percent: int) -> None:
        if self._syncing:
            return
        hue, sat, _old = self.wheel.hsv()
        value = int(round(percent * 255 / 100))
        self.wheel.set_value_only(value)
        self._set_current_color(QColor.fromHsv(hue, sat, value).name().upper())

    def _on_hex_edited(self) -> None:
        if self._syncing:
            return
        text = self.hex_edit.text().strip()
        if not theme.is_hex_color(text):
            self.hex_edit.setText(self._tokens[self._current])   # 输错就还原
            return
        self._set_current_color(text.upper())

    def _set_current_color(self, color: str) -> None:
        self._tokens[self._current] = color
        self._rows[self._current].set_color(color)
        self._sync_editor_to_current()
        self._refresh_preview()

    def _reset_from_preset(self, name: str) -> None:
        if name not in theme.THEMES:      # 下拉里的「自定义」不是可重置目标
            return
        self.load_tokens(theme.tokens_for(name))

    def _sync_preset_combo(self) -> None:
        """色值与某预设一致就选中它，否则显示「自定义」。

        不做这一步的话，打开初音主题时下拉仍写着「亮色」，
        用户会以为当前是亮色。
        """
        matched = theme.match_preset(self._tokens)
        self.preset_combo.blockSignals(True)
        if matched:
            self.preset_combo.setCurrentText(matched)
        else:
            if self.preset_combo.findText(theme.CUSTOM_THEME_NAME) < 0:
                self.preset_combo.addItem(theme.CUSTOM_THEME_NAME)
            self.preset_combo.setCurrentText(theme.CUSTOM_THEME_NAME)
        self.preset_combo.blockSignals(False)

    def _refresh_preview(self) -> None:
        self.preview.apply_tokens(self._tokens)
        self._sync_preset_combo()
        self._refresh_contrast()

    def _refresh_contrast(self) -> None:
        report = theme.contrast_report(self._tokens)
        failed = [(label, ratio, minimum) for label, ratio, minimum, ok in report if not ok]
        if not failed:
            self.contrast_label.setText("对比度检查：7 项全部达标（WCAG AA 4.5:1）")
            self.contrast_label.setStyleSheet("")
            return
        lines = "；".join(f"{label} {ratio:.2f}（需 {minimum}）"
                         for label, ratio, minimum in failed)
        self.contrast_label.setText(f"⚠ 对比度不足：{lines}")
        self.contrast_label.setStyleSheet("color: #C4342B;")

    def _apply(self) -> None:
        self.theme_applied.emit(dict(self._tokens))
        self.accept()

    def tokens(self) -> Dict[str, str]:
        return dict(self._tokens)
