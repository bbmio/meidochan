"""静态图媒体源（M1 唯一实现）。

性能约定（ARCHITECTURE_V3 §5.5 性能红线）：
- 虚化**不**对每帧实时全分辨率处理，而是"降采样 → 模糊 → 升采样"烘焙进缓存 pixmap；
- 拖动滑杆时走 `fast=True`（低分辨率近似），松手后走 `fast=False`（精算）；
- 透明度同样在合成 pixmap 时一次性写入，运行期不挂 QGraphicsEffect（避免与模糊效果互斥）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import (
    QGraphicsBlurEffect,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QLabel,
)

from ..base import AgentState, AssetSpec, LayerStyle, MediaKind, MediaSource

_FAST_DOWNSCALE = 0.25     # 拖动时的降采样系数


def blur_pixmap(src: QPixmap, radius: float, fast: bool = False) -> QPixmap:
    """用 QGraphicsBlurEffect 把模糊烘焙进 pixmap（只作用于缓存图，不作用于控件）。"""
    if radius <= 0.5 or src.isNull():
        return src

    work = src
    factor = 1.0
    if fast:
        factor = _FAST_DOWNSCALE
        work = src.scaled(
            max(1, int(src.width() * factor)),
            max(1, int(src.height() * factor)),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(work)
    effect = QGraphicsBlurEffect()
    effect.setBlurRadius(radius * factor if fast else radius)
    item.setGraphicsEffect(effect)
    scene.addItem(item)

    out = QPixmap(work.size())
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    scene.render(painter, QRectF(out.rect()), QRectF(work.rect()))
    painter.end()
    scene.clear()

    if fast and out.size() != src.size():
        out = out.scaled(src.size(), Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    return out


def compute_rect(host: QSize, pix: QSize, style: LayerStyle) -> QRect:
    """按 anchor / scale / offset 计算层在舞台中的矩形。"""
    if pix.isEmpty() or host.width() <= 0 or host.height() <= 0:
        return QRect()
    anchor = style.anchor or "center"
    if anchor == "fill":
        ratio = max(host.width() / pix.width(), host.height() / pix.height())
    else:
        ratio = min(host.width() / pix.width(), host.height() / pix.height())
    ratio *= max(0.01, style.scale)
    w = max(1, int(pix.width() * ratio))
    h = max(1, int(pix.height() * ratio))

    if anchor.startswith("top"):
        y = 0
    elif anchor.startswith("bottom"):
        y = host.height() - h
    else:
        y = (host.height() - h) // 2

    if anchor.endswith("left"):
        x = 0
    elif anchor.endswith("right"):
        x = host.width() - w
    else:
        x = (host.width() - w) // 2

    x += int(style.offset[0])
    y += int(style.offset[1])
    return QRect(x, y, w, h)


class ImageSource(MediaSource):
    """把一个静态图片文件渲染为舞台上的一层。"""

    kind = MediaKind.IMAGE

    def __init__(self, spec: AssetSpec) -> None:
        self.spec = spec
        self._host = None
        self._label: Optional[QLabel] = None
        self._origin = QPixmap(str(spec.path)) if spec.path else QPixmap()
        self._cache_key: Optional[Tuple] = None
        self._cache: Optional[QPixmap] = None

    # ── MediaSource 接口 ──

    def attach(self, host) -> None:
        self._host = host
        if self._label is None:
            self._label = QLabel(host)
            self._label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self._label.setScaledContents(False)
            self._label.setStyleSheet("background: transparent;")
        self._label.show()
        self.refresh()

    def detach(self) -> None:
        if self._label is not None:
            self._label.setParent(None)
            self._label.deleteLater()
            self._label = None
        self._host = None

    def apply_style(self, style: LayerStyle) -> None:
        self.spec.style = style
        self.refresh(fast=False)

    def apply_style_fast(self, style: LayerStyle) -> None:
        """拖动滑杆时的近似渲染（降采样模糊）。"""
        self.spec.style = style
        self.refresh(fast=True)

    def set_state(self, state: AgentState) -> None:
        # 静态图忽略状态（动效实现接管此处即可驱动动画）
        return None

    # ── 渲染 ──

    @property
    def available(self) -> bool:
        return not self._origin.isNull()

    def refresh(self, fast: bool = False) -> None:
        """按当前样式重绘本层；fast=True 用于拖动滑杆时的低开销近似。"""
        if self._label is None or self._host is None:
            return
        style = self.spec.style
        if not style.visible or not self.available:
            self._label.hide()
            return
        self._label.show()

        rect = compute_rect(self._host.size(), self._origin.size(), style)
        if rect.isEmpty():
            return
        key = (rect.width(), rect.height(), round(style.blur_radius, 2),
               round(style.opacity, 3), fast)
        if key != self._cache_key:
            scaled = self._origin.scaled(
                QSize(rect.width(), rect.height()),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            processed = blur_pixmap(scaled, style.blur_radius, fast=fast)
            if style.opacity < 0.999:
                faded = QPixmap(processed.size())
                faded.fill(Qt.GlobalColor.transparent)
                painter = QPainter(faded)
                painter.setOpacity(max(0.0, min(1.0, style.opacity)))
                painter.drawPixmap(0, 0, processed)
                painter.end()
                processed = faded
            self._cache_key = key
            self._cache = processed

        if self._cache is not None:
            self._label.setPixmap(self._cache)
        self._label.setGeometry(rect)
        self._label.raise_()


def image_spec(asset_id: str, path: Path | str, style: LayerStyle | None = None) -> AssetSpec:
    """便捷构造静态图 AssetSpec。"""
    return AssetSpec(
        id=asset_id,
        kind=MediaKind.IMAGE,
        path=Path(path),
        style=style or LayerStyle(),
    )
