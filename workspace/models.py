"""工作空间数据模型"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List


@dataclass
class Workspace:
    """一个工作空间的完整配置"""
    id: str                                      # 唯一标识符，e.g. "default", "client_a"
    name: str                                    # 显示名称
    persona_prompt: str = ""                     # System Prompt 覆盖（空 = 使用全局默认）
    plugin_enabled: List[str] = field(default_factory=list)  # 启用的插件名列表（空 = 全部启用）
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    # 派生路径 — 由 WorkspaceManager 管理，不存储到 TOML
    root_dir: str = ""                           # 工作空间根目录（运行时注入）
    history_dir: str = ""                        # 对话历史目录
    kb_dir: str = ""                             # 知识库目录
    memory_file: str = ""                        # 长期记忆文件路径

    def to_dict(self) -> dict:
        """序列化为 TOML 兼容的字典"""
        return {
            "id": self.id,
            "name": self.name,
            "persona_prompt": self.persona_prompt,
            "plugin_enabled": self.plugin_enabled,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Workspace":
        """从字典反序列化"""
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            persona_prompt=d.get("persona_prompt", ""),
            plugin_enabled=d.get("plugin_enabled", []),
            created_at=d.get("created_at", ""),
        )
