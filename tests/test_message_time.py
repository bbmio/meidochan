"""对话消息时间戳测试。

对应需求「现在的对话消息没有时间戳，加个时间戳比较好」。

背景（改动前的事实，别被误导）：
- `HistoryManager.save_message()` 是唯一写 `time` 的函数，但**生产路径不调用它**，
  落盘统一走 `save_api_state(api_state)`，而 api_state 只有 `{role, content}`。
  于是真实会话文件里从来没有 `time`，`history.py` 的格式注释是错的。
- 界面侧 `MessageBubble` 也没有任何时间渲染。

所以这条链路有两段要守：
1. **写盘**：time 要真的落下去，而且重复保存不能把旧时间刷成新时间。
2. **发送**：`time` 是本地字段，绝不能跟着消息发给模型（brain 原样透传 messages）。

再加一段界面：解析失败/为空时**不显示**，而不是拿别的时刻冒充。
"""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.brain import _api_messages  # noqa: E402
from core.history import (  # noqa: E402
    STAMP_FORMAT,
    HistoryManager,
    now_stamp,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ui_qt.chat_view import ChatView, MessageBubble, format_time  # noqa: E402


# ── 1. 时间格式化 ──

class TestFormatTime:
    def test_today_shows_hh_mm_only(self):
        today = datetime.now().strftime(STAMP_FORMAT)
        assert format_time(today) == datetime.now().strftime("%H:%M")

    def test_other_day_in_same_year_shows_month_day(self):
        yesterday = datetime.now() - timedelta(days=1)
        out = format_time(yesterday.strftime(STAMP_FORMAT))
        assert len(out) == 11          # "MM-DD HH:MM"
        assert out == yesterday.strftime("%m-%d %H:%M")

    def test_other_year_shows_full_date(self):
        stamp = (datetime.now() - timedelta(days=400)).strftime(STAMP_FORMAT)
        assert format_time(stamp) == (datetime.now() - timedelta(days=400)) \
            .strftime("%Y-%m-%d %H:%M")

    @pytest.mark.parametrize("bad", ["", None, "not-a-time", "2026/09/24 15:30", "15:30"])
    def test_unparsable_returns_empty(self, bad):
        """旧会话文件没有 time —— 那就不显示，绝不拿别的时刻冒充。"""
        assert format_time(bad) == ""

    def test_surrounding_whitespace_is_tolerated(self):
        today = datetime.now().strftime(STAMP_FORMAT)
        assert format_time(f"  {today}  ") == format_time(today)


class TestNowStamp:
    def test_round_trips_through_stamp_format(self):
        """now_stamp() 必须能被 format_time 解析 —— 两边共用 STAMP_FORMAT。"""
        stamp = now_stamp()
        assert datetime.strptime(stamp, STAMP_FORMAT)
        assert format_time(stamp) != ""


# ── 2. 写盘：补 time 且不覆盖 ──

class TestStampMissingTime:
    def test_fills_missing(self):
        out = HistoryManager._stamp_missing_time(
            [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])
        assert all(m.get("time") for m in out)

    def test_existing_time_is_preserved(self):
        """反复保存不能把旧时间刷成新时间 —— 否则历史消息的日期会整体漂移。"""
        old = "2001-02-03 04:05:06"
        out = HistoryManager._stamp_missing_time(
            [{"role": "user", "content": "a", "time": old},
             {"role": "assistant", "content": "b"}])
        assert out[0]["time"] == old
        assert out[1]["time"] != old

    def test_returns_same_object_when_nothing_missing(self):
        """快路径：一条都不缺时不该重建列表（每次落盘都复制一遍没意义）。"""
        msgs = [{"role": "user", "content": "a", "time": now_stamp()}]
        assert HistoryManager._stamp_missing_time(msgs) is msgs

    def test_non_dict_entries_do_not_crash(self):
        out = HistoryManager._stamp_missing_time(["raw", {"role": "user", "content": "a"}])
        assert out[0] == "raw"
        assert out[1].get("time")

    def test_does_not_mutate_input(self):
        msgs = [{"role": "user", "content": "a"}]
        HistoryManager._stamp_missing_time(msgs)
        assert "time" not in msgs[0]


class TestHistoryRoundTrip:
    @pytest.fixture()
    def hm(self, tmp_path):
        return HistoryManager(str(tmp_path / "history"))

    def test_save_api_state_writes_time(self, hm):
        hm.save_api_state([{"role": "user", "content": "你好"}])
        on_disk = json.loads(hm.current_file.read_text(encoding="utf-8"))
        assert on_disk[0].get("time")

    def test_get_messages_exposes_time(self, hm):
        """界面回放走 get_messages()，时间必须能从这条路拿到。"""
        hm.save_api_state([{"role": "user", "content": "你好"}])
        assert hm.get_messages()[0].get("time")

    def test_resaving_keeps_original_time(self, hm):
        hm.save_api_state([{"role": "user", "content": "你好"}])
        first = hm.get_messages()[0]["time"]

        # 第二轮：老消息原样带回来 + 新增一条
        state = hm.get_messages()
        state.append({"role": "assistant", "content": "在的"})
        hm.save_api_state(state)

        after = hm.get_messages()
        assert after[0]["time"] == first          # 旧时间没被刷新
        assert after[1].get("time")


# ── 3. 发送：本地字段必须剥离 ──

class TestApiMessages:
    def test_strips_time(self):
        out = _api_messages([{"role": "user", "content": "a", "time": now_stamp()}])
        assert out == [{"role": "user", "content": "a"}]

    def test_fast_path_returns_same_object(self):
        msgs = [{"role": "user", "content": "a"}]
        assert _api_messages(msgs) is msgs

    def test_keeps_other_keys(self):
        """剥离只能动 time —— tool_calls / tool_call_id 丢了会直接让工具循环报错。"""
        msg = {"role": "assistant", "content": None, "time": now_stamp(),
               "tool_calls": [{"id": "c1"}], "tool_call_id": "c1", "name": "f"}
        out = _api_messages([msg])[0]
        assert "time" not in out
        assert out["tool_calls"] == [{"id": "c1"}]
        assert out["tool_call_id"] == "c1"
        assert out["name"] == "f"

    def test_non_dict_entries_pass_through(self):
        assert _api_messages(["raw"]) == ["raw"]


# ── 4. 界面：常显 / 无 time 时不显示 ──

@pytest.fixture(scope="session")
def qapp():
    pytest.importorskip("PySide6", reason="界面测试需要 PySide6")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class TestMessageBubbleTime:
    def test_label_shown_when_time_given(self, qapp):
        stamp = datetime.now().strftime(STAMP_FORMAT)
        bubble = MessageBubble("user", "你好", stamp)
        assert bubble.time_label is not None
        assert bubble.time_label.text() == datetime.now().strftime("%H:%M")

    def test_tooltip_carries_full_stamp(self, qapp):
        """界面上只有 HH:MM，完整时间戳放 tooltip —— 跨年回放时才分得清。"""
        stamp = "2025-08-20 15:55:12"
        bubble = MessageBubble("user", "你好", stamp)
        assert bubble.time_label.toolTip() == stamp

    @pytest.mark.parametrize("bad", ["", None, "not-a-time"])
    def test_no_label_when_time_unusable(self, qapp, bad):
        """旧会话没有 time → 干脆不显示，不留一个空占位。"""
        bubble = MessageBubble("user", "你好", bad)
        assert bubble.time_label is None

    def test_label_sits_before_actions_row(self, qapp):
        """时间必须排在操作行**之前**。

        操作行的按钮虽然默认隐藏，但容器本身始终占着固定高度；
        排在它后面会让时间与正文之间凭空多出一段空白。
        """
        bubble = MessageBubble("user", "你好", now_stamp())
        layout = bubble.body.layout()
        idx = [layout.indexOf(w) for w in (bubble.time_label, bubble.actions)]
        assert all(i >= 0 for i in idx)
        assert idx[0] < idx[1]


class TestChatViewTime:
    def test_load_messages_carries_time_through(self, qapp):
        view = ChatView()
        stamp = datetime.now().strftime(STAMP_FORMAT)
        view.load_messages([{"role": "user", "content": "你好", "time": stamp}])
        assert view._bubbles[-1].time_label is not None

    def test_load_messages_without_time_shows_none(self, qapp):
        """旧会话文件（无 time）回放时不能凭空长出时间戳。"""
        view = ChatView()
        view.load_messages([{"role": "user", "content": "你好"}])
        assert view._bubbles[-1].time_label is None

    def test_begin_response_stamps_assistant(self, qapp):
        view = ChatView()
        view.begin_response()
        assert view._bubbles[-1].time_label is not None

    def test_add_message_defaults_to_no_time(self, qapp):
        """不传 time 就不显示 —— 避免默认值把「没有时间」伪装成「有时间」。"""
        view = ChatView()
        view.add_message("user", "你好")
        assert view._bubbles[-1].time_label is None
