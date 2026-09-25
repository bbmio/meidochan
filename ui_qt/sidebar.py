"""导航侧栏（ARCHITECTURE_V3 §5.2 / §5.3）：工作空间管理 / 会话列表 / 角色 / 命令帮助。"""
from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

COMMAND_HELP = """
- `/model flash|pro` 切换模型
- `/think on|off` 思考模式
- `/effort high|medium|low` 推理强度
- `/memory` 概览卡（`/memory clear` 清空）
- `/remember <内容>` 写入长期记忆
- `/history` 会话概况 / `/history new` / `/history export`
- `/pin [规则]` 固定规则
- `/self_scan` `/self_status` 自我认知
- `/search` `/deep` `/crawl` 联网搜索
- `/kb_add` `/kb_search` `/kb_hybrid` 知识库
- `/ls` `/view` `/info` 文件浏览
- `/plugin_reload` 重载插件
"""


def _collapsible(title: str, expanded: bool = True):
    """返回 (容器, 头部按钮, 内容容器)。"""
    header = QToolButton()
    header.setCheckable(True)
    header.setChecked(expanded)
    header.setText(("▼ " if expanded else "▶ ") + title)
    header.setStyleSheet("QToolButton { border: none; background: transparent;"
                         " font-weight: 600; font-size: 12px; padding: 4px 0; }")

    content = QWidget()
    content.setVisible(expanded)
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    layout.addWidget(header)
    layout.addWidget(content)

    def _toggle(checked: bool) -> None:
        content.setVisible(checked)
        header.setText(("▼ " if checked else "▶ ") + title)

    header.toggled.connect(_toggle)
    return box, content


class _ElidedLabel(QLabel):
    """单行标签：宽度不足时显示省略号，且不参与父布局的最小宽度计算。

    历史会话的按钮被裁出视口的根因就在这类标签上：普通 QLabel 不换行也不压缩，
    minimumSizeHint 等于全文宽度（30 个中文字符实测 321px），会把 QScrollArea 的
    内容容器撑到视口（208px）之外，横向滚动条又被关掉，右侧按钮就彻底点不到了。
    这里固定用 Ignored 策略切断这条传导链，再按当前宽度做省略号显示。
    """

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self._shown = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 (Qt 命名)
        self._full_text = text or ""
        self._apply_elide()

    def fullText(self) -> str:  # noqa: N802 (Qt 命名)
        return self._full_text

    def resizeEvent(self, event):  # noqa: N802 (Qt 命名)
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self) -> None:
        width = self.width()
        if width <= 0:
            shown = self._full_text
        else:
            shown = QFontMetrics(self.font()).elidedText(
                self._full_text, Qt.TextElideMode.ElideRight, width)
        # 只有变化才回写，避免 resizeEvent ↔ setText 之间来回触发
        if shown != self._shown:
            self._shown = shown
            super().setText(shown)


class _SessionRow(QFrame):
    """单行历史会话：整行可点即打开，右侧「×」独立删除。

    删除按钮是子控件且自行接受鼠标事件，点它不会冒泡到整行；两个标签设了
    WA_TransparentForMouseEvents，点文字同样会落到整行上。
    """

    clicked = Signal(str)
    delete_requested = Signal(str, str)

    def __init__(self, file_name: str, title: str, summary: str,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._file_name = file_name
        self.setProperty("class", "session-row")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(title if not summary else f"{title}\n{summary}")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 4, 4)
        layout.setSpacing(4)

        text_box = QVBoxLayout()
        text_box.setSpacing(0)
        title_label = _ElidedLabel(title)
        title_label.setProperty("class", "session-title")
        title_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        text_box.addWidget(title_label)
        if summary:
            summary_label = _ElidedLabel(summary)
            summary_label.setProperty("class", "session-summary")
            summary_label.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            text_box.addWidget(summary_label)
        layout.addLayout(text_box, 1)

        del_btn = QPushButton("×")
        del_btn.setProperty("class", "tight")
        del_btn.setObjectName("danger")
        del_btn.setToolTip(f"删除会话「{title}」")
        del_btn.clicked.connect(
            lambda _=False: self.delete_requested.emit(self._file_name, title))
        layout.addWidget(del_btn)

    def mouseReleaseEvent(self, event):  # noqa: N802 (Qt 命名)
        # 用 release + 区域内判定，符合「按下再松开算一次点击」的常规语义
        if (event.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(event.position().toPoint())):
            self.clicked.emit(self._file_name)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class Sidebar(QWidget):
    workspace_switch = Signal(str)
    workspace_create = Signal(str)
    workspace_delete = Signal()
    session_open = Signal(str)
    session_delete = Signal(str)
    role_change = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._syncing = False

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(6)

        brand = QLabel("导航")
        brand.setProperty("class", "section")
        root.addWidget(brand)

        # ── 工作空间 ──
        self.ws_combo = QComboBox()
        self.ws_combo.setToolTip("切换工作空间（知识库 / 历史 / 记忆 / 人设互相隔离）")
        self.ws_combo.currentIndexChanged.connect(self._on_ws_changed)
        root.addWidget(self.ws_combo)

        ws_row = QHBoxLayout()
        ws_row.setSpacing(4)
        self.ws_name = QLineEdit()
        self.ws_name.setPlaceholderText("新工作空间名称")
        self.ws_name.returnPressed.connect(self._on_ws_create)
        ws_create = QPushButton("新建")
        ws_create.setProperty("class", "tight")
        ws_create.clicked.connect(self._on_ws_create)
        ws_row.addWidget(self.ws_name, 1)
        ws_row.addWidget(ws_create)
        root.addLayout(ws_row)

        self.ws_delete_btn = QPushButton("删除当前工作空间")
        self.ws_delete_btn.setObjectName("danger")
        self.ws_delete_btn.setProperty("class", "tight")
        self.ws_delete_btn.clicked.connect(self._on_ws_delete)
        root.addWidget(self.ws_delete_btn)

        self.status = QLabel("")
        self.status.setProperty("class", "status")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        # ── 历史会话（可折叠 + 内部滚动） ──
        sessions_box, sessions_content = _collapsible("历史会话", expanded=True)
        s_layout = QVBoxLayout(sessions_content)
        s_layout.setContentsMargins(0, 0, 0, 0)
        s_layout.setSpacing(2)
        self.session_note = QLabel("暂无历史会话，发消息后自动创建")
        self.session_note.setObjectName("session-note")
        self.session_note.setWordWrap(True)
        s_layout.addWidget(self.session_note)

        self.session_scroll = QScrollArea()
        self.session_scroll.setWidgetResizable(True)
        self.session_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.session_scroll.setMinimumHeight(140)
        holder = QWidget()
        self._session_layout = QVBoxLayout(holder)
        self._session_layout.setContentsMargins(0, 0, 0, 0)
        self._session_layout.setSpacing(3)
        self._session_layout.addStretch(1)
        self.session_scroll.setWidget(holder)
        s_layout.addWidget(self.session_scroll, 1)
        sessions_box.setMinimumHeight(180)
        root.addWidget(sessions_box, 3)

        # ── 角色 ──
        role_title = QLabel("角色")
        role_title.setProperty("class", "section")
        root.addWidget(role_title)
        self.role_combo = QComboBox()
        self.role_combo.setToolTip("切换角色指令（叠加在 system prompt 的 [角色指令] 段）")
        self.role_combo.currentIndexChanged.connect(self._on_role_changed)
        root.addWidget(self.role_combo)

        # ── 命令帮助（可折叠） ──
        help_box, help_content = _collapsible("命令帮助", expanded=False)
        h_layout = QVBoxLayout(help_content)
        h_layout.setContentsMargins(0, 0, 0, 0)
        help_label = QLabel(COMMAND_HELP.strip())
        help_label.setWordWrap(True)
        help_label.setTextFormat(Qt.TextFormat.MarkdownText)
        help_label.setProperty("class", "status")
        h_layout.addWidget(help_label)
        root.addWidget(help_box)

        root.addStretch(1)

    # ── 刷新 ──

    def set_workspaces(self, workspaces: List, current_id: str) -> None:
        self._syncing = True
        try:
            self.ws_combo.clear()
            for ws in workspaces:
                mark = "● " if ws.id == current_id else "○ "
                self.ws_combo.addItem(mark + ws.name, ws.id)
            index = self.ws_combo.findData(current_id)
            if index >= 0:
                self.ws_combo.setCurrentIndex(index)
        finally:
            self._syncing = False

    def set_sessions(self, sessions: List[dict], max_sessions: int) -> None:
        while self._session_layout.count() > 1:
            item = self._session_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # 先 hide 再解除父子关系：setParent(None) 会让控件瞬间变成顶层
                # 窗口，刷新列表时可能闪出一个孤立小窗
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()

        # 空会话（0 条消息）没有任何内容可以切回去，列出来只会误导 → 不进列表
        sessions = [s for s in sessions if int(s.get("count") or 0) > 0]

        if not sessions:
            self.session_note.setText("暂无历史会话，发消息后自动创建")
            return
        self.session_note.setText(f"{len(sessions)}/{max_sessions} 个会话，点 × 删除")

        for session in sessions:
            row = self._make_session_row(session)
            self._session_layout.insertWidget(self._session_layout.count() - 1, row)

    def _make_session_row(self, session: dict) -> QWidget:
        file_name = session.get("file", "")
        title = (session.get("title") or "无标题").strip() or "无标题"
        for ch in ("*", "_", "#", "`", "|", "[", "]", "~"):
            title = title.replace(ch, "")
        summary = (session.get("summary") or "").strip().replace("\n", " ")

        row = _SessionRow(file_name, title, summary)
        row.clicked.connect(self.session_open.emit)
        row.delete_requested.connect(self._confirm_delete)
        return row

    def set_roles(self, roles: List[dict], current_file: str = "") -> None:
        self._syncing = True
        try:
            self.role_combo.clear()
            self.role_combo.addItem("默认", "")
            for role in roles:
                self.role_combo.addItem(role.get("name", ""), role.get("file", ""))
            index = self.role_combo.findData(current_file)
            self.role_combo.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self._syncing = False

    def set_status(self, text: str) -> None:
        self.status.setText(text or "")

    # ── 交互 ──

    def _on_ws_changed(self, _index: int) -> None:
        if self._syncing:
            return
        ws_id = self.ws_combo.currentData()
        if ws_id:
            self.workspace_switch.emit(ws_id)

    def _on_ws_create(self) -> None:
        name = self.ws_name.text().strip()
        if not name:
            self.set_status("请输入名称")
            return
        self.ws_name.clear()
        self.workspace_create.emit(name)

    def _on_ws_delete(self) -> None:
        answer = QMessageBox.question(
            self, "删除工作空间",
            "确定删除当前工作空间吗？其历史、记忆、知识库将一并删除，且不可恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.workspace_delete.emit()

    def _on_role_changed(self, _index: int) -> None:
        if self._syncing:
            return
        self.role_change.emit(self.role_combo.currentData() or "")

    def _confirm_delete(self, file_name: str, title: str) -> None:
        answer = QMessageBox.question(
            self, "删除会话",
            f"确定删除会话「{title}」吗？删除后不可恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.session_delete.emit(file_name)
