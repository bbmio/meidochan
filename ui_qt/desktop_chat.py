"""独立立绘配套的迷你对话框：显示最近几条消息 + 输入框，可随时隐藏。

为什么不做成第二个 ChatView：ChatView 带了历史工具行、导出、代码块折叠等一整套
重交互，塞进桌宠旁边太重。这里只要「看得到最近说了什么 + 能打字」，
所以用纯文本流水账，不渲染 Markdown —— 省掉大量控件与重排开销。

与主窗口共用同一条引擎链路（send_requested / stop_requested 直接接 main_window），
所以两边看到的是同一段对话，不会各聊各的。
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .chat_view import detect_status, split_reply

MAX_LINES = 40          # 流水账最多保留多少条，避免越长越卡
ROLE_LABEL = {"user": "你", "assistant": "妹抖酱"}


class _TitleBar(QWidget):
    """对话框标题栏：按住可以把整个对话框拖走。"""

    def __init__(self, window: "DesktopChatWindow") -> None:
        super().__init__(window)
        self._window = window
        self._origin: Optional[Tuple[QPoint, QPoint]] = None

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = (event.globalPosition().toPoint(),
                            self._window.frameGeometry().topLeft())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._origin is not None:
            gp, tl = self._origin
            self._window.move(tl + event.globalPosition().toPoint() - gp)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._origin = None
        super().mouseReleaseEvent(event)


class DesktopChatWindow(QWidget):
    """桌宠旁边的迷你对话窗。"""

    send_requested = Signal(str)
    stop_requested = Signal()
    hide_requested = Signal()

    WIDTH = 330
    HEIGHT = 380

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(None)
        # 与立绘保持一致：立绘是置顶的，对话框跟着沉到后面会显得很割裂。
        # 觉得挡事的话直接点「隐藏」即可。
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setObjectName("desktopchat")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle("妹抖酱 · 对话")
        self.resize(self.WIDTH, self.HEIGHT)

        self._items: List[Tuple[str, str]] = []
        self._status = ""
        self._busy = False

        root = QVBoxLayout(self)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)

        # ── 标题栏 ──
        bar = _TitleBar(self)
        bar.setObjectName("chatbar")
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(10, 6, 6, 6)
        bar_layout.setSpacing(6)
        title = QLabel("对话")
        title.setObjectName("chatbar-title")
        bar_layout.addWidget(title)
        bar_layout.addStretch(1)
        self.hide_btn = QPushButton("隐藏")
        self.hide_btn.setObjectName("chatbar-btn")
        self.hide_btn.setToolTip("隐藏这个对话框（右键点立绘可以再叫出来）")
        self.hide_btn.clicked.connect(self.hide_requested.emit)
        bar_layout.addWidget(self.hide_btn)
        root.addWidget(bar)

        # ── 流水账 ──
        self.transcript = QPlainTextEdit()
        self.transcript.setObjectName("chattranscript")
        self.transcript.setReadOnly(True)
        self.transcript.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.transcript.setPlaceholderText("还没有对话，下面输入点什么吧")
        root.addWidget(self.transcript, 1)

        # ── 状态行 ──
        self.status = QLabel("")
        self.status.setObjectName("chatstatus")
        self.status.setVisible(False)
        root.addWidget(self.status)

        # ── 输入区 ──
        self.input = QPlainTextEdit()
        self.input.setObjectName("chatinput")
        self.input.setPlaceholderText("输入消息…  Enter 发送，Shift+Enter 换行")
        self.input.setFixedHeight(58)
        self.input.installEventFilter(self)
        root.addWidget(self.input)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(8, 4, 8, 8)
        buttons.setSpacing(6)
        buttons.addStretch(1)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("chatstop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_requested.emit)
        buttons.addWidget(self.stop_btn)
        self.send_btn = QPushButton("发送")
        self.send_btn.setObjectName("chatsend")
        self.send_btn.clicked.connect(self.submit)
        buttons.addWidget(self.send_btn)
        root.addLayout(buttons)

    # ── 输入 ──

    def eventFilter(self, obj, event):  # noqa: N802 (Qt 命名)
        # Enter 发送 / Shift+Enter 换行（与主窗口输入框一致的手感）
        if obj is self.input and event.type() == event.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    return False
                self.submit()
                return True
            if key == Qt.Key.Key_Escape:
                self.stop_requested.emit()
                return True
        return super().eventFilter(obj, event)

    def submit(self) -> None:
        if self._busy:
            return
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.input.clear()
        self.send_requested.emit(text)

    def focus_input(self) -> None:
        self.input.setFocus()

    # ── 对话内容 ──

    def add_message(self, role: str, text: str) -> None:
        self._items.append((role, text or ""))
        if len(self._items) > MAX_LINES:
            del self._items[:-MAX_LINES]
        self._render(stick=True)

    def begin_response(self) -> None:
        self.add_message("assistant", "")
        self.set_busy(True)

    def update_response(self, raw: str) -> None:
        status = detect_status(raw)
        if status:
            self.set_status(status)
            return
        reasoning, body = split_reply(raw)
        if not body.strip() and reasoning.strip():
            self.set_status("思考中…")
        else:
            self.set_status("")
        if not self._items or self._items[-1][0] != "assistant":
            self._items.append(("assistant", ""))
        self._items[-1] = ("assistant", body)
        self._render(stick=True)

    def end_response(self) -> None:
        self.set_busy(False)
        self.set_status("")
        self._render(stick=True)

    def fail(self, message: str) -> None:
        self.set_busy(False)
        self.set_status("")
        self.add_message("assistant", f"出错了：{message}")

    def drop_last_assistant(self) -> None:
        """主窗口点「重新生成」时同步撤掉这里的旧回复，避免两边对不上。"""
        for index in range(len(self._items) - 1, -1, -1):
            if self._items[index][0] == "assistant":
                del self._items[index]
                break
        self._render(stick=True)

    def load_messages(self, messages: List[Tuple[str, str]]) -> None:
        """用已有对话铺一遍（独立出来时同步当前上下文）。"""
        self._items = [(r, t) for r, t in (messages or [])][-MAX_LINES:]
        self._render(stick=True)

    def set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self.send_btn.setEnabled(not busy)
        self.stop_btn.setEnabled(bool(busy))
        if not busy:
            self.input.setFocus()

    def set_status(self, text: str) -> None:
        self._status = text or ""
        if self._status:
            self.status.setText(f"… {self._status}")
            self.status.setVisible(True)
        else:
            self.status.setVisible(False)
            self.status.setText("")

    # ── 渲染 ──

    def _render(self, stick: bool = True) -> None:
        bar = self.transcript.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        prev = bar.value()
        lines = []
        for role, text in self._items:
            body = text.strip()
            if not body:
                continue
            lines.append(f"{ROLE_LABEL.get(role, role)}：{body}")
        self.transcript.setPlainText("\n\n".join(lines))
        if stick and at_bottom:
            bar.setValue(bar.maximum())
        else:
            # setPlainText 会把滚动位置重置到顶部 —— 正在往回翻的时候
            # 每次流式刷新都会被拽到最上面，得把位置还原回去
            bar.setValue(min(prev, bar.maximum()))
