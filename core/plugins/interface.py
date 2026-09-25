"""插件标准接口（ABC）"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Dict, Any


@dataclass
class ToolDefinition:
    """LLM 函数调用工具定义"""
    name: str
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    require_approval: bool = False

    def to_openai_format(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class PluginManifest:
    name: str
    version: str
    type: str  # "tool", "stand", "adapter", "memory"
    description: str = ""
    author: str = ""
    dependencies: List[str] = field(default_factory=list)


class BasePlugin(ABC):
    manifest: PluginManifest

    @abstractmethod
    def on_load(self) -> None:
        ...

    @abstractmethod
    def on_unload(self) -> None:
        ...

    def get_tools(self) -> List[ToolDefinition]:
        return []

    def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        raise NotImplementedError(f"Tool '{tool_name}' not implemented")

    def get_stand_image(self) -> str:
        return ""
