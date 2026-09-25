"""Live2D 媒体源：用 QtWebEngine 承载 assets/live2d/viewer 里的播放器。

为什么走 WebEngine：Live2D Cubism 的官方运行时只有 Web / Native 两套，
Python 侧没有可用实现；本机 QtWebEngine 可用（实测能加载模型并渲染）。

两个必须知道的约束：
1. **Qt 的图形特效对 QWebEngineView 无效** —— 它是独立渲染表面，
   `QGraphicsBlurEffect` / `QGraphicsOpacityEffect` 都不起作用。
   所以虚化与透明改由页面内的 CSS 承担（viewer 暴露了 setBlur / setOpacity）。
2. **QWebEngineView 是异步的** —— 页面加载 + 模型解析要 1~3 秒。
   就绪之前的调用会排队，就绪后一次性补发；`ready` / `error` 供上层显示状态。
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, List, Optional

from PySide6.QtCore import QRect, QTimer, QUrl, QUrlQuery, Qt
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView

from core.live2d_assets import scan_model_assets

from ..base import AgentState, AssetSpec, LayerStyle, MediaKind, MediaSource

READY_POLL_MS = 200        # 就绪轮询间隔
READY_TIMEOUT_MS = 30000   # 超过这个时间还没就绪就报错，避免无限等
STYLE_COALESCE_MS = 50     # 拖动滑杆时合并样式推送，避免每步都过一遍 CSS


class Live2DPage(QWebEnginePage):
    """把页面里的 console 输出转到 Python，便于排查加载失败。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.messages: List[str] = []

    def javaScriptConsoleMessage(self, level, message, line, source) -> None:  # noqa: N802
        text = f"{message} ({source}:{line})"
        self.messages.append(text)
        if len(self.messages) > 50:
            del self.messages[:-50]
        print(f" [live2d] {text}")


class Live2DSource(MediaSource):
    """把 Live2D 播放器页面渲染为舞台上的一层。"""

    kind = MediaKind.LIVE2D

    def __init__(self, spec: AssetSpec) -> None:
        self.spec = spec
        self._host = None
        self._view: Optional[QWebEngineView] = None
        self._page: Optional[Live2DPage] = None
        self._ready = False
        self._error: Optional[str] = None
        self._queue: List[str] = []
        self._poll: Optional[QTimer] = None
        self._poll_started = 0
        self._style_timer: Optional[QTimer] = None
        self._last_state: Optional[AgentState] = None

    # ── 状态出口（供上层显示加载态） ──

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def error(self) -> Optional[str]:
        return self._error

    # ── MediaSource 接口 ──

    def attach(self, host) -> None:
        # 控件已存在但挂在别的宿主上（例如从独立桌面窗口收回舞台）→ 换父级，不重建
        if self._view is not None and self._view.parent() is not host:
            self.reparent(host)
            return
        self._host = host
        if self._view is None:
            self._view = QWebEngineView(host)
            self._view.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
            # 不要 WebEngine 自带的浏览器右键菜单（重新加载 / 检查元素那一套）
            self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
            self._page = Live2DPage(self._view)
            self._view.setPage(self._page)
            # 页面自身透明，否则 WebEngine 会铺一层白底
            self._page.setBackgroundColor(Qt.GlobalColor.transparent)
            self._page.loadFinished.connect(self._on_load_finished)

            self._style_timer = QTimer(self._view)
            self._style_timer.setSingleShot(True)
            self._style_timer.setInterval(STYLE_COALESCE_MS)
            self._style_timer.timeout.connect(self._push_style)

            self._view.load(self._build_url())
        self._view.show()
        self.refresh()

    def detach(self) -> None:
        for timer in (self._poll, self._style_timer):
            if timer is not None:
                timer.stop()
        self._poll = None
        if self._view is not None:
            self._view.setParent(None)
            self._view.deleteLater()
            self._view = None
            self._page = None
        self._host = None
        self._ready = False
        self._queue.clear()

    def reparent(self, host) -> None:
        """换一个宿主（主窗口舞台 ↔ 独立桌面窗口），**不重新加载模型**。

        直接把 QWebEngineView 挂到新宿主上即可 —— 重建的话要重跑一遍
        WebEngine 启动 + 模型解析（1~3 秒），切换会有明显卡顿。
        """
        self._host = host
        if self._view is None:
            self.attach(host)
            return
        self._view.setParent(host)
        self._view.show()
        self.refresh()
        if self._last_state is not None:
            state, self._last_state = self._last_state, None
            self.set_state(state)

    def apply_style(self, style: LayerStyle) -> None:
        self.spec.style = style
        self.refresh(fast=False)

    def apply_style_fast(self, style: LayerStyle) -> None:
        self.spec.style = style
        self.refresh(fast=True)

    def set_state(self, state: AgentState, skip_expression: bool = False) -> None:
        """按对话状态切换动作 / 表情（映射表在 config/live2d.toml 的 state_map）。

        `skip_expression=True` 时**只播动作、不动表情** —— AI 显式设的表情
        正处于保持期，不该被状态机覆盖（见 main_window 的保持期逻辑）。
        """
        if state == self._last_state and not skip_expression:
            return
        self._last_state = state
        self._js("MeidoLive2D.setState(%s, %s);" % (
            json.dumps(state.value), "true" if skip_expression else "false"))

    # ── AI 主动控制 ──
    # 由 plugins/live2d_control 写请求 → core/live2d_control 队列 → main_window
    # 在 GUI 线程取走并调到这里。**不要**在工具线程直接调这些方法。
    #
    # 名字一律走 _js_string 转义：动作/表情名来自模型目录的文件名，
    # 是不可控输入，直接拼进 JS 会被引号或反斜杠拼坏。

    @staticmethod
    def _js_string(value: str) -> str:
        """把名字转成可安全嵌进 JS 的字符串字面量。

        `ensure_ascii=False` 是刻意的：默认会把中文转成 `\\uXXXX`，
        JS 能正确还原（功能没问题），但日志里就成了一串转义码，
        排查时看不出到底切了哪个表情。项目里构建 manifest 也是这么写的。
        """
        return json.dumps(str(value), ensure_ascii=False)

    def play_motion(self, name: str) -> None:
        """播一次指定动作（播完自己停）。"""
        self._js(f"MeidoLive2D.playMotion({self._js_string(name)});")

    def set_expression(self, name: str) -> None:
        """切到指定表情。"""
        self._js(f"MeidoLive2D.setExpression({self._js_string(name)});")

    def clear_expression(self) -> None:
        """清掉表情、回到模型默认脸。"""
        self._js("MeidoLive2D.clearExpression();")

    def stop(self) -> None:
        if self._poll is not None:
            self._poll.stop()

    # ── 渲染 ──

    def refresh(self, fast: bool = False) -> None:
        if self._view is None or self._host is None:
            return
        style = self.spec.style
        if not style.visible:
            self._view.hide()
            return
        self._view.show()
        self._view.setGeometry(self._geometry(style))
        self._view.raise_()
        # 虚化 / 透明 / 缩放只能走页面内 CSS —— Qt 特效对 WebEngine 无效。
        # 拖动滑杆时会高频触发，用单次定时器合并，避免每步都重排一次。
        if self._style_timer is not None:
            self._style_timer.start()

    def _geometry(self, style: LayerStyle) -> QRect:
        """Live2D 铺满宿主，模型的对齐与缩放由页面内 fit() 负责（底边居中）。"""
        host = self._host.size()
        return QRect(int(style.offset[0]), int(style.offset[1]),
                     host.width(), host.height())

    def _push_style(self) -> None:
        style = self.spec.style
        self._js(f"MeidoLive2D.setBlur({float(style.blur_radius):.2f});")
        self._js(f"MeidoLive2D.setOpacity({float(style.opacity):.3f});")
        self._js(f"MeidoLive2D.setZoom({float(style.scale):.3f});")

    # ── 页面加载 ──

    def _build_url(self) -> QUrl:
        viewer = Path(self.spec.path)
        model = Path(str(self.spec.params.get("model") or ""))
        try:
            rel = os.path.relpath(model, viewer.parent).replace("\\", "/")
        except ValueError:
            rel = model.as_posix()
        url = QUrl.fromLocalFile(str(viewer))
        query = QUrlQuery()
        query.addQueryItem("model", rel)
        query.addQueryItem("manifest", self._build_manifest(model))
        url.setQuery(query)
        return url

    def _build_manifest(self, model: Path) -> str:
        """把动作/表情清单 + 状态映射表一起塞进 manifest。

        模型自带的 model3.json 没声明 Motions / Expressions，viewer 按这份清单补进去。
        """
        # 动作/表情清单走 core/live2d_assets 的单一出处 —— 设置界面的下拉框
        # 也用同一份，否则会出现「界面里选得出、播放器认不出」的错配。
        motions, expressions = scan_model_assets(model.parent)
        # 交互参数在 TOML 里是 snake_case（与项目其他配置一致），
        # 页面侧读的是 camelCase —— 这里显式映射，避免键名静默对不上
        # （之前就是踩了这个坑：click_expressions 没被页面读到，列表是空的）。
        raw_interaction = self.spec.params.get("interaction") or {}
        payload = {
            "motions": motions,
            "expressions": expressions,
            "stateMap": self.spec.params.get("state_map") or {},
            "interaction": {
                "clickExpressions": list(raw_interaction.get("click_expressions") or []),
                "clickRevertSeconds": float(
                    raw_interaction.get("click_revert_seconds", 2.0) or 0.0),
                "focusGain": float(raw_interaction.get("focus_gain", 0.35) or 0.0),
                "focusRestSeconds": float(
                    raw_interaction.get("focus_rest_seconds", 1.2) or 0.0),
            },
        }
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def _on_load_finished(self, ok: bool) -> None:
        if not ok:
            self._error = "播放器页面加载失败"
            print(" [live2d] 页面加载失败")
            return
        if self._view is None:
            return
        self._poll = QTimer(self._view)
        self._poll.setInterval(READY_POLL_MS)
        self._poll.timeout.connect(self._check_ready)
        self._poll_started = 0
        self._poll.start()

    def _check_ready(self) -> None:
        if self._view is None:
            return
        self._poll_started += READY_POLL_MS
        if self._poll_started > READY_TIMEOUT_MS:
            if self._poll is not None:
                self._poll.stop()
            self._error = "模型加载超时（30 秒内未就绪）"
            print(" [live2d] 模型加载超时")
            return
        self._view.page().runJavaScript(
            "JSON.stringify({r:!!(window.MeidoLive2D&&MeidoLive2D.ready),"
            "e:(window.MeidoLive2D&&MeidoLive2D.error)||null})",
            0, self._on_ready_probe)

    def _on_ready_probe(self, value: Any) -> None:
        if not isinstance(value, str) or not value:
            return
        try:
            info = json.loads(value)
        except Exception:
            return
        if info.get("e"):
            if self._poll is not None:
                self._poll.stop()
            self._error = str(info["e"])[:300]
            print(f" [live2d] 模型加载失败：{self._error}")
            return
        if not info.get("r"):
            return
        if self._poll is not None:
            self._poll.stop()
        self._ready = True
        self._error = None
        print(" [live2d] 模型已就绪")
        self._flush_queue()
        self.refresh()
        if self._last_state is not None:
            state, self._last_state = self._last_state, None
            self.set_state(state)

    # ── JS 通道 ──

    def _js(self, code: str) -> None:
        """就绪前排队，就绪后补发 —— 避免在页面还没准备好时丢指令。"""
        if not code:
            return
        if not self._ready or self._view is None:
            self._queue.append(code)
            if len(self._queue) > 40:
                del self._queue[:-40]
            return
        self._view.page().runJavaScript(code)

    def _flush_queue(self) -> None:
        if self._view is None:
            self._queue.clear()
            return
        pending, self._queue = self._queue, []
        for code in pending:
            self._view.page().runJavaScript(code)


def live2d_spec(asset_id: str, viewer: Path | str, model: Path | str,
                style: LayerStyle | None = None,
                state_map: dict | None = None,
                interaction: dict | None = None) -> AssetSpec:
    """便捷构造 Live2D 的 AssetSpec。

    state_map  —— 对话状态 → 动作/表情
    interaction —— 点击反应 / 视线跟随系数等交互参数
    """
    return AssetSpec(
        id=asset_id,
        kind=MediaKind.LIVE2D,
        path=Path(viewer),
        style=style or LayerStyle(),
        params={"model": str(model), "state_map": state_map or {},
                "interaction": interaction or {}},
    )
