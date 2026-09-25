"""
工作空间管理器 — 多工作空间上下文隔离的核心组件。
每个工作空间拥有独立的：知识库、对话历史、System Prompt、插件启用列表。
"""
from .manager import WorkspaceManager
from .models import Workspace

__all__ = ["WorkspaceManager", "Workspace"]
