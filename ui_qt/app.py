"""应用装配 + 主循环（ARCHITECTURE_V3 §5、§6-M1）。

引擎在后台 QThread 中跑 `engine.respond()` 生成器，通过信号把流式文本推给界面；
引擎原有线程模型不改动（§2 并发约定）。
"""
from __future__ import annotations

import sys
import threading
import traceback
from typing import List

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import QApplication

from core.app_info import APP_NAME, APP_VERSION
from core.brain import request_stop
from core.engine import WhaleGirlEngine
from core.logging_utils import log, log_file, set_log_level

from .main_window import MainWindow
from . import theme


class _RespondWorker(QThread):
    """单次对话的后台工作线程：把 respond() 的每次 yield 转成信号。"""

    partial = Signal(str)
    phase_changed = Signal(str)      # 引擎当前阶段（thinking/tool/found/writing）
    completed = Signal(list)
    failed = Signal(str)
    usage_changed = Signal(dict)     # 主对话的 token 用量（含 prompt cache 命中数）

    def __init__(self, engine, user_msg: str, history: list,
                 api_state: list, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._engine = engine
        self._user_msg = user_msg
        self._history = history
        self._api_state = api_state

    def run(self) -> None:  # noqa: D401
        try:
            latest = ""
            # 引擎上还留着**上一轮**的用量，先记下来 —— 否则开局第一轮会把旧值
            # 当成新数据重发一次（状态栏数字没变，但会闪一下）
            last_seen = getattr(self._engine, "last_usage", None)
            for _msg, current, api in self._engine.respond(
                    self._user_msg, self._history, self._api_state):
                self._api_state = api
                if current:
                    tail = current[-1]
                    if isinstance(tail, dict) and tail.get("role") == "assistant":
                        latest = tail.get("content") or ""
                self.partial.emit(latest)
                # 阶段单独走一路：UI 的表情直接读它，不再从展示文本反推
                self.phase_changed.emit(
                    getattr(self._engine, "current_phase", "") or "")
                # 用量同理：只在真的换了一次新数据时才发（工具循环每轮都会更新）
                usage = getattr(self._engine, "last_usage", None)
                if usage is not None and usage is not last_seen:
                    last_seen = usage
                    self.usage_changed.emit(usage)
            self.completed.emit(list(self._api_state))
        except Exception:
            self.failed.emit(traceback.format_exc())


class _EngineStartThread(QThread):
    """后台跑 `engine.start()`（插件加载 + 工作空间恢复）。

    实测热缓存约 1.0s、冷缓存约 3.9s。原先它在主线程上同步执行，而那段时间
    事件循环还没启动 —— 窗口虽然已经画出来了，却完全点不动（Windows 还会把
    它标记成"无响应"）。放到后台线程后窗口立刻可交互，进度由
    `MainWindow.begin_startup()` 呈现在侧栏。

    ⚠️ 这里只允许跑**不碰 Qt** 的代码。已确认 `plugins/` 下无任何 Qt 引用；
    而 `on_engine_ready()`（要建 WebEngine、要动控件）仍由信号回主线程执行。
    """

    started_ok = Signal()
    failed = Signal(str)

    def __init__(self, engine, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._engine = engine

    def run(self) -> None:  # noqa: D401
        try:
            self._engine.start()
            self.started_ok.emit()
        except Exception:
            self.failed.emit(traceback.format_exc())


class EngineBridge(QObject):
    """界面与引擎之间的线程桥：保证同一时刻只有一次生成在跑。"""

    partial = Signal(str)
    phase_changed = Signal(str)
    finished = Signal(list)
    failed = Signal(str)
    usage_changed = Signal(dict)     # 透传 _RespondWorker 的 token 用量

    def __init__(self, engine, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._engine = engine
        self._worker: _RespondWorker | None = None

    def busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def send(self, user_msg: str, history: list, api_state: list) -> bool:
        if self.busy():
            return False
        worker = _RespondWorker(self._engine, user_msg, history, api_state, self)
        worker.partial.connect(self.partial)
        worker.phase_changed.connect(self.phase_changed)
        worker.completed.connect(self.finished)
        worker.failed.connect(self.failed)
        worker.usage_changed.connect(self.usage_changed)
        worker.finished.connect(self._cleanup)
        self._worker = worker
        worker.start()
        return True

    def stop(self) -> None:
        request_stop()

    def _cleanup(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()


def _describe_engine(engine) -> None:
    """启动信息（写入日志，便于排障）。"""
    mc = engine.model_config
    log.info("[配置] 模型服务商: %s", mc.provider)
    log.info("[配置] 端点: %s", mc.base_url)
    log.info("[配置] 默认模型: %s | 当前模型: %s", mc.default_model, engine.brain.current_model)
    log.info("[配置] 可用模型: %s", list(mc.available_models.values()))
    log.info("[配置] 已加载插件: %d 个", len(engine.plugin_manager.get_all_plugins()))
    current = engine.workspace_mgr.current
    if current is not None:
        log.info("[配置] 当前工作空间: [%s] %s", current.id, current.name)


def _on_engine_started(engine, window) -> None:
    """引擎就绪（已由信号回到主线程）：装配素材、列表与启动自检。"""
    _describe_engine(engine)

    # 默认模型是本地 Ollama 时，启动即预加载并常驻显存（keep_alive=-1）
    mc = engine.model_config
    if mc.provider == "ollama" and engine.brain.current_model:
        threading.Thread(
            target=engine.brain.keep_model_loaded,
            args=(engine.brain.current_model,), daemon=True).start()

    window.end_startup()
    window.on_engine_ready()
    window.maybe_onboarding()


def _on_engine_start_failed(window, detail: str) -> None:
    """引擎启动失败：解锁界面并指路日志，不让程序带着半启动状态静默运行。"""
    log.error("引擎启动失败：\n%s", detail)
    window.end_startup()
    window.sidebar.set_status("引擎启动失败，详见日志（托盘 → 查看日志）")


def main(argv: List[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    # 先用默认主题铺一层底（此时还没读配置）；读到 appearance.toml 后会重设一次
    app.setStyleSheet(theme.build_qss())
    # 托盘常驻时，关闭最后一个窗口不应结束进程
    app.setQuitOnLastWindowClosed(False)

    log.info("[启动] %s v%s 正在启动…（日志：%s）", APP_NAME, APP_VERSION, log_file())
    engine = WhaleGirlEngine()

    # 按 config/bot.toml 的 [logging] level 调整日志级别（配置与代码分离）
    try:
        level = str(engine.config.load("bot.toml").get("logging", {}).get("level", "INFO"))
        set_log_level(level)
    except Exception:
        pass

    # 主题必须在 MainWindow 构造**之前**设好：否则窗口会先按默认亮色建一遍，
    # 再被 setStyleSheet 刷成目标色，启动瞬间闪一下。
    try:
        appearance = engine.config.get_appearance_config()
        tokens = (appearance.tokens
                  if appearance.preset == theme.CUSTOM_THEME_NAME
                  else theme.tokens_for(appearance.preset))
        theme.set_active_theme(tokens)
        app.setStyleSheet(theme.build_qss())
    except Exception:
        log.error("应用主题失败，回落默认亮色：\n%s", traceback.format_exc())

    bridge = EngineBridge(engine)
    window = MainWindow(engine, bridge)
    window.show()
    app.processEvents()   # 先把窗口画出来，再做耗时的引擎启动

    # 引擎启动走后台线程：窗口一出现就能拖动 / 切主题 / 看设置，
    # 不再有 1~4 秒的"窗口画出来了但点不动"。starter 挂在 window 下保活。
    window.begin_startup()
    starter = _EngineStartThread(engine, window)
    starter.started_ok.connect(lambda: _on_engine_started(engine, window))
    starter.failed.connect(lambda detail: _on_engine_start_failed(window, detail))
    starter.start()
    return app.exec()
