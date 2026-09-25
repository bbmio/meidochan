"""媒体源实现集合。M1 只有 image_source（静态图）。"""
from .image_source import ImageSource  # noqa: F401

__all__ = ["ImageSource"]
