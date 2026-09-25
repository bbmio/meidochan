"""配置「保存 → 生效」链路的回归测试。

覆盖三条容易**静默失效**的路径（都是「界面显示保存成功、实际没生效」那一类）：

1. loader 的 `save_*` / `get_*` 往返 —— `_toml_emit` 曾把字符串数组整段吞掉，
   `click_expressions` 保存后消失，界面上却显示成功；
2. `reload_persona` / `reload_plugins` —— 人设与插件在启动期各有一份快照，
   缓存漏刷就是「改完没反应」；
3. `MainWindow._on_config_saved` 的按段分发。

第 2、3 项用「只带相关属性的 stub」而不是真 Engine：`Engine.__init__` 会向
`core.memory` 注册全局生成器（`set_summary_generator` / `set_extract_generator`），
测完不还原会污染同进程的其他测试。被测方法只碰
`config` / `brain` / `context_engine` / `plugin_manager` / `pm` 这几个属性，
stub 与真实调用路径等价。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.engine as engine_mod  # noqa: E402
from core.config.loader import ConfigLoader  # noqa: E402
from core.config.models import PersonaConfig, PluginsConfig  # noqa: E402
from core.context_engine import ContextEngine  # noqa: E402
from core.paths import APP_DIR  # noqa: E402


@pytest.fixture()
def cfg(tmp_path) -> Path:
    """模拟一份最小配置目录（与真实 config/ 的结构一致）。"""
    d = tmp_path / "config"
    d.mkdir()
    (d / "persona.toml").write_text(
        "[persona]\n"
        'name = "鲸鱼娘"\n'
        'greeting = "噗咕～鲸鱼娘来啦！"\n'
        "\n"
        "[persona.system_prompt]\n"
        'text = "你是鲸鱼娘，说话要诚实。"\n',
        encoding="utf-8",
    )
    (d / "plugins.toml").write_text(
        "[plugins]\n"
        'directory = "plugins"\n'
        "disabled = []\n"
        "\n"
        "[plugins.web_search]\n"
        "enabled = true\n"
        'proxy_http = "http://127.0.0.1:7897"\n',
        encoding="utf-8",
    )
    (d / "live2d.toml").write_text(
        "[model]\n"
        'dir = "assets/live2d/DS鲸鱼娘"\n'
        'definition = "c_0120.model3.json"\n'
        'viewer = "assets/live2d/viewer/index.html"\n'
        "fallback_to_static = true\n"
        "\n"
        "[behavior]\n"
        "clear_expression_on_idle = true\n"
        "\n"
        "[interaction]\n"
        'click_expressions = ["调皮", "吐舌"]\n'
        "click_revert_seconds = 2.0\n"
        "focus_gain = 0.35\n"
        "focus_rest_seconds = 1.2\n"
        "\n"
        "[state_map.idle]\n"
        'motion = "idle"\n'
        'expression = ""\n',
        encoding="utf-8",
    )
    return d


@pytest.fixture()
def plugins_dir(tmp_path, monkeypatch) -> Path:
    """两个假插件 + 把 core.engine 眼里的 plugins/ 指过来。"""
    d = tmp_path / "plugins"
    d.mkdir()
    for name in ("alpha", "beta"):
        sub = d / name
        sub.mkdir()
        (sub / "manifest.json").write_text(
            json.dumps({"name": name, "version": "1.0",
                        "description": f"{name} 描述", "type": "tool",
                        "entry": "main.py"}, ensure_ascii=False),
            encoding="utf-8",
        )
        (sub / "main.py").write_text("# 空插件，仅用于验证加载开关\n", encoding="utf-8")
    monkeypatch.setattr(engine_mod, "app_path", lambda *parts: d)
    return d


class _FakeBrain:
    persona_config = None


class _EngineStub:
    """只带 reload_persona / reload_plugins 用到的属性。"""

    reload_persona = engine_mod.WhaleGirlEngine.reload_persona
    reload_plugins = engine_mod.WhaleGirlEngine.reload_plugins
    _disabled_plugins = engine_mod.WhaleGirlEngine._disabled_plugins

    def __init__(self, config_dir):
        self.config = ConfigLoader(str(config_dir))
        self.context_engine = ContextEngine(str(config_dir))
        self.brain = _FakeBrain()
        # 真实 Engine.__init__ 会把启动期的 persona 快照传给 Brain，
        # 这里必须复现这一步，否则「重载前 brain 还是旧值」的断言就失去意义
        self.brain.persona_config = self.config.get_persona_config()
        self.persona_config = self.brain.persona_config
        self.plugin_manager = None
        self.pm = None


# ── 1. loader 往返 ──

class TestLoaderRoundTrip:
    def test_persona_round_trip(self, cfg):
        """多行 system_prompt 在往返中不能丢掉。"""
        loader = ConfigLoader(str(cfg))
        assert loader.get_persona_config().greeting == "噗咕～鲸鱼娘来啦！"

        loader.save_persona_config(PersonaConfig(
            name="鲸鱼娘",
            greeting="改过的问候",
            system_prompt="第一行\n第二行\n第三行",
        ))

        fresh = ConfigLoader(str(cfg)).get_persona_config()   # 新实例 = 从磁盘读
        assert fresh.greeting == "改过的问候"
        assert fresh.system_prompt == "第一行\n第二行\n第三行"

    def test_persona_quotes_survive(self, cfg):
        """人设里含英文双引号时，persona.toml 不能被写坏。"""
        loader = ConfigLoader(str(cfg))
        cur = loader.get_persona_config()
        cur.system_prompt = '自称要写成 "鲸鱼娘"，不要说"我"'
        loader.save_persona_config(cur)
        assert ConfigLoader(str(cfg)).get_persona_config().system_prompt \
            == '自称要写成 "鲸鱼娘"，不要说"我"'

    def test_live2d_string_array_survives(self, cfg):
        """click_expressions 是字符串数组 —— _toml_emit 曾把它整段吞掉。"""
        loader = ConfigLoader(str(cfg))
        data = loader.get_live2d_config()
        assert data["interaction"]["click_expressions"] == ["调皮", "吐舌"]

        data["interaction"]["click_expressions"] = ["调皮", "吐舌", "爱心眼"]
        loader.save_live2d_config(data)

        fresh = ConfigLoader(str(cfg)).get_live2d_config()
        assert fresh["interaction"]["click_expressions"] == ["调皮", "吐舌", "爱心眼"]
        assert fresh["interaction"]["focus_gain"] == 0.35
        assert fresh["state_map"]["idle"]["motion"] == "idle"

    def test_live2d_absolute_path_is_relativized(self, cfg):
        """项目内的绝对路径回写时必须相对化，否则把用户名写进配置文件。"""
        loader = ConfigLoader(str(cfg))
        data = loader.get_live2d_config()
        data["model"]["dir"] = str(APP_DIR / "assets" / "live2d" / "DS鲸鱼娘")
        loader.save_live2d_config(data)

        text = (cfg / "live2d.toml").read_text(encoding="utf-8")
        assert str(APP_DIR) not in text
        assert 'dir = "assets/live2d/DS鲸鱼娘"' in text

    def test_live2d_foreign_absolute_path_falls_back(self, cfg, tmp_path):
        """项目外的绝对路径回退成默认值，绝不能原样落盘。"""
        loader = ConfigLoader(str(cfg))
        data = loader.get_live2d_config()
        data["model"]["dir"] = str(tmp_path / "别处的模型")
        loader.save_live2d_config(data)

        text = (cfg / "live2d.toml").read_text(encoding="utf-8")
        assert str(tmp_path) not in text
        assert 'dir = "assets/live2d"' in text

    def test_plugins_round_trip(self, cfg):
        """插件参数与禁用清单往返，directory 要回落成相对写法。"""
        loader = ConfigLoader(str(cfg))
        cur = loader.get_plugins_config()
        assert cur.disabled == []

        loader.save_plugins_config(PluginsConfig(
            directory=cur.directory,
            disabled=["beta"],
            per_plugin={**cur.per_plugin, "alpha": {"enabled": False}},
        ))

        fresh = ConfigLoader(str(cfg)).get_plugins_config()
        assert fresh.disabled == ["beta"]
        assert fresh.per_plugin["alpha"]["enabled"] is False
        assert fresh.per_plugin["web_search"]["proxy_http"] == "http://127.0.0.1:7897"
        text = (cfg / "plugins.toml").read_text(encoding="utf-8")
        assert str(APP_DIR) not in text


# ── 2. 重载：缓存与快照 ──

class TestReloadPersona:
    def test_cache_and_snapshot_both_refreshed(self, cfg):
        """只改文件不重载，prompt 仍是旧的；重载后两处持有者都要更新。"""
        eng = _EngineStub(cfg)
        assert "你是鲸鱼娘" in eng.context_engine.build_system_prompt()

        cur = eng.config.get_persona_config()
        eng.config.save_persona_config(PersonaConfig(
            name=cur.name,
            greeting="新问候",
            system_prompt="你是改过的人设。",
        ))

        # 未重载 → ContextEngine 自己的缓存还在，这就是「保存了没生效」的成因
        assert "改过的人设" not in eng.context_engine.build_system_prompt()
        assert eng.brain.persona_config.greeting == "噗咕～鲸鱼娘来啦！"

        eng.reload_persona()

        assert eng.context_engine._persona_cache is None
        assert eng.brain.persona_config.greeting == "新问候"
        assert eng.brain.persona_config is eng.persona_config
        assert "改过的人设" in eng.context_engine.build_system_prompt()

    def test_out_of_band_edit_is_picked_up(self, cfg):
        """直接改文件（绕过 save_*）也要生效 —— 重载以磁盘为准。"""
        eng = _EngineStub(cfg)
        eng.context_engine.build_system_prompt()          # 先预热缓存

        p = cfg / "persona.toml"
        p.write_text(
            p.read_text(encoding="utf-8").replace("你是鲸鱼娘", "手改的人设"),
            encoding="utf-8")

        eng.reload_persona()

        assert "手改的人设" in eng.context_engine.build_system_prompt()
        assert "手改的人设" in eng.brain.persona_config.system_prompt


class TestReloadPlugins:
    def test_disabled_list_is_respected(self, cfg, plugins_dir):
        """disabled 里的插件必须真的不被加载（旧实现完全无视这份清单）。"""
        eng = _EngineStub(cfg)
        base = eng.config.get_plugins_config()
        eng.config.save_plugins_config(PluginsConfig(
            directory=base.directory, disabled=["beta"], per_plugin=base.per_plugin))

        eng.reload_plugins()

        assert eng.plugin_manager.disabled == {"beta"}
        names = set(eng.plugin_manager.get_all_plugins())
        assert "alpha" in names
        assert "beta" not in names

    def test_enabled_false_also_disables(self, cfg, plugins_dir):
        """`enabled = false` 与 disabled 列表两条来源都要算数。"""
        eng = _EngineStub(cfg)
        base = eng.config.get_plugins_config()
        eng.config.save_plugins_config(PluginsConfig(
            directory=base.directory,
            disabled=[],
            per_plugin={**base.per_plugin, "alpha": {"enabled": False}},
        ))

        eng.reload_plugins()

        names = set(eng.plugin_manager.get_all_plugins())
        assert "alpha" not in names
        assert "beta" in names

    def test_pm_alias_is_synced(self, cfg, plugins_dir):
        """pm 是 plugin_manager 的兼容别名，漏同步会让它指向旧管理器。"""
        eng = _EngineStub(cfg)
        old = eng.plugin_manager
        eng.reload_plugins()
        assert eng.plugin_manager is not old
        assert eng.pm is eng.plugin_manager

    def test_out_of_band_disable_is_picked_up(self, cfg, plugins_dir):
        """手工改 plugins.toml 后重载也要生效（loader 缓存必须先失效）。"""
        eng = _EngineStub(cfg)
        eng.config.get_plugins_config()        # 先把旧值落进 loader 缓存

        (cfg / "plugins.toml").write_text(
            "[plugins]\n"
            'directory = "plugins"\n'
            'disabled = ["alpha"]\n',
            encoding="utf-8")

        eng.reload_plugins()

        assert "alpha" not in set(eng.plugin_manager.get_all_plugins())


# ── 3. 保存信号的分发 ──

class _SidebarStub:
    def __init__(self):
        self.msgs = []

    def set_status(self, text):
        self.msgs.append(text)


class _StageStub:
    def __init__(self):
        self.calls = []

    def set_layer(self, slot, source):
        self.calls.append((slot, source))


class _PanelStub:
    def __init__(self):
        self.detached = []

    def set_stand_detached(self, value):
        self.detached.append(value)


class _RegistryStub:
    def create(self, spec):
        return object()


class _PluginManagerStub:
    def __init__(self, image=None):
        self.image = image

    def get_stand_image(self):
        return self.image


def _main_window_stub(engine):
    """把真实的 _on_config_saved / _reload_live2d_stand 挂到一个最小宿主上。

    导入放在函数里：ui_qt.main_window 会拉起 QtWebEngine，
    万一环境缺 GUI 依赖，不希望整个测试文件的收集都失败。
    """
    from ui_qt.main_window import MainWindow

    class _Win:
        _on_config_saved = MainWindow._on_config_saved
        _reload_live2d_stand = MainWindow._reload_live2d_stand

        def __init__(self):
            self.engine = engine
            self.sidebar = _SidebarStub()
            self._live2d_watch = None
            self._live2d = None
            self._stand_window = None
            self.stage = _StageStub()
            self.stage_panel = _PanelStub()
            self._appearance = {"stand_depth": 0.0}
            self._registry = _RegistryStub()
            self.l2d_ok = False
            self.reload_calls = []

        def _load_live2d_stand(self):
            self.reload_calls.append(True)
            return self.l2d_ok

    return _Win()


class TestConfigSavedDispatch:
    def test_identity_clears_cache(self, cfg):
        eng = _EngineStub(cfg)
        win = _main_window_stub(eng)
        eng.context_engine.load_identity()
        assert eng.context_engine._identity_cache is not None

        win._on_config_saved("identity")

        assert eng.context_engine._identity_cache is None
        assert "身份" in win.sidebar.msgs[-1]

    def test_persona_reloads(self, cfg):
        eng = _EngineStub(cfg)
        win = _main_window_stub(eng)
        eng.context_engine.load_persona()
        assert eng.context_engine._persona_cache is not None

        win._on_config_saved("persona")

        assert eng.context_engine._persona_cache is None
        assert eng.brain.persona_config is eng.persona_config
        assert "人设" in win.sidebar.msgs[-1]

    def test_plugins_rebuilds_manager(self, cfg, plugins_dir):
        eng = _EngineStub(cfg)
        win = _main_window_stub(eng)
        old = eng.plugin_manager

        win._on_config_saved("plugins")

        assert eng.plugin_manager is not old
        assert eng.pm is eng.plugin_manager
        assert "插件" in win.sidebar.msgs[-1]

    def test_live2d_stops_old_watch_and_clears_layer(self, cfg, plugins_dir):
        """旧装配必须彻底停掉：就绪轮询还在跑会覆盖新立绘的状态提示。"""
        eng = _EngineStub(cfg)
        win = _main_window_stub(eng)

        class _Timer:
            stopped = 0

            def stop(self):
                self.stopped += 1

        timer = _Timer()
        win._live2d_watch = timer
        win._live2d = object()
        win.l2d_ok = True
        eng.plugin_manager = _PluginManagerStub()

        win._on_config_saved("live2d")

        assert timer.stopped == 1
        assert win._live2d_watch is None
        assert win._live2d is None
        assert win.stage.calls == [("stand", None)]
        assert win.reload_calls == [True]

    def test_live2d_falls_back_to_static_stand(self, cfg, plugins_dir):
        """装配失败时退回插件提供的静态立绘，与启动路径一致。"""
        eng = _EngineStub(cfg)
        win = _main_window_stub(eng)
        win.l2d_ok = False
        eng.plugin_manager = _PluginManagerStub(
            image="plugins/static_stand/stand.png")

        win._on_config_saved("live2d")

        assert len(win.stage.calls) == 2
        assert win.stage.calls[0] == ("stand", None)      # 先清旧层
        assert win.stage.calls[1][0] == "stand"
        assert win.stage.calls[1][1] is not None          # 再挂静态立绘
        assert "回退静态立绘" in win.sidebar.msgs[-1]

    def test_live2d_reloads_while_detached(self, cfg, plugins_dir):
        """立绘独立在桌面时重载：旧源释放、窗口收起，装配好后再独立出去。"""
        eng = _EngineStub(cfg)
        win = _main_window_stub(eng)
        win.l2d_ok = True
        eng.plugin_manager = _PluginManagerStub()

        class _Window:
            def __init__(self):
                self.released = 0
                self.hidden = 0
                self._source = object()

            def isVisible(self):
                return True

            def release_source(self):
                self.released += 1
                source, self._source = self._source, None
                return source

            def hide(self):
                self.hidden += 1

        window = _Window()
        win._stand_window = window
        win._detached = []
        win._on_stand_window_toggled = win._detached.append

        win._on_config_saved("live2d")

        assert window.released == 1
        assert window.hidden == 1
        assert win.stage_panel.detached == [False]
        assert win._detached == [True]

    def test_unknown_section_is_ignored(self, cfg):
        """未知段名不应抛异常（信号参数将来可能扩展）。"""
        eng = _EngineStub(cfg)
        win = _main_window_stub(eng)
        win._on_config_saved("还没实现的段")
