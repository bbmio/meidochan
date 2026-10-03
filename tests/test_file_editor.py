"""文件写入工具测试（write_file / edit_file / delete_file）。

覆盖：白名单拒绝、新建与覆盖、备份生成、非文本拒绝、片段唯一性、
replace_all、删除默认禁用、两段式确认、自身代码校验失败自动回滚。
"""
import sys

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugins.file_explorer import main as fe  # noqa: E402


BASE_CFG = {
    "enabled": True,
    "require_confirm": False,
    "allow_delete": False,
    "backup_dir": ".meido_backups",
    "max_backups": 5,
    "max_bytes": 524288,
    "protected": ["core/**"],
    "verify_command": "",
    "verify_timeout": 30,
}


def _setup(monkeypatch, tmp_path, **overrides):
    """把白名单与项目根都指到 tmp 目录，避免测试污染真实项目。"""
    monkeypatch.setattr(fe, "WHITELIST", [tmp_path], raising=False)
    monkeypatch.setattr(fe, "APP_DIR", tmp_path, raising=False)
    monkeypatch.setattr(fe, "_PENDING_CONFIRM", {}, raising=False)
    cfg = dict(BASE_CFG)
    cfg.update(overrides)
    monkeypatch.setattr(fe, "_write_cfg", lambda: cfg, raising=False)
    return cfg


# ── 写入 ──

def test_write_new_file(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    target = tmp_path / "note.md"
    result = fe.write_file(path=str(target), content="# hi\n")
    assert "已写入" in result
    assert target.read_text(encoding="utf-8") == "# hi\n"


def test_write_creates_backup_on_overwrite(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    target = tmp_path / "note.md"
    target.write_text("old", encoding="utf-8")
    result = fe.write_file(path=str(target), content="new")
    assert "备份" in result
    backups = list((tmp_path / ".meido_backups").rglob("*.bak"))
    assert backups, "覆盖写入应生成备份"
    assert target.read_text(encoding="utf-8") == "new"


def test_write_outside_whitelist_rejected(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    outside = tmp_path.parent / "outside_note.md"
    result = fe.write_file(path=str(outside), content="x")
    assert "权限不足" in result
    assert not outside.exists()


def test_write_non_text_rejected(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    result = fe.write_file(path=str(tmp_path / "a.png"), content="x")
    assert "拒绝写入" in result


def test_write_over_size_rejected(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, max_bytes=16)
    result = fe.write_file(path=str(tmp_path / "big.txt"), content="x" * 100)
    assert "超过上限" in result


# ── 片段替换 ──

def test_edit_unique_snippet(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    target = tmp_path / "a.py"
    target.write_text("def f():\n    return 1\n", encoding="utf-8")
    result = fe.edit_file(path=str(target), old="return 1", new="return 2")
    assert "已修改" in result
    assert target.read_text(encoding="utf-8") == "def f():\n    return 2\n"


def test_edit_ambiguous_snippet_reports_count(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    target = tmp_path / "a.py"
    target.write_text("x = 1\nx = 1\n", encoding="utf-8")
    result = fe.edit_file(path=str(target), old="x = 1", new="x = 2")
    assert "2 次" in result
    assert target.read_text(encoding="utf-8") == "x = 1\nx = 1\n"   # 未改动


def test_edit_replace_all(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    target = tmp_path / "a.py"
    target.write_text("x = 1\nx = 1\n", encoding="utf-8")
    result = fe.edit_file(path=str(target), old="x = 1", new="x = 2", replace_all=True)
    assert "已修改" in result
    assert target.read_text(encoding="utf-8") == "x = 2\nx = 2\n"


def test_edit_missing_snippet_rejected(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    target = tmp_path / "a.py"
    target.write_text("hello\n", encoding="utf-8")
    result = fe.edit_file(path=str(target), old="nope", new="x")
    assert "没找到" in result


def test_edit_nonexistent_file_rejected(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    result = fe.edit_file(path=str(tmp_path / "missing.py"), old="a", new="b")
    assert "文件不存在" in result


# ── 删除 ──

def test_delete_disabled_when_config_off(monkeypatch, tmp_path):
    """allow_delete=false 时拒绝删除（manifest 可随时关掉这个能力）。"""
    _setup(monkeypatch, tmp_path)
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    result = fe.delete_file(path=str(target))
    assert "禁用" in result
    assert target.exists()


def test_delete_enabled_removes_file_and_backs_up(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, allow_delete=True)
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    result = fe.delete_file(path=str(target))
    assert "已删除" in result
    assert not target.exists()
    assert list((tmp_path / ".meido_backups").rglob("*.bak")), "删除前应生成备份"


# ── 两段式确认 ──

def test_require_confirm_two_phase(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, require_confirm=True)
    target = tmp_path / "a.txt"
    first = fe.write_file(path=str(target), content="x")
    assert "二次确认" in first
    assert not target.exists(), "未确认前不应落盘"

    second = fe.write_file(path=str(target), content="x", confirm=True)
    assert "已写入" in second
    assert target.read_text(encoding="utf-8") == "x"


def test_confirm_wrong_content_does_not_write(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, require_confirm=True)
    target = tmp_path / "a.txt"
    fe.write_file(path=str(target), content="aaa")          # 预览
    result = fe.write_file(path=str(target), content="bbb", confirm=True)  # 内容变了
    assert "二次确认" in result
    assert not target.exists()


# ── 自身代码：校验失败自动回滚 ──

def test_protected_write_rolls_back_when_verify_fails(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path,
           verify_command="echo verify-failed && exit 1", verify_timeout=30)
    target = tmp_path / "core" / "engine.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old\n", encoding="utf-8")

    result = fe.write_file(path=str(target), content="new\n")
    assert "未通过" in result
    assert target.read_text(encoding="utf-8") == "old\n", "校验失败应自动回滚"


def test_protected_write_keeps_when_verify_passes(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path,
           verify_command="echo verify-ok && exit 0", verify_timeout=30)
    target = tmp_path / "core" / "engine.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old\n", encoding="utf-8")

    result = fe.write_file(path=str(target), content="new\n")
    assert "校验通过" in result
    assert target.read_text(encoding="utf-8") == "new\n"


# ── 校验环境不具备 → 跳过而不回滚（打包版的核心问题） ──

def test_missing_verify_requirement_skips_and_keeps(monkeypatch, tmp_path):
    """⚠️ 打包版里没有 tests/，校验命令必然失败。

    以前这被当成「校验未通过」→ 每次改 plugins/** 都被回滚，表现成
    「打包版改不了自己的代码」。现在应当**跳过校验且不回滚**。

    这条**不起子进程**：`verify_requires` 在跑命令之前就短路了，
    所以不受沙箱拦 subprocess 的影响（见下面 TestVerifyEnvironment 的说明）。
    """
    _setup(monkeypatch, tmp_path,
           verify_command="python -m pytest tests -q",
           verify_requires=["tests"], verify_timeout=30)
    target = tmp_path / "core" / "engine.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old\n", encoding="utf-8")

    result = fe.write_file(path=str(target), content="new\n")

    assert "跳过校验" in result
    assert "未通过" not in result
    assert target.read_text(encoding="utf-8") == "new\n", "环境不具备时不该回滚"


class TestVerifyEnvironment:
    """校验环境判定的纯函数测试。

    ⚠️ 刻意**不真跑校验命令**：沙箱里 `subprocess.run(shell=True)` 会被拦，
    报 `[WinError 50] / [WinError 6] 句柄无效`。那正是本仓那条「已知环境性抖动」
    （`test_protected_write_keeps_when_verify_passes`）的根因，不是业务缺陷 ——
    所以新测试全部走纯函数，不再往那条不稳定的路上加负担。
    """

    def test_missing_requirement_is_reported(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        reason = fe._verify_missing_reason({"verify_requires": ["tests"]})
        assert "tests" in reason and "不存在" in reason

    def test_present_requirement_passes(self, monkeypatch, tmp_path):
        (tmp_path / "tests").mkdir()
        _setup(monkeypatch, tmp_path)
        assert fe._verify_missing_reason({"verify_requires": ["tests"]}) == ""

    def test_no_requirement_configured(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        assert fe._verify_missing_reason({}) == ""
        assert fe._verify_missing_reason({"verify_requires": []}) == ""

    @pytest.mark.parametrize("code,expected", [
        (0, "pass"),
        (1, "fail"),
        (2, "fail"),
        (3, "fail"),
        (4, "skip"),      # pytest usage error：file or directory not found
        (5, "skip"),      # pytest：没收集到用例
    ])
    def test_exit_code_mapping(self, code, expected):
        assert fe._classify_verify(code) == expected

    def test_empty_command_skips(self, monkeypatch, tmp_path):
        """没配 verify_command → 跳过（这条也不起子进程）。"""
        _setup(monkeypatch, tmp_path, verify_command="")
        assert fe._verify()[0] == fe.VERIFY_SKIP
