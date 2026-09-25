"""媒体抽象层入口：导出契约对象并装配默认注册表。"""
from .base import (  # noqa: F401
    AgentState,
    AssetSpec,
    LayerStyle,
    MediaKind,
    MediaRegistry,
    MediaSource,
)
from .sources.image_source import ImageSource, image_spec  # noqa: F401
from .sources.live2d_source import Live2DSource, live2d_spec  # noqa: F401


def create_default_registry() -> MediaRegistry:
    """注册已实现的媒体类型：IMAGE（静态图）与 LIVE2D（WebEngine 播放器）。"""
    registry = MediaRegistry()
    registry.register(MediaKind.IMAGE, ImageSource)
    registry.register(MediaKind.LIVE2D, Live2DSource)
    return registry


__all__ = [
    "AgentState", "AssetSpec", "LayerStyle", "MediaKind",
    "MediaRegistry", "MediaSource", "ImageSource", "Live2DSource",
    "image_spec", "live2d_spec", "create_default_registry",
]
