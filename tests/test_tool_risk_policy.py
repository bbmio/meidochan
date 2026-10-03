"""风险工具策略测试：管理器过滤 / 引擎硬门 / 自我认知一致性。

对应设计规格 docs/superpowers/specs/2026-09-28-risk-tools-profile-cards-design.md。
"""
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.plugins.manager import PluginManager  # noqa: E402

FAKE_FS_MAIN = '''
def register_commands():
    return {
        "/fake_read": lambda path="": "read",
        "/fake_write": lambda path="", content="": "written",
        "/fake_open": lambda path="": "opened",
    }


def is_risky_tool(tool_name, arguments):
    if tool_name != "fake_open":
        return False
    return str((arguments or {}).get("path", "")).lower().endswith(".exe")
'''

FAKE_DYN_MAIN = '''
def get_dynamic_tools():
    return [
        {"type": "function", "function": {
            "name": "dyn_safe", "description": "安全", "parameters": {}}},
        {"type": "function", "function": {
            "name": "dyn_risky", "description": "危险", "parameters": {}}},
    ]


def owns_tool(tool_name):
    return tool_name in {"dyn_safe", "dyn_risky"}


def is_risky_tool(tool_name, arguments):
    return tool_name == "dyn_risky"
'''


@pytest.fixture()
def pm(tmp_path) -> PluginManager:
    pdir = tmp_path / "plugins"
    fs = pdir / "fakefs"
    fs.mkdir(parents=True)
    (fs / "main.py").write_text(FAKE_FS_MAIN, encoding="utf-8")
    (fs / "manifest.json").write_text(json.dumps({
        "name": "fakefs", "version": "1.0", "type": "tool",
        "entry": "main.py", "description": "测试用文件插件",
        "tools": [
            {"name": "fake_read", "description": "只读",
             "parameters": {"type": "object", "properties": {}}},
            {"name": "fake_write", "description": "写", "risk": "high",
             "parameters": {"type": "object", "properties": {}}},
            {"name": "fake_open", "description": "打开",
             "parameters": {"type": "object", "properties": {}}},
        ],
    }, ensure_ascii=False), encoding="utf-8")

    dyn = pdir / "fakedyn"
    dyn.mkdir()
    (dyn / "main.py").write_text(FAKE_DYN_MAIN, encoding="utf-8")
    (dyn / "manifest.json").write_text(json.dumps({
        "name": "fakedyn", "version": "1.0", "type": "tool",
        "entry": "main.py", "description": "测试用动态插件",
    }, ensure_ascii=False), encoding="utf-8")

    mgr = PluginManager(str(pdir), disabled=set())
    mgr.discover_and_load()
    return mgr


class TestToolDefinitionsFilter:
    def test_risky_hidden_when_disallowed(self, pm):
        names = {d["function"]["name"] for d in pm.get_tool_definitions(False)}
        assert "fake_write" not in names          # 静态声明 risk=high
        assert "dyn_risky" not in names           # 动态工具条件判定
        assert {"fake_read", "fake_open", "dyn_safe"} <= names

    def test_risky_exposed_when_allowed(self, pm):
        names = {d["function"]["name"] for d in pm.get_tool_definitions(True)}
        assert {"fake_write", "fake_open", "dyn_risky"} <= names

    def test_risk_field_never_sent_to_model(self, pm):
        for d in pm.get_tool_definitions(True):
            assert "risk" not in d
            assert "risk" not in d.get("function", {})


class TestIsToolRisky:
    def test_static_declaration(self, pm):
        assert pm.is_tool_risky("fake_write", {}) is True
        assert pm.is_tool_risky("fake_read", {}) is False

    def test_conditional_predicate(self, pm):
        assert pm.is_tool_risky("fake_open", {"path": "note.txt"}) is False
        assert pm.is_tool_risky("fake_open", {"path": "game.exe"}) is True

    def test_dynamic_tool_via_owner(self, pm):
        assert pm.is_tool_risky("dyn_risky", {}) is True
        assert pm.is_tool_risky("dyn_safe", {}) is False

    def test_unknown_tool_is_not_risky(self, pm):
        assert pm.is_tool_risky("no_such_tool", {}) is False

    def test_predicate_error_is_fail_closed(self, pm, monkeypatch):
        """判定异常不能把工具降级为安全。"""
        mod = pm._load_module("fakefs")

        def boom(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(mod, "is_risky_tool", boom)
        assert pm.is_tool_risky("fake_open", {"path": "note.txt"}) is True


# ── 引擎执行硬门 ──

from core import engine as engine_mod  # noqa: E402


class _PMStub:
    def __init__(self):
        self.executed = []
        self.last_allow = None

    def is_tool_risky(self, tool_name, arguments):
        return tool_name in {"write_file", "delete_file"}

    def execute_tool(self, tool_name, arguments):
        self.executed.append((tool_name, dict(arguments or {})))
        return "（已执行）"

    def get_tool_definitions(self, allow_risky_tools=False):
        self.last_allow = allow_risky_tools
        return []


class _ConfigStub:
    def __init__(self, allow=True, boom=False):
        self._allow = allow
        self._boom = boom

    def get_plugins_config(self):
        if self._boom:
            raise RuntimeError("boom")
        return types.SimpleNamespace(allow_risky_tools=self._allow)


class _EngineStub:
    """把引擎的真实方法挂到最小宿主上。

    方法在实例化时才取 —— 在类体里取的话，方法还没实现时会变成
    「收集错误」，把同一文件里其它已经能跑的用例也一起挡住。
    """

    def __init__(self, allow=True, boom=False):
        self.plugin_manager = _PMStub()
        self.config = _ConfigStub(allow=allow, boom=boom)
        cls = engine_mod.WhaleGirlEngine
        self.tool_executor = cls.tool_executor.__get__(self, cls)
        self._allow_risky_tools = cls._allow_risky_tools.__get__(self, cls)
        self._plugin_tools_for_model = cls._plugin_tools_for_model.__get__(self, cls)


class TestEngineGate:
    def test_blocks_risky_when_disabled(self):
        eng = _EngineStub(allow=False)
        out = eng.tool_executor("write_file", {"path": "x"})
        assert out == engine_mod.RISKY_TOOLS_DISABLED_MSG
        assert eng.plugin_manager.executed == []       # 底层完全没有被调用

    def test_allows_risky_when_enabled(self):
        eng = _EngineStub(allow=True)
        eng.tool_executor("write_file", {"path": "x"})
        assert eng.plugin_manager.executed == [("write_file", {"path": "x"})]

    def test_safe_tool_passes_when_disabled(self):
        eng = _EngineStub(allow=False)
        assert eng.tool_executor("kb_search", {"query": "x"}) == "（已执行）"

    def test_fail_closed_when_config_read_breaks(self):
        eng = _EngineStub(boom=True)
        assert eng.tool_executor("write_file", {"path": "x"}) == engine_mod.RISKY_TOOLS_DISABLED_MSG

    def test_tool_list_reads_policy(self):
        eng = _EngineStub(allow=False)
        eng._plugin_tools_for_model()
        assert eng.plugin_manager.last_allow is False
        eng2 = _EngineStub(allow=True)
        eng2._plugin_tools_for_model()
        assert eng2.plugin_manager.last_allow is True


class TestSelfKnowledgeRespectsPolicy:
    def test_get_plugin_tool_text_forwards_policy(self):
        from core.self_knowledge import get_plugin_tool_text
        pm = _PMStub()
        get_plugin_tool_text(pm, allow_risky_tools=True)
        assert pm.last_allow is True
        get_plugin_tool_text(pm)             # 默认走安全侧
        assert pm.last_allow is False


# ── 真实插件的谓词（上面用的是假插件，这里补上真货） ──

def _load_plugin(name: str):
    """按文件路径加载真实插件模块（不经过 PluginManager，避免拉起全部插件）。"""
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "plugins" / name / "main.py"
    spec = importlib.util.spec_from_file_location(f"real_plugin_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestRealPluginPredicates:
    def test_file_explorer_open_path_is_conditional(self, tmp_path, monkeypatch):
        """只有「真的能打开的可执行类文件」算风险。

        谓词刻意复用 open_path 自己的 `_resolve_open_target`，所以口径完全一致：
        解析失败 / 不在白名单 / 不存在 / 是目录 —— 这些 open_path 本来就会拒绝，
        因此不算风险；只有「存在 + 非目录 + 后缀命中 confirm_extensions」才算。
        """
        mod = _load_plugin("file_explorer")
        # 真实白名单是「桌面 + 项目目录」；测试里放开白名单，专测后缀判定
        monkeypatch.setattr(mod, "is_allowed", lambda _p: True)

        exe = tmp_path / "game.exe"
        exe.write_text("x", encoding="utf-8")
        note = tmp_path / "note.txt"
        note.write_text("x", encoding="utf-8")

        # write/edit/delete 的风险在 manifest 里静态声明，谓词本身只管道 open_path
        assert mod.is_risky_tool("write_file", {"path": str(note)}) is False
        assert mod.is_risky_tool("open_path", {"path": str(note)}) is False
        assert mod.is_risky_tool("open_path", {"path": str(exe)}) is True
        assert mod.is_risky_tool("open_path", {"path": str(tmp_path)}) is False       # 目录
        assert mod.is_risky_tool("open_path", {}) is False
        assert mod.is_risky_tool("open_path", {"path": str(tmp_path / "没这个.exe")}) is False

    def test_mcp_disabled_tool_always_risky(self):
        """disabled_tools 里的工具永远算风险 —— 授权与否不影响。"""
        mod = _load_plugin("mcp_client")
        mod._SERVERS["playwright"] = types.SimpleNamespace(
            cfg={"disabled_tools": ["browser_run_code_unsafe"]})
        assert mod.is_risky_tool("playwright__browser_run_code_unsafe", {}) is True
        assert mod.is_risky_tool("playwright__browser_navigate", {}) is False
        assert mod.is_risky_tool("noserver__x", {}) is False
        assert mod.is_risky_tool("no_sep_tool", {}) is False
