"""
鲸鱼娘 消息总线
简单的观察者模式，连接所有组件。同步优先，未来可叠加异步。

事件流：
  ui_qt  MESSAGE_RECEIVED  engine  brain  MESSAGE_SEND  ui_qt
  PluginManager  TOOL_BEFORE_EXECUTE  sandbox  approve/deny
"""
from enum import Enum
from typing import Callable, Dict, List, Any


class EventType(Enum):
    MESSAGE_RECEIVED = "message.received"
    MESSAGE_SEND = "message.send"
    TOOL_BEFORE_EXECUTE = "tool.before_execute"
    TOOL_AFTER_EXECUTE = "tool.after_execute"
    PLUGIN_LOADED = "plugin.loaded"
    PLUGIN_UNLOADED = "plugin.unloaded"
    CONFIG_CHANGED = "config.changed"
    OATH_TRIGGERED = "oath.triggered"
    SANDBOX_REQUEST = "sandbox.request"


class MessageBus:
    """消息总线：发布/订阅模式，所有组件通过它解耦通信。"""

    def __init__(self):
        self._subscribers: Dict[EventType, List[Callable]] = {}
        for et in EventType:
            self._subscribers[et] = []

    def subscribe(self, event: EventType, callback: Callable) -> None:
        if callback not in self._subscribers[event]:
            self._subscribers[event].append(callback)

    def unsubscribe(self, event: EventType, callback: Callable) -> None:
        if callback in self._subscribers[event]:
            self._subscribers[event].remove(callback)

    def publish(self, event: EventType, data: Dict[str, Any]) -> List[Any]:
        results = []
        for cb in self._subscribers[event]:
            try:
                result = cb(data)
                results.append(result)
            except Exception as e:
                import traceback
                print(f" [MessageBus] 事件 {event.value} 处理异常: {e}")
                traceback.print_exc()
        return results
