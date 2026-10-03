"""桌宠回复气泡的回归测试（悬浮不收 + 高度封顶出滑条）。

## 两件事

1. **鼠标悬浮时不消失**：回复结束后气泡本来 9 秒就收，读长回复根本来不及。
   现在鼠标停在上面就不收，移开后再给 `LEAVE_GRACE_MS` 就收。
2. **高度封顶**：气泡按内容自适应大小，长回复会一路长下去。现在内容区最高
   `MAX_CONTENT_HEIGHT`，超出出滑条。

## ⚠️ 前置条件：气泡必须能收鼠标事件

原先气泡带 `WindowTransparentForInput`（点击穿透），代价是**收不到任何鼠标事件**
—— 滑条拖不动、`enterEvent`/`leaveEvent` 永不触发。所以这两件事都建立在
「去掉那个标志」之上，测试里把它钉住。
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="需要 PySide6")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QEnterEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui_qt import theme  # noqa: E402
from ui_qt.speech_bubble import (  # noqa: E402
    HOLD_MS,
    LEAVE_GRACE_MS,
    MAX_CONTENT_HEIGHT,
    MAX_WIDTH,
    PAD_H,
    PAD_V,
    TAIL_H,
    SpeechBubble,
)

LONG_TEXT = "这是一段很长的回复内容，用来验证气泡不会无限长下去。" * 40


@pytest.fixture(scope="session")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def bubble(qapp):
    qapp.setStyleSheet(theme.build_qss())
    b = SpeechBubble()
    yield b
    b.clear()
    b.deleteLater()


def _enter(b):
    b.enterEvent(QEnterEvent(QPointF(1, 1), QPointF(1, 1), QPointF(1, 1)))


def _leave(b):
    b.leaveEvent(QEvent(QEvent.Type.Leave))


# ═══════════════════════════════════════════════════════════
# 前置条件：能收鼠标事件
# ═══════════════════════════════════════════════════════════

class TestWindowFlags:
    def test_not_click_through(self, bubble):
        """必须**不**是点击穿透，否则滑条与悬浮检测都无从实现。"""
        flags = bubble.windowFlags()
        assert not (flags & Qt.WindowType.WindowTransparentForInput), (
            "气泡又变回点击穿透了 —— 滑条会拖不动、enterEvent 永不触发"
        )

    def test_still_does_not_steal_focus(self, bubble):
        flags = bubble.windowFlags()
        assert flags & Qt.WindowType.WindowDoesNotAcceptFocus

    def test_still_on_top(self, bubble):
        assert bubble.windowFlags() & Qt.WindowType.WindowStaysOnTopHint


# ═══════════════════════════════════════════════════════════
# 悬浮时不消失
# ═══════════════════════════════════════════════════════════

class TestHoverKeepsVisible:
    def test_enter_stops_countdown(self, bubble):
        bubble.show_text("读我一会儿", hold_ms=HOLD_MS)
        assert bubble._hide_timer.isActive()

        _enter(bubble)
        assert not bubble._hide_timer.isActive(), "鼠标进来了还在倒计时，气泡会中途消失"

    def test_leave_restarts_with_short_grace(self, bubble):
        """移开鼠标意味着「看完了」，给的是短宽限而不是重新等满 HOLD_MS。"""
        bubble.show_text("读我一会儿", hold_ms=HOLD_MS)
        _enter(bubble)
        _leave(bubble)

        assert bubble._hide_timer.isActive()
        assert bubble._hide_timer.remainingTime() <= LEAVE_GRACE_MS + 50
        assert LEAVE_GRACE_MS < HOLD_MS

    def test_hold_then_hide_is_ignored_while_hovering(self, bubble):
        """回复刚结束时鼠标就停在气泡上 —— 这一轮不该启动倒计时。"""
        bubble.show_text("正在生成", hold_ms=0)
        _enter(bubble)

        bubble.hold_then_hide(HOLD_MS)
        assert not bubble._hide_timer.isActive()

    def test_hold_then_hide_works_when_not_hovering(self, bubble):
        bubble.show_text("回复完毕", hold_ms=0)
        bubble.hold_then_hide(HOLD_MS)
        assert bubble._hide_timer.isActive()

    def test_generation_mode_never_auto_hides(self, bubble):
        """生成中（hold_ms=0）本来就该一直显示，进出鼠标都不该启动倒计时。"""
        bubble.show_text("生成中…", hold_ms=0)
        _enter(bubble)
        _leave(bubble)
        assert not bubble._hide_timer.isActive()

    def test_expiry_guard_checks_hover(self, bubble):
        """兜底：万一 enterEvent 没触发（窗口重建等），超时回调里再判一次。"""
        bubble.show_text("读我一会儿", hold_ms=HOLD_MS)
        bubble._hovering = True
        bubble._on_hold_expired()
        assert bubble.isVisible(), "悬浮状态下超时回调把气泡收掉了"


# ═══════════════════════════════════════════════════════════
# 高度封顶 + 滑条
# ═══════════════════════════════════════════════════════════

class TestHeightCap:
    def _shown(self, qapp, bubble, text):
        bubble.show_text(text)
        for _ in range(6):
            qapp.processEvents()
        return bubble

    def test_short_text_has_no_scrollbar(self, qapp, bubble):
        self._shown(qapp, bubble, "你好呀")
        assert bubble.height() < MAX_CONTENT_HEIGHT
        assert bubble.scroll.verticalScrollBar().maximum() == 0

    def test_long_text_is_capped(self, qapp, bubble):
        """长回复不能无限长 —— 内容区封顶，总高 = 内容 + 上下内边距 + 尖角。"""
        self._shown(qapp, bubble, LONG_TEXT)
        assert bubble.scroll.height() == MAX_CONTENT_HEIGHT
        assert bubble.height() == MAX_CONTENT_HEIGHT + 2 * PAD_V + TAIL_H
        assert bubble.width() <= MAX_WIDTH

    def test_long_text_gets_scrollbar(self, qapp, bubble):
        self._shown(qapp, bubble, LONG_TEXT)
        bar = bubble.scroll.verticalScrollBar()
        assert bar.maximum() > 0, "内容超出却没有滑条"

    def test_label_keeps_full_height(self, qapp, bubble):
        """标签要按**完整**内容撑开，比视口高的部分靠滚动看 —— 不是被裁掉。"""
        self._shown(qapp, bubble, LONG_TEXT)
        assert bubble.label.height() > bubble.scroll.height()

    def test_growth_is_monotonic(self, qapp, bubble):
        """文字越长气泡越高，但到顶就不再加高。"""
        heights = []
        for n in (5, 40, 400):
            self._shown(qapp, bubble, "内容填充" * n)
            heights.append(bubble.height())
        assert heights[0] <= heights[1] <= heights[2]
        assert heights[-1] <= MAX_CONTENT_HEIGHT + 2 * PAD_V + TAIL_H


class TestScrollFollow:
    """滚动位置：跟着最新内容走，但别拽正在往回翻的用户。

    ⚠️ 「是否贴底」必须在**改尺寸之前**判断 —— 内容一变长，滚动条的 range 就变了，
    改完再判会永远是「没贴底」，于是永远不跟随（实测踩过）。
    贴底本身也要延迟一拍，等布局稳定后再设值。
    """

    def _grow(self, qapp, bubble, text):
        bubble.show_text(text)
        for _ in range(6):
            qapp.processEvents()

    def test_follows_bottom_while_growing(self, qapp, bubble):
        bar = bubble.scroll.verticalScrollBar()
        for n in (20, 60, 120):
            self._grow(qapp, bubble, "流式追加的内容段落。" * n)
            assert bar.value() >= bar.maximum() - 4, (
                "流式追加时没跟着走，用户看不到正在生成的内容"
            )

    def test_does_not_yank_user_who_scrolled_up(self, qapp, bubble):
        self._grow(qapp, bubble, LONG_TEXT)
        bar = bubble.scroll.verticalScrollBar()
        assert bar.maximum() > 0
        bar.setValue(30)

        self._grow(qapp, bubble, LONG_TEXT + "追加了一段")
        assert bar.value() == 30, "用户往回翻时被拽到底了"

    def test_resumes_following_after_returning_to_bottom(self, qapp, bubble):
        self._grow(qapp, bubble, LONG_TEXT)
        bar = bubble.scroll.verticalScrollBar()
        bar.setValue(30)
        bar.setValue(bar.maximum())          # 用户自己滚回底部

        self._grow(qapp, bubble, LONG_TEXT + "又追加了一段")
        assert bar.value() >= bar.maximum() - 4, "回到最底后应恢复跟随"


# ═══════════════════════════════════════════════════════════
# 绘制
# ═══════════════════════════════════════════════════════════

class TestPainting:
    @pytest.mark.parametrize("tokens", [theme.LIGHT_TOKENS, theme.DARK_TOKENS])
    def test_background_is_painted(self, qapp, bubble, tokens):
        """背景必须真的画出来。

        ⚠️ 防回归重点：`QScrollArea` 的 viewport 默认会用窗口底色填充自己那块，
        把父级 `paintEvent` 画的圆角背景**整块抹掉**（实测内容区 alpha=0）。
        只有给 viewport 设透明样式表才压得住。
        """
        theme.set_active_theme(tokens)
        qapp.setStyleSheet(theme.build_qss())
        bubble.show_text("检查背景是否正常绘制")
        for _ in range(6):
            qapp.processEvents()

        img = bubble.grab().toImage()
        center = img.pixelColor(bubble.width() // 2, bubble.height() // 2)
        side = img.pixelColor(PAD_H // 2, bubble.height() // 2)
        corner = img.pixelColor(0, 0)

        assert side.alpha() == 255, "气泡背景没被绘制（边距区是透明的）"
        assert center.alpha() == 255, "内容区是透明的 —— viewport 把背景抹掉了"
        assert corner.alpha() == 0, "圆角没有生效（角落不是透明的）"

    def test_empty_text_hides(self, bubble):
        bubble.show_text("有内容")
        assert bubble.isVisible()
        bubble.show_text("   ")
        assert not bubble.isVisible()


# ═══════════════════════════════════════════════════════════
# 流式节流（长文本生成时立绘掉帧）
# ═══════════════════════════════════════════════════════════

def _wait(qapp, ms):
    import time
    t0 = time.perf_counter()
    while (time.perf_counter() - t0) * 1000 < ms:
        qapp.processEvents()
        time.sleep(0.005)


class TestStreamThrottle:
    """⚠️ 流式生成时 `show_text` 每秒被调几十次，每次都 `resize()` + `raise_()`。

    `raise_()` 是 `SetWindowPos(HWND_TOP)`，强制桌面合成器重算 z 序 ——
    长回复持续几十秒，立绘（WebEngine）就明显掉帧（用户实测反馈）。
    """

    def test_rapid_updates_are_coalesced(self, qapp, bubble, monkeypatch):
        applied = []
        orig = bubble._apply_text
        monkeypatch.setattr(
            bubble, "_apply_text",
            lambda t, h: (applied.append(t), orig(t, h))[1])

        for i in range(1, 41):                  # 40 次极速更新
            bubble.show_text("内容" * i)
            qapp.processEvents()

        assert len(applied) < 40, "完全没有节流，原生窗口操作次数没降下来"
        assert len(applied) >= 1

    def test_final_text_always_lands(self, qapp, bubble):
        """节流不能把最后一段吞掉 —— `hold_then_hide` 必须先把攒下的补上。"""
        for i in range(1, 21):
            bubble.show_text("最终内容" * i)
        expected = "最终内容" * 20

        bubble.hold_then_hide(HOLD_MS)

        assert bubble._text == expected, "气泡停在了半截文本上"

    def test_single_call_is_immediate(self, qapp, bubble):
        """非流式（一次性显示）不该被节流拖慢。"""
        bubble.show_text("一次性的短文本")
        assert bubble._text == "一次性的短文本"
        assert bubble.isVisible()

    def test_raise_only_when_becoming_visible(self, qapp, bubble, monkeypatch):
        """`raise_()` 只在「从隐藏变可见」时调一次。

        窗口本来就是 WindowStaysOnTopHint，不需要每次更新都抬 z 序。
        """
        calls = []
        monkeypatch.setattr(bubble, "raise_", lambda: calls.append(1))

        bubble.show_text("第一段")
        assert len(calls) == 1

        _wait(qapp, 150)                        # 越过节流窗口
        bubble.show_text("第二段")
        assert len(calls) == 1, "已经可见了还在反复抬 z 序"

    def test_clear_drops_pending(self, qapp, bubble):
        """清空后节流攒下的文本必须丢掉 —— 否则定时器到点会把气泡又弹回来。"""
        bubble.show_text("第一段")
        bubble.show_text("第二段")               # 被节流攒下
        bubble.show_text("")

        _wait(qapp, 200)
        assert not bubble.isVisible(), "清空后气泡被节流的定时器弹回来了"

    def test_hidden_bubble_stays_hidden(self, qapp, bubble):
        bubble.show_text("有内容")
        bubble.hide()
        _wait(qapp, 200)
        assert not bubble.isVisible()
