"""无边框、贴合主题的对话框（替代裸用 `QMessageBox`）。

## 为什么不用 `QMessageBox` 的默认外观

`QMessageBox` 是**带系统标题栏**的顶层窗口。本项目界面全程无边框自绘
（`MainWindow` 自己画标题栏、自己画圆角），所以在深色主题下，弹窗顶上那条
系统标题栏仍是系统色（通常是白的），与界面割裂得很明显。

这里把边框去掉，改用主题令牌画背景 / 描边 / 圆角，并自己实现拖动 ——
外观与主窗口同一种设计语言。

## 用法

    from .themed_dialog import Choice, ask, confirm, info, warn

    if confirm(self, "删除会话", f"确定删除「{title}」吗？", danger=True):
        ...

    action = ask(self, "关闭妹抖酱", "要最小化到托盘，还是直接退出？",
                 [Choice("tray", "最小化到托盘", primary=True),
                  Choice("quit", "直接退出", danger=True),
                  Choice("cancel", "取消", role=QMessageBox.ButtonRole.RejectRole)],
                 informative="最小化后可从托盘图标恢复。")

    if action == "tray": ...

⚠️ 这些函数是**模态阻塞**的（内部 `exec()`）。测试里必须 monkeypatch 掉，
否则离屏环境下会永久阻塞（与直接调 `QMessageBox.warning` 是同一个坑）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QMessageBox, QWidget

from . import theme


@dataclass(frozen=True)
class Choice:
    """对话框上的一个按钮。

    - `primary`：主推操作，套 `#primary` 样式（描边强调）
    - `danger`：破坏性操作，套 `#danger` 样式（红色 hover）
    """

    key: str
    label: str
    role: int = QMessageBox.ButtonRole.AcceptRole
    primary: bool = False
    danger: bool = False


class ThemedMessageBox(QMessageBox):
    """去掉系统边框、改用主题令牌绘制的消息框。

    拖动是自己实现的：无边框窗口没有系统标题栏可拖，而弹窗一旦挡住内容
    又挪不动，体验很差。拖动只作用于对话框**空白处** —— 按钮是子控件，
    自己处理鼠标事件，不会受影响。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Dialog
                            | Qt.WindowType.FramelessWindowHint)
        # 透明底，圆角外才不会被系统填成方形
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setObjectName("themedbox")
        self._drag_offset = None

    # ── 背景自绘 ──

    def paintEvent(self, event):  # noqa: N802
        """自己画背景与描边。

        ⚠️ **不能**用 QSS 的 `background` 来画。本窗口设了
        `WA_TranslucentBackground`（它隐含 `WA_NoSystemBackground`），Qt 会因此
        **跳过 `paintBackground`** —— QSS 里写的背景色根本不会被绘制。
        实测：整个弹窗区域 alpha=0，完全透明，只有子控件（文字、按钮）看得见。

        主窗口是靠一个子控件 `QWidget#root` 绕开这个坑的（子控件不透明，
        背景正常绘制）；但 QMessageBox 的内部布局插不进额外子控件，
        所以直接自绘 —— 与 `speech_bubble` 是同一套做法。
        """
        super().paintEvent(event)
        tokens = theme.active_tokens()
        radius = float(theme.RADIUS["lg"])
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # 内缩 0.5px：1px 描边正好落在像素中心，否则会糊成 2px
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        painter.fillPath(path, QColor(tokens.get("bg", "#FFFFFF")))
        painter.setPen(QPen(QColor(tokens.get("border", "#D9D9E0")), 1.0))
        painter.drawPath(path)
        painter.end()

    # ── 拖动 ──

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = (event.globalPosition().toPoint()
                                 - self.frameGeometry().topLeft())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if (self._drag_offset is not None
                and event.buttons() & Qt.MouseButton.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        self._drag_offset = None
        super().mouseReleaseEvent(event)


def build_box(parent: Optional[QWidget], title: str, text: str,
              choices: Sequence[Choice], *, informative: str = "",
              icon: QMessageBox.Icon = QMessageBox.Icon.NoIcon,
              default: Optional[str] = None) -> tuple:
    """装配对话框但**不显示**，返回 `(box, {按钮: key})`。

    拆出来是为了能单独验证「按钮 → key 的映射」与样式套用，不必真的弹窗。
    """
    box = ThemedMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(icon)
    if informative:
        box.setInformativeText(informative)

    mapping = {}
    default_button = None
    for choice in choices:
        button = box.addButton(choice.label, choice.role)
        if choice.primary:
            button.setObjectName("primary")
        elif choice.danger:
            button.setObjectName("danger")
        mapping[button] = choice.key
        if default is not None and choice.key == default:
            default_button = button
    if default_button is not None:
        box.setDefaultButton(default_button)
    elif box.buttons():
        box.setDefaultButton(box.buttons()[0])
    return box, mapping


def ask(parent: Optional[QWidget], title: str, text: str,
        choices: Sequence[Choice], *, informative: str = "",
        icon: QMessageBox.Icon = QMessageBox.Icon.Question,
        default: Optional[str] = None) -> Optional[str]:
    """弹一个主题化对话框，返回被点按钮的 `key`。

    直接关闭窗口（或按 Esc 命中取消按钮）时返回 `None` —— 调用方必须把
    `None` 当作「用户什么都没选」，不要当成某个选项。
    """
    box, mapping = build_box(parent, title, text, choices,
                             informative=informative, icon=icon, default=default)
    box.exec()
    return mapping.get(box.clickedButton())


def info(parent: Optional[QWidget], title: str, text: str, *,
         informative: str = "") -> None:
    ask(parent, title, text, [Choice("ok", "知道了")],
        informative=informative, icon=QMessageBox.Icon.Information)


def warn(parent: Optional[QWidget], title: str, text: str, *,
         informative: str = "") -> None:
    ask(parent, title, text, [Choice("ok", "知道了")],
        informative=informative, icon=QMessageBox.Icon.Warning)


def confirm(parent: Optional[QWidget], title: str, text: str, *,
            informative: str = "", ok: str = "确定", cancel: str = "取消",
            danger: bool = False) -> bool:
    """是/否确认。破坏性操作默认落在「取消」上，避免一路回车就把东西删了。"""
    answer = ask(
        parent, title, text,
        [Choice("ok", ok, primary=not danger, danger=danger),
         Choice("cancel", cancel, role=QMessageBox.ButtonRole.RejectRole)],
        informative=informative,
        icon=QMessageBox.Icon.Warning if danger else QMessageBox.Icon.Question,
        default="cancel" if danger else "ok",
    )
    return answer == "ok"
