"""妹抖酱主窗口（ARCHITECTURE_V3 §5.2 / §5.3 / §5.4）。

无边框窗口 + 自绘标题栏；左侧导航侧栏、中间聊天区、右侧素材舞台；
引擎在后台线程流式输出，通过信号推进界面。
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QPoint, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from core.app_info import APP_DISPLAY, APP_VERSION, APP_NAME
from core.brain import request_stop
from core import live2d_control
from core.config.models import AppearanceConfig
from core.logging_utils import log, log_file
from core.paths import APP_DIR, app_path
from core.selfcheck import CheckResult, format_report, run_selfcheck

from . import theme
from .chat_view import ChatView, now_stamp
from .agent_state import AgentStateTracker
from .chat_view import detect_status, split_reply
from .desktop_chat import DesktopChatWindow
from .live2d_window import Live2DWindow
from .media import (AgentState, LayerStyle, create_default_registry,
                    image_spec, live2d_spec)
from .speech_bubble import SpeechBubble, bubble_style
from .settings_dialog import OnboardingDialog, SettingsDialog
from .sidebar import Sidebar
from .stage_view import StagePanel, StageView
from .theme import APPEARANCE_PRESETS, STAGE_WIDTH, TITLEBAR_HEIGHT, WINDOW_MARGIN


def _load_live2d_config(engine=None) -> dict:
    """读取 config/live2d.toml（缺失或解析失败都按空配置处理 → 回退静态立绘）。

    原先本函数直读文件；现统一走 ConfigLoader，让「设置 → Live2D」界面
    与运行时共用同一个配置入口和缓存。
    """
    from core.config.loader import ConfigLoader

    try:
        loader = engine.config if engine is not None else ConfigLoader()
        return loader.get_live2d_config()
    except Exception as exc:
        print(f" [live2d] config/live2d.toml 解析失败，回退静态立绘：{exc}")
        return {}


class _ModelProbe(QThread):
    """后台探测本地 Ollama 模型，避免阻塞界面（探测内部超时 2.5s）。"""

    probed = Signal(list)

    def __init__(self, engine, parent=None) -> None:
        super().__init__(parent)
        self._engine = engine

    def run(self) -> None:  # noqa: D401
        try:
            base_url = (self._engine.model_config.base_url
                        if self._engine.model_config.is_local()
                        else "http://localhost:11434/v1")
            self.probed.emit(self._engine.brain.list_local_models(base_url))
        except Exception:
            self.probed.emit([])


class _SelfCheckThread(QThread):
    """后台跑启动自检（含网络探测），避免阻塞界面。"""

    checked = Signal(list)

    def __init__(self, engine, parent=None) -> None:
        super().__init__(parent)
        self._engine = engine

    def run(self) -> None:  # noqa: D401
        try:
            self.checked.emit(run_selfcheck(self._engine))
        except Exception as exc:   # run_selfcheck 自身已兜底，这里只防御性处理
            self.checked.emit([CheckResult("启动自检", False, str(exc))])


class _TitleBar(QWidget):
    """自绘标题栏：可拖拽、双击最大化。"""

    def __init__(self, window: "MainWindow") -> None:
        super().__init__(window)
        self.setObjectName("titlebar")
        self.setFixedHeight(TITLEBAR_HEIGHT)
        self._window = window

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self._window.windowHandle()
            if handle is not None:
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        self._window.toggle_maximize()
        event.accept()


class MainWindow(QWidget):
    """主窗口：装配侧栏 / 聊天区 / 舞台，并接管全部事件。"""

    def __init__(self, engine, bridge, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self.bridge = bridge

        self.api_state: List[dict] = []
        self._local_models: List[str] = []
        self._syncing_model = False
        self._syncing_ws = False
        self._force_quit = False
        self._appearance = dict(APPEARANCE_PRESETS["标准"])
        self._effects_preset = "标准"
        self._theme_tokens = dict(theme.LIGHT_TOKENS)
        self._theme_preset = theme.DEFAULT_THEME_NAME
        # 特效滑杆是**连续**触发的（拖动中每一格都发信号），逐次写盘会把磁盘敲烂。
        # 统一走这个去抖计时器：停手 800ms 后才落盘。
        self._appearance_save_timer = QTimer(self)
        self._appearance_save_timer.setSingleShot(True)
        self._appearance_save_timer.setInterval(800)
        self._appearance_save_timer.timeout.connect(self._save_appearance)
        self._load_appearance()
        self._layer_visible: Dict[str, bool] = {
            "backdrop": True, "stand": True, "overlay": True}
        self._settings_dialog: Optional[SettingsDialog] = None
        # Agent 状态 → 立绘表情的追踪器（阶段由引擎上报，见 core/brain.py）
        self._agent_tracker = AgentStateTracker()
        self._live2d = None
        self._live2d_watch: Optional[QTimer] = None
        #: AI 显式设的表情的「保持期」——期间跳过状态驱动的表情切换，
        #: 否则工具一返回、阶段变成 writing，表情立刻被换成「画笔」，等于白设。
        self._live2d_hold = False
        self._live2d_hold_release = QTimer(self)
        self._live2d_hold_release.setSingleShot(True)
        self._live2d_hold_release.setInterval(1500)   # 回复很短时留一点时间让表情被看见
        self._live2d_hold_release.timeout.connect(self._release_live2d_hold)
        #: 把 AI 写的立绘请求从 core 的队列搬到 GUI 线程（工具线程不能碰 Qt 对象）
        self._live2d_pump = QTimer(self)
        self._live2d_pump.setInterval(150)
        self._live2d_pump.timeout.connect(self._drain_live2d_requests)
        self._live2d_pump.start()
        self._stand_window: Optional[Live2DWindow] = None
        self._desktop_chat: Optional[DesktopChatWindow] = None
        self._bubble: Optional[SpeechBubble] = None

        self.setWindowTitle(APP_DISPLAY)
        self.setWindowIcon(_app_icon())
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setMinimumSize(980, 640)
        self.resize(1280, 800)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(WINDOW_MARGIN, WINDOW_MARGIN, WINDOW_MARGIN, WINDOW_MARGIN)
        self.root = QWidget()
        self.root.setObjectName("root")
        self.root.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer.addWidget(self.root)

        inner = QVBoxLayout(self.root)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(0)
        inner.addWidget(self._build_titlebar())

        content = QHBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)
        self.sidebar = Sidebar()
        self.sidebar.setFixedWidth(232)
        self.chat = ChatView()
        self._registry = create_default_registry()
        self.stage = StageView(self._registry)
        self.stage_panel = StagePanel()
        self.stage_panel.setFixedWidth(STAGE_WIDTH)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right_layout.addWidget(self.stage, 3)
        right_layout.addWidget(self.stage_panel, 2)

        content.addWidget(self.sidebar)
        content.addWidget(self.chat, 1)
        content.addWidget(right)
        inner.addLayout(content, 1)

        self.status_bar = QStatusBar()
        self.status_model = QLabel("")
        self.status_conn = QLabel("")
        self.status_version = QLabel(f"{APP_NAME} {APP_VERSION}")
        self.status_bar.addWidget(self.status_model, 1)
        self.status_bar.addPermanentWidget(self.status_conn)
        self.status_bar.addPermanentWidget(self.status_version)
        inner.addWidget(self.status_bar)

        self._wire()
        self._setup_tray()
        self._apply_appearance(self._appearance)

    # ── 构建 ──

    def _build_titlebar(self) -> QWidget:
        bar = _TitleBar(self)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 0, 6, 0)
        layout.setSpacing(8)

        brand = QLabel(APP_DISPLAY)
        brand.setObjectName("brand")
        layout.addWidget(brand)

        self.ws_combo = QComboBox()
        self.ws_combo.setMinimumWidth(150)
        self.ws_combo.setToolTip("切换工作空间")
        layout.addWidget(self.ws_combo)
        layout.addStretch(1)

        self.model_combo = QComboBox()
        self.model_combo.setMinimumWidth(220)
        self.model_combo.setToolTip("模型：云端 / 本地（本地列表定期探测）")
        layout.addWidget(self.model_combo)
        layout.addStretch(1)

        self.preset_combo = QComboBox()
        for name in APPEARANCE_PRESETS:
            self.preset_combo.addItem(name, name)
        self.preset_combo.setCurrentIndex(1)
        self.preset_combo.setToolTip("外观预设（虚化 / 透明）")
        layout.addWidget(self.preset_combo)

        settings_btn = QPushButton("设置")
        settings_btn.setObjectName("winbtn")
        settings_btn.setToolTip("模型 / 外观 / 日志（Ctrl+,）")
        layout.addWidget(settings_btn)

        self.pin_btn = QPushButton("置顶")
        # objectName 必须是 pinbtn —— QSS 里的选中态是 QPushButton#pinbtn:checked，
        # 之前写成 winbtn 选择器命不中，按钮点了没有任何视觉反馈，
        # 看起来就像"置顶没生效"（实际 WS_EX_TOPMOST 是设上了的）。
        self.pin_btn.setObjectName("pinbtn")
        self.pin_btn.setCheckable(True)
        self.pin_btn.setToolTip("让窗口始终浮在最前")
        self.pin_btn.toggled.connect(self._on_pin_toggled)
        layout.addWidget(self.pin_btn)

        for text, slot, name in (
            ("─", self.showMinimized, "winbtn"),
            ("▢", self.toggle_maximize, "winbtn"),
            ("✕", self.close, "winbtn-close"),
        ):
            btn = QPushButton(text)
            btn.setObjectName(name)
            btn.setFixedWidth(38)
            btn.clicked.connect(lambda _=False, s=slot: s())
            layout.addWidget(btn)

        self.settings_btn = settings_btn
        return bar

    def _wire(self) -> None:
        self.sidebar.workspace_switch.connect(self._on_workspace_switch)
        self.sidebar.workspace_create.connect(self._on_workspace_create)
        self.sidebar.workspace_delete.connect(self._on_workspace_delete)
        self.sidebar.session_open.connect(self._on_session_open)
        self.sidebar.session_delete.connect(self._on_session_delete)
        self.sidebar.role_change.connect(self._on_role_change)

        self.ws_combo.currentIndexChanged.connect(self._on_topbar_ws_changed)
        self.model_combo.currentIndexChanged.connect(self._on_model_selected)
        self.preset_combo.currentIndexChanged.connect(
            lambda _i: self._on_preset_selected(self.preset_combo.currentData()))
        self.settings_btn.clicked.connect(self.open_settings)

        self.chat.send_requested.connect(self._on_send)
        self.chat.stop_requested.connect(self._on_stop)
        self.chat.regenerate_requested.connect(self._on_regenerate)
        self.chat.history_load_requested.connect(self._on_history_load)
        self.chat.new_session_requested.connect(self._on_new_session)
        self.chat.export_requested.connect(self._on_export)

        self.stage_panel.blur_changed.connect(self._on_blur_changed)
        self.stage_panel.window_opacity_changed.connect(self._on_window_opacity)
        self.stage_panel.visible_changed.connect(self._on_layer_visible)
        self.stage_panel.stand_window_toggled.connect(self._on_stand_window_toggled)

        self.bridge.partial.connect(self._on_partial)
        self.bridge.phase_changed.connect(self._on_phase)
        self.bridge.finished.connect(self._on_response_finished)
        self.bridge.failed.connect(self._on_response_failed)

        # Ctrl+Enter 由聊天输入框自身处理（避免与全局快捷键重复触发一次发送）
        QShortcut(QKeySequence("Ctrl+,"), self, self.open_settings)

    def _setup_tray(self) -> None:
        self.tray: Optional[QSystemTrayIcon] = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(_app_icon(), self)
        self.tray.setToolTip(APP_DISPLAY)
        menu = QMenu()
        show_action = QAction("显示主窗口", self)
        show_action.triggered.connect(self._restore_from_tray)
        log_action = QAction("查看日志", self)
        log_action.triggered.connect(self.open_log)
        data_action = QAction("打开数据目录", self)
        data_action.triggered.connect(lambda: self.open_path(app_path("data")))
        settings_action = QAction("设置", self)
        settings_action.triggered.connect(self.open_settings)
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self._quit)
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(settings_action)
        menu.addAction(log_action)
        menu.addAction(data_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self._restore_from_tray()
            if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

    def _load_assets(self) -> None:
        """装载立绘与可选背景（assets/default/backdrop.*）。

        立绘优先用 Live2D（config/live2d.toml 配的模型），加载不出来或没配
        则回退到插件提供的静态图。
        """
        if not self._load_live2d_stand():
            stand_path = self.engine.plugin_manager.get_stand_image()
            if stand_path:
                spec = image_spec("stand", stand_path, LayerStyle(
                    anchor="bottom-center", z=10, scale=1.0,
                    blur_radius=self._appearance["stand_depth"]))
                self.stage.set_layer("stand", self._registry.create(spec))
            else:
                self.sidebar.set_status("立绘未加载（plugins/static_stand/stand.png 缺失）")

        backdrop = next(
            (p for p in (
                app_path("assets", "default", "backdrop.png"),
                app_path("assets", "default", "backdrop.jpg"),
                app_path("assets", "default", "backdrop.jpeg"),
                app_path("assets", "default", "backdrop.webp"),
            ) if p.exists()), None)
        if backdrop is not None:
            spec = image_spec("backdrop", backdrop, LayerStyle(
                anchor="fill", z=0, blur_radius=self._appearance["backdrop_blur"]))
            self.stage.set_layer("backdrop", self._registry.create(spec))
        self.stage_panel.set_backdrop_available(backdrop is not None)

    def _load_live2d_stand(self) -> bool:
        """按 config/live2d.toml 装配 Live2D 立绘；成功返回 True。"""
        cfg = _load_live2d_config(self.engine)
        model_cfg = cfg.get("model") or {}
        viewer = app_path(*str(model_cfg.get("viewer") or "").replace("\\", "/").split("/"))
        model = app_path(*str(model_cfg.get("dir") or "").replace("\\", "/").split("/"),
                         str(model_cfg.get("definition") or ""))
        if not (viewer.exists() and model.exists()):
            print(f" [live2d] 模型或播放器缺失，回退静态立绘：{model}")
            return False
        try:
            spec = live2d_spec(
                "stand", viewer, model,
                LayerStyle(anchor="fill", z=10, scale=1.0,
                           blur_radius=self._appearance["stand_depth"]),
                state_map=(cfg.get("state_map") or {}),
                interaction=(cfg.get("interaction") or {}),
            )
            source = self._registry.create(spec)
            self.stage.set_layer("stand", source)
            self._live2d = source
            self.sidebar.set_status("Live2D 立绘加载中…")
            self._live2d_watch = QTimer(self)
            self._live2d_watch.setInterval(500)
            self._live2d_watch.timeout.connect(self._check_live2d_ready)
            self._live2d_watch.start()
            return True
        except Exception as exc:
            print(f" [live2d] 装配失败，回退静态立绘：{type(exc).__name__}: {exc}")
            return False

    # ── Agent 状态 → 立绘表情 ──

    def _on_partial(self, raw: str) -> None:
        """引擎的每段展示文本：刷新聊天区、桌面对话框与头顶气泡。

        注意这里**不再推断状态** —— 状态改由 _on_phase 直接读引擎上报的阶段。
        """
        self.chat.update_response(raw)
        if self._desktop_chat is not None and self._desktop_chat.isVisible():
            self._desktop_chat.update_response(raw)
        self._update_bubble(raw)

    def _on_phase(self, phase: str) -> None:
        """引擎上报的对话阶段 → 立绘状态（不再从展示文本反推）。

        不做「最短停留」：状态到了就切。动作本身是一次性的会自己播完，
        状态则会一直保持到下一个阶段，所以长回复的「输出中」自然持续到结束。

        AI 显式设的表情处于保持期时**只播动作、不动表情**（见 `_live2d_hold`）。
        """
        state = self._agent_tracker.feed_phase(phase)
        if state is not None:
            self._sync_agent_state(state, skip_expression=self._live2d_hold)

    def _sync_agent_state(self, state, skip_expression: bool = False) -> None:
        if state is None:
            return
        # 立绘独立到桌面后就不在舞台的层登记表里了，StageView.on_agent_state()
        # 够不着它 —— 这里直接补一路给它，否则表情不再随对话状态变化。
        window = self._stand_window
        if window is not None and window.isVisible() and window.source is not None:
            window.source.set_state(state, skip_expression=skip_expression)
        self.stage.on_agent_state(state, skip_expression=skip_expression)

    # ── AI 主动控制立绘 ──

    def _drain_live2d_requests(self) -> None:
        """把 AI 写的立绘请求从 core 的队列搬到 GUI 线程执行。

        工具跑在 worker 线程，直接碰 Qt 对象会崩 —— 所以插件只往
        `core/live2d_control` 的队列里塞一条请求，由这个定时器搬运。
        150ms 的延迟对表情切换无感。
        """
        items = live2d_control.take_pending()
        if not items:
            return
        for kind, value in items:
            self._apply_live2d_command(kind, value)
            if kind in (live2d_control.KIND_EXPRESSION, live2d_control.KIND_CLEAR):
                # 进入保持期：本轮剩余阶段不再用 state_map 覆盖表情
                self._live2d_hold = True
                self._live2d_hold_release.stop()

    def _apply_live2d_command(self, kind: str, value: str) -> None:
        for source in self._live2d_targets():
            try:
                if kind == live2d_control.KIND_EXPRESSION:
                    source.set_expression(value)
                elif kind == live2d_control.KIND_MOTION:
                    source.play_motion(value)
                elif kind == live2d_control.KIND_CLEAR:
                    source.clear_expression()
            except Exception as exc:
                print(f"[UI] 执行立绘指令失败（{kind}={value}）：{exc}")

    def _live2d_targets(self) -> list:
        """当前在显示的立绘源（独立窗口优先，否则舞台上的那个）。"""
        targets = []
        window = self._stand_window
        if window is not None and window.isVisible() and window.source is not None:
            targets.append(window.source)
        if self._live2d is not None and self._live2d not in targets:
            targets.append(self._live2d)
        return targets

    def _release_live2d_hold(self) -> None:
        """保持期结束：回到由对话状态驱动的表情。

        ⚠️ 这里**必须**用 `self._agent_tracker.reset()` 而不是直接
        `_sync_agent_state(AgentState.IDLE)` —— 见 `_on_response_finished`
        里为什么不能在回合结束时提前 reset。
        """
        self._live2d_hold = False
        self._sync_agent_state(self._agent_tracker.reset())

    def _check_live2d_ready(self) -> None:
        """轮询 Live2D 就绪状态，把加载中/失败如实反馈到侧栏。"""
        source = self._live2d
        if source is None or self._live2d_watch is None:
            return
        if source.ready:
            self._live2d_watch.stop()
            self.sidebar.set_status("Live2D 立绘已就绪")
            # 补一次当前状态：加载期间发出的指令被 source 排队了，这里再对齐一下
            self._sync_agent_state(self._agent_tracker.state)
        elif source.error:
            self._live2d_watch.stop()
            self.sidebar.set_status(f"Live2D 加载失败：{source.error}")

    def _on_stand_window_toggled(self, detached: bool) -> None:
        """立绘在「右侧舞台」与「独立桌面窗口」之间切换。

        切换走 Live2DSource.reparent()，只换父级、不重建控件 ——
        否则每切一次都要重跑 WebEngine 启动 + 模型解析（1~3 秒）。
        """
        if detached:
            source = self.stage.take_layer("stand")
            if source is None:
                self.sidebar.set_status("没有可独立的立绘")
                self.stage_panel.set_stand_detached(False)
                return
            if self._stand_window is None:
                self._stand_window = Live2DWindow()
                self._stand_window.restore_requested.connect(
                    lambda: self._on_stand_window_toggled(False))
                self._stand_window.moved.connect(self._position_desktop_overlays)
                self._stand_window.send_requested.connect(self._on_send)
                self._stand_window.toggle_chat_requested.connect(
                    lambda: self._set_desktop_chat_visible(
                        not (self._desktop_chat is not None
                             and self._desktop_chat.isVisible())))
            self._stand_window.adopt(source)
            self._stand_window.restore_position()
            self._stand_window.show()
            self._stand_window.raise_()
            self._stand_window.activateWindow()
            self.stage_panel.set_stand_detached(True)
            # 输入条已经在立绘下方了，回复走头顶气泡；
            # 完整对话记录改成「需要时才开」（右键菜单 → 显示对话框）。
            self._ensure_bubble()
            self.sidebar.set_status("立绘已独立到桌面：按住拖动、下方可直接输入")
            QTimer.singleShot(120, lambda: self._stand_window
                              and self._stand_window.focus_input())
        else:
            window = self._stand_window
            if window is None:
                self.stage_panel.set_stand_detached(False)
                return
            self._set_desktop_chat_visible(False)
            if self._bubble is not None:
                self._bubble.clear()
            source = window.release_source()
            window.hide()
            if source is not None:
                self.stage.set_layer("stand", source)
            self.stage_panel.set_stand_detached(False)
            self.sidebar.set_status("立绘已收回舞台")

    # ── 桌宠配套的迷你对话框 ──

    def _ensure_desktop_chat(self) -> DesktopChatWindow:
        if self._desktop_chat is None:
            chat = DesktopChatWindow()
            chat.send_requested.connect(self._on_send)
            chat.stop_requested.connect(self._on_stop)
            chat.hide_requested.connect(lambda: self._set_desktop_chat_visible(False))
            # 用会话内容铺一遍，独立出来就能看到之前聊了什么。
            # 不能只看 self.api_state —— 刚启动时它还是空的（要等第一轮回复结束才填），
            # 那样独立出来会是一片空白；回退到会话文件才是完整上下文。
            history = self.api_state or self.engine.history.load_api_state() or []
            seed = []
            for msg in history[-20:]:
                role = msg.get("role")
                content = msg.get("content")
                if role in ("user", "assistant") and isinstance(content, str):
                    _reasoning, body = split_reply(content)
                    if body.strip():
                        seed.append((role, body))
            chat.load_messages(seed)
            self._desktop_chat = chat
        return self._desktop_chat

    def _set_desktop_chat_visible(self, visible: bool) -> None:
        chat = self._ensure_desktop_chat() if visible else self._desktop_chat
        if chat is None:
            return
        if visible:
            chat.show()
            chat.raise_()
            self._position_desktop_chat()
            chat.focus_input()
        else:
            chat.hide()
        if self._stand_window is not None:
            self._stand_window.set_chat_visible(visible)

    def _position_desktop_chat(self) -> None:
        """把对话框摆在立绘左边；左边放不下就换右边。"""
        chat = self._desktop_chat
        window = self._stand_window
        if chat is None or window is None or not chat.isVisible():
            return
        screen = window.screen() or QApplication.primaryScreen()
        area = screen.availableGeometry() if screen is not None else None
        x = window.x() - chat.width() - 12
        y = window.y() + max(0, (window.height() - chat.height()) // 2)
        if area is not None:
            if x < area.left():
                x = window.x() + window.width() + 12
            x = max(area.left(), min(x, area.right() - chat.width()))
            y = max(area.top(), min(y, area.bottom() - chat.height()))
        chat.move(x, y)

    # ── 头顶气泡 ──

    def _ensure_bubble(self) -> SpeechBubble:
        if self._bubble is None:
            bubble = SpeechBubble()
            bubble.setStyleSheet(bubble_style())
            self._bubble = bubble
        return self._bubble

    def _position_desktop_overlays(self) -> None:
        """立绘移动时，对话框和气泡都跟着走。"""
        self._position_desktop_chat()
        bubble = self._bubble
        window = self._stand_window
        if bubble is not None and bubble.isVisible() and window is not None:
            bubble.place_above(window)

    def _update_bubble(self, raw: str) -> None:
        """把当前这段输出同步到头顶气泡（自适应大小）。"""
        bubble = self._bubble
        window = self._stand_window
        if bubble is None or window is None or not window.isVisible():
            return
        status = detect_status(raw)
        if status:
            bubble.show_text(status)
            bubble.place_above(window)
            return
        reasoning, body = split_reply(raw)
        if body.strip():
            bubble.show_text(body)
        elif reasoning.strip():
            bubble.show_text("思考中…")
        else:
            return
        bubble.place_above(window)

    # ── 引擎就绪 ──

    def on_engine_ready(self) -> None:
        """引擎启动完成后再装配素材与列表（插件此时才加载完毕）。"""
        self._load_assets()
        self._apply_appearance(self._appearance)
        self._prune_empty_sessions()
        self.refresh_workspaces()
        self.refresh_roles()
        self.refresh_sessions()
        self._refresh_model_combo()
        self._probe_local_models()
        self.status_model.setText(f"模型：{self.engine.brain.current_model}")
        self.sidebar.set_status("")
        self.run_startup_selfcheck()
        self.chat.input.setFocus()

    def maybe_onboarding(self) -> None:
        """首次启动（无 workspaces/.initialized）时弹出引导，保留原 Gradio 版功能。"""
        marker = app_path("workspaces", ".initialized")
        if marker.exists():
            return
        dialog = OnboardingDialog(self)
        if dialog.exec() != OnboardingDialog.DialogCode.Accepted:
            return
        name, persona = dialog.values()
        if not name:
            name = "鲸鱼娘"
        if not persona:
            persona = "乐于帮助用户解决问题。"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(datetime.now().isoformat(), encoding="utf-8")
        app_path("workspaces", "boot_config.json").write_text(
            json.dumps({"name": name, "persona": persona,
                        "created_at": datetime.now().isoformat(), "version": APP_VERSION},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            from .settings_dialog import seed_boot_memory
            seed_boot_memory(name, persona)
        except Exception as exc:
            print(f"[WARN] Boot seed: {exc}")
        prompt = f"你是名叫{name}的虚拟助手。{persona}"
        self.engine.context_engine.set_workspace_persona(prompt)
        current = self.engine.workspace_mgr.current
        if current is not None:
            current.persona_prompt = prompt
            from workspace.storage import WorkspaceStorage
            WorkspaceStorage(str(app_path("workspaces"))).save(current)
        self.refresh_workspaces()
        self.refresh_roles()
        self.sidebar.set_status("引导完成，可以开始对话了")

    # ── 刷新 ──

    def refresh_workspaces(self) -> None:
        workspaces = self.engine.workspace_mgr.list_all()
        current = self.engine.workspace_mgr.current
        current_id = current.id if current else ""
        self.sidebar.set_workspaces(workspaces, current_id)

        self._syncing_ws = True
        try:
            self.ws_combo.clear()
            for ws in workspaces:
                mark = "● " if ws.id == current_id else "○ "
                self.ws_combo.addItem(mark + ws.name, ws.id)
            index = self.ws_combo.findData(current_id)
            if index >= 0:
                self.ws_combo.setCurrentIndex(index)
        finally:
            self._syncing_ws = False

    def refresh_sessions(self) -> None:
        self.sidebar.set_sessions(self.engine.history.list_sessions(),
                                  self.engine.history.max_sessions)

    def refresh_roles(self, current_file: str = "") -> None:
        self.sidebar.set_roles(self.engine.context_engine.list_roles(), current_file)

    # ── 启动自检 / 日志 / 数据目录 ──

    def run_startup_selfcheck(self) -> None:
        """后台跑自检；有问题则弹人话指引（不抛栈、不阻塞启动）。"""
        self._selfcheck = _SelfCheckThread(self.engine, self)
        self._selfcheck.checked.connect(self._on_selfcheck_done)
        self._selfcheck.start()

    def _on_selfcheck_done(self, results: list) -> None:
        report = format_report(results)
        problems = [r for r in results if not getattr(r, "ok", True)]
        if not problems:
            log.info("[自检] %s", report)
            return
        log.warning("[自检] 发现问题：\n%s", report)
        self.sidebar.set_status("启动自检发现 %d 个问题，详见弹出提示或托盘「查看日志」"
                                % len(problems))
        # 非模态提示：不阻塞启动（用户可以直接去改配置，改完重启即可）
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("启动自检")
        box.setText(report)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.setModal(False)
        box.show()
        self._selfcheck_box = box

    def open_log(self) -> None:
        """用系统默认程序打开日志文件（不存在时给出可读提示）。"""
        path = log_file()
        if not path.exists():
            QMessageBox.information(
                self, "日志",
                f"日志文件尚未生成：\n{path}\n\n"
                "程序运行过程中的信息会持续写入该文件（data/logs/）。")
            return
        self.open_path(path)

    @staticmethod
    def open_path(path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _probe_local_models(self) -> None:
        probe = getattr(self, "_probe", None)
        if probe is not None and probe.isRunning():
            return
        self._probe = _ModelProbe(self.engine, self)
        self._probe.probed.connect(self._on_models_probed)
        self._probe.start()

    def _on_models_probed(self, models: List[str]) -> None:
        self._local_models = models or []
        self._refresh_model_combo()
        self.status_conn.setText(
            f"Ollama：{'已连接' if self._local_models else '未连接'} · MCP：未接入")

    def _refresh_model_combo(self) -> None:
        choices = self._model_choices()
        current = self.engine.brain.current_model
        prefix = "local" if self.engine.model_config.is_local() else "cloud"
        value = f"{prefix}:{current}"
        self._syncing_model = True
        try:
            self.model_combo.clear()
            for label, key in choices:
                self.model_combo.addItem(label, key)
            index = self.model_combo.findData(value)
            if index < 0:
                index = 0
            self.model_combo.setCurrentIndex(index)
        finally:
            self._syncing_model = False

    def _model_choices(self) -> List[tuple]:
        choices = [
            ("DeepSeek Flash（云端）", "cloud:deepseek-v4-flash"),
            ("DeepSeek Pro（云端）", "cloud:deepseek-v4-pro"),
        ]
        for name in self._local_models:
            choices.append((f"本地 · {name}", f"local:{name}"))
        current = self.engine.brain.current_model
        if current:
            prefix = "local" if self.engine.model_config.is_local() else "cloud"
            key = f"{prefix}:{current}"
            if key not in [c[1] for c in choices]:
                if prefix == "local":
                    # 本地模型已不在 Ollama 列表里 → 确实失效，给一个移除入口
                    choices.insert(0, (f"❌ 移除无效模型：{current}（选中即可移除）",
                                       f"del:{key}"))
                else:
                    # 云端模型不在预置列表里只是列表不全，模型本身有效（例如在
                    # config/model.toml 里自定义的）。正常列出来并置顶即可 ——
                    # 否则下拉框会把「移除无效模型」当成当前项显示，看着像一条报错。
                    choices.insert(0, (f"当前 · {current}", key))
        return choices

    # ── 工作空间 ──

    def _on_topbar_ws_changed(self, _index: int) -> None:
        if self._syncing_ws:
            return
        ws_id = self.ws_combo.currentData()
        if ws_id:
            self._switch_workspace(ws_id)

    def _on_workspace_switch(self, ws_id: str) -> None:
        self._switch_workspace(ws_id)

    def _switch_workspace(self, ws_id: str) -> None:
        result = self.engine.switch_workspace(ws_id)
        self.api_state = []
        self.chat.clear()
        self.refresh_workspaces()
        self.refresh_sessions()
        self.sidebar.set_status(result)
        self.status_model.setText(f"模型：{self.engine.brain.current_model}")

    def _on_workspace_create(self, name: str) -> None:
        message = self.engine.create_workspace(name)
        current = self.engine.workspace_mgr.current
        if current is not None:
            self.engine.switch_workspace(current.id)
        self.api_state = []
        self.chat.clear()
        self.refresh_workspaces()
        self.refresh_sessions()
        self.sidebar.set_status(message)

    def _on_workspace_delete(self) -> None:
        current = self.engine.workspace_mgr.current
        if current is None:
            self.sidebar.set_status("无工作空间")
            return
        others = [w for w in self.engine.workspace_mgr.list_all() if w.id != current.id]
        if not others:
            self.sidebar.set_status("至少保留一个工作空间")
            return
        name = current.name
        # 当前空间不能直接删：先切到另一个（优先 default）
        target = next((w for w in others if w.id == "default"), others[0])
        self.engine.switch_workspace(target.id)
        self.engine.delete_workspace(current.id)
        self.api_state = []
        self.chat.clear()
        self.refresh_workspaces()
        self.refresh_sessions()
        self.sidebar.set_status(f"已删除工作空间「{name}」，已切换到「{target.name}」")

    # ── 会话 ──

    def _on_session_open(self, file_name: str) -> None:
        if not file_name:
            return
        self.engine.history.switch_to_session(file_name)
        api_state = self.engine.history.load_api_state() or []
        self.api_state = list(api_state)
        messages = self.engine.history.get_messages() or []
        self.chat.load_messages(messages)
        self.sidebar.set_status(f"已切换到会话 {file_name}")

    def _on_session_delete(self, file_name: str) -> None:
        result = self.engine.delete_session(file_name)
        api_state = self.engine.history.load_api_state() or []
        self.api_state = list(api_state)
        messages = self.engine.history.get_messages() or []
        self.chat.load_messages(messages)
        self.refresh_sessions()
        self.sidebar.set_status(result)

    def _prune_empty_sessions(self) -> None:
        """清掉 0 条消息的历史会话（不含当前会话）。

        它们会被按时间排在最前面，把真正有内容的对话挤下去，
        而点开又什么都没有 —— 看起来就像"切不回历史对话"。
        """
        try:
            current = self.engine.history.current_file
            for session in self.engine.history.list_sessions():
                if int(session.get("count") or 0) > 0:
                    continue
                name = session.get("file", "")
                if not name or name == current.name:
                    continue
                self.engine.delete_session(name)
        except Exception as exc:
            log.debug("清理空会话失败：%s", exc)

    def _on_new_session(self) -> None:
        # 当前会话还是空的就先删掉，避免连点「新会话」堆出一串空会话
        try:
            if self.engine.history.count_messages() == 0:
                self.engine.delete_session(self.engine.history.current_file.name)
        except Exception as exc:
            log.debug("清理空的新会话失败：%s", exc)

        result = self.engine.new_session()
        if "上限" in result or "无法开启" in result:
            self.sidebar.set_status(result)
            return
        self.api_state = []
        self.chat.clear()
        self.refresh_sessions()
        self.sidebar.set_status(result)

    def _on_history_load(self, turns: int) -> None:
        messages = self.engine.history.load_api_state() or []
        if not messages:
            self.sidebar.set_status("暂无记录")
            return
        # start 必须默认 0，不能用 len(messages)：会话不足 turns 轮时，
        # 下面的循环会一直走到头也不改 start，用 len 就会切出空列表 ——
        # 表现为「点了加载历史但一条消息都没出来」（会话越短越容易踩到）。
        start = 0
        count = 0
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                count += 1
            if count >= int(turns):
                start = i
                break
        recent = messages[start:]
        self.api_state = list(recent)
        self.chat.load_messages(recent)
        self.sidebar.set_status(f"已加载 {count} 轮对话")

    def _on_export(self, fmt: str) -> None:
        self.sidebar.set_status(self.engine.history.export(fmt or "markdown"))

    def _on_role_change(self, role_file: str) -> None:
        if not role_file:
            self.engine.context_engine.set_active_role("")
            self.sidebar.set_status("默认模式")
            return
        data = self.engine.context_engine.load_role(role_file)
        if data:
            self.engine.context_engine.set_active_role(data["prompt_content"])
            self.refresh_roles(role_file)
            self.sidebar.set_status(f"角色：{data['name']}")
        else:
            self.sidebar.set_status("角色加载失败")

    # ── 模型 ──

    def _on_model_selected(self, _index: int) -> None:
        if self._syncing_model:
            return
        value = self.model_combo.currentData()
        if not value:
            return
        try:
            prefix, _, name = value.partition(":")
            if prefix == "del":
                candidates = [c[1] for c in self._model_choices() if not c[1].startswith("del:")]
                if not candidates:
                    self.sidebar.set_status("没有可用模型：请先启动 Ollama 或配置云端模型")
                    return
                target = candidates[0]
                t_prefix, _, t_name = target.partition(":")
                if t_prefix == "local":
                    self.engine.brain.switch_provider("ollama", model=t_name)
                    self.engine.model_config.api_key = "ollama"
                else:
                    self.engine.brain.switch_provider("deepseek", model=t_name)
                self.sidebar.set_status(f"已移除无效模型「{name}」，自动切换到 {t_name}")
            elif prefix == "local":
                self.engine.brain.switch_provider("ollama", model=name)
                self.engine.model_config.api_key = "ollama"
                self.sidebar.set_status(f"已切换到本地模型：{name}")
            elif prefix == "cloud":
                if self.engine.model_config.provider != "deepseek":
                    self.engine.brain.switch_provider("deepseek", model=name)
                else:
                    self.engine.brain.current_model = name
                    self.engine.model_config.default_model = name
                self.sidebar.set_status(f"已切换到云端模型：{name}")
            else:
                self.sidebar.set_status("未知选择")
                return
        except Exception as exc:
            self.sidebar.set_status(f"切换失败：{exc}")
        self._refresh_model_combo()
        self.status_model.setText(f"模型：{self.engine.brain.current_model}")

    # ── 外观 ──

    def _on_preset_selected(self, name: str) -> None:
        preset = APPEARANCE_PRESETS.get(name)
        if not preset:
            return
        self._effects_preset = name
        self._apply_appearance(preset)
        self.stage_panel.apply_appearance(
            preset["window_opacity"], preset["backdrop_blur"], preset["stand_depth"])
        self.preset_combo.blockSignals(True)
        index = self.preset_combo.findData(name)
        if index >= 0:
            self.preset_combo.setCurrentIndex(index)
        self.preset_combo.blockSignals(False)
        if self._settings_dialog is not None:
            self._settings_dialog.sync_appearance(self._appearance)
        self._touch_appearance()

    def _on_blur_changed(self, slot: str, radius: float, fast: bool) -> None:
        style = replace(self.stage.style(slot), blur_radius=radius)
        self.stage.set_style(slot, style, fast=fast)
        key = "backdrop_blur" if slot == "backdrop" else "stand_depth"
        self._appearance[key] = radius
        self._touch_appearance()

    def _on_window_opacity(self, value: float) -> None:
        self._appearance["window_opacity"] = value
        self.setWindowOpacity(value)
        self._touch_appearance()

    # ── 主题与外观的持久化 ──

    def _load_appearance(self) -> None:
        """从 config/appearance.toml 载入主题与特效。

        读不到 / 解析失败就用默认值 —— 外观配置坏了不该让程序起不来，
        最坏也就是回到亮色 + 标准特效。
        """
        try:
            cfg = self.engine.config.get_appearance_config()
        except Exception as exc:
            print(f"[UI] 读取外观配置失败，改用默认值：{exc}")
            return
        self._theme_preset = cfg.preset
        self._theme_tokens = (dict(cfg.tokens)
                              if cfg.preset == theme.CUSTOM_THEME_NAME
                              else theme.tokens_for(cfg.preset))
        self._effects_preset = cfg.effects_preset
        self._appearance = {
            "window_opacity": cfg.window_opacity,
            "backdrop_blur": cfg.backdrop_blur,
            "stand_depth": cfg.stand_depth,
        }

    def apply_theme(self, tokens: Dict[str, str], preset: str) -> None:
        """切换主题并立即生效。

        ⚠️ 只 `setStyleSheet` 是不够的：`speech_bubble` 是**自绘**的
        （paintEvent 里 fillPath 取令牌色），必须显式 `update()` 触发重绘，
        否则它会一直留着上一个主题的颜色。
        """
        self._theme_tokens = dict(tokens)
        self._theme_preset = preset
        theme.set_active_theme(self._theme_tokens)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.build_qss())
        if self._bubble is not None:
            self._bubble.setStyleSheet(bubble_style())
            self._bubble.update()

    def _touch_appearance(self) -> None:
        """外观有改动 → 重置去抖计时器（停手后才落盘）。"""
        if hasattr(self, "_appearance_save_timer"):
            self._appearance_save_timer.start()

    def _save_appearance(self) -> None:
        """把主题与特效写回 config/appearance.toml。"""
        try:
            self.engine.config.save_appearance_config(AppearanceConfig(
                preset=self._theme_preset,
                tokens=self._theme_tokens,
                effects_preset=self._effects_preset,
                window_opacity=self._appearance["window_opacity"],
                backdrop_blur=self._appearance["backdrop_blur"],
                stand_depth=self._appearance["stand_depth"],
            ))
        except Exception as exc:
            self.sidebar.set_status(f"外观保存失败：{exc}")

    def _on_settings_theme(self, tokens: Dict[str, str], preset: str) -> None:
        self.apply_theme(tokens, preset)
        self._touch_appearance()

    def _on_layer_visible(self, slot: str, visible: bool) -> None:
        self._layer_visible[slot] = visible
        # set_style → MediaSource.apply_style → refresh，visible=False 时层自行隐藏
        self.stage.set_style(slot, replace(self.stage.style(slot), visible=visible))

    def _apply_appearance(self, appearance: dict) -> None:
        self._appearance.update(appearance)
        self.setWindowOpacity(self._appearance["window_opacity"])
        for slot, key in (("backdrop", "backdrop_blur"), ("stand", "stand_depth")):
            style = replace(self.stage.style(slot),
                            blur_radius=self._appearance[key],
                            visible=self._layer_visible[slot])
            self.stage.set_style(slot, style)

    def open_settings(self) -> None:
        if self._settings_dialog is None:
            dialog = SettingsDialog(self.engine, self)
            dialog.appearance_changed.connect(self._on_settings_appearance)
            dialog.theme_changed.connect(self._on_settings_theme)
            dialog.model_saved.connect(self._on_model_saved)
            dialog.config_saved.connect(self._on_config_saved)
            self._settings_dialog = dialog
        self._settings_dialog.sync_appearance(self._appearance)
        self._settings_dialog.sync_theme(self._theme_tokens, self._theme_preset)
        self._settings_dialog.show()
        self._settings_dialog.raise_()
        self._settings_dialog.activateWindow()

    def _on_settings_appearance(self, appearance: dict) -> None:
        self._apply_appearance(appearance)
        self.stage_panel.apply_appearance(
            self._appearance["window_opacity"],
            self._appearance["backdrop_blur"],
            self._appearance["stand_depth"])
        self._touch_appearance()

    def _on_model_saved(self) -> None:
        self._probe_local_models()
        self._refresh_model_combo()
        self.status_model.setText(f"模型：{self.engine.brain.current_model}")

    def _on_config_saved(self, section: str) -> None:
        """设置界面保存配置后，把新值从文件拉进内存。

        各段的生效代价差别很大，所以分开处理，不做无差别重载：
        identity 改的是身份与 pins，保存时已同步 ContextEngine 缓存，只需清一次；
        persona / plugins / live2d 都各有一份启动期快照，必须显式重建。
        """
        if section == "persona":
            self.engine.reload_persona()
            self.sidebar.set_status("人设已生效（当前工作空间人设非空时仍以其为准）")
        elif section == "identity":
            # save_identity_config 内部已同步 _identity_cache，这里是双保险：
            # pins 参与每轮 build_system_prompt，缓存漏刷会让新规则不生效
            self.engine.context_engine.invalidate_caches()
            self.sidebar.set_status("身份与固定规则已生效")
        elif section == "plugins":
            self.engine.reload_plugins()
            self.sidebar.set_status("插件已按新开关重新加载")
        elif section == "live2d":
            self._reload_live2d_stand()

    def _reload_live2d_stand(self) -> None:
        """按新的 live2d.toml 重新装配立绘（重建 WebEngine，约 1~3 秒）。

        不复用旧 source：模型目录、定义文件、state_map 都可能变了，
        增量改参数比重建更容易出错，代价是几秒的重新加载。
        """
        # 旧装配必须彻底停掉：就绪轮询还在跑的话，旧 source 的
        # 成功/失败回调会覆盖掉新 source 的状态提示
        if self._live2d_watch is not None:
            self._live2d_watch.stop()
            self._live2d_watch = None
        self._live2d = None

        detached = bool(self._stand_window is not None
                        and self._stand_window.isVisible())
        if detached:
            # 独立窗口里的旧 source 随 hide 一起弃用；release 掉引用，
            # 免得 _sync_agent_state 继续往一个已废弃的 source 推状态
            self._stand_window.release_source()
            self._stand_window.hide()
            self.stage_panel.set_stand_detached(False)
        else:
            # set_layer(slot, None) 会 stop() + detach()，把旧 WebEngine 一并销毁
            self.stage.set_layer("stand", None)

        if self._load_live2d_stand():
            if detached:
                # 重新装配好的 source 现在挂在舞台上，按原状态再独立出去
                self._on_stand_window_toggled(True)
            return

        # Live2D 装不出来 → 退回插件提供的静态立绘，与启动路径保持一致
        stand_path = self.engine.plugin_manager.get_stand_image()
        if stand_path:
            spec = image_spec("stand", stand_path, LayerStyle(
                anchor="bottom-center", z=10, scale=1.0,
                blur_radius=self._appearance["stand_depth"]))
            self.stage.set_layer("stand", self._registry.create(spec))
            self.sidebar.set_status("Live2D 未装配，已回退静态立绘")
        else:
            self.sidebar.set_status("立绘未加载（plugins/static_stand/stand.png 缺失）")

    # ── 对话 ──

    def _on_send(self, text: str) -> None:
        if self.bridge.busy():
            return
        self._sync_agent_state(self._agent_tracker.reset())
        self.chat.add_message("user", text, now_stamp())
        self.chat.begin_response()
        if self._desktop_chat is not None and self._desktop_chat.isVisible():
            self._desktop_chat.add_message("user", text)
            self._desktop_chat.begin_response()
        # 先收掉上一条回复的气泡，免得新一轮开始时还挂着旧内容
        if self._bubble is not None:
            self._bubble.clear()
        if self._stand_window is not None:
            self._stand_window.set_input_enabled(False)
        self.bridge.send(text, list(self.api_state), list(self.api_state))

    def _on_stop(self) -> None:
        request_stop()
        self.sidebar.set_status("已请求停止生成")

    def _on_regenerate(self) -> None:
        """重新生成最后一条回复：撤掉末尾助手消息，用同一条用户输入重跑。

        不从界面重新取用户输入，而是复用 history 里最后一条 user 消息的正文 ——
        界面上的用户气泡文本就是它，二者一致，且不必去翻 api_state 的结构。
        """
        if self.bridge.busy():
            self.sidebar.set_status("正在生成中，无法重新生成")
            return
        text = self.chat.last_user_text()
        if not text:
            self.sidebar.set_status("找不到可重新生成的用户消息")
            return
        # 末尾的助手回复（可能多条）从 api_state 里去掉，否则会带着旧答案再问一遍
        while self.api_state and self.api_state[-1].get("role") == "assistant":
            self.api_state.pop()
        self.chat.drop_last_assistant()
        if self._desktop_chat is not None and self._desktop_chat.isVisible():
            self._desktop_chat.drop_last_assistant()
        self.chat.begin_response()
        self.bridge.send(text, list(self.api_state), list(self.api_state))
        self.sidebar.set_status("正在重新生成…")

    def _on_response_finished(self, api_state: list) -> None:
        self.api_state = list(api_state or [])
        self.chat.end_response()
        if self._desktop_chat is not None and self._desktop_chat.isVisible():
            self._desktop_chat.end_response()
        if self._bubble is not None:
            # 回复说完后气泡再停留一会儿，给用户看清的时间
            self._bubble.hold_then_hide()
        if self._stand_window is not None:
            self._stand_window.set_input_enabled(True)
        # ⚠️ 保持期内**不能**在这里先调 tracker.reset()：它会把状态置为 IDLE，
        # 之后定时器里再 reset() 就返回 None（状态无变化），
        # _sync_agent_state(None) 直接 early-return —— AI 设的表情永远回不去。
        # 所以推迟到定时器里一次性完成。
        if self._live2d_hold:
            self._live2d_hold_release.start()
        else:
            self._sync_agent_state(self._agent_tracker.reset())
        self.refresh_sessions()

    def _on_response_failed(self, message: str) -> None:
        self.chat.end_response()
        self.chat.add_message("assistant", f"出错了：{message}", now_stamp())
        if self._desktop_chat is not None and self._desktop_chat.isVisible():
            self._desktop_chat.fail(message)
        if self._stand_window is not None:
            self._stand_window.set_input_enabled(True)
        self._live2d_hold = False      # 出错就直接恢复正常，不留保持期
        self._sync_agent_state(self._agent_tracker.fail())
        self.sidebar.set_status("生成失败，详见控制台日志")
        print(f"[UI] respond 失败：{message}")

    # ── 窗口行为 ──

    def toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _on_pin_toggled(self, checked: bool) -> None:
        flags = self.windowFlags()
        if checked:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()
        # 文案也跟着变，免得只靠底色差看不出状态
        self.pin_btn.setText("已置顶" if checked else "置顶")
        self.pin_btn.setToolTip("已置顶：窗口始终浮在最前（再点一次取消）" if checked
                                else "让窗口始终浮在最前")

    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit(self) -> None:
        self._force_quit = True
        self.close()

    def closeEvent(self, event):  # noqa: N802
        # 有托盘时 ✕ = 最小化到托盘（托盘菜单「退出」才真正结束进程）
        if self.tray is not None and not self._force_quit:
            event.ignore()
            self.hide()
            self.tray.showMessage(APP_DISPLAY, "已最小化到托盘，点击托盘图标可恢复。",
                                  QSystemTrayIcon.MessageIcon.Information, 2500)
            return
        # 独立立绘与配套对话框是 Tool 窗口，不随主窗口自动关闭，退出时要一并收掉
        if self._desktop_chat is not None:
            self._desktop_chat.close()
            self._desktop_chat = None
        if self._bubble is not None:
            self._bubble.close()
            self._bubble = None
        if self._stand_window is not None:
            self._stand_window.close()
            self._stand_window = None
        try:
            from core.memory import profile_cards
            profile_cards.flush_to_disk()
        except Exception:
            pass
        try:
            if self.engine.retrieval is not None:
                self.engine.retrieval.close()
        except Exception:
            pass
        super().closeEvent(event)
        # app 设了 setQuitOnLastWindowClosed(False)，这里显式结束进程
        QApplication.quit()

    # ── 无边框缩放 ──

    def _edges_at(self, pos: QPoint):
        margin = WINDOW_MARGIN + 2
        rect = self.rect()
        left = pos.x() <= margin
        right = pos.x() >= rect.width() - margin
        top = pos.y() <= margin
        bottom = pos.y() >= rect.height() - margin
        edges = Qt.Edge(0)
        if left:
            edges |= Qt.Edge.LeftEdge
        if right:
            edges |= Qt.Edge.RightEdge
        if top:
            edges |= Qt.Edge.TopEdge
        if bottom:
            edges |= Qt.Edge.BottomEdge
        return edges

    @staticmethod
    def _cursor_for(edges):
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

    def mouseMoveEvent(self, event):  # noqa: N802
        if not self.isMaximized():
            self.setCursor(self._cursor_for(self._edges_at(event.position().toPoint())))
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and not self.isMaximized():
            edges = self._edges_at(event.position().toPoint())
            handle = self.windowHandle()
            if edges and handle is not None:
                handle.startSystemResize(edges)
                event.accept()
                return
        super().mousePressEvent(event)

    def keyPressEvent(self, event):  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self._on_stop()
            event.accept()
            return
        super().keyPressEvent(event)


def _app_icon() -> QIcon:
    """托盘 / 窗口图标：优先用立绘，缺失时程序绘制一个占位图标。"""
    stand = app_path("plugins", "static_stand", "stand.png")
    if stand.exists():
        pixmap = QPixmap(str(stand))
        if not pixmap.isNull():
            return QIcon(pixmap)
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#1A73E8"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(3, 3, 58, 58)
    painter.setPen(QColor("#FFFFFF"))
    font = QFont()
    font.setPointSize(26)
    font.setBold(True)
    painter.setFont(font)
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "妹")
    painter.end()
    return QIcon(pixmap)
