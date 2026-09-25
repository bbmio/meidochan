"""MCP 客户端插件：连接外部 MCP server（stdio），把它们的工具动态注册给 AI。

设计要点
--------
1. **异步桥接**：MCP SDK 是 asyncio 的，而插件的 execute_tool 是同步的。
   这里用一条后台线程跑常驻事件循环，把异步调用包成同步接口。
2. **会话归属**：MCP 的 stdio_client / ClientSession 必须在**同一个任务**里进出，
   所以每个 server 由一个常驻协程持有会话，外部调用通过 asyncio.Queue 投递进去
   串行执行（AI 本来就是顺序调用工具，串行没有损失）。
3. **非阻塞启动**：server 在后台线程启动（npx 首次拉包实测约 40s），
   启动期间 get_dynamic_tools() 返回空列表，不会卡住界面。
4. **命名空间**：工具名统一加 `<server>__` 前缀，避免与现有插件工具重名。
5. **受限工具**：config/mcp.toml 的 disabled_tools 默认不暴露；
   妹抖酱需先用 mcp_tool_access 向用户申请，用户同意后授权，下次对话即生效。
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.paths import APP_DIR, app_path

try:
    import tomllib          # Python 3.11+
except ImportError:         # pragma: no cover - 项目要求 3.11+，这里只做兜底提示
    tomllib = None          # type: ignore[assignment]

PLUGIN_DIR = Path(__file__).parent
CONFIG_PARTS = ("config", "mcp.toml")
DEFAULT_GRANTS_PARTS = ("data", "mcp_grants.json")
NAME_SEP = "__"
MAX_DESCRIPTION = 1024


# ═══════════════════════════════════════════════════════════
# 配置 / 授权记录
# ═══════════════════════════════════════════════════════════

def _load_manifest() -> dict:
    with open(PLUGIN_DIR / "manifest.json", "r", encoding="utf-8") as handle:
        return json.load(handle)


def _load_config() -> dict:
    """读取 config/mcp.toml；缺失或无法解析时返回空配置（等于不启用）。"""
    path = app_path(*CONFIG_PARTS)
    if not path.exists():
        return {}
    if tomllib is None:
        print(" [mcp_client] 需要 Python 3.11+ 的 tomllib 才能读取 config/mcp.toml")
        return {}
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except Exception as exc:
        print(f" [mcp_client] config/mcp.toml 解析失败，按未启用处理：{exc}")
        return {}


def _grants_path() -> Path:
    raw = str((_load_manifest().get("config") or {}).get("grants_file")
              or "/".join(DEFAULT_GRANTS_PARTS))
    path = Path(os.path.expandvars(raw))
    return path if path.is_absolute() else app_path(*path.parts)


def _load_grants() -> dict:
    path = _grants_path()
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f" [mcp_client] 授权记录读取失败，按空处理：{exc}")
    return {}


def _save_grants(data: dict) -> None:
    path = _grants_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f" [mcp_client] 授权记录写入失败：{exc}")


# ═══════════════════════════════════════════════════════════
# 异步桥接
# ═══════════════════════════════════════════════════════════

class _Loop:
    """一条后台线程 + 常驻 asyncio 事件循环。"""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro, timeout: Optional[float] = None):
        """把协程投到循环里执行并同步等结果。"""
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout)


class _Server:
    """一个 MCP server 的常驻会话。

    会话由 `_serve()` 这一个常驻协程持有（MCP 的 stdio_client / ClientSession
    必须在同一任务里进出），外部调用通过 asyncio.Queue 投递进来串行执行。
    """

    def __init__(self, name: str, cfg: dict, loop: _Loop) -> None:
        self.name = name
        self.cfg = cfg
        self.loop = loop
        self.tools: List[Any] = []
        self.error: Optional[str] = None
        self._queue: Optional[asyncio.Queue] = None
        self._stop: Optional[asyncio.Event] = None
        self._ready = threading.Event()

    # ── 生命周期 ──

    def start(self, timeout: float) -> None:
        """在事件循环里拉起常驻协程，并等它完成握手（或超时）。"""
        asyncio.run_coroutine_threadsafe(self._serve(), self.loop.loop)
        if not self._ready.wait(timeout):
            self.error = self.error or f"启动超时（>{timeout:.0f}s）"

    def stop(self) -> None:
        if self._stop is not None:
            try:
                self.loop.loop.call_soon_threadsafe(self._stop.set)
            except Exception:
                pass

    async def _serve(self) -> None:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            self.error = f"缺少 mcp 依赖（pip install mcp）：{exc}"
            self._ready.set()
            return

        env = dict(os.environ)
        for key, value in (self.cfg.get("env") or {}).items():
            env[str(key)] = str(value)
        params = StdioServerParameters(
            command=str(self.cfg.get("command") or ""),
            args=[str(a) for a in (self.cfg.get("args") or [])],
            env=env,
            # 固定子进程工作目录为项目根：server 里的相对路径（如 --output-dir）
            # 才有确定含义，不会随启动方式漂移
            cwd=str(APP_DIR),
        )

        self._queue = asyncio.Queue()
        self._stop = asyncio.Event()
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    self.tools = list(listed.tools)
                    self.error = None
                    self._ready.set()
                    await self._pump(session)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            self._ready.set()

    async def _pump(self, session) -> None:
        """常驻循环：从队列取请求 → 调 MCP → 回填 future。"""
        assert self._queue is not None and self._stop is not None
        while not self._stop.is_set():
            try:
                tool, arguments, future = await asyncio.wait_for(
                    self._queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if future.cancelled():
                continue
            try:
                future.set_result(await session.call_tool(tool, arguments))
            except Exception as exc:
                future.set_exception(exc)

    # ── 调用 ──

    def call(self, tool: str, arguments: dict, timeout: float) -> Any:
        if self._queue is None:
            raise RuntimeError(
                f"MCP server [{self.name}] 尚未就绪：{self.error or '仍在启动'}")
        return self.loop.run(self._submit(tool, arguments, timeout),
                             timeout=timeout + 5)

    async def _submit(self, tool: str, arguments: dict, timeout: float) -> Any:
        assert self._queue is not None
        future = self.loop.loop.create_future()
        await self._queue.put((tool, arguments, future))
        return await asyncio.wait_for(future, timeout=timeout)


# ═══════════════════════════════════════════════════════════
# 插件级状态
# ═══════════════════════════════════════════════════════════

_LOOP: Optional[_Loop] = None
_SERVERS: Dict[str, _Server] = {}
_BOOTSTRAP_DONE = threading.Event()
_BOOTSTRAP_LOCK = threading.Lock()
_BOOTSTRAP_STARTED = False


def _bootstrap() -> None:
    """后台启动所有启用的 MCP server（不阻塞插件加载）。"""
    global _LOOP
    cfg = _load_config()
    settings = cfg.get("settings") or {}
    if not settings.get("enabled", True):
        _BOOTSTRAP_DONE.set()
        return

    _LOOP = _Loop()
    timeout = float(settings.get("startup_timeout", 120) or 120)
    for entry in (cfg.get("servers") or []):
        if not isinstance(entry, dict) or not entry.get("enabled", True):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        server = _Server(name, entry, _LOOP)
        _SERVERS[name] = server
        try:
            server.start(timeout)
        except Exception as exc:
            server.error = f"{type(exc).__name__}: {exc}"
        if server.tools:
            print(f" [mcp_client] server [{name}] 就绪，工具 {len(server.tools)} 个")
        else:
            print(f" [mcp_client] server [{name}] 不可用：{server.error}")
    _BOOTSTRAP_DONE.set()


def _ensure_bootstrap() -> None:
    """只启动一次后台线程（用独立标志位，避免 _LOOP 还没赋值时被重复触发）。"""
    global _BOOTSTRAP_STARTED
    with _BOOTSTRAP_LOCK:
        if _BOOTSTRAP_STARTED:
            return
        _BOOTSTRAP_STARTED = True
    threading.Thread(target=_bootstrap, daemon=True).start()


# ═══════════════════════════════════════════════════════════
# 工具过滤 / 暴露
# ═══════════════════════════════════════════════════════════

def _disabled_of(server: _Server) -> set:
    return {str(t) for t in (server.cfg.get("disabled_tools") or [])}


def _allowed_of(server: _Server) -> Optional[set]:
    raw = server.cfg.get("allowed_tools")
    return {str(t) for t in raw} if raw else None


def _full_name(server_name: str, tool_name: str) -> str:
    return f"{server_name}{NAME_SEP}{tool_name}"


def _disabled_tool_names() -> List[Tuple[str, str]]:
    """所有被禁用的工具（含已授权的），返回 [(完整工具名, server名)]。

    用于按裸名解析工具 —— 已授权的工具已经不在 _restricted_tools() 里了，
    但撤销时仍需要能找到它。
    """
    out: List[Tuple[str, str]] = []
    for name, server in _SERVERS.items():
        disabled = _disabled_of(server)
        for tool in server.tools:
            if tool.name in disabled:
                out.append((_full_name(name, tool.name), name))
    return out


def _restricted_tools() -> List[Tuple[str, str]]:
    """当前被禁用、且尚未授权的工具，返回 [(完整工具名, server名)]。"""
    grants = _load_grants()
    return [(full, srv) for full, srv in _disabled_tool_names()
            if not (grants.get(full) or {}).get("granted")]


def _exposed_tools() -> List[dict]:
    """当前应暴露给 AI 的工具定义（OpenAI 格式）。"""
    grants = _load_grants()
    out: List[dict] = []
    for name, server in _SERVERS.items():
        disabled = _disabled_of(server)
        allowed = _allowed_of(server)
        for tool in server.tools:
            if allowed is not None and tool.name not in allowed:
                continue
            full = _full_name(name, tool.name)
            if tool.name in disabled and not (grants.get(full) or {}).get("granted"):
                continue
            out.append({
                "type": "function",
                "function": {
                    "name": full,
                    "description": (tool.description or "")[:MAX_DESCRIPTION],
                    "parameters": tool.inputSchema
                    or {"type": "object", "properties": {}},
                },
            })
    return out


def get_dynamic_tools() -> List[dict]:
    """供 manager.get_tool_definitions() 调用的动态工具钩子。

    server 还在后台启动时返回空列表（不阻塞）。
    """
    _ensure_bootstrap()
    return _exposed_tools()


def owns_tool(tool_name: str) -> bool:
    """本插件是否认领该工具名（供 manager.execute_tool 路由动态工具）。"""
    if NAME_SEP not in tool_name:
        return False
    server_name = tool_name.split(NAME_SEP, 1)[0]
    return server_name in _SERVERS


def call_dynamic_tool(tool_name: str, arguments: dict) -> str:
    """执行一个动态（MCP）工具。"""
    server_name, raw = tool_name.split(NAME_SEP, 1)
    server = _SERVERS.get(server_name)
    if server is None:
        return f" 未找到 MCP server：{server_name}"

    # 受限工具必须先授权，防止绕过 mcp_tool_access 直接调用
    if raw in _disabled_of(server):
        full = _full_name(server_name, raw)
        if not (_load_grants().get(full) or {}).get("granted"):
            return (f" 工具 `{full}` 默认禁用（高风险）。"
                    f"请先用 mcp_tool_access 向用户申请，用户同意后再带 confirm=true 授权。")

    timeout = 120.0
    cfg = _load_config()
    timeout = float((cfg.get("settings") or {}).get("call_timeout", timeout) or timeout)
    try:
        result = server.call(raw, dict(arguments or {}), timeout)
    except Exception as exc:
        return f" 调用 MCP 工具 {tool_name} 失败：{type(exc).__name__}: {exc}"
    return _render_result(result)


def _render_result(result: Any) -> str:
    """把 MCP 的 CallToolResult 渲染成给模型看的文本。"""
    parts: List[str] = []
    for item in (getattr(result, "content", None) or []):
        text = getattr(item, "text", None)
        if text:
            parts.append(str(text))
            continue
        kind = getattr(item, "type", None) or type(item).__name__
        parts.append(f"[{kind} 内容已省略]")
    if not parts:
        parts.append("（MCP 工具未返回文本内容）")
    body = "\n".join(parts)
    if getattr(result, "isError", False):
        return " MCP 工具返回错误：\n" + body
    return body


# ═══════════════════════════════════════════════════════════
# 静态工具（写在 manifest.json 里，始终可用）
# ═══════════════════════════════════════════════════════════

def mcp_status() -> str:
    """查看 MCP 连接状态、可用工具与受限工具。"""
    _ensure_bootstrap()
    cfg = _load_config()
    if not cfg:
        return (f" 未读到 config/mcp.toml（路径：{app_path(*CONFIG_PARTS)}），"
                f"MCP 客户端未启用。")
    if not (cfg.get("settings") or {}).get("enabled", True):
        return " MCP 客户端已在 config/mcp.toml 的 [settings].enabled 中关闭。"

    if not _BOOTSTRAP_DONE.is_set():
        lines = ["MCP 正在后台启动中（首次跑 npx 要下载包，可能需 30-60 秒），请稍后再查。"]
        for name in (cfg.get("servers") or []):
            if isinstance(name, dict) and name.get("name"):
                lines.append(f"  - {name['name']}")
        return "\n".join(lines)

    if not _SERVERS:
        return " config/mcp.toml 里没有启用的 server。"

    grants = _load_grants()
    lines = ["【MCP 状态】"]
    for name, server in _SERVERS.items():
        if server.tools:
            lines.append(f"- [{name}] 就绪，工具 {len(server.tools)} 个")
        else:
            lines.append(f"- [{name}] 不可用：{server.error}")
        disabled = _disabled_of(server)
        if disabled:
            lines.append(f"    受限工具（默认禁用）：{', '.join(sorted(disabled))}")
        granted = [f for f in grants if f.startswith(name + NAME_SEP) and grants[f].get("granted")]
        lines.append(f"    已授权：{', '.join(granted) if granted else '无'}")
    exposed = _exposed_tools()
    lines.append(f"当前暴露给 AI 的工具数：{len(exposed)}")
    return "\n".join(lines)


def mcp_tool_access(tool: str = "", reason: str = "",
                    confirm: bool = False, revoke: bool = False) -> str:
    """申请 / 授权 / 撤销受限工具的使用权。

    流程：妹抖酱调本工具说明理由 → 返回申请文本给你看 → 你同意后
    妹抖酱再带 confirm=true 调一次 → 写入授权记录 → 下次对话该工具即可用。
    """
    _ensure_bootstrap()
    tool = (tool or "").strip()
    if not tool:
        restricted = _restricted_tools()
        if not restricted:
            return " 当前没有受限工具。"
        return ("受限工具列表（需申请后才能使用）：\n"
                + "\n".join(f"  - {full}（来自 {srv}）" for full, srv in restricted)
                + "\n\n如需使用，请带 tool 与 reason 调用本工具，我会把申请理由转达给用户。")

    # 允许只写裸工具名（如 browser_run_code_unsafe），自动补全 server 前缀。
    # 用「所有被禁用的工具」解析，这样已授权的也能被裸名找到（撤销时要用）。
    full = tool
    if NAME_SEP not in tool:
        matches = [f for f, _ in _disabled_tool_names() if f.endswith(NAME_SEP + tool)]
        if len(matches) == 1:
            full = matches[0]
        elif not matches:
            return (f" 未找到受限工具 `{tool}`。"
                    f"请先不带参数调用本工具查看可申请列表。")

    server_name = full.split(NAME_SEP, 1)[0]
    raw = full.split(NAME_SEP, 1)[1]
    server = _SERVERS.get(server_name)
    if server is None:
        return f" 未找到 MCP server：{server_name}"

    grants = _load_grants()
    entry = grants.get(full) or {}

    if revoke:
        if full in grants:
            grants.pop(full)
            _save_grants(grants)
            return f" 已撤销 `{full}` 的授权，下次对话起该工具不再暴露。"
        return f" `{full}` 当前没有授权记录。"

    if entry.get("granted"):
        return f" `{full}` 已授权（{entry.get('granted_at', '未知时间')}），可直接使用。"

    if not confirm:
        return (
            "【工具使用申请】\n"
            f"妹抖酱想启用受限工具：`{full}`\n"
            f"理由：{reason.strip() or '（未说明）'}\n"
            "风险：该工具能在浏览器进程里执行任意 Playwright/JavaScript 代码，"
            "等价于远程代码执行，请确认你信任当前要执行的内容。\n\n"
            "若你同意，请回复确认；妹抖酱会再调用一次本工具并带 confirm=true 完成授权。"
        )

    grants[full] = {
        "granted": True,
        "reason": reason.strip(),
        "granted_at": datetime.now().isoformat(timespec="seconds"),
    }
    _save_grants(grants)
    return (f" 已授权 `{full}`，下次对话起该工具会出现在可用工具列表中。"
            f"如需收回，可调用 mcp_tool_access(tool='{full}', revoke=true)。")


# ═══════════════════════════════════════════════════════════
# 插件注册
# ═══════════════════════════════════════════════════════════

def register_commands():
    # 插件加载时就在后台把 MCP server 拉起来，避免第一次对话时才卡住
    _ensure_bootstrap()
    return {
        "/mcp_status": mcp_status,
        "/mcp_tool_access": mcp_tool_access,
    }
