"""Live2D 动作 / 表情清单，以及设置界面下拉框的测试。

## 为什么有这组用例

设置界面的「对话状态 → 动作 / 表情」以前是**自由文本输入框**，用户得自己去
翻模型目录、抄文件名。改成下拉框后，候选从 `core/live2d_assets` 来 ——
而那份清单同时喂给**播放器 manifest**，所以两边必须严格一致。

这里守四条：

1. **基名规则必须与播放器一致**：播放器用 `stem()`（去目录、去扩展名）注册动作
   与表情，`state_map` 按这个名字查。两边规则一旦有出入，表现就是
   「配置里填了却不出效果」，界面上完全看不出哪里错 —— 这是最难查的一类。
2. **扫描不能抛异常**：设置界面和自检都可能在没有模型的机器上跑，
   「一个都没有」是正常状态。
3. **刷新下拉框要保留用户已填的值**：换了模型、或者配置里存的是清单里没有的
   名字，刷新不该把它抹掉。
4. **保存时对不上的名字要提示**：只提示不阻止（可能是有意为之），
   但静默保存会让用户对着「配了却没反应」发呆。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
except ImportError:          # 没装 PySide6 时，界面用例由 qapp fixture 跳过
    Qt = None

from core.live2d_assets import (  # noqa: E402
    asset_names,
    duplicate_names,
    scan_model_assets,
    stem_of,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_MODEL_DIR = REPO_ROOT / "assets" / "live2d" / "DS鲸鱼娘"


@pytest.fixture()
def model_dir(tmp_path):
    """造一个可预测的假模型目录（不依赖仓库里那份真实模型）。"""
    root = tmp_path / "模型"
    (root / "motions").mkdir(parents=True)
    for rel in ("motions/idle.motion3.json", "motions/喷水.motion3.json",
                "standalone.motion3.json"):
        (root / rel).write_text("{}", encoding="utf-8")
    for rel in ("吐舌.exp3.json", "sub/星星眼.exp3.json"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}", encoding="utf-8")
    # 干扰项：这些不该被扫进来
    (root / "readme.txt").write_text("x", encoding="utf-8")
    (root / "model3.json").write_text("{}", encoding="utf-8")
    (root / "fake.motion3.json.bak").write_text("{}", encoding="utf-8")
    return root


# ── 1. 扫描 ──

class TestScan:
    def test_returns_relative_posix_paths(self, model_dir):
        motions, expressions = scan_model_assets(model_dir)
        assert motions == ["motions/idle.motion3.json",
                           "motions/喷水.motion3.json",
                           "standalone.motion3.json"]
        assert expressions == ["sub/星星眼.exp3.json", "吐舌.exp3.json"]

    def test_ignores_unrelated_files(self, model_dir):
        motions, expressions = scan_model_assets(model_dir)
        joined = motions + expressions
        assert not any("readme" in p for p in joined)
        assert not any("model3.json" == Path(p).name for p in joined)
        assert not any(p.endswith(".bak") for p in joined)

    def test_missing_dir_is_empty_not_error(self, tmp_path):
        assert scan_model_assets(tmp_path / "没有这个") == ([], [])

    def test_file_instead_of_dir_is_empty(self, model_dir):
        """传进来一个文件而不是目录 —— 当作扫不到，不抛异常。"""
        assert scan_model_assets(model_dir / "readme.txt") == ([], [])

    def test_empty_dir(self, tmp_path):
        empty = tmp_path / "空"
        empty.mkdir()
        assert scan_model_assets(empty) == ([], [])


# ── 2. 基名规则（必须与播放器的 stem() 一致）──

class TestStem:
    @pytest.mark.parametrize("path,expected", [
        ("motions/自拍.motion3.json", "自拍"),
        ("吐舌.exp3.json", "吐舌"),
        ("a/b/c/x.motion3.json", "x"),
        ("love.exp3.json", "love"),
        ("motions\\windows\\style.motion3.json", "style"),   # Windows 分隔符
        ("无扩展名", "无扩展名"),
        ("名字里带.motion3.json后缀", "名字里带.motion3.json后缀"),
    ])
    def test_strips_dir_and_extension(self, path, expected):
        assert stem_of(path) == expected

    def test_case_insensitive_suffix(self):
        """播放器用的是 /i 正则，这里也必须不区分大小写。"""
        assert stem_of("x.MOTION3.JSON") == "x"
        assert stem_of("y.Exp3.Json") == "y"

    def test_asset_names_are_stems(self, model_dir):
        motions, expressions = asset_names(model_dir)
        assert motions == ["idle", "standalone", "喷水"]
        # 按 Unicode 码位排序：吐(U+5410) < 星(U+661F)
        assert expressions == ["吐舌", "星星眼"]

    def test_asset_names_dedupes(self, tmp_path):
        """基名相同的两项在下拉框里长得一模一样，选了也没法区分 —— 必须去重。"""
        root = tmp_path / "m"
        (root / "a").mkdir(parents=True)
        (root / "b").mkdir(parents=True)
        (root / "a" / "idle.motion3.json").write_text("{}", encoding="utf-8")
        (root / "b" / "idle.motion3.json").write_text("{}", encoding="utf-8")
        assert asset_names(root)[0] == ["idle"]

    def test_missing_dir_gives_empty_names(self, tmp_path):
        assert asset_names(tmp_path / "没有") == ([], [])


class TestDuplicates:
    def test_detects_colliding_stems(self, tmp_path):
        root = tmp_path / "m"
        (root / "a").mkdir(parents=True)
        (root / "b").mkdir(parents=True)
        (root / "a" / "idle.motion3.json").write_text("{}", encoding="utf-8")
        (root / "b" / "idle.motion3.json").write_text("{}", encoding="utf-8")
        (root / "吐舌.exp3.json").write_text("{}", encoding="utf-8")
        dup_motions, dup_expressions = duplicate_names(root)
        assert dup_motions == ["idle"]
        assert dup_expressions == []

    def test_no_duplicates(self, model_dir):
        assert duplicate_names(model_dir) == ([], [])

    def test_missing_dir(self, tmp_path):
        assert duplicate_names(tmp_path / "没有") == ([], [])


# ── 3. 与真实模型 / 配置的一致性 ──

@pytest.mark.skipif(not REAL_MODEL_DIR.is_dir(), reason="仓库里没有这份模型")
class TestShippedModel:
    def test_real_model_scans(self):
        motions, expressions = asset_names(REAL_MODEL_DIR)
        assert len(motions) > 0 and len(expressions) > 0
        # 基名里不该残留扩展名或目录分隔符
        assert not any(".json" in n for n in motions + expressions)
        assert not any("/" in n for n in motions + expressions)

    def test_shipped_config_names_all_exist(self):
        """`config/live2d.toml` 里写的动作/表情名，在模型目录里必须都找得到。

        对不上就是「配了却没反应」，而且界面上看不出来 —— 这条用例把它挡住。
        """
        from core.config.loader import ConfigLoader

        motions, expressions = asset_names(REAL_MODEL_DIR)
        cfg = ConfigLoader(str(REPO_ROOT / "config")).get_live2d_config()
        missing = []
        for state, entry in (cfg.get("state_map") or {}).items():
            motion = (entry or {}).get("motion", "")
            expression = (entry or {}).get("expression", "")
            if motion and motion not in motions:
                missing.append(f"{state}.motion = {motion}")
            if expression and expression not in expressions:
                missing.append(f"{state}.expression = {expression}")
        for name in ((cfg.get("interaction") or {}).get("click_expressions") or []):
            if name not in expressions:
                missing.append(f"click_expressions 里的 {name}")
        assert not missing, f"配置里这些名字在模型目录里找不到：{missing}"


# ── 4. 设置界面的下拉框 ──

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
    def __init__(self, config_dir=None):
        from core.config.loader import ConfigLoader
        from core.context_engine import ContextEngine

        self.config = ConfigLoader(config_dir)
        self.context_engine = ContextEngine()
        self.model_config = self.config.get_model_config()
        self.brain = _FakeBrain()


@pytest.fixture(scope="session")
def qapp():
    pytest.importorskip("PySide6", reason="界面测试需要 PySide6")
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


@pytest.fixture()
def dialog(qapp, model_dir):
    from ui_qt.settings_dialog import SettingsDialog

    dlg = SettingsDialog(_FakeEngine())
    # 指到一个可预测的假模型目录（默认那份真实模型不一定在）
    dlg.l2d_dir_edit.setText(str(model_dir))
    dlg._refresh_live2d_asset_lists()
    return dlg


class TestStateMapCombos:
    def test_every_state_has_two_combos(self, dialog):
        assert set(dialog.l2d_state_edits) == {
            "idle", "thinking", "tool", "found", "writing", "error"}
        for motion_combo, expr_combo in dialog.l2d_state_edits.values():
            assert motion_combo.isEditable() and expr_combo.isEditable()

    def test_combos_offer_scanned_names(self, dialog):
        motion_combo, expr_combo = dialog.l2d_state_edits["idle"]
        motion_items = [motion_combo.itemText(i) for i in range(motion_combo.count())]
        expr_items = [expr_combo.itemText(i) for i in range(expr_combo.count())]
        assert "idle" in motion_items and "喷水" in motion_items
        assert "吐舌" in expr_items and "星星眼" in expr_items

    def test_first_item_is_empty_for_unspecified(self, dialog):
        """留空 = 不播动作 / 默认脸，必须能选得到。"""
        for motion_combo, expr_combo in dialog.l2d_state_edits.values():
            assert motion_combo.itemText(0) == ""
            assert expr_combo.itemText(0) == ""

    def test_popup_scrolls_when_long(self, dialog):
        """候选多时弹出框要滚动 —— 本项目模型有 44 个表情。"""
        for combo in (dialog.l2d_state_edits["idle"][0],
                      dialog.l2d_state_edits["idle"][1]):
            assert combo.maxVisibleItems() >= 5

    def test_typing_is_still_allowed(self, dialog):
        """清单里没有的名字仍然能手输，不能把用户卡死。"""
        motion_combo, _ = dialog.l2d_state_edits["idle"]
        motion_combo.setCurrentText("我自己的动作")
        assert motion_combo.currentText() == "我自己的动作"

    def test_no_insert_policy(self, dialog):
        """回车不该把手输内容塞进候选列表，否则列表越用越脏。"""
        from PySide6.QtWidgets import QComboBox
        for motion_combo, expr_combo in dialog.l2d_state_edits.values():
            for combo in (motion_combo, expr_combo):
                assert combo.insertPolicy() == QComboBox.InsertPolicy.NoInsert

    def test_status_reports_counts(self, dialog):
        text = dialog.l2d_assets_status.text()
        assert "3 个动作" in text and "2 个表情" in text

    def test_status_warns_when_dir_missing(self, dialog):
        dialog.l2d_dir_edit.setText("assets/live2d/没有这个模型")
        dialog._refresh_live2d_asset_lists()
        assert "模型目录不存在" in dialog.l2d_assets_status.text()


class TestRefreshKeepsValues:
    def test_refresh_keeps_manually_typed_value(self, dialog):
        """换了模型之后刷新，不该把配置里原有的名字抹掉。"""
        motion_combo, expr_combo = dialog.l2d_state_edits["found"]
        motion_combo.setCurrentText("喷水")
        expr_combo.setCurrentText("清单里没有的表情")
        dialog._refresh_live2d_asset_lists()
        assert motion_combo.currentText() == "喷水"
        assert expr_combo.currentText() == "清单里没有的表情"

    def test_refresh_keeps_value_when_dir_vanishes(self, dialog):
        motion_combo, _ = dialog.l2d_state_edits["idle"]
        motion_combo.setCurrentText("idle")
        dialog.l2d_dir_edit.setText("assets/live2d/不存在")
        dialog._refresh_live2d_asset_lists()
        assert motion_combo.currentText() == "idle"

    def test_changing_dir_replaces_candidates(self, dialog, tmp_path):
        other = tmp_path / "另一个模型"
        other.mkdir()
        (other / "别动.motion3.json").write_text("{}", encoding="utf-8")
        dialog.l2d_dir_edit.setText(str(other))
        dialog._refresh_live2d_asset_lists()
        motion_combo, _ = dialog.l2d_state_edits["idle"]
        items = [motion_combo.itemText(i) for i in range(motion_combo.count())]
        assert "别动" in items
        assert "idle" not in items          # 上一个模型的候选必须消失


class TestSaveAndLoad:
    def test_load_puts_config_values_in_combos(self, qapp, model_dir, tmp_path):
        """配置里的值要真的显示在框里（而不是只存在于某个隐藏变量）。"""
        import shutil
        from core.config.loader import ConfigLoader
        from ui_qt.settings_dialog import SettingsDialog

        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        ConfigLoader(str(cfg_dir)).save_live2d_config({
            "model": {"dir": str(model_dir), "definition": "x.model3.json",
                      "viewer": "v.html"},
            "state_map": {"idle": {"motion": "idle", "expression": "吐舌"}},
        })
        dlg = SettingsDialog(_FakeEngine(str(cfg_dir)))
        motion_combo, expr_combo = dlg.l2d_state_edits["idle"]
        assert motion_combo.currentText() == "idle"
        assert expr_combo.currentText() == "吐舌"

    def test_save_reads_combo_text(self, dialog, monkeypatch):
        captured = {}
        monkeypatch.setattr(dialog._engine.config, "save_live2d_config",
                            lambda payload: captured.update(payload) or "ok")
        dialog.l2d_state_edits["found"][0].setCurrentText("喷水")
        dialog.l2d_state_edits["found"][1].setCurrentText("星星眼")
        dialog._save_live2d()
        assert captured["state_map"]["found"] == {
            "motion": "喷水", "expression": "星星眼"}

    def test_save_warns_on_unknown_name(self, dialog, monkeypatch):
        monkeypatch.setattr(dialog._engine.config, "save_live2d_config",
                            lambda payload: "ok")
        dialog.l2d_state_edits["idle"][0].setCurrentText("拼错的动作")
        dialog._save_live2d()
        assert "拼错的动作" in dialog.live2d_status.text()
        assert "没找到" in dialog.live2d_status.text()

    def test_save_does_not_warn_for_valid_names(self, dialog, monkeypatch):
        monkeypatch.setattr(dialog._engine.config, "save_live2d_config",
                            lambda payload: "ok")
        # 先把所有框清空：fixture 会载入**仓库真实配置**里的表情名
        # （呆呆眼 / 流汗 / 画笔 / 晕晕），那些在假模型目录里当然找不到，
        # 不清掉就会把「有效名字不报警」这条用例污染成必然失败。
        for motion_combo, expr_combo in dialog.l2d_state_edits.values():
            motion_combo.setCurrentText("")
            expr_combo.setCurrentText("")
        dialog.l2d_state_edits["idle"][0].setCurrentText("idle")
        dialog.l2d_state_edits["idle"][1].setCurrentText("吐舌")
        dialog._save_live2d()
        assert "没找到" not in dialog.live2d_status.text()

    def test_unknown_names_helper_skips_missing_dir(self, dialog):
        """扫不到模型目录时不下结论，避免误报。"""
        dialog.l2d_dir_edit.setText("assets/live2d/不存在")
        assert dialog._unknown_live2d_names(
            {"idle": {"motion": "随便", "expression": "随便"}}) == []

    def test_unknown_names_helper_ignores_empty(self, dialog):
        assert dialog._unknown_live2d_names(
            {"idle": {"motion": "", "expression": ""}}) == []


# ── 5. 「点击轮换表情」勾选列表 ──

class TestClickExpressionList:
    """这一栏以前是逗号分隔的文本框，同样得去翻文件抄名字 —— 现在改成勾选列表。

    顺序有意义（点击立绘时按这个顺序轮换），所以**有序真值**单独维护，
    不依赖列表控件的位置。
    """

    def _item_names(self, dialog):
        lst = dialog.l2d_click_expr_list
        return [lst.item(i).data(Qt.ItemDataRole.UserRole) for i in range(lst.count())]

    def _set_checked(self, dialog, name, checked=True):
        lst = dialog.l2d_click_expr_list
        for i in range(lst.count()):
            if lst.item(i).data(Qt.ItemDataRole.UserRole) == name:
                lst.item(i).setCheckState(
                    Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
                return
        raise AssertionError(f"列表里没有 {name}")

    def test_list_holds_all_expressions(self, dialog):
        names = self._item_names(dialog)
        assert "吐舌" in names and "星星眼" in names
        assert len(names) == len(set(names))

    def test_list_scrolls(self, dialog):
        """44 个表情必须能滚 —— 限高后超出部分自动出滚动条。"""
        assert dialog.l2d_click_expr_list.maximumHeight() > 0
        assert dialog.l2d_click_expr_list.maximumHeight() <= 260

    def test_config_selection_is_checked(self, dialog, tmp_path):
        """配置里的 click_expressions 要真的勾上（而不是只存在某个变量里）。"""
        from core.config.loader import ConfigLoader
        from ui_qt.settings_dialog import SettingsDialog

        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        ConfigLoader(str(cfg_dir)).save_live2d_config({
            "model": {"dir": str(dialog.l2d_dir_edit.text())},
            "interaction": {"click_expressions": ["吐舌", "星星眼"]},
        })
        dlg = SettingsDialog(_FakeEngine(str(cfg_dir)))
        lst = dlg.l2d_click_expr_list
        checked = {lst.item(i).data(Qt.ItemDataRole.UserRole)
                   for i in range(lst.count())
                   if lst.item(i).checkState() == Qt.CheckState.Checked}
        assert checked == {"吐舌", "星星眼"}

    def test_toggle_appends_in_order(self, dialog):
        """勾选顺序 = 轮换顺序，先勾的排在前面。"""
        dialog._click_selected = []
        dialog._fill_click_expr_list(asset_names(dialog.l2d_dir_edit.text())[1])
        self._set_checked(dialog, "星星眼")
        self._set_checked(dialog, "吐舌")
        assert dialog._click_selected == ["星星眼", "吐舌"]

    def test_uncheck_removes(self, dialog):
        dialog._click_selected = ["吐舌", "星星眼"]
        dialog._fill_click_expr_list(["吐舌", "星星眼"])
        self._set_checked(dialog, "吐舌", checked=False)
        assert dialog._click_selected == ["星星眼"]

    def test_status_shows_order(self, dialog):
        dialog._click_selected = ["吐舌", "星星眼"]
        dialog._refresh_click_expr_status()
        text = dialog.l2d_click_expr_status.text()
        assert "吐舌 → 星星眼" in text

    def test_status_when_empty(self, dialog):
        dialog._click_selected = []
        dialog._refresh_click_expr_status()
        assert "未选择" in dialog.l2d_click_expr_status.text()

    def test_save_writes_ordered_list(self, dialog, monkeypatch):
        captured = {}
        monkeypatch.setattr(dialog._engine.config, "save_live2d_config",
                            lambda payload: captured.update(payload) or "ok")
        dialog._click_selected = ["星星眼", "吐舌"]
        dialog._save_live2d()
        assert captured["interaction"]["click_expressions"] == ["星星眼", "吐舌"]

    def test_unknown_names_are_preserved_not_dropped(self, dialog):
        """配置里有、但当前模型目录里没有的名字，**不能**因为重建列表而丢失。

        丢掉它就是静默数据丢失：用户一保存，配置里那几项就没了。
        """
        dialog._click_selected = ["吐舌", "上个模型才有的表情"]
        dialog._fill_click_expr_list(["吐舌", "星星眼"])
        names = self._item_names(dialog)
        assert "上个模型才有的表情" in names          # 仍在列表里（带标注）
        assert dialog._click_selected == ["吐舌", "上个模型才有的表情"]
        # 而且它是勾上的、顺序也在
        assert dialog._click_selected.index("上个模型才有的表情") == 1

    def test_unknown_names_are_marked(self, dialog):
        dialog._click_selected = ["上个模型才有的表情"]
        dialog._fill_click_expr_list(["吐舌"])
        lst = dialog.l2d_click_expr_list
        marked = [lst.item(i).text() for i in range(lst.count())
                  if lst.item(i).data(Qt.ItemDataRole.UserRole) == "上个模型才有的表情"]
        assert marked and "模型里没有" in marked[0]

    def test_refresh_keeps_selection(self, dialog):
        """换模型目录重扫时，已选内容要留住。"""
        dialog._click_selected = ["吐舌"]
        dialog._refresh_live2d_asset_lists()
        assert dialog._click_selected == ["吐舌"]

    def test_list_order_is_stable(self, dialog):
        """列表顺序不因为勾选而跳动 —— 44 项里勾一下就把该项弹到顶部会让人找不着北。"""
        before = self._item_names(dialog)
        self._set_checked(dialog, "吐舌")
        assert self._item_names(dialog) == before


# ── 6. manifest 与下拉框同源 ──

class TestManifestSharesSource:
    def test_manifest_motions_match_scan(self, model_dir):
        """播放器 manifest 里的动作清单必须就是下拉框那份（同一函数）。"""
        from ui_qt.media.sources.live2d_source import Live2DSource

        spec = type("S", (), {"path": model_dir / "viewer.html",
                              "params": {"model": str(model_dir / "x.model3.json")}})()
        source = Live2DSource.__new__(Live2DSource)
        source.spec = spec
        import base64
        import json

        raw = source._build_manifest(model_dir / "x.model3.json")
        padded = raw + "=" * (-len(raw) % 4)
        manifest = json.loads(base64.urlsafe_b64decode(padded))
        expected_motions, expected_expressions = scan_model_assets(model_dir)
        assert manifest["motions"] == expected_motions
        assert manifest["expressions"] == expected_expressions
