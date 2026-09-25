"""颜色主题与色轮面板测试。

对应需求「前端的外观可以多加一些颜色主题，例如深色，初音色，
可以加个自定义按钮，点击弹出色轮面板，可供用户自定义」。

这里守四件事：

1. **预设完整性** —— 三套预设必须覆盖全部 18 个语义色且色值合法。
   漏一个键不会报错，只会让某个控件用上"上一个主题"的颜色，极难发现。
2. **对比度** —— 预设必须全部达 WCAG AA 4.5:1。初音那套尤其容易踩：
   #39C5BB 是中间调，配白字只有 2.0，所以它用的是深色 `on-solid`。
3. **active tokens 是唯一取色源** —— `build_qss` 与 `speech_bubble` 自绘
   都必须走 `active_tokens()`，否则换肤时自绘控件会留在旧颜色上。
4. **配置往返与容错** —— 自定义主题的 18 个色值要能原样存回读回；
   手改坏的配置不能让程序起不来。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.config.loader import ConfigLoader  # noqa: E402
from core.config.models import AppearanceConfig  # noqa: E402
from ui_qt import theme  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_active_theme():
    """active tokens 是模块级状态，别让一个用例污染后面的。"""
    before = dict(theme.active_tokens())
    yield
    theme.set_active_theme(before)


@pytest.fixture(scope="session")
def qapp():
    pytest.importorskip("PySide6", reason="界面测试需要 PySide6")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


# ── 1. 预设完整性 ──

class TestPresets:
    def test_expected_presets_exist(self):
        assert set(theme.THEMES) == {"亮色", "深色", "初音"}

    @pytest.mark.parametrize("name", list(theme.THEMES))
    def test_preset_covers_every_token(self, name):
        """漏键不会报错，只会让某个控件留在上一个主题的颜色上。"""
        tokens = theme.THEMES[name]
        missing = [k for k in theme.ALL_TOKEN_KEYS if k not in tokens]
        assert not missing, f"{name} 缺令牌：{missing}"

    @pytest.mark.parametrize("name", list(theme.THEMES))
    def test_preset_has_no_unknown_token(self, name):
        extra = [k for k in theme.THEMES[name] if k not in theme.ALL_TOKEN_KEYS]
        assert not extra, f"{name} 有多余键：{extra}"

    @pytest.mark.parametrize("name", list(theme.THEMES))
    def test_preset_colors_are_hex(self, name):
        bad = [k for k, v in theme.THEMES[name].items() if not theme.is_hex_color(v)]
        assert not bad, f"{name} 有非法色值：{bad}"

    def test_token_meta_covers_every_token(self):
        """色轮面板靠 TOKEN_GROUPS 渲染，漏掉的键用户根本改不到。"""
        grouped = [k for _g, keys in theme.TOKEN_GROUPS for k in keys]
        assert sorted(grouped) == sorted(theme.ALL_TOKEN_KEYS)
        assert len(grouped) == len(set(grouped)), "同一个键不能出现在两个分组里"
        missing = [k for k in theme.ALL_TOKEN_KEYS if k not in theme.TOKEN_LABELS]
        assert not missing, f"缺中文名：{missing}"

    def test_dark_and_light_actually_differ(self):
        assert theme.DARK_TOKENS["bg"] != theme.LIGHT_TOKENS["bg"]
        assert theme.DARK_TOKENS["text"] != theme.LIGHT_TOKENS["text"]

    def test_dark_theme_has_light_text_on_dark_bg(self):
        """深色主题最容易犯的错：底色改了、文字还是深色。"""
        assert theme.relative_luminance(theme.DARK_TOKENS["text"]) \
            > theme.relative_luminance(theme.DARK_TOKENS["bg"])


# ── 2. 对比度 ──

class TestContrast:
    def test_black_on_white_is_21(self):
        assert theme.contrast_ratio("#000000", "#FFFFFF") == pytest.approx(21.0, abs=0.01)

    def test_same_color_is_1(self):
        assert theme.contrast_ratio("#39C5BB", "#39C5BB") == pytest.approx(1.0, abs=0.01)

    def test_order_does_not_matter(self):
        assert theme.contrast_ratio("#111113", "#EDEEF0") \
            == pytest.approx(theme.contrast_ratio("#EDEEF0", "#111113"), abs=0.001)

    def test_invalid_color_is_zero_luminance(self):
        assert theme.relative_luminance("not-a-color") == 0.0

    @pytest.mark.parametrize("name", list(theme.THEMES))
    def test_preset_passes_all_checks(self, name):
        failed = [(label, round(ratio, 2), minimum)
                  for label, ratio, minimum, ok in theme.contrast_report(theme.THEMES[name])
                  if not ok]
        assert not failed, f"{name} 对比度不达标：{failed}"

    def test_miku_accent_needs_dark_text(self):
        """初音标志色 #39C5BB 配白字只有 2.0 —— 所以它必须用深色 on-solid。

        这条用例把「为什么初音主题的 on-solid 不是白色」钉住，
        免得日后有人"顺手统一"成白色。
        """
        assert theme.contrast_ratio("#FFFFFF", theme.MIKU_TOKENS["accent"]) < 4.5
        assert theme.contrast_ratio(
            theme.MIKU_TOKENS["on-solid"], theme.MIKU_TOKENS["accent"]) >= 4.5

    def test_report_flags_bad_combination(self):
        """用户有权调出低对比度配色，但报告必须指出来。"""
        tokens = dict(theme.LIGHT_TOKENS)
        tokens["accent"] = "#FFFF00"      # 白字配亮黄 → 必然不达标
        failed = [label for label, _r, _m, ok in theme.contrast_report(tokens) if not ok]
        assert "实色底上的文字 / 主色" in failed


# ── 3. active tokens 是唯一取色源 ──

class TestActiveTokens:
    def test_defaults_to_light(self):
        theme.set_active_theme(theme.LIGHT_TOKENS)
        assert theme.active_tokens() == theme.LIGHT_TOKENS

    def test_switch_changes_active(self):
        theme.set_active_theme(theme.DARK_TOKENS)
        assert theme.active_tokens()["bg"] == theme.DARK_TOKENS["bg"]

    def test_missing_keys_fall_back_to_light(self):
        theme.set_active_theme({"bg": "#123456"})
        assert theme.active_tokens()["bg"] == "#123456"
        assert theme.active_tokens()["text"] == theme.LIGHT_TOKENS["text"]

    @pytest.mark.parametrize("bad", ["", "red", "#12345", "#GGGGGG", None, 123, "#1234567"])
    def test_illegal_values_are_dropped(self, bad):
        """坏色值不该让界面变黑 —— 丢弃并保留亮色。"""
        theme.set_active_theme({"bg": bad})
        assert theme.active_tokens()["bg"] == theme.LIGHT_TOKENS["bg"]

    def test_active_tokens_is_not_shared_reference(self):
        theme.set_active_theme(theme.LIGHT_TOKENS)
        theme.active_tokens()["bg"] = "#000000"
        assert theme.active_tokens()["bg"] == theme.LIGHT_TOKENS["bg"]

    def test_tokens_for_unknown_name_falls_back(self):
        assert theme.tokens_for("不存在的主题") == theme.LIGHT_TOKENS

    def test_tokens_for_returns_copy(self):
        got = theme.tokens_for("深色")
        got["bg"] = "#000000"
        assert theme.THEMES["深色"]["bg"] != "#000000"


class TestMatchPreset:
    def test_matches_exact_preset(self):
        assert theme.match_preset(theme.tokens_for("深色")) == "深色"

    def test_returns_none_when_edited(self):
        tokens = theme.tokens_for("深色")
        tokens["bg"] = "#123456"
        assert theme.match_preset(tokens) is None


class TestBuildQss:
    def test_no_unsubstituted_placeholder(self):
        for name in theme.THEMES:
            qss = theme.build_qss(theme.THEMES[name])
            assert "{t[" not in qss, f"{name} 的 QSS 有未替换的占位符"

    def test_follows_active_theme(self):
        theme.set_active_theme(theme.LIGHT_TOKENS)
        light = theme.build_qss()
        theme.set_active_theme(theme.DARK_TOKENS)
        dark = theme.build_qss()
        assert light != dark
        assert theme.DARK_TOKENS["bg"] in dark
        assert theme.LIGHT_TOKENS["bg"] in light

    def test_explicit_tokens_override_active(self):
        theme.set_active_theme(theme.LIGHT_TOKENS)
        qss = theme.build_qss({"bg": "#ABCDEF"})
        assert "#ABCDEF" in qss

    def test_tooltip_uses_background_not_on_solid(self):
        """提示条是反色。用 on-solid 会在初音主题里变成深底深字。"""
        qss = theme.build_qss(theme.MIKU_TOKENS)
        assert f"background: {theme.MIKU_TOKENS['text']}; color: {theme.MIKU_TOKENS['bg']}" in qss


class TestIsHexColor:
    @pytest.mark.parametrize("good", ["#000000", "#FFFFFF", "#39c5bb", "  #39C5BB  "])
    def test_accepts(self, good):
        assert theme.is_hex_color(good)

    @pytest.mark.parametrize("bad", ["", "#12345", "#1234567", "39C5BB", "#GGGGGG",
                                     None, 123, "rgb(1,2,3)"])
    def test_rejects(self, bad):
        assert not theme.is_hex_color(bad)


# ── 4. 配置往返与容错 ──

class TestAppearanceConfig:
    @pytest.fixture()
    def cfg_dir(self, tmp_path):
        d = tmp_path / "config"
        d.mkdir()
        return d

    def test_defaults_round_trip(self, cfg_dir):
        ConfigLoader(str(cfg_dir)).save_appearance_config(AppearanceConfig())
        got = ConfigLoader(str(cfg_dir)).get_appearance_config()
        assert got.preset == "亮色"
        assert got.effects_preset == "标准"
        assert got.window_opacity == pytest.approx(0.98)

    def test_custom_tokens_round_trip(self, cfg_dir):
        ConfigLoader(str(cfg_dir)).save_appearance_config(AppearanceConfig(
            preset=theme.CUSTOM_THEME_NAME, tokens=theme.MIKU_TOKENS))
        got = ConfigLoader(str(cfg_dir)).get_appearance_config()
        assert got.preset == theme.CUSTOM_THEME_NAME
        assert got.tokens == theme.MIKU_TOKENS

    def test_effects_are_persisted(self, cfg_dir):
        ConfigLoader(str(cfg_dir)).save_appearance_config(AppearanceConfig(
            effects_preset="沉浸", window_opacity=0.92,
            backdrop_blur=16.0, stand_depth=2.0))
        got = ConfigLoader(str(cfg_dir)).get_appearance_config()
        assert got.effects_preset == "沉浸"
        assert got.window_opacity == pytest.approx(0.92)
        assert got.backdrop_blur == pytest.approx(16.0)
        assert got.stand_depth == pytest.approx(2.0)

    def test_preset_does_not_write_token_copy(self, cfg_dir):
        """存预设名就够了 —— 存副本会让预设日后的改进传不到用户那儿。"""
        ConfigLoader(str(cfg_dir)).save_appearance_config(AppearanceConfig(preset="深色"))
        text = (cfg_dir / "appearance.toml").read_text(encoding="utf-8")
        # 只看有没有真的写出这个**段**（header 注释里会提到它，不能算数）
        assert not any(line.strip() == "[theme.tokens]" for line in text.splitlines())
        assert 'preset = "深色"' in text

    def test_missing_file_falls_back_to_defaults(self, cfg_dir):
        assert ConfigLoader(str(cfg_dir)).get_appearance_config().preset == "亮色"

    def test_broken_file_falls_back_to_defaults(self, cfg_dir):
        """配置文件是可以手改的，改坏了不能让程序起不来。"""
        (cfg_dir / "appearance.toml").write_text("这不是 toml {{{", encoding="utf-8")
        assert ConfigLoader(str(cfg_dir)).get_appearance_config().preset == "亮色"

    def test_bad_numeric_falls_back(self, cfg_dir):
        (cfg_dir / "appearance.toml").write_text(
            '[effects]\nwindow_opacity = "很透明"\n', encoding="utf-8")
        assert ConfigLoader(str(cfg_dir)).get_appearance_config().window_opacity \
            == pytest.approx(0.98)

    def test_illegal_token_values_are_dropped_on_save(self, cfg_dir):
        ConfigLoader(str(cfg_dir)).save_appearance_config(AppearanceConfig(
            preset=theme.CUSTOM_THEME_NAME,
            tokens={"bg": "不是颜色", "text": "#123456"}))
        got = ConfigLoader(str(cfg_dir)).get_appearance_config()
        assert "bg" not in got.tokens
        assert got.tokens["text"] == "#123456"


# ── 5. 色轮面板 ──

class TestColorWheel:
    def test_clamps_out_of_range(self, qapp):
        from ui_qt.theme_editor import ColorWheel
        wheel = ColorWheel()
        wheel.set_hsv(999, -5, 300)
        assert wheel.hsv() == (359, 0, 255)

    def test_click_emits_picked(self, qapp):
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtCore import QEvent, Qt
        from ui_qt.theme_editor import ColorWheel
        wheel = ColorWheel()
        got = []
        wheel.picked.connect(lambda h, s: got.append((h, s)))
        # 点在圆心右侧 → 色相 0（红）、饱和度接近满
        center = wheel.width() / 2.0
        pos = QPointF(center + center - 3, center)
        event = QMouseEvent(QEvent.Type.MouseButtonPress, pos, pos,
                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier)
        wheel.mousePressEvent(event)
        assert got, "点击色轮应发出 picked"
        hue, sat = got[-1]
        assert hue in (0, 359)
        assert sat > 200


class TestThemeEditorDialog:
    def test_loads_tokens(self, qapp):
        from ui_qt.theme_editor import ThemeEditorDialog
        dlg = ThemeEditorDialog(theme.tokens_for("初音"))
        assert dlg.tokens() == theme.MIKU_TOKENS

    def test_select_token_syncs_wheel(self, qapp):
        from PySide6.QtGui import QColor
        from ui_qt.theme_editor import ThemeEditorDialog
        dlg = ThemeEditorDialog(theme.tokens_for("初音"))
        dlg._select_token("accent")
        expected = QColor(theme.MIKU_TOKENS["accent"])
        assert dlg.wheel.hsv()[0] == max(0, expected.hue())

    def test_edit_color_updates_tokens_and_row(self, qapp):
        from ui_qt.theme_editor import ThemeEditorDialog
        dlg = ThemeEditorDialog(theme.tokens_for("亮色"))
        dlg._select_token("accent")
        dlg._set_current_color("#123456")
        assert dlg.tokens()["accent"] == "#123456"
        assert dlg._rows["accent"].value.text() == "#123456"

    def test_hex_input_rejects_garbage(self, qapp):
        from ui_qt.theme_editor import ThemeEditorDialog
        dlg = ThemeEditorDialog(theme.tokens_for("亮色"))
        original = dlg.tokens()["bg"]
        dlg.hex_edit.setText("不是颜色")
        dlg._on_hex_edited()
        assert dlg.tokens()["bg"] == original
        assert dlg.hex_edit.text() == original       # 输入框要还原

    def test_load_tokens_drops_illegal_and_fills_missing(self, qapp):
        from ui_qt.theme_editor import ThemeEditorDialog
        dlg = ThemeEditorDialog({"bg": "#123456", "text": "不是颜色"})
        assert dlg.tokens()["bg"] == "#123456"
        assert dlg.tokens()["text"] == theme.LIGHT_TOKENS["text"]

    def test_apply_emits_full_token_table(self, qapp):
        from ui_qt.theme_editor import ThemeEditorDialog
        dlg = ThemeEditorDialog(theme.tokens_for("深色"))
        got = []
        dlg.theme_applied.connect(lambda t: got.append(t))
        dlg._apply()
        assert got and set(got[0]) == set(theme.ALL_TOKEN_KEYS)

    def test_preset_combo_reflects_current_theme(self, qapp):
        """打开初音主题时下拉不能还写着「亮色」。"""
        from ui_qt.theme_editor import ThemeEditorDialog
        dlg = ThemeEditorDialog(theme.tokens_for("初音"))
        assert dlg.preset_combo.currentText() == "初音"

    def test_preset_combo_shows_custom_after_edit(self, qapp):
        from ui_qt.theme_editor import ThemeEditorDialog
        dlg = ThemeEditorDialog(theme.tokens_for("初音"))
        dlg._select_token("bg")
        dlg._set_current_color("#010203")
        assert dlg.preset_combo.currentText() == theme.CUSTOM_THEME_NAME

    def test_reset_from_preset_restores_colors(self, qapp):
        from ui_qt.theme_editor import ThemeEditorDialog
        dlg = ThemeEditorDialog(theme.tokens_for("亮色"))
        dlg._select_token("bg")
        dlg._set_current_color("#010203")
        dlg._reset_from_preset("深色")
        assert dlg.tokens() == theme.DARK_TOKENS

    def test_contrast_label_warns_on_bad_theme(self, qapp):
        from ui_qt.theme_editor import ThemeEditorDialog
        dlg = ThemeEditorDialog(theme.tokens_for("亮色"))
        dlg._select_token("accent")
        dlg._set_current_color("#FFFF00")
        assert "对比度不足" in dlg.contrast_label.text()

    def test_contrast_label_ok_for_presets(self, qapp):
        from ui_qt.theme_editor import ThemeEditorDialog
        for name in theme.THEMES:
            dlg = ThemeEditorDialog(theme.tokens_for(name))
            assert "全部达标" in dlg.contrast_label.text(), name

    def test_preview_uses_real_qss(self, qapp):
        """预览必须与真实界面共用 QSS 生成器，否则「预览好看、应用后变样」。"""
        from ui_qt.theme_editor import ThemeEditorDialog
        dlg = ThemeEditorDialog(theme.tokens_for("深色"))
        assert theme.DARK_TOKENS["bg"] in dlg.preview.styleSheet()


# ── 6. 自绘控件跟随主题 ──

class TestSpeechBubbleFollowsTheme:
    def test_bubble_style_uses_active_tokens(self, qapp):
        from ui_qt.speech_bubble import bubble_style
        theme.set_active_theme(theme.DARK_TOKENS)
        assert theme.DARK_TOKENS["text"] in bubble_style()
        theme.set_active_theme(theme.MIKU_TOKENS)
        assert theme.MIKU_TOKENS["text"] in bubble_style()

    def test_token_helper_uses_active_tokens(self, qapp):
        from ui_qt.speech_bubble import _token
        theme.set_active_theme(theme.MIKU_TOKENS)
        assert _token("bg", "#000000").name().upper() == theme.MIKU_TOKENS["bg"]
