"""独立桌宠窗口：把 Live2D 立绘从主窗口里拿出来，放到桌面任意位置。

三条约束都是实测确认过的（见项目记忆）：

1. **透明背景可行** —— 无边框 + `WA_TranslucentBackground` + 页面 `setBackgroundColor(透明)`
   三层配合后，实测窗口角落像素是 `rgba(0,0,0,0)`，没有 Windows 上常见的黑底问题。
2. **鼠标事件落在 WebEngine 的内部子控件上**，不是 QWebEngineView 本身，
   所以拖拽用**应用级事件过滤器**（按 `widget.window() is self` 判断归属），
   在 view 上装过滤器抓不到。
3. **过滤器不消费事件**（返回 False）—— 页面还要用同一批事件做点击反应与视线跟随；
   拖拽与点击的区分交给页面自己按位移阈值判断，两边互不干扰。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QMenu, QVBoxLayout, QWidget

from .pet_input_bar import PetInputBar

# 位置持久化：桌面宠物的位置应该记住，不然每次重启都跳回默认点
STATE_FILE = "desktop_stand.json"


class Live2DWindow(QWidget):
    """无边框 + 置顶 + 透明背景的独立立绘窗口。

    - **拖动**：按住立绘任意位置拖（WebEngine 会吞掉鼠标事件，所以走应用级过滤器）
    - **缩放**：拖边缘/四角，用原生 `startSystemResize`（与主窗口一致，带系统吸附）
    """

    restore_requested = Signal()    # 右键「收回舞台」
    toggle_chat_requested = Signal()  # 右键「显示/隐藏对话框」
    moved = Signal()                # 位置变化（配套对话框要跟着走）
    send_requested = Signal(str)    # 底部输入条发出的消息

    # 边缘多少像素内算「缩放区」；同时也是窗口的最小尺寸
    EDGE = 8
    MIN_W = 160
    MIN_H = 200

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(None)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool              # Tool：不出现在任务栏与 Alt+Tab
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setWindowTitle("妹抖酱 · 桌面立绘")

        self._source = None
        self._drag_from: Optional[tuple] = None
        self._dragging = False
        self._chat_visible = False

        # 立绘占满上方，底部留 EDGE 像素给缩放边缘 —— 否则输入条会把下边缘盖住，
        # 底边和两个下角就没法拖拽缩放了。
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, self.EDGE)
        root.setSpacing(0)

        self._stage = QWidget(self)
        self._stage.setObjectName("petstage")
        self._stage.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        root.addWidget(self._stage, 1)

        self._input_bar = PetInputBar(self)
        self._input_bar.send_requested.connect(self.send_requested.emit)
        root.addWidget(self._input_bar)

        # 缩放走系统原生循环，release 不一定回到我们手里，
        # 所以尺寸变化用防抖定时器落盘。
        # 必须在 resize() 之前建好 —— resizeEvent 会用到它。
        self._save_timer: Optional[QTimer] = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._save_position)

        self.setMinimumSize(self.MIN_W, self.MIN_H)
        self.resize(340, 440)
        self.setMouseTracking(True)

        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    # ── 立绘接管 / 交还 ──

    def adopt(self, source) -> None:
        """把 Live2D 媒体源接过来（reparent，不重新加载模型）。"""
        self._source = source
        try:
            source.reparent(self._stage)
        except AttributeError:
            # 非 Live2D 的媒体源（例如静态图）没有 reparent，退回 attach
            source.attach(self._stage)
        self._resize_to_content()

    def release_source(self):
        """交还媒体源，由调用方决定接到哪里去。"""
        source, self._source = self._source, None
        return source

    @property
    def source(self):
        return self._source

    @property
    def input_bar(self) -> PetInputBar:
        return self._input_bar

    def focus_input(self) -> None:
        self._input_bar.focus_input()

    def set_input_enabled(self, enabled: bool) -> None:
        self._input_bar.set_enabled_input(enabled)

    def _resize_to_content(self) -> None:
        """立绘铺满舞台区（输入条占的那一条不算）。"""
        view = getattr(self._source, "_view", None)
        if view is None:
            return
        view.setGeometry(0, 0, self._stage.width(), self._stage.height())

    def resizeEvent(self, event):  # noqa: N802 (Qt 命名)
        super().resizeEvent(event)
        self._resize_to_content()
        if self._save_timer is not None:
            self._save_timer.start()

    # ── 边缘缩放（与主窗口同一套做法） ──

    def _edges_at(self, pos) -> Qt.Edge:
        # 输入条那一条不参与缩放，否则点输入框会被当成拖边缘
        if self._input_bar is not None and self._input_bar.geometry().contains(pos):
            return Qt.Edge(0)
        x, y = pos.x(), pos.y()
        w, h = self.width(), self.height()
        edges = Qt.Edge(0)
        if x <= self.EDGE:
            edges |= Qt.Edge.LeftEdge
        elif x >= w - self.EDGE:
            edges |= Qt.Edge.RightEdge
        if y <= self.EDGE:
            edges |= Qt.Edge.TopEdge
        elif y >= h - self.EDGE:
            edges |= Qt.Edge.BottomEdge
        return edges

    @staticmethod
    def _cursor_for(edges) -> Qt.CursorShape:
        if edges in (Qt.Edge.LeftEdge | Qt.Edge.TopEdge,
                     Qt.Edge.RightEdge | Qt.Edge.BottomEdge):
            return Qt.CursorShape.SizeFDiagCursor
        if edges in (Qt.Edge.RightEdge | Qt.Edge.TopEdge,
                     Qt.Edge.LeftEdge | Qt.Edge.BottomEdge):
            return Qt.CursorShape.SizeBDiagCursor
        if edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge):
            return Qt.CursorShape.SizeHorCursor
        if edges & (Qt.Edge.TopEdge | Qt.Edge.BottomEdge):
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.ArrowCursor

    def _update_cursor(self, global_pos) -> None:
        """鼠标在窗口内移动（未拖拽）时切换边缘光标。

        光标设在窗口上即可 —— WebEngine 的内部子控件没有自己的光标，会继承父级。
        """
        local = self.mapFromGlobal(global_pos)
        self.setCursor(self._cursor_for(self._edges_at(local)))

    # ── 拖动 / 缩放（应用级过滤器） ──

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt 命名)
        if not self.isVisible():
            return False
        etype = event.type()
        widget = obj if isinstance(obj, QWidget) else None
        if widget is None or widget.window() is not self:
            return False

        # 右键菜单必须在这里处理：真实右键落在 WebEngine 的内部子控件上，
        # 窗口自己的 contextMenuEvent 收不到 —— 之前就是这么变成"隐藏后叫不出来"的。
        if etype == QEvent.Type.ContextMenu:
            if self._input_bar is not None and (
                    widget is self._input_bar or self._input_bar.isAncestorOf(widget)):
                return False
            self._show_context_menu(event.globalPos())
            return True     # 吃掉，免得再弹出 WebEngine 自带的浏览器菜单

        if etype not in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseMove,
                         QEvent.Type.MouseButtonRelease):
            return False
        # 输入条区域交给输入条自己，不参与拖动/缩放
        if self._input_bar is not None and (
                widget is self._input_bar or self._input_bar.isAncestorOf(widget)):
            return False

        if etype == QEvent.Type.MouseButtonPress:
            if event.button() != Qt.MouseButton.LeftButton:
                return False
            global_pos = event.globalPosition().toPoint()
            edges = self._edges_at(self.mapFromGlobal(global_pos))
            if edges:
                # 边缘 → 交给系统原生缩放（带吸附，与主窗口一致）；
                # 返回 True 消费掉，免得页面同时把它当成一次点击
                handle = self.windowHandle()
                if handle is not None:
                    handle.startSystemResize(edges)
                    self._drag_from = None
                    return True
            self._drag_from = (global_pos, self.frameGeometry().topLeft())
            self._dragging = False

        elif etype == QEvent.Type.MouseMove:
            global_pos = event.globalPosition().toPoint()
            if self._drag_from is not None:
                origin_gp, origin_tl = self._drag_from
                if (global_pos - origin_gp).manhattanLength() > 3:
                    self._dragging = True
                self.move(origin_tl + global_pos - origin_gp)
            else:
                self._update_cursor(global_pos)

        elif etype == QEvent.Type.MouseButtonRelease:
            if self._drag_from is not None:
                self._drag_from = None
                self._save_position()

        # 关键：返回 False，页面还要用这批事件做点击反应和视线跟随
        return False

    # ── 右键菜单 ──

    def contextMenuEvent(self, event):  # noqa: N802 (Qt 命名)
        # 走底部留白那条时窗口自己能收到；立绘区域收到的是 eventFilter 那一路
        self._show_context_menu(event.globalPos())

    def _show_context_menu(self, global_pos) -> None:
        menu = QMenu(self)

        chat = QAction("隐藏对话框" if self._chat_visible else "显示对话框", self)
        chat.triggered.connect(self.toggle_chat_requested.emit)
        menu.addAction(chat)

        focus = QAction("聚焦输入框", self)
        focus.triggered.connect(self.focus_input)
        menu.addAction(focus)
        menu.addSeparator()

        restore = QAction("收回舞台", self)
        restore.triggered.connect(self.restore_requested.emit)
        menu.addAction(restore)

        pin = QAction("取消置顶" if self._is_pinned() else "保持置顶", self)
        pin.triggered.connect(self._toggle_pin)
        menu.addAction(pin)

        menu.addSeparator()
        hide_action = QAction("隐藏立绘", self)
        hide_action.triggered.connect(self.hide)
        menu.addAction(hide_action)
        menu.exec(global_pos)

    def set_chat_visible(self, visible: bool) -> None:
        """记住对话框的显隐，右键菜单文案才能对得上。"""
        self._chat_visible = bool(visible)

    def moveEvent(self, event):  # noqa: N802 (Qt 命名)
        super().moveEvent(event)
        self.moved.emit()

    def _is_pinned(self) -> bool:
        return bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)

    def _toggle_pin(self) -> None:
        flags = self.windowFlags()
        if self._is_pinned():
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        else:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()

    # ── 位置持久化 ──

    def _state_path(self) -> Optional[Path]:
        try:
            from core.paths import app_path
        except Exception:
            return None
        return app_path("data", STATE_FILE)

    def _save_position(self) -> None:
        path = self._state_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(
                {"x": self.x(), "y": self.y(),
                 "w": self.width(), "h": self.height()},
                ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            print(f" [live2d] 桌面立绘位置保存失败：{exc}")

    @staticmethod
    def _pick_area(areas, x, y):
        """从若干屏幕可用区里挑一块：优先「包含该点」的，否则取离它最近的。

        抽成纯函数是为了能测 —— 多屏环境不好造，用合成坐标就能覆盖。
        """
        if not areas:
            return None
        if isinstance(x, int) and isinstance(y, int):
            for area in areas:
                if area.contains(QPoint(x, y)):
                    return area
            best = None
            best_dist = None
            for area in areas:
                dx = max(area.left() - x, 0, x - area.right())
                dy = max(area.top() - y, 0, y - area.bottom())
                dist = dx * dx + dy * dy
                if best_dist is None or dist < best_dist:
                    best, best_dist = area, dist
            if best is not None:
                return best
        return areas[0]

    @classmethod
    def _screen_area_for(cls, x, y):
        """挑一块合适的屏幕可用区。

        不能直接用 primaryScreen() —— 副屏上的立绘每次启动都会被拽回主屏，
        而且夹取用的还是主屏边界，位置会被算歪。
        """
        app = QApplication.instance()
        if app is None:
            return None
        screens = app.screens()
        if not screens:
            return None
        primary = app.primaryScreen()
        areas = [s.availableGeometry() for s in screens]
        # 主屏排第一，作为「坐标缺失」时的兜底
        if primary is not None:
            pa = primary.availableGeometry()
            areas = [pa] + [a for a in areas if a != pa]
        return cls._pick_area(areas, x, y)

    def restore_position(self) -> None:
        """按上次的位置/尺寸摆放；没有记录就放在主屏右下角。"""
        path = self._state_path()
        data = {}
        if path is not None and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        if isinstance(data.get("w"), int) and isinstance(data.get("h"), int):
            self.resize(max(self.MIN_W, data["w"]), max(self.MIN_H, data["h"]))
        x = data.get("x")
        y = data.get("y")
        area = self._screen_area_for(x, y)
        if isinstance(x, int) and isinstance(y, int) and area is not None:
            # 夹回该屏幕内：换了分辨率后旧坐标可能落在屏幕外，窗口会“消失”
            x = max(area.left(), min(x, area.right() - self.width()))
            y = max(area.top(), min(y, area.bottom() - self.height()))
            self.move(x, y)
        elif area is not None:
            self.move(area.right() - self.width() - 40,
                      area.bottom() - self.height() - 60)

    # ── 清理 ──

    def closeEvent(self, event):  # noqa: N802 (Qt 命名)
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self._save_position()
        super().closeEvent(event)
