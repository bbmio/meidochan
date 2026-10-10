"""MCP 客户端插件回归测试。

覆盖 2026-10-04 修复的两处缺陷：
1. **stdio 子进程 stderr 无 fd**：`stdio_client` 默认拿 `sys.stderr` 当子进程
   stderr，而无控制台运行时它是 `_LogStream`（`fileno()` 抛 OSError）→
   server 永远起不来。修法是显式传一个**真文件句柄**。
2. **只支持 stdio**：新增 `type = "http"` / `"sse"` 传输，以及标准
   `mcpServers` 格式的 `config/mcp_servers.json`。
"""
import asyncio
import importlib.util
import json
import logging
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_plugin(name: str):
    """按文件路径加载真实插件模块（不经过 PluginManager，避免拉起全部插件）。"""
    path = ROOT / "plugins" / name / "main.py"
    spec = importlib.util.spec_from_file_location(f"real_plugin_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _DummyLoop:
    """`_Server` 只把 loop 存起来；stdio/http 的 _serve 分支用不到它。"""
    loop = None


@pytest.fixture()
def mod(tmp_path, monkeypatch):
    """加载插件，并把 app_path 指向临时目录（避免写进真实 data/）。"""
    m = _load_plugin("mcp_client")
    monkeypatch.setattr(m, "app_path", lambda *parts: tmp_path.joinpath(*parts))
    return m


# ═══════════════════════════════════════════════════════════
# 缺陷 1：errlog 必须是真文件句柄
# ═══════════════════════════════════════════════════════════

class TestLogStreamRootCause:
    def test_logstream_fileno_raises(self):
        """钉住根因：`_LogStream.fileno()` 抛 OSError，正是报错文本的来源。"""
        from core.logging_utils import _LogStream
        stream = _LogStream(logging.getLogger("mcp_test"))
        with pytest.raises(OSError):
            stream.fileno()


class TestOpenErrlog:
    def test_returns_real_fileno(self, mod, tmp_path):
        handle = mod._open_errlog("playwright")
        try:
            assert handle.fileno() >= 0            # 有真 fd → 子进程能创建
        finally:
            handle.close()

    def test_writes_under_data_logs(self, mod, tmp_path):
        handle = mod._open_errlog("my server")
        handle.close()
        logs = tmp_path / "data" / "logs"
        files = list(logs.glob("mcp_*_stderr.log"))
        assert len(files) == 1
        assert "my_server" in files[0].name        # 名字被安全化

    def test_falls_back_to_devnull_when_dir_unwritable(self, mod, monkeypatch):
        """日志目录建不出来时退化为 devnull，但**仍必须有真 fd**。"""
        def boom(*parts):
            raise OSError("read-only")
        monkeypatch.setattr(mod, "app_path", boom)
        handle = mod._open_errlog("x")
        try:
            assert handle.fileno() >= 0
        finally:
            handle.close()


class TestStdioPassesErrlog:
    def test_stdio_client_receives_real_errlog(self, mod, monkeypatch):
        """核心守卫：`_serve_stdio` 必须把**真文件句柄**作为 errlog 传下去。"""
        captured = {}

        class FakeStdio:
            def __init__(self, params, errlog=None):
                captured["errlog"] = errlog
                # errlog 会在 _serve_stdio 的 finally 里被关闭，所以当场取 fd
                captured["fileno"] = None if errlog is None else errlog.fileno()

            async def __aenter__(self):
                return (object(), object())

            async def __aexit__(self, *exc):
                return False

        import mcp.client.stdio as stdio_mod
        monkeypatch.setattr(stdio_mod, "stdio_client", FakeStdio)

        async def noop_loop(self, session_cls, read, write):
            return
        monkeypatch.setattr(mod._Server, "_session_loop", noop_loop)

        server = mod._Server("demo", {"command": "npx", "args": []}, _DummyLoop())
        asyncio.run(server._serve_stdio())

        assert captured.get("errlog") is not None, "必须显式传 errlog，否则默认取 sys.stderr"
        assert captured["fileno"] is not None and captured["fileno"] >= 0, \
            "errlog 必须是真文件句柄"


# ═══════════════════════════════════════════════════════════
# 缺陷 2：传输类型
# ═══════════════════════════════════════════════════════════

class TestTransport:
    @pytest.mark.parametrize("raw,expect", [
        ("stdio", "stdio"),
        ("", "stdio"),
        (None, "stdio"),
        ("HTTP", "http"),
        ("http", "http"),
        ("streamable-http", "http"),
        ("streamable_http", "http"),
        ("streamablehttp", "http"),
        ("sse", "sse"),
        ("SSE", "sse"),
        ("weird", "stdio"),          # 未知按 stdio（最保守）
    ])
    def test_normalize(self, mod, raw, expect):
        cfg = {} if raw is None else {"type": raw}
        assert mod._Server("s", cfg, _DummyLoop()).transport() == expect


class TestDispatch:
    @pytest.mark.parametrize("typ,expect", [
        ("stdio", "stdio"), ("http", "http"), ("sse", "sse"), ("", "stdio"),
    ])
    def test_serve_dispatches(self, mod, monkeypatch, typ, expect):
        calls = []

        async def fake_stdio(self):
            calls.append("stdio")

        async def fake_http(self):
            calls.append("http")

        async def fake_sse(self):
            calls.append("sse")

        monkeypatch.setattr(mod._Server, "_serve_stdio", fake_stdio)
        monkeypatch.setattr(mod._Server, "_serve_http", fake_http)
        monkeypatch.setattr(mod._Server, "_serve_sse", fake_sse)

        server = mod._Server("s", {"type": typ}, _DummyLoop())
        asyncio.run(server._serve())
        assert calls == [expect]

    def test_http_without_url_reports_error(self, mod):
        server = mod._Server("s", {"type": "http"}, _DummyLoop())
        asyncio.run(server._serve())
        assert "url" in (server.error or "")

    def test_sse_without_url_reports_error(self, mod):
        server = mod._Server("s", {"type": "sse"}, _DummyLoop())
        asyncio.run(server._serve())
        assert "url" in (server.error or "")


# ═══════════════════════════════════════════════════════════
# 缺陷 2：标准 mcpServers JSON
# ═══════════════════════════════════════════════════════════

def _write_json(tmp_path: Path, payload) -> None:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (cfg_dir / "mcp_servers.json").write_text(text, encoding="utf-8")


class TestJsonServers:
    def test_missing_file_is_empty(self, mod):
        assert mod._load_json_servers() == []

    def test_broken_json_is_empty(self, mod, tmp_path):
        _write_json(tmp_path, "{ not json ")
        assert mod._load_json_servers() == []

    def test_without_mcpservers_key_is_empty(self, mod, tmp_path):
        _write_json(tmp_path, {"other": 1})
        assert mod._load_json_servers() == []

    def test_parses_standard_format(self, mod, tmp_path):
        _write_json(tmp_path, {"mcpServers": {
            "utools": {"type": "http",
                       "url": "http://127.0.0.1:3501/mcp",
                       "headers": {"x-mcp-key": "secret"}}}})
        got = mod._load_json_servers()
        assert len(got) == 1
        assert got[0]["name"] == "utools"
        assert got[0]["type"] == "http"
        assert got[0]["url"] == "http://127.0.0.1:3501/mcp"
        assert got[0]["headers"] == {"x-mcp-key": "secret"}


class TestServerEntries:
    def test_merge_toml_and_json(self, mod, tmp_path):
        _write_json(tmp_path, {"mcpServers": {"utools": {"type": "http", "url": "http://y"}}})
        cfg = {"servers": [{"name": "playwright", "command": "npx"}]}
        entries = mod._server_entries(cfg)
        assert [e["name"] for e in entries] == ["playwright", "utools"]

    def test_toml_wins_on_duplicate_name(self, mod, tmp_path):
        _write_json(tmp_path, {"mcpServers": {
            "playwright": {"type": "http", "url": "http://from-json"}}})
        cfg = {"servers": [{"name": "playwright", "command": "npx"}]}
        entries = mod._server_entries(cfg)
        assert len(entries) == 1
        assert entries[0]["command"] == "npx"       # TOML 版保留
        assert "url" not in entries[0]

    def test_ignores_non_dict_entries(self, mod, tmp_path):
        cfg = {"servers": [{"name": "ok", "command": "npx"}, "garbage", 42]}
        entries = mod._server_entries(cfg)
        assert [e["name"] for e in entries] == ["ok"]


class TestSafeFilePart:
    @pytest.mark.parametrize("raw,expect", [
        ("playwright", "playwright"),
        ("my server", "my_server"),
        ("../../etc/passwd", "etc_passwd"),
        ("a/b\\c", "a_b_c"),
        ("", "server"),
        ("///", "server"),
    ])
    def test_sanitize(self, mod, raw, expect):
        assert mod._safe_file_part(raw) == expect


# ═══════════════════════════════════════════════════════════
# 异常展开：ExceptionGroup 不能吞掉根因
# ═══════════════════════════════════════════════════════════

class TestExceptionDescription:
    def test_plain_exception(self, mod):
        assert mod._describe_exception(ValueError("boom")) == "ValueError: boom"

    def test_unwraps_single_group(self, mod):
        """回归：`str(ExceptionGroup)` 只有一句废话，根因必须被展开出来。"""
        inner = RuntimeError("Method not found")
        group = ExceptionGroup("unhandled errors in a TaskGroup", [inner])
        text = mod._describe_exception(group)
        assert "TaskGroup" in text
        assert "RuntimeError: Method not found" in text

    def test_unwraps_nested_groups(self, mod):
        leaf = RuntimeError("深层根因")
        outer = ExceptionGroup("outer", [ExceptionGroup("mid", [leaf])])
        assert "RuntimeError: 深层根因" in mod._describe_exception(outer)

    def test_dedupes_repeated_leaves(self, mod):
        group = ExceptionGroup("g", [ValueError("same"), ValueError("same")])
        assert mod._describe_exception(group).count("ValueError: same") == 1

    def test_flatten_returns_leaves(self, mod):
        leaf = KeyError("k")
        group = ExceptionGroup("g", [ExceptionGroup("inner", [leaf])])
        assert mod._flatten_exceptions(group) == [leaf]


# ═══════════════════════════════════════════════════════════
# 配置编辑 / 重载（P5）
# ═══════════════════════════════════════════════════════════

@pytest.fixture()
def cfg_mod(mod, monkeypatch):
    """mcp_config 用：不真的去 bootstrap / 重载（避免拉起 npx）。"""
    monkeypatch.setattr(mod, "_ensure_bootstrap", lambda: None)
    monkeypatch.setattr(mod, "_reload_servers", lambda target="": f"RELOADED:{target}")
    return mod


class TestNormalizeSpec:
    def test_stdio_requires_command(self, mod):
        clean, err = mod._normalize_spec({"type": "stdio"})
        assert clean is None and "command" in err

    def test_http_requires_url(self, mod):
        clean, err = mod._normalize_spec({"type": "http"})
        assert clean is None and "url" in err

    @pytest.mark.parametrize("raw,expect", [
        ("http", "http"), ("streamable-http", "http"), ("streamable_http", "http"),
        ("sse", "sse"), ("stdio", "stdio"), (None, "stdio"),
    ])
    def test_type_normalized(self, mod, raw, expect):
        spec = {"command": "npx"} if expect == "stdio" else {"url": "http://x"}
        if raw is not None:
            spec["type"] = raw
        clean, err = mod._normalize_spec(spec)
        assert err is None and clean["type"] == expect

    def test_unknown_type_rejected(self, mod):
        clean, err = mod._normalize_spec({"type": "carrier-pigeon", "command": "x"})
        assert clean is None and "type" in err

    def test_args_must_be_list(self, mod):
        clean, err = mod._normalize_spec({"command": "npx", "args": "oops"})
        assert clean is None and "args" in err

    def test_headers_must_be_object(self, mod):
        clean, err = mod._normalize_spec({"type": "http", "url": "http://x",
                                          "headers": ["a"]})
        assert clean is None and "headers" in err

    def test_drops_unknown_keys(self, mod):
        clean, _ = mod._normalize_spec({"command": "npx", "evil": "x", "args": []})
        assert "evil" not in clean and clean["command"] == "npx"


class TestJsonRoundTrip:
    def test_save_then_load(self, mod):
        mod._save_json_servers({"utools": {"type": "http", "url": "http://y"}})
        assert mod._load_json_raw()["utools"]["url"] == "http://y"

    def test_backup_created_on_overwrite(self, mod, tmp_path):
        mod._save_json_servers({"a": {"command": "x"}})
        mod._save_json_servers({"a": {"command": "y"}})
        assert (tmp_path / "config" / "mcp_servers.json.bak").exists()

    def test_no_tmp_left_behind(self, mod, tmp_path):
        mod._save_json_servers({"a": {"command": "x"}})
        assert not list((tmp_path / "config").glob("*.tmp"))


class TestServerSources:
    def test_toml_shadows_json(self, mod, monkeypatch):
        mod._save_json_servers({"playwright": {"command": "other"}})
        cfg = {"servers": [{"name": "playwright", "command": "npx"}]}
        assert mod._server_sources(cfg)["playwright"] == "toml"

    def test_json_only(self, mod):
        mod._save_json_servers({"utools": {"type": "http", "url": "http://y"}})
        assert mod._server_sources({})["utools"] == "json"


class TestMcpConfig:
    def test_list_empty(self, cfg_mod):
        assert "没有任何" in cfg_mod.mcp_config(action="list")

    def test_add_needs_confirm_before_writing(self, cfg_mod, tmp_path):
        out = cfg_mod.mcp_config(action="add", server="demo",
                                 spec={"command": "npx", "args": []})
        assert "申请" in out
        assert not (tmp_path / "config" / "mcp_servers.json").exists()

    def test_add_with_confirm_writes_and_reloads(self, cfg_mod):
        out = cfg_mod.mcp_config(action="add", server="demo",
                                 spec={"type": "http", "url": "http://x"},
                                 confirm=True)
        assert "已写入" in out and "RELOADED:demo" in out
        assert cfg_mod._load_json_raw()["demo"]["url"] == "http://x"

    def test_add_invalid_spec_rejected(self, cfg_mod, tmp_path):
        out = cfg_mod.mcp_config(action="add", server="demo",
                                 spec={"type": "http"}, confirm=True)
        assert "不合法" in out
        assert not (tmp_path / "config" / "mcp_servers.json").exists()

    def test_remove_missing(self, cfg_mod):
        assert "不在" in cfg_mod.mcp_config(action="remove", server="ghost")

    def test_remove_with_confirm(self, cfg_mod):
        cfg_mod._save_json_servers({"demo": {"type": "http", "url": "http://x"}})
        out = cfg_mod.mcp_config(action="remove", server="demo", confirm=True)
        assert "已从" in out and "RELOADED:demo" in out
        assert "demo" not in cfg_mod._load_json_raw()

    def test_toml_shadow_warns(self, cfg_mod, monkeypatch):
        monkeypatch.setattr(cfg_mod, "_load_config",
                            lambda: {"servers": [{"name": "playwright", "command": "npx"}]})
        out = cfg_mod.mcp_config(action="add", server="playwright",
                                 spec={"command": "npx"}, confirm=True)
        assert "TOML 优先" in out

    def test_unknown_action(self, cfg_mod):
        assert "未知 action" in cfg_mod.mcp_config(action="destroy")


class TestReloadServers:
    def test_unknown_target_reports_available(self, mod, monkeypatch):
        monkeypatch.setattr(mod, "_ensure_bootstrap", lambda: None)
        monkeypatch.setattr(mod, "_load_config", lambda: {})
        out = mod._reload_servers("ghost")
        assert "没有 server" in out
