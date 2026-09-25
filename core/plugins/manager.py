"""插件管理器（重构版）

关键改进：
1. 模块缓存 - 不再每次调用都重新加载模块
2. 热重载 - unload + load
3. 兼容旧 register_commands() 模式
"""
import json
import sys
import types
import inspect
import importlib.util
from pathlib import Path
from typing import Dict, Optional, Any, List, Callable

from core.paths import app_path

from .interface import BasePlugin, PluginManifest, ToolDefinition


class PluginManager:
    def __init__(self, plugin_dir: str | None = None, disabled=None):
        self.plugin_dir = Path(plugin_dir) if plugin_dir else app_path("plugins")
        # plugins.toml 里声明的禁用清单（disabled 列表 + enabled=false 的插件）。
        # 旧实现完全不读这个清单，导致「设置 → 插件」的开关是**死配置**：
        # 勾了没反应，因为 discover_and_load() 会把 plugins/ 下所有目录都加载。
        self.disabled = {str(n) for n in (disabled or [])}
        self._plugins: Dict[str, dict] = {}          # name  {manifest, path, type, entry}
        self._loaded_modules: Dict[str, Any] = {}     # name  cached module
        self._tool_registry: Dict[str, tuple] = {}    # tool_name  (func, plugin_name)

    def discover_and_load(self):
        if not self.plugin_dir.exists():
            print(f" 插件目录 '{self.plugin_dir}' 不存在，跳过。")
            return
        for subdir in self.plugin_dir.iterdir():
            if not subdir.is_dir():
                continue
            manifest_path = subdir / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
            except json.JSONDecodeError as e:
                print(f" 插件 [{subdir.name}] 的 manifest.json 格式错误: {e}")
                continue

            plugin_name = manifest.get("name", subdir.name)
            # 目录名与 manifest.name 都可能出现在 disabled 列表里，两个都比对
            if plugin_name in self.disabled or subdir.name in self.disabled:
                print(f" 插件 [{plugin_name}] 已禁用（config/plugins.toml），跳过")
                continue
            plugin_type = manifest.get("type", "unknown")
            entry_file = manifest.get("entry", "main.py")
            entry_path = subdir / entry_file
            if not entry_path.exists():
                print(f" 插件 [{plugin_name}] 入口文件缺失，跳过")
                continue

            print(f" 发现插件: {plugin_name} (类型: {plugin_type})")
            self._plugins[plugin_name] = {
                "manifest": manifest,
                "path": subdir,
                "type": plugin_type,
                "entry": entry_path,
            }
        self._build_tool_registry()

    @staticmethod
    def _pkg_name(name: str) -> str:
        """插件在 sys.modules 中的包名"""
        return f"plugin_{name}"

    def _ensure_package(self, name: str, plugin_dir: Path) -> str:
        """把插件目录注册为包，使插件内部可以正常使用 `from .兄弟模块 import x`。

        旧实现用 spec_from_file_location 直接把入口文件当"孤立模块"加载，
        没有包上下文 → 插件内任何相对导入都会抛
        "attempted relative import with no known parent package"。
        """
        pkg_name = self._pkg_name(name)
        pkg = sys.modules.get(pkg_name)
        if not isinstance(pkg, types.ModuleType):
            pkg = types.ModuleType(pkg_name)
            pkg.__path__ = [str(plugin_dir)]      # 子模块从这里找
            pkg.__package__ = pkg_name
            sys.modules[pkg_name] = pkg
        return pkg_name

    def _purge_modules(self, name: str):
        """清掉该插件（含子模块）在 sys.modules 里的缓存，保证热重载真正生效"""
        prefix = self._pkg_name(name)
        for key in [k for k in list(sys.modules) if k == prefix or k.startswith(prefix + ".")]:
            del sys.modules[key]

    def _load_module(self, name: str) -> Optional[Any]:
        """加载插件模块（带缓存）"""
        if name in self._loaded_modules:
            return self._loaded_modules[name]
        info = self._plugins.get(name)
        if not info:
            return None
        entry_path = info["entry"]
        pkg_name = self._ensure_package(name, info["path"])
        mod_name = f"{pkg_name}.{entry_path.stem}"
        # 重新加载（热重载 / /plugin_reload 全量重建）时先清掉旧子模块，
        # 否则 `from .xxx import y` 会拿到上一版代码
        for key in [k for k in list(sys.modules) if k.startswith(pkg_name + ".")]:
            del sys.modules[key]
        try:
            spec = importlib.util.spec_from_file_location(mod_name, entry_path)
            mod = importlib.util.module_from_spec(spec)
            # 先注册再执行：这样插件内 `from .main import x` 拿到的是同一个模块实例
            # （否则 main.py 会被重复执行，出现两份单例/缓存）
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            self._loaded_modules[name] = mod
            return mod
        except Exception as e:
            sys.modules.pop(mod_name, None)
            print(f" 加载插件 [{name}] 失败: {e}")
            return None

    def _build_tool_registry(self):
        """扫描所有 tool 类型插件，建立工具名函数映射"""
        self._tool_registry.clear()
        for name, info in self._plugins.items():
            if info["type"] != "tool":
                continue
            mod = self._load_module(name)
            if not mod:
                continue
            if hasattr(mod, "register_commands"):
                try:
                    commands = mod.register_commands()
                    for cmd, func in commands.items():
                        tool_name = cmd.lstrip("/")
                        self._tool_registry[tool_name] = (func, name)
                except Exception as e:
                    print(f" 插件 [{name}] register_commands 失败: {e}")

    def get_plugin(self, name: str):
        return self._plugins.get(name)

    def get_stand_image(self) -> Optional[str]:
        """获取 stand 类型插件的图片路径"""
        for name, info in self._plugins.items():
            if info["type"] == "stand":
                mod = self._load_module(name)
                if mod and hasattr(mod, "get_stand_image"):
                    try:
                        img = mod.get_stand_image()
                        if img and Path(img).exists():
                            return img
                    except Exception as e:
                        print(f" [{name}] get_stand_image 失败: {e}")
                break
        return None

    def get_tool_definitions(self) -> List[dict]:
        """获取所有工具的 OpenAI 格式定义"""
        tools = []
        # 优先使用 manifest.json 中的 tools 定义
        for name, info in self._plugins.items():
            if info["type"] != "tool":
                continue
            manifest = info.get("manifest", {})
            if "tools" in manifest:
                for tool_def in manifest["tools"]:
                    tools.append({
                        "type": "function",
                        "function": {
                            "name": tool_def["name"],
                            "description": tool_def["description"],
                            "parameters": tool_def.get("parameters", {"type": "object", "properties": {}}),
                        },
                    })
            # 动态工具：连上外部服务后才知道工具名的插件（如 MCP 客户端）
            # 在模块里实现 get_dynamic_tools() 即可，返回同样的 OpenAI 格式列表
            mod = self._load_module(name)
            if mod and hasattr(mod, "get_dynamic_tools"):
                try:
                    dynamic = mod.get_dynamic_tools()
                    if dynamic:
                        tools.extend(dynamic)
                except Exception as e:
                    print(f" 插件 [{name}] get_dynamic_tools 失败: {e}")
        return tools

    def get_tool_commands(self) -> Dict[str, tuple]:
        """返回 {cmd: (func, plugin_name)} 映射（兼容旧代码）"""
        result = {}
        for tool_name, (func, plugin_name) in self._tool_registry.items():
            result[f"/{tool_name}"] = (func, plugin_name)
        return result

    def execute_tool(self, tool_name: str, arguments: dict) -> str:
        """根据工具名执行插件工具"""
        if tool_name not in self._tool_registry:
            # 动态工具（如 MCP 客户端连上 server 后才知道的工具名）：
            # 插件实现 owns_tool() + call_dynamic_tool() 即可认领并执行
            for name, info in self._plugins.items():
                if info["type"] != "tool":
                    continue
                mod = self._load_module(name)
                if not (mod and hasattr(mod, "owns_tool")
                        and hasattr(mod, "call_dynamic_tool")):
                    continue
                try:
                    if not mod.owns_tool(tool_name):
                        continue
                    return str(mod.call_dynamic_tool(tool_name, arguments or {}))
                except Exception as e:
                    return f" 执行 {tool_name} 时出错: {e}"
            return f" 未找到工具: {tool_name}"

        func, plugin_name = self._tool_registry[tool_name]
        cmd = f"/{tool_name}"
        try:
            # 根据命令类型处理参数
            if cmd == "/view":
                arg = arguments.get("path", "")
                lines_param = arguments.get("lines")
                if lines_param is not None:
                    if isinstance(lines_param, int):
                        if lines_param <= 0:
                            return " 行数必须为正整数"
                        arg += f" {lines_param}"
                    elif isinstance(lines_param, str):
                        arg += f" {lines_param}"
                return func(arg)
            elif cmd == "/ls":
                return func(arguments.get("path", "."))
            elif cmd == "/info":
                return func(arguments.get("path", ""))
            elif cmd == "/kb_search":
                return func(arguments.get("query", ""))
            elif cmd in ("/kb_rebuild", "/kb_status"):
                return func()
            elif cmd == "/search":
                return func(
                    arguments.get("query", ""),
                    force_refresh=arguments.get("force_refresh", False),
                )
            elif cmd == "/deep":
                return func(
                    arguments.get("url", ""),
                    offset=arguments.get("offset", 0),
                )
            elif cmd == "/crawl":
                return func(arguments.get("url", ""))
            elif cmd == "/search_config":
                return func(
                    key=arguments.get("key", ""),
                    value=arguments.get("value", ""),
                )
            elif cmd == "/kb_add":
                return func(
                    arguments.get("text", ""),
                    arguments.get("source", "用户提供的资料"),
                )
            elif cmd == "/kb_hybrid":
                return func(arguments.get("query", ""))
            elif cmd == "/kb_related":
                return func(arguments.get("file_path", ""))
            elif cmd == "/kb_mcp":
                return func()
            elif cmd == "/write_file":
                return func(
                    path=arguments.get("path", ""),
                    content=arguments.get("content", ""),
                    confirm=bool(arguments.get("confirm", False)),
                )
            elif cmd == "/edit_file":
                return func(
                    path=arguments.get("path", ""),
                    old=arguments.get("old", ""),
                    new=arguments.get("new", ""),
                    replace_all=bool(arguments.get("replace_all", False)),
                    confirm=bool(arguments.get("confirm", False)),
                )
            elif cmd == "/delete_file":
                return func(
                    path=arguments.get("path", ""),
                    confirm=bool(arguments.get("confirm", False)),
                )
            elif cmd == "/open_path":
                return func(
                    path=arguments.get("path", ""),
                    confirm=bool(arguments.get("confirm", False)),
                )
            else:
                # 通用分发：按函数签名绑定参数（新增的多参数工具走这条路，
                # 例如 mcp_tool_access(tool=..., reason=..., confirm=...)）。
                # 注意：调用放在 try 之外，避免函数内部的 TypeError 被误判为
                # 「绑定失败」而用 arg_str 再调一次（会重复执行）。
                bound = None
                try:
                    bound = inspect.signature(func).bind(**arguments)
                except (TypeError, ValueError):
                    bound = None
                if bound is not None:
                    return func(*bound.args, **bound.kwargs)
                arg_str = " ".join(str(v) for v in arguments.values()) if arguments else ""
                return func(arg_str) if arg_str else func()
        except Exception as e:
            return f" 执行 {cmd} 时出错: {e}"

    def reload_plugin(self, name: str) -> str:
        if name in self._loaded_modules:
            # 从 module cache 和 tool registry 中移除
            del self._loaded_modules[name]
            self._purge_modules(name)
            stale_tools = [t for t, (f, p) in self._tool_registry.items() if p == name]
            for t in stale_tools:
                del self._tool_registry[t]
            # 重新加载
            mod = self._load_module(name)
            if mod:
                self._build_tool_registry()
                return f" 插件 [{name}] 已重载"
            return f" 插件 [{name}] 重载失败"
        return f" 插件 [{name}] 未加载"

    def get_all_plugins(self) -> Dict[str, dict]:
        return dict(self._plugins)

    def is_loaded(self, name: str) -> bool:
        return name in self._loaded_modules
