"""桌宠头顶的对话气泡：按内容自适应大小，圆角、透明。

为什么单独开一个窗口而不是画在立绘窗口里：立绘是 QWebEngineView 渲染的独立表面，
Qt 控件盖在它上面不保证能画出来；独立窗口最稳，也不影响立绘的拖拽与缩放。

⚠️ **气泡刻意不设 `WindowTransparentForInput`**（历史上设过）。那个标志让气泡对鼠标
完全透明，代价是它**收不到任何鼠标事件** —— 滑条拖不动，`enterEvent`/`leaveEvent`
也永远不触发，「悬浮时不消失」就无从实现。去掉它之后：
- 气泡能收鼠标事件 → 滑条可用、悬浮检测用原生事件（不必轮询全局光标位置）
- 代价是点气泡不再穿透到下面的窗口。但气泡是摆在立绘**上方**的
  （上方放不下才翻到下方），正常不覆盖立绘，所以实际影响很小。

高度封顶见 `MAX_CONTENT_HEIGHT`：超长回复出滑条，不再让气泡无限长下去。
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame, QLabel, QScrollArea, QWidget

from . import theme

MAX_WIDTH = 300        # 气泡最大宽度（超出换行）
MIN_WIDTH = 96
PAD_H = 12
PAD_V = 9
RADIUS = 12
TAIL_W = 14
TAIL_H = 8
HOLD_MS = 9000         # 回复结束后气泡再停留多久
#: 内容区最高多少像素 —— 超过就出滑条，不让气泡无限长下去
MAX_CONTENT_HEIGHT = 280
#: 鼠标移开后再留多久就收起。**不是** HOLD_MS：移开鼠标意味着「看完了」，
#: 再等 9 秒会显得赖着不走
LEAVE_GRACE_MS = 1500
#: 流式更新的最小间隔。
#
# ⚠️ 流式生成时 `show_text` 每秒会被调几十次，每次都 `resize()` + `raise_()`。
# `raise_()` 是 `SetWindowPos(HWND_TOP)`，会强制桌面合成器重算 z 序 —— 长回复时
# 持续几十秒，立绘（WebEngine）就明显掉帧。100ms ≈ 10fps，肉眼几乎无感，
# 却把原生窗口操作砍掉约 5 倍。最后一段文本一定会落地（见 `hold_then_hide`）。
MIN_UPDATE_INTERVAL_MS = 100


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
            | Qt.WindowType.WindowDoesNotAcceptFocus   # 不抢焦点，但不挡鼠标
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        # ── 内容区：可滚动 ──
        # 尺寸全部自己算（`setWidgetResizable(False)` + 给标签定死大小）——
        # 交给 QScrollArea 按 sizeHint 猜会和 QLabel 的自动换行形成循环依赖
        self.scroll = QScrollArea(self)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidgetResizable(False)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # ⚠️ 必须显式把 **viewport** 也设成透明。`QScrollArea` 的 viewport 是个普通
        # QWidget，默认会用窗口底色填充自己那块区域 —— 结果就是把父级 `paintEvent`
        # 画的圆角背景**整块抹掉**（实测内容区 alpha=0，只剩文字浮在空中）。
        # `viewport().setAutoFillBackground(False)` / `WA_TranslucentBackground` /
        # `WA_NoSystemBackground` 都**不管用**，只有样式表能压住它。
        self.scroll.setStyleSheet(
            "QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }")
        self.scroll.viewport().setAutoFillBackground(False)
        self.scroll.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setObjectName("bubbletext")
        self.label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.scroll.setWidget(self.label)

        self._text = ""
        self._hovering = False
        #: 当前的自动收起时长；0 = 不自动收起（生成过程中就是 0）
        self._auto_hide_ms = 0

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._on_hold_expired)

        # 贴底延迟一拍：`_relayout` 里刚改完尺寸时，滚动条的 range 还没更新
        # （Qt 要等布局稳定），这时候读 `maximum()` 拿到的是**旧值**。
        self._stick_timer = QTimer(self)
        self._stick_timer.setSingleShot(True)
        self._stick_timer.timeout.connect(self._stick_to_bottom)

        # 流式节流：间隔内来的更新只记下文本，由定时器补一次
        self._pending_text: Optional[str] = None
        self._pending_hold = 0
        self._throttle = QTimer(self)
        self._throttle.setSingleShot(True)
        self._throttle.timeout.connect(self._flush_pending)

    # ── 悬浮：鼠标在上面就不收 ──

    def enterEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        self._hovering = True
        self._hide_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        self._hovering = False
        if self._auto_hide_ms > 0:
            self._hide_timer.start(LEAVE_GRACE_MS)
        super().leaveEvent(event)

    def _on_hold_expired(self) -> None:
        # 兜底：万一 enterEvent 因为窗口重建没触发，这里再判一次
        if self._hovering:
            return
        self.hide()

    # ── 内容 ──

    def show_text(self, text: str, hold_ms: int = 0) -> None:
        """显示一段文字；hold_ms > 0 表示过一会儿自动收起。

        流式生成时每秒会被调几十次，所以**节流**（见 `MIN_UPDATE_INTERVAL_MS`）：
        间隔内来的更新只记下文本，由定时器补一次。间隔外的第一条**立即落地**，
        所以一次性调用（非流式）不受影响。
        """
        text = (text or "").strip()
        if not text:
            self._pending_text = None
            self.hide()
            return
        self._hide_timer.stop()
        self._auto_hide_ms = max(0, hold_ms)
        if text == self._text and self.isVisible():
            if hold_ms > 0 and not self._hovering:
                self._hide_timer.start(hold_ms)
            return
        if self._throttle.isActive():
            self._pending_text = text
            self._pending_hold = max(0, hold_ms)
            return
        self._apply_text(text, hold_ms)
        self._throttle.start(MIN_UPDATE_INTERVAL_MS)

    def _apply_text(self, text: str, hold_ms: int) -> None:
        """真正落地一次：重排 + 显示。"""
        self._text = text
        self._relayout()
        was_visible = self.isVisible()
        self.show()
        if not was_visible:
            # ⚠️ **只在「从隐藏变可见」时抬一次**。旧实现每次更新都调 raise_()，
            # 那是 SetWindowPos(HWND_TOP)，会强制桌面合成器重算 z 序 ——
            # 长回复时每秒几十次，立绘（WebEngine）就明显掉帧。
            # 窗口本来就是 WindowStaysOnTopHint，不需要反复抬。
            self.raise_()
        if hold_ms > 0 and not self._hovering:
            self._hide_timer.start(hold_ms)

    def _flush_pending(self) -> None:
        """节流定时器到点：把攒下的那段文本补上。"""
        if self._pending_text is None:
            return
        text, hold = self._pending_text, self._pending_hold
        self._pending_text = None
        self._apply_text(text, hold)
        self._throttle.start(MIN_UPDATE_INTERVAL_MS)

    def hold_then_hide(self, hold_ms: int = HOLD_MS) -> None:
        # 先把节流攒下的最后一段文本落地，否则气泡会停在半截
        self._flush_pending()
        self._auto_hide_ms = max(0, hold_ms)
        # 鼠标正停在气泡上时不启动倒计时 —— 否则「悬浮时不消失」会在这一轮失效
        if self.isVisible() and hold_ms > 0 and not self._hovering:
            self._hide_timer.start(hold_ms)

    def clear(self) -> None:
        self._hide_timer.stop()
        self._throttle.stop()
        self._pending_text = None
        self._auto_hide_ms = 0
        self._text = ""
        self.hide()

    def hideEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        # 任何隐藏都要把节流攒下的文本丢掉 —— 否则定时器到点会把气泡**又弹回来**
        self._throttle.stop()
        self._pending_text = None
        super().hideEvent(event)

    # ── 自适应尺寸 ──

    def _relayout(self) -> None:
        metrics = QFontMetrics(self.label.font())
        avail = MAX_WIDTH - 2 * PAD_H
        wrap = int(Qt.TextFlag.TextWordWrap)

        # **改尺寸之前**先记下「用户是不是正在看底部」。改完再读就晚了 ——
        # 内容一变长，range 就变了，那时再判会永远是「没贴底」。
        bar = self.scroll.verticalScrollBar()
        was_at_bottom = bar.value() >= bar.maximum() - 4

        # 先按最大宽度试排，拿到换行后的实际包围盒，再据此定窗口大小
        box = metrics.boundingRect(QRect(0, 0, avail, 10000), wrap, self._text)
        needs_scroll = box.height() > MAX_CONTENT_HEIGHT
        sb_w = self.scroll.verticalScrollBar().sizeHint().width() if needs_scroll else 0

        width = max(MIN_WIDTH, min(MAX_WIDTH, box.width() + 2 * PAD_H))
        label_w = max(1, width - 2 * PAD_H - sb_w)
        if needs_scroll:
            # 出了滑条 → 视口变窄 → 文字重新换行 → 高度又不一样。
            # 所以必须**扣掉滑条宽度再算一次**，否则会在「要不要滑条」之间反复横跳。
            box = metrics.boundingRect(QRect(0, 0, label_w, 10000), wrap, self._text)

        content_h = min(box.height(), MAX_CONTENT_HEIGHT)
        height = content_h + 2 * PAD_V + TAIL_H
        self.resize(width, height)
        self.scroll.setGeometry(PAD_H, PAD_V, label_w + sb_w, content_h)
        # 标签按**完整内容**撑开：比视口高的部分靠滚动看
        self.label.setFixedSize(label_w, max(box.height(), content_h))
        self.label.setText(self._text)

        if was_at_bottom:
            self._stick_timer.start(0)      # 等布局稳定后再贴底

    def _stick_to_bottom(self) -> None:
        """把滚动条拉到底（内容增长时跟着最新文字走）。

        与 `desktop_chat._render()` 同一套做法 —— 流式刷新会重建文本，
        不跟着走的话用户永远停在第一屏，看不到正在生成的内容。
        """
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

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
