"""概览卡并发隔离与同步落盘测试。

对应设计规格 §5：代次隔离旧空间抽取结果、同步原子保存、revision 保护。
全部用事件 / 直接调用来控制时序，不用 sleep。
"""
import json
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import engine as engine_mod        # noqa: E402
from core.memory import profile_cards as pc  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_profile_cards():
    """每个用例前后复位模块全局状态，避免互相污染。"""
    pc.set_profile_file("")
    pc.set_extract_generator(None)
    yield
    pc.set_profile_file("")
    pc.set_extract_generator(None)


class TestEpochIsolation:
    def test_scheduled_extraction_after_switch_is_discarded(self, tmp_path, monkeypatch):
        """A 空间排队的抽取在切到 B 之后才完成 → 结果必须丢弃，不能污染 B。"""
        captured = {}

        class _FakeThread:
            def __init__(self, target, args=(), daemon=None):
                captured["target"] = target
                captured["args"] = args

            def start(self):
                captured["started"] = True

        monkeypatch.setattr(pc, "threading", types.SimpleNamespace(Thread=_FakeThread))
        # 抽取器返回的是「文本」（真实接口如此），不是 dict
        pc.set_extract_generator(
            lambda msgs, max_tokens=300, temperature=0:
            '{"称呼": {"名字": "A 空间的名字"}}')
        pc.set_profile_file(str(tmp_path / "a.json"))
        for _ in range(pc.THRESHOLD):
            pc.schedule_extraction([{"role": "user", "content": "hi"}])
        assert captured["started"] is True

        b = tmp_path / "b.json"
        b.write_text(json.dumps({"cards": {"称呼": {"名字": "B 空间的名字"}}},
                                ensure_ascii=False), encoding="utf-8")
        pc.set_profile_file(str(b))

        captured["target"](*captured["args"])   # A 的「线程」现在才跑

        assert pc.get_cards()["称呼"]["名字"] == "B 空间的名字"


class TestFlush:
    def test_finalize_writes_synchronously(self, tmp_path):
        """finalize_session 返回时文件已完整落盘（不再依赖后台线程）。"""
        pc.set_extract_generator(
            lambda msgs, max_tokens=300, temperature=0: '{"偏好": {"饮料": "茶"}}')
        target = tmp_path / "ws" / "profile_cards.json"    # 目录不存在 → 验证自动创建
        pc.set_profile_file(str(target))

        assert pc.finalize_session([{"role": "user", "content": "hi"}]) is True
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["cards"]["偏好"]["饮料"] == "茶"
        assert not (tmp_path / "ws" / "profile_cards.json.tmp").exists()

    def test_flush_failure_keeps_dirty_and_old_file(self, tmp_path, monkeypatch):
        """写盘失败：原文件不被破坏、dirty 保留，修好后能补写。"""
        target = tmp_path / "a.json"
        target.write_text('{"cards": {"称呼": {"名字": "旧"}}}', encoding="utf-8")
        pc.set_profile_file(str(target))

        pc._cards = {"称呼": {"名字": "新"}}
        pc._dirty = True
        pc._revision += 1

        def boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(pc.os, "replace", boom)
        assert pc.flush_to_disk() is False
        assert json.loads(target.read_text(encoding="utf-8"))["cards"]["称呼"]["名字"] == "旧"
        assert pc._dirty is True

        monkeypatch.undo()
        assert pc.flush_to_disk() is True
        assert json.loads(target.read_text(encoding="utf-8"))["cards"]["称呼"]["名字"] == "新"

    def test_new_changes_during_write_keep_dirty(self, tmp_path, monkeypatch):
        """保存期间产生的新修改，不能被这次保存误清（revision 保护）。"""
        target = tmp_path / "a.json"
        pc.set_profile_file(str(target))
        pc._cards = {"称呼": {"名字": "第一版"}}
        pc._dirty = True
        pc._revision += 1

        real_replace = os.replace

        def replace_with_new_change(src, dst):
            # 模拟：写盘进行中，后台抽取又合并了一条新偏好
            pc._cards.setdefault("偏好", {})["饮料"] = "茶"
            with pc._lock:
                pc._dirty = True
                pc._revision += 1
            real_replace(src, dst)

        monkeypatch.setattr(pc.os, "replace", replace_with_new_change)

        assert pc.flush_to_disk() is True
        assert pc._dirty is True                              # 新修改未被误清
        first = json.loads(target.read_text(encoding="utf-8"))
        assert "饮料" not in first["cards"].get("偏好", {})    # 第一次写的是旧快照

        monkeypatch.undo()
        assert pc.flush_to_disk() is True
        final = json.loads(target.read_text(encoding="utf-8"))
        assert final["cards"]["偏好"]["饮料"] == "茶"


class _WsMgrStub:
    def __init__(self):
        self.switched = []
        self.current = None

    def switch(self, wid):
        self.switched.append(wid)
        return types.SimpleNamespace(id=wid, name="目标空间")


class _HistoryStub:
    def __init__(self):
        self.new_calls = 0

    def load_api_state(self):
        return []

    def new_session(self):
        self.new_calls += 1
        return " 已开启新会话"


class _EngineStub:
    """把引擎的真实方法挂到最小宿主上（实例化时才取，避免收集期报错）。"""

    def __init__(self):
        self.workspace_mgr = _WsMgrStub()
        self.history = _HistoryStub()
        cls = engine_mod.WhaleGirlEngine
        self.switch_workspace = cls.switch_workspace.__get__(self, cls)
        self.new_session = cls.new_session.__get__(self, cls)

    def _bind_workspace(self):
        pass


class TestEngineGuards:
    def test_switch_refused_when_flush_fails(self, monkeypatch):
        eng = _EngineStub()
        monkeypatch.setattr(pc, "flush_to_disk", lambda: False)
        out = eng.switch_workspace("b")
        assert "取消切换" in out
        assert eng.workspace_mgr.switched == []               # 没有真的切换

    def test_switch_proceeds_when_flush_ok(self):
        eng = _EngineStub()
        out = eng.switch_workspace("b")
        assert "已切换" in out
        assert eng.workspace_mgr.switched == ["b"]

    def test_new_session_refused_when_finalize_fails(self, monkeypatch):
        eng = _EngineStub()
        monkeypatch.setattr(pc, "finalize_session", lambda msgs: False)
        out = eng.new_session()
        assert "未创建" in out
        assert eng.history.new_calls == 0

    def test_new_session_proceeds_when_finalize_ok(self, monkeypatch):
        eng = _EngineStub()
        monkeypatch.setattr(pc, "finalize_session", lambda msgs: True)
        eng.new_session()
        assert eng.history.new_calls == 1
