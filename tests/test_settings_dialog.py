"""设置对话框四个配置页的回填与保存测试。

对应需求「做一个配置面板：api / 人设 等等」。
这里守的是最容易静默失效的一环：**页面能不能把真实配置读进控件**，
以及保存时有没有把控件里的值如实交出去。

做法上刻意做到**零副作用**：

- 只读真实 `config/`（FakeEngine 里的 ConfigLoader / ContextEngine 都是真货）；
- 保存路径全部用猴子补丁拦截，只把参数捕获下来断言，绝不写盘。

如果哪天有人把 `_load_*_into_form` 里的字段名写错，或保存时漏传某个字段，
这些用例会红。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# offscreen 必须在导入 QtWidgets 之前设好，否则 CI/无显示环境起不来
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="设置界面测试需要 PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from core.config.loader import ConfigLoader  # noqa: E402
from core.context_engine import ContextEngine  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    """整个会话共用一个 QApplication —— Qt 不允许创建第二个。"""
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeBrain:
    current_model = "测试模型"
    thinking_enabled = False
    reasoning_effort = "medium"

    def get_status(self):
        return "[测试] 就绪"

    def list_local_models(self, base_url=None):
        return []

    def reconfigure_client(self, *a, **k):
        pass


class _FakeEngine:
    """只补 SettingsDialog 真正会碰到的属性。"""

    def __init__(self):
        self.config = ConfigLoader()
        self.context_engine = ContextEngine()
        self.model_config = self.config.get_model_config()
        self.brain = _FakeBrain()


@pytest.fixture()
def dialog(qapp):
    from ui_qt.settings_dialog import SettingsDialog

    return SettingsDialog(_FakeEngine())


@pytest.fixture()
def captured(monkeypatch, dialog):
    """拦截四个保存入口，返回捕获字典（key = section 名）。"""
    box = {}

    def grab(section):
        def _save(*args):
            # save_live2d_config 收 dict，其余收 dataclass，统一存第一个位置参数
            box[section] = args[0] if args else None
            return "（测试拦截，未写盘）"
        return _save

    monkeypatch.setattr(dialog._engine.config, "save_persona_config",
                        grab("persona"))
    monkeypatch.setattr(dialog._engine.config, "save_plugins_config",
                        grab("plugins"))
    monkeypatch.setattr(dialog._engine.config, "save_live2d_config",
                        grab("live2d"))
    monkeypatch.setattr(dialog._engine.context_engine, "save_identity_config",
                        grab("identity"))
    return box


class TestTabs:
    def test_all_tabs_present(self, dialog):
        labels = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
        for want in ("模型", "人设", "身份", "插件", "Live2D", "外观", "日志"):
            assert want in labels, f"缺少标签页 {want}，实际：{labels}"


class TestPersonaTab:
    def test_backfill(self, dialog):
        cfg = dialog._engine.config.get_persona_config()
        assert dialog.persona_name_edit.text() == cfg.name
        assert dialog.persona_greeting_edit.text() == cfg.greeting
        assert dialog.persona_prompt_edit.toPlainText() == cfg.system_prompt

    def test_multiline_prompt_is_not_truncated(self, dialog):
        """人设正文是多行长文本，回填时不能只拿到第一行。"""
        cfg = dialog._engine.config.get_persona_config()
        assert "\n" in dialog.persona_prompt_edit.toPlainText()
        assert len(dialog.persona_prompt_edit.toPlainText()) == len(cfg.system_prompt)

    def test_no_dead_memory_controls(self, dialog):
        """[persona.memory] 已废弃，人设页不应再有「长期记忆 / 注入格式」两个控件。

        它们曾真实存在于界面上，但改了对任何行为都没有影响。
        """
        assert not hasattr(dialog, "persona_memory_check")
        assert not hasattr(dialog, "persona_format_edit")

    def test_save_keeps_full_prompt(self, dialog, captured):
        """保存时正文必须原样带出去（曾担心控件回填/取值不一致）。"""
        original = dialog.persona_prompt_edit.toPlainText()
        dialog._save_persona()
        assert captured["persona"].system_prompt == original

    def test_empty_name_is_rejected(self, dialog, captured):
        dialog.persona_name_edit.setText("   ")
        dialog._save_persona()
        assert "persona" not in captured
        assert "请填写名称" in dialog.persona_status.text()

    def test_emits_config_saved(self, dialog, captured):
        seen = []
        dialog.config_saved.connect(seen.append)
        dialog._save_persona()
        assert seen == ["persona"]


class TestIdentityTab:
    def test_backfill(self, dialog):
        cfg = dialog._engine.context_engine.get_identity_config()
        assert dialog.identity_name_edit.text() == cfg.user_name
        assert dialog.identity_city_edit.text() == cfg.user_city
        assert dialog.identity_lang_combo.currentData() == (cfg.language or "zh")
        assert dialog.pin_list.count() == len(cfg.pins)

    def test_unknown_style_value_is_kept(self, dialog):
        """配置里的值不在候选里时补一项，不能静默丢成默认值。"""
        combo = dialog.identity_style_combo
        dialog._select_combo(combo, "某种没见过的风格")
        assert combo.currentData() == "某种没见过的风格"

    def test_add_and_remove_pin(self, dialog, captured):
        dialog.pin_edit.setText("测试用固定规则")
        dialog.pin_scope_combo.setCurrentIndex(
            dialog.pin_scope_combo.findData("coding"))
        dialog._add_pin_local()

        assert dialog.pin_list.count() == 1
        assert dialog._pins_draft[0]["scope"] == "coding"
        assert dialog.pin_edit.text() == ""        # 添加后清空输入框

        dialog._save_identity()
        saved = captured["identity"]
        assert [p["rule"] for p in saved.pins] == ["测试用固定规则"]

        dialog.pin_list.setCurrentRow(0)
        dialog._remove_pin_local()
        assert dialog.pin_list.count() == 0
        assert dialog._pins_draft == []

    def test_same_second_pins_get_distinct_ids(self, dialog):
        """同一秒内连加多条，ID 不能撞车（否则删一条会删错）。"""
        for i in range(3):
            dialog.pin_edit.setText(f"规则{i}")
            dialog._add_pin_local()
        ids = [p["id"] for p in dialog._pins_draft]
        assert len(set(ids)) == 3, ids

    def test_emits_config_saved(self, dialog, captured):
        seen = []
        dialog.config_saved.connect(seen.append)
        dialog._save_identity()
        assert seen == ["identity"]


class TestPluginsTab:
    def test_discovers_plugins_with_manifest(self, dialog):
        from core.paths import app_path

        expected = {d.name for d in app_path("plugins").iterdir()
                    if d.is_dir() and (d / "manifest.json").exists()}
        assert set(dialog.plugin_checks) == expected
        assert expected, "plugins/ 下应至少有一个带 manifest.json 的插件"

    def test_unchecking_writes_disabled(self, dialog, captured):
        target = sorted(dialog.plugin_checks)[0]
        dialog.plugin_checks[target].setChecked(False)

        dialog._save_plugins()

        assert target in list(captured["plugins"].disabled)

    def test_other_plugins_stay_enabled(self, dialog, captured):
        """取消勾选一个，不能把其余的也写成 enabled=false。"""
        target = sorted(dialog.plugin_checks)[0]
        dialog.plugin_checks[target].setChecked(False)

        dialog._save_plugins()

        saved = captured["plugins"]
        for name in dialog.plugin_checks:
            if name == target:
                continue
            assert (saved.per_plugin.get(name) or {}).get("enabled") is not False

    def test_plugin_params_are_kept(self, dialog, captured):
        """插件参数段不能被插件开关覆盖掉（写回时要带上原参数）。"""
        before = dialog._engine.config.get_plugins_config().per_plugin
        dialog._save_plugins()
        after = captured["plugins"].per_plugin
        for name, params in before.items():
            for key, value in params.items():
                if key == "enabled":
                    continue
                assert after[name][key] == value, f"{name}.{key} 丢了"

    def test_emits_config_saved(self, dialog, captured):
        seen = []
        dialog.config_saved.connect(seen.append)
        dialog._save_plugins()
        assert seen == ["plugins"]


class TestLive2DTab:
    def test_backfill_model_section(self, dialog):
        cfg = dialog._engine.config.get_live2d_config()
        model = cfg.get("model") or {}
        assert dialog.l2d_dir_edit.text() == str(model.get("dir", ""))
        assert dialog.l2d_definition_edit.text() == str(model.get("definition", ""))
        assert dialog.l2d_viewer_edit.text() == str(model.get("viewer", ""))
        assert dialog.l2d_fallback_check.isChecked() \
            == bool(model.get("fallback_to_static", True))

    def test_backfill_interaction_section(self, dialog):
        cfg = dialog._engine.config.get_live2d_config()
        interaction = cfg.get("interaction") or {}
        # 「点击轮换表情」现在是勾选列表：列表控件只管勾选状态，
        # **有序真值**在 _click_selected（勾选顺序 = 轮换顺序）
        assert dialog._click_selected \
            == [str(e) for e in (interaction.get("click_expressions") or [])]
        assert dialog.l2d_click_revert_spin.value() \
            == pytest.approx(float(interaction.get("click_revert_seconds", 2.0)))
        assert dialog.l2d_focus_gain_spin.value() \
            == pytest.approx(float(interaction.get("focus_gain", 0.35)))

    def test_backfill_state_map(self, dialog):
        """六个对话状态的动作/表情都要回填，不能只填上第一行。

        注意这两列是**可编辑下拉框**（不是输入框）：候选来自
        `core/live2d_assets` 扫出来的模型动作/表情，取值用 currentText()。
        """
        cfg = dialog._engine.config.get_live2d_config()
        state_map = cfg.get("state_map") or {}
        assert set(dialog.l2d_state_edits) == set(state_map)
        for state, entry in state_map.items():
            motion_combo, expr_combo = dialog.l2d_state_edits[state]
            assert motion_combo.currentText() == str(entry.get("motion", ""))
            assert expr_combo.currentText() == str(entry.get("expression", ""))

    def test_save_round_trips_string_array(self, dialog, captured):
        """click_expressions 是字符串数组，保存后不能丢（曾经的序列化 bug）。"""
        cfg = dialog._engine.config.get_live2d_config()
        expected = [str(e) for e in (cfg["interaction"]["click_expressions"])]

        dialog._save_live2d()

        assert captured["live2d"]["interaction"]["click_expressions"] == expected

    def test_save_keeps_six_states(self, dialog, captured):
        dialog._save_live2d()
        assert len(captured["live2d"]["state_map"]) == 6

    def test_blank_expression_stays_blank(self, dialog, captured):
        """表情留空 = 回到模型默认脸，不能被写成字符串 'None' 之类。"""
        dialog.l2d_state_edits["idle"][1].setCurrentText("")
        dialog._save_live2d()
        assert captured["live2d"]["state_map"]["idle"]["expression"] == ""

    def test_emits_config_saved(self, dialog, captured):
        seen = []
        dialog.config_saved.connect(seen.append)
        dialog._save_live2d()
        assert seen == ["live2d"]


class TestLogTabConsoleButton:
    """「日志」页的「打开调试窗口」按钮。

    发布版是 GUI 子系统，双击本来就没有黑窗口（`core/console_window.py` 用
    `AllocConsole()` 按需新建）。这个按钮让开发者**不用改配置、不用重启**
    就能把控制台开出来。

    ⚠️ 测试里一律 monkeypatch 掉真正的 `attach_console` ——
    它会 `AllocConsole()`，在测试进程里真的开一个窗口，既吵又会污染输出。
    """

    def _patch(self, monkeypatch, ok=True):
        import core.console_window as cw

        calls = []
        monkeypatch.setattr(cw, "attach_console",
                            lambda title="x": (calls.append(title), ok)[1])
        # 弹窗是模态的，离屏测试里会**永久阻塞**，必须换掉
        warned = []
        monkeypatch.setattr("ui_qt.settings_dialog.QMessageBox.warning",
                            lambda *a, **k: warned.append(a))
        return calls, warned

    def test_button_exists(self, dialog):
        from PySide6.QtWidgets import QPushButton

        texts = [b.text() for b in dialog.findChildren(QPushButton)]
        assert "打开调试窗口" in texts

    def test_click_allocates_console(self, dialog, monkeypatch):
        calls, _warned = self._patch(monkeypatch, ok=True)
        dialog._open_debug_console()
        assert len(calls) == 1

    def test_success_shows_no_dialog(self, dialog, monkeypatch):
        _calls, warned = self._patch(monkeypatch, ok=True)
        dialog._open_debug_console()
        assert warned == []

    def test_failure_warns_but_does_not_crash(self, dialog, monkeypatch):
        _calls, warned = self._patch(monkeypatch, ok=False)
        dialog._open_debug_console()          # 不该抛
        assert len(warned) == 1
        assert "失败" in warned[0][2]

    def test_status_when_config_off(self, dialog, monkeypatch):
        monkeypatch.setattr("core.console_window.allocated_by_us", lambda: False)
        monkeypatch.setattr("core.console_window.console_enabled", lambda: False)
        dialog._refresh_console_status()
        text = dialog.console_status.text()
        assert "未打开" in text
        assert "show_console" in text          # 要告诉用户怎么改成常开

    def test_status_when_config_on(self, dialog, monkeypatch):
        monkeypatch.setattr("core.console_window.allocated_by_us", lambda: False)
        monkeypatch.setattr("core.console_window.console_enabled", lambda: True)
        dialog._refresh_console_status()
        assert "自动打开" in dialog.console_status.text()

    def test_status_when_already_open(self, dialog, monkeypatch):
        monkeypatch.setattr("core.console_window.allocated_by_us", lambda: True)
        monkeypatch.setattr("core.console_window.console_enabled", lambda: False)
        dialog._refresh_console_status()
        text = dialog.console_status.text()
        assert "已打开" in text
        # 配置是 false 但窗口开着 —— 措辞不能反过来讲成「启动时会自动打开」
        assert "不会自动打开" in text

    def test_status_survives_import_failure(self, dialog, monkeypatch):
        """拿不到控制台模块时不该把设置面板拖崩。"""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "core.console_window":
                raise ImportError("模拟缺失")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        dialog._refresh_console_status()
        assert "未打开" in dialog.console_status.text()
