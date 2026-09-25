"""历史管理测试：消息落盘 / 统计 / 脏数据去重 / 会话隔离。

对应关键功能里的**历史记录**与工作空间之间的历史隔离。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.history import HistoryManager


@pytest.fixture()
def hm(tmp_path) -> HistoryManager:
    return HistoryManager(history_dir=str(tmp_path / "history"))


class TestCleanDuplicates:
    """_clean_duplicates 会自愈旧版本双写留下的重复消息。"""

    def test_adjacent_duplicates_removed(self):
        msgs = [{"role": "user", "content": "hi"}, {"role": "user", "content": "hi"}]
        assert len(HistoryManager._clean_duplicates(msgs)) == 1

    def test_keeps_the_one_with_time(self):
        msgs = [{"role": "user", "content": "hi"},
                {"role": "user", "content": "hi", "time": "2026-01-01"}]
        cleaned = HistoryManager._clean_duplicates(msgs)
        assert len(cleaned) == 1
        assert cleaned[0].get("time") == "2026-01-01"

    def test_different_content_kept(self):
        msgs = [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]
        assert len(HistoryManager._clean_duplicates(msgs)) == 2

    def test_non_adjacent_duplicates_kept(self):
        msgs = [{"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
                {"role": "user", "content": "a"}]
        assert len(HistoryManager._clean_duplicates(msgs)) == 3


class TestHistoryIO:
    def test_save_and_count(self, hm):
        hm.save_message("user", "你好")
        hm.save_message("assistant", "在的")
        assert hm.count_messages() == 2
        assert hm.count_user_turns() == 1
        # load_recent 返回的是 [user_text, bot_text] 轮次对
        assert hm.load_recent(5) == [["你好", "在的"]]

    def test_load_recent_keeps_last_n_turns(self, hm):
        for i in range(3):
            hm.save_message("user", f"问{i}")
            hm.save_message("assistant", f"答{i}")
        assert hm.load_recent(2) == [["问1", "答1"], ["问2", "答2"]]

    def test_new_session_creates_new_file(self, hm):
        hm.save_message("user", "第一段")
        before = len(hm.list_sessions())
        hm.new_session()
        hm.save_message("user", "第二段")
        assert len(hm.list_sessions()) == before + 1


class TestIsolation:
    def test_different_dirs_are_fully_isolated(self, tmp_path):
        a = HistoryManager(history_dir=str(tmp_path / "a"))
        b = HistoryManager(history_dir=str(tmp_path / "b"))
        a.save_message("user", "只属于A的消息")
        a.save_message("assistant", "好的")
        assert b.count_messages() == 0
        assert a.load_recent(10) == [["只属于A的消息", "好的"]]

    def test_history_is_written_to_disk(self, tmp_path):
        d = tmp_path / "history"
        HistoryManager(history_dir=str(d)).save_message("user", "落盘检查")
        files = list(Path(d).glob("session_*.json"))
        assert files, "历史应写入 session_*.json"
        assert "落盘检查" in files[0].read_text(encoding="utf-8")
