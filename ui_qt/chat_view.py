"""聊天区（ARCHITECTURE_V3 §5.2 / §5.4）。

- 气泡 + Markdown 渲染；思考链以可折叠块呈现（不用 <details>，Qt 不渲染它）。
- 输入区：输入框 / 附件 / 发送 / 停止；生成中输入区禁用并显示「停止」。
- 快捷键：Enter 或 Ctrl+Enter 发送，Shift+Enter 换行，Esc 停止生成。
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QKeyEvent
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

# 时间戳格式由存储层定义（core.history），这里只负责显示 —— 别另写一份格式串
from core.history import STAMP_FORMAT, now_stamp  # noqa: E402

# 引擎把思考链包成 <details><summary>[思考过程]</summary>…</details>（见 core/engine.py）
_DETAILS_RE = re.compile(
    r"<details\b[^>]*>\s*<summary[^>]*>(?P<title>.*?)</summary>(?P<body>.*?)</details>",
    re.DOTALL | re.IGNORECASE,
)
_STATUS_RE = re.compile(r"^_ (?P<text>[^\n]{1,200})_$")


def format_time(stamp: str) -> str:
    """把落盘时间戳压成界面上好读的形式。

    同一天只显示 HH:MM，跨天补上 MM-DD，跨年再补上年份。
    解析失败或为空一律返回空串 —— 旧会话文件里根本没有 time，
    那就**不显示**，而不是拿别的时刻冒充。
    """
    if not stamp:
        return ""
    try:
        dt = datetime.strptime(str(stamp).strip(), STAMP_FORMAT)
    except (ValueError, TypeError):
        return ""
    now = datetime.now()
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    if dt.year == now.year:
        return dt.strftime("%m-%d %H:%M")
    return dt.strftime("%Y-%m-%d %H:%M")


def split_reply(raw: str) -> tuple:
    """把引擎给的展示文本拆成 (思考链, 正文)。"""
    if not raw:
        return "", ""
    reasoning = ""
    body = raw
    match = _DETAILS_RE.search(raw)
    if match:
        reasoning = match.group("body").strip()
        body = (raw[:match.start()] + raw[match.end():]).strip()
    return reasoning, body


def detect_status(raw: str) -> Optional[str]:
    """识别引擎的临时状态行（形如 `_ 正在调用 web_search..._`）。

    引擎的状态行统一是「下划线 + 空格 + 内容 + 下划线」（见 core/engine.py）。
    这里**不能**用 `[^_]` 排除下划线 —— 工具名里就有下划线（write_file / kb_search /
    mcp_status…，20 个工具里 14 个），排除掉会让这些状态行识别失败，
    原文 `_ 正在调用 xxx..._` 就会漏进聊天气泡。改用「下划线 + 空格」开头来限定，
    既容得下内容里的下划线，又不会误判 Markdown 的 `_斜体_`。
    """
    if not raw:
        return None
    match = _STATUS_RE.match(raw.strip())
    return match.group("text") if match else None


class _InputEdit(QTextEdit):
    """输入框：Enter/Ctrl+Enter 发送，Shift+Enter 换行，Esc 停止。"""

    submitted = Signal()
    escaped = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
            else:
                self.submitted.emit()
                event.accept()
            return
        if key == Qt.Key.Key_Escape:
            self.escaped.emit()
            event.accept()
            return
        super().keyPressEvent(event)


AUTO_FOLD_LINES = 20      # 超过这个行数的代码块默认折叠（仍可手动展开）
MAX_CODE_HEIGHT = 420     # 展开时代码区最大高度，超出内部滚动


def split_markdown(text: str) -> List[tuple]:
    """把 Markdown 切成 `("text", 文本)` / `("code", (语言, 代码))` 序列。

    流式中间态（``` 还没闭合）也会被当作一个代码块，
    所以生成过程中就能看到并折叠它。
    """
    source = text or ""
    parts: List[tuple] = []
    pos = 0
    # 顺序扫描（不能用 finditer 找开头：闭合的 ``` 同样符合"开头"形态，会误判）
    while True:
        start = source.find("```", pos)
        if start == -1:
            break
        if start > pos:
            parts.append(("text", source[pos:start]))

        line_end = source.find("\n", start + 3)
        if line_end == -1:                  # 只有 ``` 没有正文
            parts.append(("code", ("", "")))
            pos = len(source)
            break
        lang = source[start + 3:line_end].strip()
        body_start = line_end + 1
        end = source.find("```", body_start)
        if end == -1:                       # 未闭合：到文本末尾都算代码块
            parts.append(("code", (lang, source[body_start:])))
            pos = len(source)
            break
        parts.append(("code", (lang, source[body_start:end].rstrip("\n"))))
        pos = end + 3
    if pos < len(source):
        parts.append(("text", source[pos:]))

    result: List[tuple] = []
    for kind, payload in parts:
        if kind == "text" and payload.strip():
            result.append((kind, payload))
        elif kind == "code" and payload[1].strip():
            result.append((kind, payload))
    return result


class CodeBlock(QFrame):
    """可折叠代码块：头部（语言 · 行数 · 折叠/展开 · 复制）+ 等宽只读正文。"""

    COPY_LABEL = "复制"
    COPIED_LABEL = "已复制"

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setProperty("class", "codeblock")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(0)

        self.toggle = QToolButton()
        self.toggle.setObjectName("code-toggle")
        self.toggle.setCheckable(True)
        self.toggle.setToolTip("点击折叠 / 展开这段代码块")
        self.toggle.toggled.connect(self._on_toggled)
        header_layout.addWidget(self.toggle, 1)

        self.copy_btn = QPushButton(self.COPY_LABEL)
        self.copy_btn.setObjectName("code-copy")
        self.copy_btn.setToolTip("复制这段代码")
        self.copy_btn.clicked.connect(self._copy)
        header_layout.addWidget(self.copy_btn)

        self.viewer = QPlainTextEdit()
        self.viewer.setObjectName("code-body")
        self.viewer.setReadOnly(True)
        self.viewer.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.viewer.setMaximumHeight(MAX_CODE_HEIGHT)

        layout.addWidget(header)
        layout.addWidget(self.viewer)

        self._lang = ""
        self._lines = 0
        self._user_touched = False    # 用户手动切过之后，流式刷新不再改他的选择
        self._reset_timer = QTimer(self)
        self._reset_timer.setSingleShot(True)
        self._reset_timer.timeout.connect(
            lambda: self.copy_btn.setText(self.COPY_LABEL))

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.viewer.toPlainText())
        self.copy_btn.setText(self.COPIED_LABEL)
        self._reset_timer.start(1200)

    def set_code(self, lang: str, code: str) -> None:
        self._lang = lang or ""
        self.viewer.setPlainText(code)
        self._lines = len(code.splitlines()) or 1
        if not self._user_touched:
            collapsed = self._lines > AUTO_FOLD_LINES
            self.toggle.blockSignals(True)
            self.toggle.setChecked(not collapsed)
            self.toggle.blockSignals(False)
            self.viewer.setVisible(not collapsed)
        self._update_label()

    def _on_toggled(self, checked: bool) -> None:
        self._user_touched = True
        self.viewer.setVisible(checked)
        self._update_label()

    def _update_label(self) -> None:
        arrow = "▼" if self.toggle.isChecked() else "▶"
        lang = f" · {self._lang}" if self._lang else ""
        state = "" if self.toggle.isChecked() else "（已折叠）"
        self.toggle.setText(f"{arrow} 代码块{lang} · {self._lines} 行{state}")


class MessageBubble(QWidget):
    """一条消息行；user 靠右、assistant 靠左。

    assistant 的正文按「文本段 / 代码块段」渲染：文本段走 Markdown，
    代码块段用可折叠的 CodeBlock，长代码默认折叠、随时可手动展开。
    每条消息带消息级操作（复制；assistant 另有重新生成），悬停时出现。
    """

    COPY_LABEL = "复制"
    COPIED_LABEL = "已复制"
    # 操作行占位高度固定：只切换按钮可见性而不收起整行，避免悬停时上下抖动
    ACTION_ROW_HEIGHT = 24

    regenerate_requested = Signal()

    def __init__(self, role: str, text: str = "", time: str = "",
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.role = role
        self._body_text = text or ""
        self._time_text = format_time(time)
        self._raw_time = time or ""
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 2, 0, 2)
        row.setSpacing(0)

        self.body = QWidget(self)
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(4)

        self.think_toggle: Optional[QToolButton] = None
        self.think_body: Optional[QLabel] = None
        self.label: Optional[QLabel] = None
        self.cursor: Optional[QLabel] = None
        self._blink: Optional[QTimer] = None
        self._widgets: List[QWidget] = []
        self._seg_kinds: List[str] = []

        if role == "assistant":
            self.think_toggle = QToolButton()
            self.think_toggle.setObjectName("think-toggle")
            self.think_toggle.setCheckable(True)
            self.think_toggle.setChecked(False)
            self.think_toggle.setText("▶ [思考过程]")
            self.think_toggle.setVisible(False)
            self.think_toggle.toggled.connect(self._toggle_reasoning)
            body_layout.addWidget(self.think_toggle, 0, Qt.AlignmentFlag.AlignLeft)

            self.think_body = QLabel()
            self.think_body.setObjectName("think-body")
            self.think_body.setWordWrap(True)
            self.think_body.setTextFormat(Qt.TextFormat.MarkdownText)
            self.think_body.setVisible(False)
            body_layout.addWidget(self.think_body)

            self.content_box = QWidget(self)
            self.content_layout = QVBoxLayout(self.content_box)
            self.content_layout.setContentsMargins(0, 0, 0, 0)
            self.content_layout.setSpacing(6)
            body_layout.addWidget(self.content_box)

            # 流式光标：生成期间显示在正文末尾。没有它，暂停的流式看起来和
            # 「已生成完」完全一样，用户会以为卡死了。
            self.cursor = QLabel("▍")
            self.cursor.setObjectName("stream-cursor")
            self.cursor.setVisible(False)
            body_layout.addWidget(self.cursor, 0, Qt.AlignmentFlag.AlignLeft)
            self._blink = QTimer(self)
            self._blink.setInterval(530)
            self._blink.timeout.connect(self._toggle_cursor)
        else:
            self.label = QLabel(text or "")
            self.label.setWordWrap(True)
            self.label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.LinksAccessibleByMouse
            )
            self.label.setOpenExternalLinks(True)
            self.label.setProperty("class", "bubble")
            self.label.setObjectName("bubble-user")
            self.label.setTextFormat(Qt.TextFormat.PlainText)
            body_layout.addWidget(self.label)

        if role == "user":
            row.addStretch(1)
            row.addWidget(self.body, 0)
        else:
            row.addWidget(self.body, 1)

        # 时间戳：常显，跟着气泡那一侧对齐。必须加在操作行**之前** ——
        # 操作行的按钮虽然默认隐藏，但容器本身始终占着固定高度，
        # 放它后面会平白多出一段空白。
        self.time_label: Optional[QLabel] = None
        if self._time_text:
            self.time_label = QLabel(self._time_text)
            self.time_label.setObjectName("msg-time")
            self.time_label.setToolTip(self._raw_time)
            body_layout.addWidget(
                self.time_label, 0,
                Qt.AlignmentFlag.AlignRight if role == "user"
                else Qt.AlignmentFlag.AlignLeft)

        self._build_actions(body_layout)

    # ── 消息级操作 ──

    def _build_actions(self, body_layout: QVBoxLayout) -> None:
        self.actions = QWidget(self)
        self.actions.setFixedHeight(self.ACTION_ROW_HEIGHT)
        actions = QHBoxLayout(self.actions)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(4)

        self._action_buttons: List[QPushButton] = []
        if self.role == "user":
            actions.addStretch(1)      # 用户消息靠右，操作也跟着靠右

        self.copy_btn = QPushButton(self.COPY_LABEL)
        self.copy_btn.setObjectName("msg-action")
        self.copy_btn.setToolTip("复制这条消息")
        self.copy_btn.clicked.connect(self._copy)
        actions.addWidget(self.copy_btn)
        self._action_buttons.append(self.copy_btn)

        if self.role == "assistant":
            self.regen_btn = QPushButton("重新生成")
            self.regen_btn.setObjectName("msg-action")
            self.regen_btn.setToolTip("用上一条输入重新生成回复")
            self.regen_btn.clicked.connect(self.regenerate_requested.emit)
            actions.addWidget(self.regen_btn)
            self._action_buttons.append(self.regen_btn)

        if self.role == "assistant":
            actions.addStretch(1)

        body_layout.addWidget(self.actions)
        self._set_actions_visible(False)

        self._copy_timer = QTimer(self)
        self._copy_timer.setSingleShot(True)
        self._copy_timer.timeout.connect(
            lambda: self.copy_btn.setText(self.COPY_LABEL))

    def _set_actions_visible(self, visible: bool) -> None:
        for btn in self._action_buttons:
            btn.setVisible(visible)

    def enterEvent(self, event):  # noqa: N802 (Qt 命名)
        self._set_actions_visible(True)
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802 (Qt 命名)
        self._set_actions_visible(False)
        super().leaveEvent(event)

    def body_text(self) -> str:
        """这条消息的正文纯文本（复制用）。"""
        return self._body_text

    def _copy(self) -> None:
        text = self._body_text or (self.label.text() if self.label is not None else "")
        QGuiApplication.clipboard().setText(text)
        self.copy_btn.setText(self.COPIED_LABEL)
        self._copy_timer.start(1200)

    def _toggle_reasoning(self, checked: bool) -> None:
        self.think_body.setVisible(checked)
        self.think_toggle.setText(("▼ " if checked else "▶ ") + "[思考过程]")

    def _toggle_cursor(self) -> None:
        if self.cursor is None:
            return
        # 只换文字、**不切可见性**：切换可见性会让气泡高度反复变化（实测 21px），
        # 滚动条跟着上下抽动。用不换行空格占位，高度宽度都不变。
        self.cursor.setText("\u00a0" if self.cursor.text() else "▍")

    def set_streaming(self, active: bool) -> None:
        """生成中显示闪烁光标；结束即隐藏（否则看起来像还在写）。"""
        if self.cursor is None or self._blink is None:
            return
        if active:
            self.cursor.setText("▍")
            self.cursor.setVisible(True)
            self._blink.start()
        else:
            self._blink.stop()
            self.cursor.setVisible(False)
        # 生成中不给操作（内容还没定稿，复制/重新生成都没有意义）
        if active:
            self._set_actions_visible(False)

    def set_content(self, reasoning: str, body: str) -> None:
        self._body_text = body or ""
        if self.think_toggle is not None and reasoning:
            if not self.think_toggle.isVisible():
                self.think_toggle.setVisible(True)
            self.think_body.setText(reasoning)
        if self.role == "user":
            self.label.setText(body or "")
            return
        self._render_segments(body or "")

    def _render_segments(self, body: str) -> None:
        parts = split_markdown(body)
        kinds = [kind for kind, _ in parts]
        if kinds != self._seg_kinds:
            # 段结构变了才重建控件；否则只更新文本，避免流式刷新时闪烁/丢折叠状态
            self._rebuild_segments(kinds)
            self._seg_kinds = kinds
        for (kind, payload), widget in zip(parts, self._widgets):
            if kind == "text":
                widget.setText(payload)
            else:
                lang, code = payload
                widget.set_code(lang, code)

    def _rebuild_segments(self, kinds: List[str]) -> None:
        for widget in self._widgets:
            self.content_layout.removeWidget(widget)
            widget.setParent(None)
            widget.deleteLater()
        self._widgets = []

        for kind in kinds:
            if kind == "text":
                label = QLabel()
                label.setWordWrap(True)
                label.setTextFormat(Qt.TextFormat.MarkdownText)
                label.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse
                    | Qt.TextInteractionFlag.LinksAccessibleByMouse
                )
                label.setOpenExternalLinks(True)
                label.setProperty("class", "bubble")
                label.setObjectName("bubble-assistant")
                widget: QWidget = label
            else:
                widget = CodeBlock()
            self.content_layout.addWidget(widget)
            self._widgets.append(widget)

    def set_max_width(self, width: int, side: int = 0) -> None:
        """限制消息宽度；side>0 时左右各留 side 边距，把整列在面板里居中。

        居中是必要的：助手消息左对齐、用户消息右对齐，两者共用同一条「阅读列」，
        窗口拉宽时若不居中，内容会贴到两侧、中间空一大块。
        """
        self.body.setMaximumWidth(max(160, width))
        row = self.layout()
        if row is not None:
            row.setContentsMargins(side, 2, side, 2)


class StatusLine(QLabel):
    """生成过程中的临时状态行（如「正在检索历史…」），出现正式文本后消失。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("", parent)
        self.setObjectName("bubble-status")
        self.setWordWrap(True)
        self.setContentsMargins(12, 0, 12, 0)


class ChatView(QWidget):
    """聊天面板（消息区 + 状态行 + 输入区 + 历史工具行）。"""

    # 正文列宽上限（px）：参考 Claude.ai / ChatGPT 的 ≈768px 阅读列
    MAX_FLOW_WIDTH = 768
    # 视口距底部多少像素以内才自动跟随滚动（用户往上回看时不要把他拽走）
    NEAR_BOTTOM_PX = 100

    send_requested = Signal(str)
    stop_requested = Signal()
    regenerate_requested = Signal()
    history_load_requested = Signal(int)
    new_session_requested = Signal()
    export_requested = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatpanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        self._bubbles: List[MessageBubble] = []
        self._current: Optional[MessageBubble] = None
        self._pending: Optional[tuple] = None
        self._busy = False
        self._stick_bottom = True
        self._last_flow: Optional[tuple] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 10, 16, 10)
        root.setSpacing(8)

        # ── 消息滚动区 ──
        self.scroll = QScrollArea()
        self.scroll.setObjectName("chatscroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        self._msg_layout = QVBoxLayout(holder)
        self._msg_layout.setContentsMargins(2, 2, 2, 2)
        self._msg_layout.setSpacing(6)
        self._msg_layout.addStretch(1)
        self.scroll.setWidget(holder)
        # QScrollArea 会给内容控件打开 autoFillBackground，于是它用系统窗口色
        # （实测 #F3F3F3）铺底，而不是我们的 bg 令牌 —— QSS 盖不住，必须在控件层关掉，
        # 让 chatpanel 的白底透上来。否则助手消息就不是贴在"应用背景"上。
        holder.setAutoFillBackground(False)
        root.addWidget(self.scroll, 1)
        scrollbar = self.scroll.verticalScrollBar()
        scrollbar.valueChanged.connect(self._on_scrolled)
        # 用 rangeChanged 而不是定时器来"贴底"：布局何时落定只有 Qt 自己知道，
        # 定时器（哪怕 interval=0）可能抢在布局之前跑，拿到还是 0 的 maximum。
        scrollbar.rangeChanged.connect(self._on_range_changed)

        # ── 浮层：空状态提示 / 回到最新 ──
        # 两者都做成 ChatView 的子控件并按滚动区几何定位，不动消息布局本身。
        # 注意必须传 self 作父控件，否则会变成独立顶层窗口。
        self.empty_hint = QLabel(
            "开始对话吧\n\n直接输入消息，或用 /help 查看可用命令", self)
        self.empty_hint.setObjectName("empty-hint")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint.setWordWrap(True)
        self.empty_hint.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self.jump_btn = QPushButton("↓ 回到最新", self)
        self.jump_btn.setObjectName("jumpbtn")
        self.jump_btn.setToolTip("有新内容在下方（往上回看时不会自动跳回）")
        self.jump_btn.setVisible(False)
        self.jump_btn.clicked.connect(self._jump_to_bottom)

        # ── 状态行 ──
        self.status = StatusLine()
        self.status.setVisible(False)
        root.addWidget(self.status)

        # ── 输入区 ──
        input_row = QHBoxLayout()
        input_row.setSpacing(6)
        self.input = _InputEdit()
        self.input.setPlaceholderText(
            "输入消息…  Enter / Ctrl+Enter 发送，Shift+Enter 换行；/model pro 切换模型")
        self.input.setFixedHeight(64)
        self.input.submitted.connect(self._on_submit)
        self.input.escaped.connect(self._on_stop)
        input_row.addWidget(self.input, 1)

        self.attach_btn = QPushButton("附件")
        self.attach_btn.setToolTip("插入要处理的文件路径（配合 /view、/info 等命令）")
        self.attach_btn.clicked.connect(self._pick_attachment)
        input_row.addWidget(self.attach_btn)

        self.send_btn = QPushButton("发送")
        self.send_btn.setObjectName("primary")
        self.send_btn.clicked.connect(self._on_submit)
        input_row.addWidget(self.send_btn)

        self.stop_btn = QPushButton("停止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._on_stop)
        input_row.addWidget(self.stop_btn)
        root.addLayout(input_row)

        # ── 历史工具行 ──
        tools = QHBoxLayout()
        tools.setSpacing(6)
        self.turns = QSpinBox()
        self.turns.setRange(1, 200)
        self.turns.setValue(20)
        self.turns.setToolTip("加载最近 N 轮对话")
        self.load_btn = QPushButton("加载历史")
        self.load_btn.clicked.connect(
            lambda: self.history_load_requested.emit(int(self.turns.value())))
        self.new_btn = QPushButton("新会话")
        self.new_btn.clicked.connect(self.new_session_requested.emit)
        self.export_fmt = QComboBox()
        for fmt in ("markdown", "json", "txt"):
            self.export_fmt.addItem(fmt, fmt)
        self.export_btn = QPushButton("导出")
        self.export_btn.clicked.connect(
            lambda: self.export_requested.emit(self.export_fmt.currentData()))
        tools.addWidget(self.turns)
        tools.addWidget(self.load_btn)
        tools.addWidget(self.new_btn)
        tools.addStretch(1)
        tools.addWidget(self.export_fmt)
        tools.addWidget(self.export_btn)
        root.addLayout(tools)

        # 流式刷新节流：避免每个 token 都重排 Markdown
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(60)
        self._flush_timer.timeout.connect(self._flush)

        self._place_overlays()
        self._update_empty_hint()

    # ── 浮层定位与状态 ──

    def _place_overlays(self) -> None:
        """空状态铺满滚动区；回到最新按钮贴在滚动区右下角。"""
        geo = self.scroll.geometry()
        self.empty_hint.setGeometry(geo)
        self.jump_btn.adjustSize()
        self.jump_btn.move(geo.right() - self.jump_btn.width() - 24,
                           geo.bottom() - self.jump_btn.height() - 12)
        if self.empty_hint.isVisible():
            self.empty_hint.raise_()
        if self.jump_btn.isVisible():
            self.jump_btn.raise_()

    def _update_empty_hint(self) -> None:
        visible = not self._bubbles
        self.empty_hint.setVisible(visible)
        if visible:
            self.empty_hint.raise_()

    def _set_jump_visible(self, visible: bool) -> None:
        self.jump_btn.setVisible(visible)
        if visible:
            self.jump_btn.raise_()

    def _at_bottom(self) -> bool:
        bar = self.scroll.verticalScrollBar()
        return (bar.maximum() - bar.value()) <= self.NEAR_BOTTOM_PX

    def _update_jump_button(self) -> None:
        self._set_jump_visible(bool(self._bubbles) and not self._at_bottom())

    def _on_scrolled(self, _value: int) -> None:
        near = self._at_bottom()
        self._stick_bottom = near
        self._set_jump_visible(bool(self._bubbles) and not near)

    def _on_range_changed(self, _minimum: int, maximum: int) -> None:
        """内容高度变化时：仍在跟随就贴住底部，否则什么都不做。

        这是"自动跟随"的主通道 —— 新消息插入、流式文本变长都会改变 range。
        """
        if self._stick_bottom:
            self.scroll.verticalScrollBar().setValue(maximum)

    def _jump_to_bottom(self) -> None:
        self._stick_bottom = True
        self._scroll_to_bottom(force=True)

    # ── 对外：消息渲染 ──

    def clear(self) -> None:
        for bubble in self._bubbles:
            self._msg_layout.removeWidget(bubble)
            bubble.setParent(None)
            bubble.deleteLater()
        self._bubbles.clear()
        self._current = None
        self._pending = None
        self._stick_bottom = True
        self._last_flow = None
        self.jump_btn.setVisible(False)
        self._update_empty_hint()
        self.set_status(None)

    def add_message(self, role: str, text: str, time: str = "") -> MessageBubble:
        # 先记录「加之前是否贴着底部」：新内容不该把正在往上回看的用户拽下去
        stick = self._at_bottom()
        bubble = MessageBubble(role, text, time)
        bubble.regenerate_requested.connect(self.regenerate_requested)
        self._msg_layout.insertWidget(self._msg_layout.count() - 1, bubble)
        self._bubbles.append(bubble)
        self._last_flow = None      # 有新气泡，必须重新套一遍列宽
        self._apply_widths()
        self._update_empty_hint()
        if stick:
            self._scroll_to_bottom(force=True)
        else:
            QTimer.singleShot(0, self._update_jump_button)
        return bubble

    def last_user_text(self) -> str:
        """最近一条用户消息的正文（重新生成时重发它）。"""
        for bubble in reversed(self._bubbles):
            if bubble.role == "user":
                return bubble.body_text()
        return ""

    def drop_last_assistant(self) -> bool:
        """移除最后一条助手消息（重新生成前先撤掉旧回复）。"""
        for index in range(len(self._bubbles) - 1, -1, -1):
            if self._bubbles[index].role != "assistant":
                continue
            bubble = self._bubbles.pop(index)
            self._msg_layout.removeWidget(bubble)
            bubble.setParent(None)
            bubble.deleteLater()
            self._update_empty_hint()
            return True
        return False

    def load_messages(self, messages: List[dict]) -> None:
        """把历史消息（api 格式）铺到界面；思考链不参与回放（历史里本来也存的是干净正文）。"""
        self.clear()
        for msg in messages or []:
            role = msg.get("role")
            content = msg.get("content")
            if role not in ("user", "assistant") or not isinstance(content, str):
                continue
            reasoning, body = split_reply(content)
            bubble = self.add_message(role, "", msg.get("time", ""))
            bubble.set_content(reasoning, body)
        self._update_empty_hint()
        self._scroll_to_bottom(force=True)

    # ── 对外：生成过程 ──

    def begin_response(self) -> None:
        self.set_busy(True)
        self._current = self.add_message("assistant", "", now_stamp())
        self._current.set_streaming(True)

    def update_response(self, raw: str) -> None:
        status = detect_status(raw)
        if status is not None:
            self.set_status(status)
            return
        self.set_status(None)
        if self._current is None:
            self._current = self.add_message("assistant", "")
            self._current.set_streaming(True)
        self._pending = split_reply(raw)
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def end_response(self) -> None:
        self._flush()
        self._flush_timer.stop()
        if self._current is not None:
            self._current.set_streaming(False)
        self.set_busy(False)
        self._current = None
        self.set_status(None)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.send_btn.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)
        self.input.setReadOnly(busy)
        if not busy:
            self.input.setFocus()

    @property
    def busy(self) -> bool:
        return self._busy

    def set_status(self, text: Optional[str]) -> None:
        if text:
            self.status.setText(f"… {text}")
            self.status.setVisible(True)
        else:
            self.status.setVisible(False)
            self.status.setText("")

    def insert_text(self, text: str) -> None:
        self.input.insertPlainText(text)

    # ── 内部 ──

    def _flush(self) -> None:
        if self._pending is None or self._current is None:
            return
        stick = self._at_bottom()      # 同样先记录再改内容
        reasoning, body = self._pending
        self._current.set_content(reasoning, body)
        self._apply_widths()
        if stick:
            self._scroll_to_bottom(force=True)
        else:
            QTimer.singleShot(0, self._update_jump_button)

    def submit(self) -> None:
        """发送当前输入框内容（快捷键 / 按钮 / 外部调用统一入口）。"""
        if self._busy:
            return
        text = self.input.toPlainText().strip()
        if not text:
            return
        self.input.clear()
        self.send_requested.emit(text)

    _on_submit = submit

    def _on_stop(self) -> None:
        if self._busy:
            self.stop_requested.emit()

    def _pick_attachment(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择文件")
        if path:
            prefix = "" if self.input.toPlainText().endswith((" ", "\n")) else " "
            self.input.insertPlainText(
                f"{prefix}{path}" if self.input.toPlainText() else path)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._apply_widths()
        self._place_overlays()

    def _apply_widths(self) -> None:
        """消息流宽度：固定上限 + 居中，而不是视口百分比。

        上限 768px 参考 Claude.ai / ChatGPT 的正文列宽（每行 65-80 字符是可持续阅读
        的宽度）。用百分比的话，窗口拉宽到 1600px 时行宽会到 1250px，长文很难读。
        """
        viewport = self.scroll.viewport().width()
        column = min(max(240, viewport - 32), self.MAX_FLOW_WIDTH)
        side = max(0, (viewport - column) // 2)
        if (column, side) == self._last_flow:
            # 宽度没变就别再动每一条气泡的 maximumWidth / 边距：
            # 那些调用会让整条消息列重排，流式期间每 60ms 一次会造成滚动条抖动
            return
        self._last_flow = (column, side)
        for bubble in self._bubbles:
            bubble.set_max_width(column, side)

    def _scroll_to_bottom(self, force: bool = False) -> None:
        """自动跟随：只有本来就贴着底部（或显式 force）才滚。

        无条件滚到底会把正在往上回看的用户拽回最新内容 —— 这是 AI 聊天里最
        常见的打断式体验缺陷，所以这里加了守卫。真正的高度变化由
        _on_range_changed 兜住，这里只处理"立刻贴一下"。
        """
        if not (force or self._at_bottom()):
            self._update_jump_button()
            return
        self._stick_bottom = True
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())
