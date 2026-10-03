"""桌宠立绘下方的输入条：闲时半透明，聚焦/悬停变实，右侧按钮可折叠。

为什么不用独立对话框做主入口：对话框一隐藏就找不回来了（立绘是 WebEngine 渲染的
独立表面，右键菜单挂不上），而输入条常驻在立绘下方，不存在"叫不出来"的问题。
回复走头顶气泡，不需要额外的消息列表。
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QPushButton, QWidget


class PetInputBar(QWidget):
    """立绘底部的迷你输入条。"""

    send_requested = Signal(str)
    collapsed_changed = Signal(bool)

    HEIGHT = 34
    PLACEHOLDER = "和我说点什么…"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("petinput")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(self.HEIGHT)
        self.setProperty("tone", "idle")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 4, 4)
        layout.setSpacing(4)

        self.edit = QLineEdit(self)
        self.edit.setObjectName("petinput-edit")
        self.edit.setPlaceholderText(self.PLACEHOLDER)
        self.edit.setClearButtonEnabled(False)
        self.edit.returnPressed.connect(self.submit)
        # 焦点在 QLineEdit 上，本控件收不到它的 FocusIn/FocusOut ——
        # 必须装事件过滤器，否则"聚焦变实"永远不触发。
        self.edit.installEventFilter(self)
        layout.addWidget(self.edit, 1)

        self.toggle = QPushButton(">", self)
        self.toggle.setObjectName("petinput-toggle")
        self.toggle.setFixedWidth(24)
        self.toggle.setToolTip("折叠输入框")
        self.toggle.clicked.connect(lambda: self.set_collapsed(not self._collapsed))
        layout.addWidget(self.toggle)

        self._collapsed = False
        #: 生成中仍允许打字，只是发不出去（见 set_enabled_input）
        self._can_send = True
        self._flash_timer: QTimer | None = None

    # ── 折叠 ──

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def set_collapsed(self, collapsed: bool) -> None:
        collapsed = bool(collapsed)
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self.edit.setVisible(not collapsed)
        self.toggle.setText("<" if collapsed else ">")
        self.toggle.setToolTip("展开输入框" if collapsed else "折叠输入框")
        self.setFixedHeight(24 if collapsed else self.HEIGHT)
        if not collapsed:
            self.edit.setFocus()
        self.collapsed_changed.emit(collapsed)

    # ── 发送 ──

    def submit(self) -> None:
        text = self.edit.text().strip()
        if not text:
            return
        if not self._can_send:
            # 输入框在生成中仍可编辑，所以必须给反馈，不能静默吞掉
            self._flash_placeholder("正在生成中…")
            return
        self.edit.clear()
        self.send_requested.emit(text)

    def focus_input(self) -> None:
        if self._collapsed:
            self.set_collapsed(False)
        self.edit.setFocus()

    def set_enabled_input(self, enabled: bool) -> None:
        """生成中**仍可打字**，只是发不出去（与主窗口输入框一致的手感）。

        以前这里直接 `setEnabled(False)` 把整个输入框禁掉，长回复期间连改字
        都做不到 —— 和主窗口的 `setReadOnly(True)` 是同一个反体验。
        """
        self._can_send = bool(enabled)
        self.toggle.setEnabled(True)

    def _flash_placeholder(self, text: str) -> None:
        """临时换掉 placeholder 当反馈，1.5 秒后恢复。

        这条输入条没有状态行可用，placeholder 是唯一不占额外空间的提示位。
        """
        if self._flash_timer is None:
            self._flash_timer = QTimer(self)
            self._flash_timer.setSingleShot(True)
            self._flash_timer.timeout.connect(
                lambda: self.edit.setPlaceholderText(self.PLACEHOLDER))
        self.edit.setPlaceholderText(text)
        self._flash_timer.start(1500)

    # ── 闲时半透明 / 活跃变实 ──

    def _set_tone(self, tone: str) -> None:
        if self.property("tone") == tone:
            return
        self.setProperty("tone", tone)
        # 动态属性变了要重新 polish 才会生效
        style = self.style()
        style.unpolish(self)
        style.polish(self)
        self.update()

    def _sync_tone(self) -> None:
        """闲时半透明、聚焦或鼠标悬停时变实。"""
        active = self.edit.hasFocus() or self.underMouse()
        self._set_tone("active" if active else "idle")

    def eventFilter(self, obj, event):  # noqa: N802 (Qt 命名)
        if obj is self.edit and event.type() in (QEvent.Type.FocusIn,
                                                 QEvent.Type.FocusOut):
            self._sync_tone()
        return super().eventFilter(obj, event)

    def enterEvent(self, event):  # noqa: N802 (Qt 命名)
        self._sync_tone()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802 (Qt 命名)
        # 光标还在输入框里打字时不该变淡，否则看着像被禁用了
        self._sync_tone()
        super().leaveEvent(event)
