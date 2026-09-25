"""工作空间管理器测试：创建 / 切换 / 隔离 / 持久化 / 删除保护。

对应"关键功能不得破坏"里的**多工作空间 创建/切换/隔离（知识库、历史、记忆、人设）**。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from workspace.manager import WorkspaceManager, _sanitize_id


class TestSanitizeId:
    def test_ascii_name(self):
        assert _sanitize_id("Client A") == "client_a"

    def test_same_chinese_name_is_stable(self):
        assert _sanitize_id("我的空间") == _sanitize_id("我的空间")
        assert _sanitize_id("我的空间").startswith("ws_")

    @pytest.mark.parametrize("evil", ["../../etc/passwd", "..\\..\\windows", "a/../../b"])
    def test_no_path_traversal_chars(self, evil):
        """名称里塞路径符号，不能生成能逃出 workspaces 目录的 ID。"""
        sid = _sanitize_id(evil)
        assert "/" not in sid
        assert "\\" not in sid
        assert ".." not in sid


def _mgr(tmp_path) -> WorkspaceManager:
    return WorkspaceManager(str(tmp_path / "workspaces"))


class TestCrud:
    def test_create_and_list(self, tmp_path):
        m = _mgr(tmp_path)
        ws = m.create(name="客户A")
        assert ws.id
        assert ws.name == "客户A"
        assert [w.id for w in m.list_all()] == [ws.id]

    def test_default_space_uses_friendly_id(self, tmp_path):
        assert _mgr(tmp_path).create(name="默认空间").id == "default"

    def test_duplicate_name_gets_suffix(self, tmp_path):
        m = _mgr(tmp_path)
        first = m.create(name="同一个名字")
        second = m.create(name="同一个名字")
        assert first.id != second.id
        assert second.id.startswith(first.id)

    def test_switch_missing_raises(self, tmp_path):
        with pytest.raises(ValueError):
            _mgr(tmp_path).switch("does_not_exist")

    def test_cannot_delete_active(self, tmp_path):
        m = _mgr(tmp_path)
        ws = m.create(name="活跃空间")
        m.switch(ws.id)
        with pytest.raises(RuntimeError):
            m.delete(ws.id)

    def test_delete_inactive(self, tmp_path):
        m = _mgr(tmp_path)
        a = m.create(name="待删除")
        b = m.create(name="留着")
        m.switch(b.id)
        assert m.delete(a.id) is True
        assert a.id not in [w.id for w in m.list_all()]


class TestIsolation:
    def test_each_workspace_has_own_dirs(self, tmp_path):
        """知识库 / 历史 / 记忆 / 人设必须各空间独立。"""
        m = _mgr(tmp_path)
        a = m.get(m.create(name="空间A").id)
        b = m.get(m.create(name="空间B").id)
        for attr in ("root_dir", "history_dir", "kb_dir", "memory_file"):
            assert getattr(a, attr) != getattr(b, attr), f"{attr} 未隔离"
        assert a.memory_file.endswith("memory.json")

    def test_history_files_do_not_leak(self, tmp_path):
        m = _mgr(tmp_path)
        a = m.get(m.create(name="空间A").id)
        b = m.get(m.create(name="空间B").id)
        Path(a.history_dir).mkdir(parents=True, exist_ok=True)
        (Path(a.history_dir) / "session_1.json").write_text("[]", encoding="utf-8")
        assert list(Path(b.history_dir).glob("session_*.json")) == []


class TestPersistence:
    def test_active_space_survives_restart(self, tmp_path):
        """重启后应恢复到上次活跃的工作空间（冷启动恢复能力）。"""
        root = str(tmp_path / "workspaces")
        m1 = WorkspaceManager(root)
        m1.create(name="空间A")
        target = m1.create(name="空间B")
        m1.switch(target.id)

        m2 = WorkspaceManager(root)  # 模拟重启
        m2.start()
        assert m2.current is not None
        assert m2.current.id == target.id

    def test_start_creates_default_on_empty(self, tmp_path):
        m = _mgr(tmp_path)
        m.start()
        assert m.current is not None
        assert [w.id for w in m.list_all()] == [m.current.id]
