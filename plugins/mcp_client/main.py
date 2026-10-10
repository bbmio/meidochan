"""MCP 客户端插件：连接外部 MCP server（stdio / Streamable HTTP / SSE），
把它们的工具动态注册给 AI。

设计要点
--------
1. **异步桥接**：MCP SDK 是 asyncio 的，而插件的 execute_tool 是同步的。
   这里用一条后台线程跑常驻事件循环，把异步调用包成同步接口。
2. **会话归属**：MCP 的传输客户端 / ClientSession 必须在**同一个任务**里进出，
   所以每个 server 由一个常驻协程持有会话，外部调用通过 asyncio.Queue 投递进去
   串行执行（AI 本来就是顺序调用工具，串行没有损失）。
3. **非阻塞启动**：server 在后台线程启动（npx 首次拉包实测约 40s），
   启动期间 get_dynamic_tools() 返回空列表，不会卡住界面。
4. **命名空间**：工具名统一加 `<server>__` 前缀，避免与现有插件工具重名。
5. **受限工具**：config/mcp.toml 的 disabled_tools 默认不暴露；
   妹抖酱需先用 mcp_tool_access 向用户申请，用户同意后授权，下次对话即生效。
6. **传输类型**（`[[servers]]` 的 `type` 字段，默认 `stdio`）：
   - `stdio`：`command` + `args`（本地进程，走 stdin/stdout）
   - `http` ：Streamable HTTP，`url` + 可选 `headers`
   - `sse`  ：旧式 SSE，`url` + 可选 `headers`
   另外可放一份**标准 `mcpServers` 格式**的 `config/mcp_servers.json`
   （Claude Desktop / Cursor 那种 JSON），其条目会与 mcp.toml 合并。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
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
JSON_CONFIG_PARTS = ("config", "mcp_servers.json")
DEFAULT_GRANTS_PARTS = ("data", "mcp_grants.json")
NAME_SEP = "__"
MAX_DESCRIPTION = 1024

#: `type` 的别名 → 归一化后的传输名
_HTTP_ALIASES = {"http", "streamable-http", "streamable_http", "streamablehttp"}


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


def _load_json_raw() -> dict:
    """读取 config/mcp_servers.json 的 `mcpServers` 对象（name → spec）。

    文件不存在 / 解析失败 / 结构不对一律返回空 dict（不致命）。
    """
    path = app_path(*JSON_CONFIG_PARTS)
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception as exc:
        print(f" [mcp_client] config/mcp_servers.json 解析失败，忽略：{exc}")
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        print(" [mcp_client] config/mcp_servers.json 缺少 mcpServers 对象，忽略")
        return {}
    return {str(k): v for k, v in servers.items() if isinstance(v, dict)}


def _load_json_servers() -> List[dict]:
    """把 mcp_servers.json 的条目转成与 mcp.toml `[[servers]]` 一致的结构。

    形如（Claude Desktop / Cursor 的通用格式）：
        {"mcpServers": {
            "utools": {"type": "http",
                       "url": "http://127.0.0.1:3501/mcp",
                       "headers": {"x-mcp-key": "..."}}}}
    每个条目转成与 mcp.toml `[[servers]]` 一致的结构（name 取自 key）。
    """
    out: List[dict] = []
    for name, spec in _load_json_raw().items():
        entry = dict(spec)
        entry["name"] = name
        out.append(entry)
    return out


def _save_json_servers(servers: dict) -> Path:
    """把 `mcpServers` 写回 config/mcp_servers.json（**原子写 + 先备份**）。

    ⚠️ 只写这个 JSON —— **绝不碰 mcp.toml**：TOML 是用户手写的，程序重写会
    丢掉所有注释与排版（tomllib 只读，没有保注释的写回方案）。
    """
    path = app_path(*JSON_CONFIG_PARTS)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            shutil.copyfile(path, path.with_name(path.name + ".bak"))
        except OSError as exc:
            print(f" [mcp_client] 备份 mcp_servers.json 失败（继续写）：{exc}")
    tmp = path.with_name(path.name + ".tmp")
    text = json.dumps({"mcpServers": servers}, ensure_ascii=False, indent=4) + "\n"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)                 # 原子替换，避免写一半被读到
    return path


def _server_sources(cfg: dict) -> Dict[str, str]:
    """每个 server 来自哪个文件（`toml` / `json`）。

    用于提示「同名时 TOML 遮蔽 JSON」——往 JSON 里加了个与 TOML 同名的 server
    却看到旧配置，就是踩了这个。
    """
    out: Dict[str, str] = {}
    for entry in (cfg.get("servers") or []):
        if isinstance(entry, dict) and entry.get("name"):
            out[str(entry["name"]).strip()] = "toml"
    for name in _load_json_raw():
        out.setdefault(name, "json")
    return out


def _server_entries(cfg: dict) -> List[dict]:
    """合并 mcp.toml 的 `[[servers]]` 与 mcp_servers.json 的条目。

    同名时**以 mcp.toml 为准**（TOML 是主配置，JSON 方便直接粘贴）。
    """
    entries: List[dict] = [e for e in (cfg.get("servers") or []) if isinstance(e, dict)]
    seen = {str(e.get("name") or "").strip() for e in entries}
    for entry in _load_json_servers():
        if str(entry.get("name") or "").strip() in seen:
            continue
        entries.append(entry)
    return entries


def _safe_file_part(name: str) -> str:
    """把 server 名压成安全的文件名片段（防目录穿越 / 非法字符）。"""
    keep = [ch if (ch.isalnum() or ch in "-_") else "_" for ch in str(name)]
    return "".join(keep).strip("_") or "server"


def _flatten_exceptions(exc: BaseException) -> List[BaseException]:
    """递归展开 `ExceptionGroup` / `BaseExceptionGroup`，取到最内层的真实异常。"""
    subs = getattr(exc, "exceptions", None)
    if not subs:
        return [exc]
    out: List[BaseException] = []
    for sub in subs:
        out.extend(_flatten_exceptions(sub))
    return out


def _describe_exception(exc: BaseException) -> str:
    """把异常渲染成**露出根因**的可读文本。

    ⚠️ 为什么需要这个：MCP SDK 的传输层跑在 anyio 的 `TaskGroup` 里，一旦握手/请求
    失败，抛出来的是 `ExceptionGroup`，而 `str(ExceptionGroup)` 只会给出
    `"unhandled errors in a TaskGroup (1 sub-exception)"` —— **真正的原因被包在里面**，
    日志和 `mcp_status` 里全都看不到。实测踩到过：真实根因是
    `McpError: Method not found`（uTools 未实现 tools/list），但外面只看得到那句废话。
    这里把嵌套结构展开，逐条列出最内层异常。
    """
    leaves = _flatten_exceptions(exc)
    head = f"{type(exc).__name__}: {exc}"
    if len(leaves) == 1 and leaves[0] is exc:
        return head

    parts = [head]
    seen = set()
    for leaf in leaves:
        text = f"{type(leaf).__name__}: {leaf}"
        if text == head or text in seen:
            continue
        seen.add(text)
        parts.append(f"  ↳ {text}")
    return "\n".join(parts)


def _open_errlog(name: str):
    """给 stdio server 的子进程 stderr 一个**真文件句柄**。

    背景：mcp 的 `stdio_client` 默认把 `sys.stderr` 当子进程 stderr
    （`errlog: TextIO = sys.stderr`），而无控制台运行时 `sys.stderr` 是
    `core.logging_utils._LogStream` —— 它的 `fileno()` 抛 OSError，
    于是子进程创建阶段就报「OSError: 日志流没有文件描述符」，server 永远起不来。
    这里显式指向 data/logs 下的文件：既绕开无 fd 的流，又保住 server 的诊断输出。
    """
    try:
        path = app_path("data", "logs", f"mcp_{_safe_file_part(name)}_stderr.log")
        path.parent.mkdir(parents=True, exist_ok=True)
        return open(path, "ab")          # 二进制追加：交给子进程直接写字节
    except OSError:
        # 连日志文件都开不了时退化为丢弃；但绝不能返回无 fd 的流（否则又崩）
        return open(os.devnull, "wb")


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

    会话由 `_serve()` 这一个常驻协程持有（MCP 的传输客户端 / ClientSession
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
        self._done = threading.Event()       # 常驻协程收尾（stop 之后等它）

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

    def wait_closed(self, timeout: float) -> bool:
        """等常驻协程收尾（应在 `stop()` 之后调用）。

        协程退出时 `async with` 会依次关掉传输上下文 —— stdio 的子进程、
        HTTP 的连接都随之释放。等它，是为了重载时**不泄漏旧进程**。
        """
        return self._done.wait(timeout)

    def transport(self) -> str:
        """归一化传输类型：`stdio` / `http` / `sse`（无法识别时按 stdio）。"""
        raw = str(self.cfg.get("type") or "stdio").strip().lower()
        if raw in _HTTP_ALIASES:
            return "http"
        if raw == "sse":
            return "sse"
        return "stdio"

    async def _serve(self) -> None:
        self._queue = asyncio.Queue()
        self._stop = asyncio.Event()
        try:
            kind = self.transport()
            if kind == "http":
                await self._serve_http()
            elif kind == "sse":
                await self._serve_sse()
            else:
                await self._serve_stdio()
        except Exception as exc:
            self.error = _describe_exception(exc)
        finally:
            self._ready.set()
            self._done.set()

    # ── 各传输 ──

    async def _serve_stdio(self) -> None:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            self.error = f"缺少 mcp 依赖（pip install mcp）：{exc}"
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

        # ⚠️ 必须显式传 errlog：`stdio_client` 的默认值是 `sys.stderr`，
        # 而无控制台运行时它是 `_LogStream`（没有真 fd）→ 子进程创建时
        # `.fileno()` 抛 OSError「日志流没有文件描述符」，server 永远起不来。
        errlog = _open_errlog(self.name)
        try:
            async with stdio_client(params, errlog=errlog) as (read, write):
                await self._session_loop(ClientSession, read, write)
        finally:
            try:
                errlog.close()
            except Exception:
                pass

    async def _serve_http(self) -> None:
        """Streamable HTTP 传输（url + headers）。"""
        url = str(self.cfg.get("url") or "").strip()
        if not url:
            self.error = "type=http 但没有配置 url"
            return
        try:
            import httpx
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
        except ImportError as exc:
            self.error = f"缺少 mcp/http 依赖：{exc}"
            return

        headers = {str(k): str(v) for k, v in (self.cfg.get("headers") or {}).items()}
        timeout = float((_load_config().get("settings") or {}).get("http_timeout", 30) or 30)
        # SDK 新版要求自行传入 httpx.AsyncClient 才能配 headers（旧的
        # streamablehttp_client(url, headers=...) 已 deprecated）。
        client = httpx.AsyncClient(
            headers=headers or None,
            timeout=httpx.Timeout(timeout, read=300.0),
            follow_redirects=True,
        )
        async with client:
            async with streamable_http_client(url, http_client=client) as (read, write, _sid):
                await self._session_loop(ClientSession, read, write)

    async def _serve_sse(self) -> None:
        """旧式 SSE 传输（url + headers）。"""
        url = str(self.cfg.get("url") or "").strip()
        if not url:
            self.error = "type=sse 但没有配置 url"
            return
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client
        except ImportError as exc:
            self.error = f"缺少 mcp 依赖：{exc}"
            return

        headers = {str(k): str(v) for k, v in (self.cfg.get("headers") or {}).items()}
        async with sse_client(url, headers=headers or None) as (read, write):
            await self._session_loop(ClientSession, read, write)

    async def _session_loop(self, session_cls, read, write) -> None:
        """握手 → 列出工具 → 进入常驻循环。三种传输共用。"""
        async with session_cls(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            self.tools = list(listed.tools)
            self.error = None
            self._ready.set()
            await self._pump(session)

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
    for entry in _server_entries(cfg):
        if not entry.get("enabled", True):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        server = _Server(name, entry, _LOOP)
        _SERVERS[name] = server
        try:
            server.start(timeout)
        except Exception as exc:
            server.error = _describe_exception(exc)
        if server.tools:
            print(f" [mcp_client] server [{name}] 就绪（{server.transport()}），"
                  f"工具 {len(server.tools)} 个")
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


#: 重载时单个 server 的握手等待上限（秒）。别用 startup_timeout（默认 120）——
#: 工具调用跑在工作线程里，等太久会把这一轮回复拖死。
_RELOAD_TIMEOUT = 30.0


def _reload_servers(target: str = "") -> str:
    """按磁盘最新配置重建 MCP server —— **不重启整个程序**。

    - `target=""`    ：全部重建
    - `target="xxx"` ：只重建这一个

    ⚠️ 与 `/plugin_reload` 的区别：这里会**真正停掉旧 server**
    （置 `_stop` → 等常驻协程收尾 → 传输上下文随之关闭子进程/连接），
    再按配置重建；不会像全量重载那样留下孤儿线程与子进程。
    """
    global _LOOP
    _ensure_bootstrap()                      # 保证 _LOOP 已建（首次调用时）
    for _ in range(100):                     # 等 _bootstrap 把 _LOOP 建好
        if _LOOP is not None:
            break
        threading.Event().wait(0.05)

    cfg = _load_config()
    entries: Dict[str, dict] = {}
    for entry in _server_entries(cfg):
        name = str(entry.get("name") or "").strip()
        if name:
            entries[name] = entry

    if target:
        if target not in entries:
            avail = ", ".join(sorted(entries)) or "无"
            return f" 配置里没有 server `{target}`（可用：{avail}）"
        wanted = {target: entries[target]}
    else:
        wanted = entries

    # ── 停掉要重建的（全量=全部；单目标=只停目标）──
    stopped: List[_Server] = []
    if target:
        old = _SERVERS.pop(target, None)
        if old is not None:
            stopped.append(old)
    else:
        stopped = list(_SERVERS.values())
        _SERVERS.clear()
    for srv in stopped:
        srv.stop()
    for srv in stopped:
        if not srv.wait_closed(5.0):
            print(f" [mcp_client] server [{srv.name}] 旧会话未在 5s 内退出（继续重建）")

    if _LOOP is None:                        # 极端兜底：bootstrap 还没建循环
        _LOOP = _Loop()

    timeout = min(float((cfg.get("settings") or {}).get("startup_timeout", 120) or 120),
                  _RELOAD_TIMEOUT)
    lines: List[str] = []
    for name, entry in wanted.items():
        if not entry.get("enabled", True):
            lines.append(f"- [{name}] 已禁用，跳过")
            continue
        server = _Server(name, entry, _LOOP)
        _SERVERS[name] = server
        try:
            server.start(timeout)
        except Exception as exc:
            server.error = _describe_exception(exc)
        if server.tools:
            lines.append(f"- [{name}] 就绪（{server.transport()}），"
                         f"工具 {len(server.tools)} 个")
        else:
            lines.append(f"- [{name}] 不可用：{server.error}")
    if not lines:
        lines.append("（没有启用的 server）")
    scope = f"`{target}`" if target else "全部"
    return f" MCP 已重载（{scope}）：\n" + "\n".join(lines)


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


def is_risky_tool(tool_name: str, arguments: dict) -> bool:
    """受限工具（server 的 disabled_tools）永远算风险 —— 即使已授权。

    供 PluginManager 在「风险工具开关关闭」时隐藏/拒绝它们；
    与 mcp_tool_access 的授权记录无关（历史授权不改变风险属性）。
    """
    if NAME_SEP not in tool_name:
        return False
    server_name, raw = tool_name.split(NAME_SEP, 1)
    server = _SERVERS.get(server_name)
    if server is None:
        return False
    return raw in _disabled_of(server)


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
        return f" 调用 MCP 工具 {tool_name} 失败：{_describe_exception(exc)}"
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
    entries = _server_entries(cfg)
    if not entries:
        return (f" 未读到 MCP server 配置（mcp.toml 路径：{app_path(*CONFIG_PARTS)}；"
                f"也可用标准格式的 {app_path(*JSON_CONFIG_PARTS)}），MCP 客户端未启用。")
    if not (cfg.get("settings") or {}).get("enabled", True):
        return " MCP 客户端已在 config/mcp.toml 的 [settings].enabled 中关闭。"

    if not _BOOTSTRAP_DONE.is_set():
        lines = ["MCP 正在后台启动中（首次跑 npx 要下载包，可能需 30-60 秒），请稍后再查。"]
        for entry in entries:
            if entry.get("name"):
                lines.append(f"  - {entry['name']}")
        return "\n".join(lines)

    if not _SERVERS:
        return " 配置里没有启用的 server。"

    grants = _load_grants()
    lines = ["【MCP 状态】"]
    for name, server in _SERVERS.items():
        if server.tools:
            lines.append(f"- [{name}] 就绪（{server.transport()}），工具 {len(server.tools)} 个")
        else:
            lines.append(f"- [{name}] 不可用（{server.transport()}）：{server.error}")
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
# 配置编辑 / 重载（让妹抖酱能自己排查、自己改）
# ═══════════════════════════════════════════════════════════

#: 允许写进 mcp_servers.json 的字段（其余一律丢弃，避免塞进奇怪东西）
_SPEC_KEYS = ("type", "command", "args", "url", "headers", "env",
              "enabled", "disabled_tools", "allowed_tools")


def _normalize_spec(spec: Any) -> Tuple[Optional[dict], Optional[str]]:
    """校验并规整一个 server 配置；返回 `(clean, error)`，二者必有一为 None。"""
    if not isinstance(spec, dict):
        return None, "spec 必须是对象"
    clean = {k: spec[k] for k in _SPEC_KEYS if k in spec}
    kind = str(clean.get("type") or "stdio").strip().lower()
    if kind in _HTTP_ALIASES:
        kind = "http"
    elif kind not in ("stdio", "sse"):
        return None, f"type 只能是 stdio / http / sse，收到 {clean.get('type')!r}"
    clean["type"] = kind
    if kind == "stdio":
        if not str(clean.get("command") or "").strip():
            return None, "stdio 必须提供 command"
        if "args" in clean and not isinstance(clean["args"], list):
            return None, "args 必须是数组"
    elif not str(clean.get("url") or "").strip():
        return None, f"{kind} 必须提供 url"
    for key in ("headers", "env"):
        if key in clean and not isinstance(clean[key], dict):
            return None, f"{key} 必须是对象"
    return clean, None


def _spec_preview(name: str, spec: dict) -> str:
    kind = str(spec.get("type") or "stdio").strip().lower()
    if kind in ("http", "sse"):
        body = f"type={kind} url={spec.get('url')!r}"
        if spec.get("headers"):
            body += f" headers={list(spec['headers'])}"
    else:
        body = (f"type=stdio command={spec.get('command')!r} "
                f"args={spec.get('args') or []}")
    return f"`{name}` → {body}"


def mcp_restart(server: str = "") -> str:
    """按磁盘最新配置重建 MCP server（**不重启整个程序**）。

    改完 `config/mcp.toml` 或 `config/mcp_servers.json` 后调用即可生效；
    server 卡死/启动失败要重试时也用它。不带参数重建全部，带名字只重建那一个。
    旧 server 会被真正停掉（含子进程），不会像全量重载那样泄漏。
    """
    return _reload_servers((server or "").strip())


def mcp_config(action: str = "list", server: str = "",
               spec: Optional[dict] = None, confirm: bool = False) -> str:
    """查看 / 增改 / 删除 MCP server 配置（只写 config/mcp_servers.json）。

    - `action="list"`  ：列出所有 server 及其来源（toml / json）
    - `action="add"`   ：新增或覆盖 `server`（需 `spec`）
    - `action="remove"`：删除 `server`

    写文件前先返回申请文本，用户同意后再带 `confirm=true` 调一次；写完自动重载。
    ⚠️ 不代改 `config/mcp.toml`（用户手写，程序重写会丢注释）。
    """
    _ensure_bootstrap()
    action = (action or "list").strip().lower()
    server = (server or "").strip()
    cfg = _load_config()
    sources = _server_sources(cfg)
    entries = {str(e.get("name") or "").strip(): e for e in _server_entries(cfg)}

    if action == "list":
        if not sources:
            return " 当前没有任何 MCP server。"
        lines = ["【MCP server 配置】"]
        for name in sorted(sources):
            entry = entries.get(name, {})
            state = "启用" if entry.get("enabled", True) else "禁用"
            lines.append(f"- {_spec_preview(name, entry)}（{state}，来自 {sources[name]}）")
        return "\n".join(lines)

    if action not in ("add", "update", "remove"):
        return f" 未知 action：{action}（可用：list / add / remove）"
    if not server:
        return " 请提供 server 名称。"

    raw = _load_json_raw()

    if action == "remove":
        if server not in raw:
            extra = ("（它在 mcp.toml 里，程序不代改手写文件）"
                     if sources.get(server) == "toml" else "")
            return f" `{server}` 不在 config/mcp_servers.json 里{extra}。"
        if not confirm:
            return ("【配置修改申请】\n"
                    f"要从 config/mcp_servers.json 删除 server `{server}`。\n"
                    "（原文件会先备份为 mcp_servers.json.bak）\n\n"
                    "同意的话请回复，我再带 confirm=true 执行。")
        raw.pop(server, None)
        path = _save_json_servers(raw)
        return f" 已从 {path.name} 删除 `{server}`。\n{_reload_servers(server)}"

    # add / update
    if not spec:
        return " add/update 需要 spec（stdio：command/args；http：url/headers）。"
    clean, err = _normalize_spec(spec)
    if err:
        return f" spec 不合法：{err}"

    shadow = ""
    if sources.get(server) == "toml":
        shadow = (f"⚠️ `{server}` 已在 config/mcp.toml 里定义，**TOML 优先**，"
                  f"写进 JSON 不会生效；要改它请直接改 mcp.toml。\n")

    if not confirm:
        return ("【配置修改申请】\n"
                f"将写入 config/mcp_servers.json：\n  {_spec_preview(server, clean)}\n"
                "（原文件会先备份为 mcp_servers.json.bak）\n"
                f"{shadow}\n同意的话请回复，我再带 confirm=true 执行。")

    raw[server] = clean
    path = _save_json_servers(raw)
    return f" 已写入 {path.name}。\n{shadow}{_reload_servers(server)}"


# ═══════════════════════════════════════════════════════════
# 插件注册
# ═══════════════════════════════════════════════════════════

def register_commands():
    # 插件加载时就在后台把 MCP server 拉起来，避免第一次对话时才卡住
    _ensure_bootstrap()
    return {
        "/mcp_status": mcp_status,
        "/mcp_tool_access": mcp_tool_access,
        "/mcp_restart": mcp_restart,
        "/mcp_config": mcp_config,
    }
