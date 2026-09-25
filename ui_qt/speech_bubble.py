"""桌宠头顶的对话气泡：按内容自适应大小，圆角、透明、点击穿透。

为什么单独开一个窗口而不是画在立绘窗口里：立绘是 QWebEngineView 渲染的独立表面，
Qt 控件盖在它上面不保证能画出来；独立窗口最稳，也不影响立绘的拖拽与缩放。

`WindowTransparentForInput` 让气泡对鼠标完全透明 —— 点到气泡等于点到下面的立绘，
不会挡住拖拽。
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QLabel, QWidget

from . import theme

MAX_WIDTH = 300        # 气泡最大宽度（超出换行）
MIN_WIDTH = 96
PAD_H = 12
PAD_V = 9
RADIUS = 12
TAIL_W = 14
TAIL_H = 8
HOLD_MS = 9000         # 回复结束后气泡再停留多久


def _token(name: str, fallback: str) -> QColor:
    # 取「当前生效主题」而不是模块常量：换肤后要立刻反映到自绘气泡上。
    # 注意气泡是 paintEvent 里 fillPath 画的，换主题时必须 update() 触发重绘，
    # 光 setStyleSheet 是刷不掉的（见 main_window.apply_theme）。
    return QColor(theme.active_tokens().get(name, fallback))


class SpeechBubble(QWidget):
    """自适应大小的对话气泡，带一个指向立绘的小尖角。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput      # 点击穿透
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setObjectName("bubbletext")
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._text = ""
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    # ── 内容 ──

    def show_text(self, text: str, hold_ms: int = 0) -> None:
        """显示一段文字；hold_ms > 0 表示过一会儿自动收起。"""
        text = (text or "").strip()
        if not text:
            self.hide()
            return
        self._hide_timer.stop()
        if text == self._text and self.isVisible():
            if hold_ms > 0:
                self._hide_timer.start(hold_ms)
            return
        self._text = text
        self._relayout()
        self.show()
        self.raise_()
        if hold_ms > 0:
            self._hide_timer.start(hold_ms)

    def hold_then_hide(self, hold_ms: int = HOLD_MS) -> None:
        if self.isVisible():
            self._hide_timer.start(hold_ms)

    def clear(self) -> None:
        self._hide_timer.stop()
        self._text = ""
        self.hide()

    # ── 自适应尺寸 ──

    def _relayout(self) -> None:
        metrics = QFontMetrics(self.label.font())
        # 先按最大宽度试排，拿到换行后的实际包围盒，再据此定窗口大小
        box = metrics.boundingRect(
            QRect(0, 0, MAX_WIDTH - 2 * PAD_H, 10000),
            int(Qt.TextFlag.TextWordWrap), self._text)
        width = max(MIN_WIDTH, min(MAX_WIDTH, box.width() + 2 * PAD_H))
        height = box.height() + 2 * PAD_V + TAIL_H
        self.resize(width, height)
        self.label.setGeometry(PAD_H, PAD_V, width - 2 * PAD_H, height - 2 * PAD_V - TAIL_H)
        self.label.setText(self._text)

    # ── 摆放 ──

    def place_above(self, anchor: QWidget, gap: int = 6) -> None:
        """摆在 anchor（立绘窗口）上方并水平居中，超出屏幕就夹回来。"""
        screen = anchor.screen()
        area = screen.availableGeometry() if screen is not None else None
        x = anchor.x() + (anchor.width() - self.width()) // 2
        y = anchor.y() - self.height() - gap
        if area is not None:
            x = max(area.left() + 4, min(x, area.right() - self.width() - 4))
            if y < area.top() + 4:
                # 上方放不下就翻到下方，尖角方向也跟着反过来（这里只挪位置）
                y = anchor.y() + anchor.height() + gap
            y = max(area.top() + 4, min(y, area.bottom() - self.height() - 4))
        self.move(x, y)

    # ── 绘制：圆角矩形 + 底部尖角 ──

    def paintEvent(self, event):  # noqa: N802 (Qt 命名)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        body_h = self.height() - TAIL_H
        path = QPainterPath()
        path.addRoundedRect(0.5, 0.5, self.width() - 1.0, body_h - 1.0,
                            RADIUS, RADIUS)
        cx = self.width() / 2
        tail = QPainterPath()
        tail.moveTo(QPointF(cx - TAIL_W / 2, body_h - 1.0))
        tail.lineTo(QPointF(cx, body_h + TAIL_H - 1.0))
        tail.lineTo(QPointF(cx + TAIL_W / 2, body_h - 1.0))
        tail.closeSubpath()
        path = path.united(tail)

        painter.fillPath(path, _token("bg", "#FFFFFF"))
        painter.setPen(QPen(_token("border", "#D9D9E0"), 1.0))
        painter.drawPath(path)
        painter.end()


def bubble_style() -> str:
    """气泡里文字的颜色（跟着当前主题走）。"""
    return (f"QLabel#bubbletext {{ color: {theme.active_tokens().get('text', '#1C2024')};"
            f" font-size: 13px; background: transparent; }}")
