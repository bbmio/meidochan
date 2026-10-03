"""Live2D 桌宠「点击轮换表情 / 拖动」判定的回归测试。

2026-10-03 修的真实缺陷：`assets/live2d/viewer/index.html` 原先用 DOM 的 `click`
事件触发表情轮换。而 `click` 只要「按下与抬起落在同一元素」就派发，**与位移无关**；
拖动桌宠时 Qt 会同步 `move()` 窗口，指针相对窗口的位置几乎不变 —— 抬起时照样
派发 click，表现就是「一拖就换表情」。

这里守两件最容易再次失守的事：

1. **阈值必须跨文件对齐** —— Qt 的 `Live2DWindow.DRAG_SLOP` 与页面的 `CLICK_SLOP`
   若不一致，两者之间会留出「Qt 认为在拖、页面认为在点」的空档，那就是一次误触。
2. **页面里不许再出现 `click` 监听** —— 否则缺陷会原样长回来。

判定逻辑本身的行为验证（阈值语义、拖出去再拖回原点等）在
`tools/probe_live2d_gesture.js`，需要 node，故不放在这里。
"""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PAGE = ROOT / "assets" / "live2d" / "viewer" / "index.html"


@pytest.fixture(scope="module")
def page_html() -> str:
    assert PAGE.exists(), f"播放器页面不存在：{PAGE}"
    return PAGE.read_text(encoding="utf-8")


def _page_click_slop(html: str) -> int:
    match = re.search(r"var\s+CLICK_SLOP\s*=\s*(\d+)\s*;", html)
    assert match, "页面里找不到 CLICK_SLOP 定义"
    return int(match.group(1))


class TestThresholdAlignment:
    def test_qt_and_page_thresholds_match(self, page_html):
        """两边的拖动阈值必须一致，否则会留出误触空档。"""
        pytest.importorskip("PySide6", reason="需要导入 Live2DWindow 取阈值")
        from ui_qt.live2d_window import Live2DWindow

        assert _page_click_slop(page_html) == Live2DWindow.DRAG_SLOP, (
            "页面 CLICK_SLOP 与 Qt DRAG_SLOP 不一致："
            "两者之间的位移会被判成「Qt 在拖、页面在点」，即一次误触"
        )

    def test_threshold_is_positive(self, page_html):
        """阈值为 0 会让正常点击（手抖 1px）都判成拖动，表情永远换不了。"""
        assert _page_click_slop(page_html) > 0


class TestNoClickListenerRegression:
    def test_page_does_not_listen_to_click(self, page_html):
        """click 事件无法区分拖动与点击，页面里不允许再用它。"""
        listeners = re.findall(r"addEventListener\(\s*[\"']click[\"']", page_html)
        assert listeners == [], (
            "页面又用上了 click 监听 —— 它只认「按下与抬起在同一元素」，"
            "拖动时照样派发，会让表情轮换再次被拖动误触"
        )

    def test_gesture_uses_screen_coordinates(self, page_html):
        """必须按屏幕坐标算位移。

        拖动时窗口跟着指针走，指针相对窗口的位置几乎不变 ——
        `clientX/clientY` 全程恒定，用它们算位移会得到 0，判不出拖动。
        """
        assert re.search(r"screenX", page_html), "页面没有使用 screenX"

    def test_mouseup_uses_max_displacement(self, page_html):
        """用「过程最大位移」而不是起止两点。

        拖出去再拖回原点时起止位移是 0，只看首尾会把它误判成一次点击。
        """
        assert re.search(r"Math\.max\(\s*start\.max\s*,", page_html), (
            "mouseup 里没有取 start.max —— 拖出去再拖回原点会被误判成点击"
        )

    def test_mousemove_tracks_max_displacement(self, page_html):
        """最大位移必须在 mousemove 里累计出来，否则 start.max 恒为 0。"""
        assert re.search(r"pressAt\.max\s*=", page_html), (
            "mousemove 里没有更新 pressAt.max"
        )


# ═══════════════════════════════════════════════════════════
# Qt 侧的拖动阈值
# ═══════════════════════════════════════════════════════════

@pytest.fixture(scope="session")
def qapp():
    pytest.importorskip("PySide6", reason="窗口拖动测试需要 PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


class TestDragThreshold:
    """`Live2DWindow` 的窗口拖动：没过阈值不许动，过了阈值要精确跟随。

    这里守的是 2026-10-03 一并修掉的另一半问题：以前 MouseMove 分支**无条件**
    调 `self.move(...)`（`_dragging` 只写不读），于是单击时手抖几像素就把桌宠
    挪走一点，反复点击会慢慢漂移。
    """

    @staticmethod
    def _drag(moves):
        """按下 → 依次移动到 moves 里的偏移量 → 返回窗口实际位移 (dx, dy)。"""
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent

        from ui_qt.live2d_window import Live2DWindow

        def make(etype, gx, gy):
            pos = QPointF(gx, gy)
            return QMouseEvent(etype, QPointF(50, 50), pos, pos,
                               Qt.MouseButton.LeftButton,
                               Qt.MouseButton.LeftButton,
                               Qt.KeyboardModifier.NoModifier)

        win = Live2DWindow()
        win._save_position = lambda: None      # 别往真实 data/ 里写位置
        win.move(500, 400)
        win.show()
        start = win.pos()
        base = (600, 500)                      # 远离边缘的按下点，避开缩放区
        win.eventFilter(win, make(QEvent.Type.MouseButtonPress, *base))
        for dx, dy in moves:
            win.eventFilter(win, make(QEvent.Type.MouseMove,
                                      base[0] + dx, base[1] + dy))
        delta = win.pos() - start
        win.close()
        return delta.x(), delta.y()

    @pytest.mark.parametrize("offset", [0, 1, 3])
    def test_within_threshold_does_not_move(self, qapp, offset):
        """点击（含手抖到阈值）时窗口必须纹丝不动。"""
        assert self._drag([(offset, 0)]) == (0, 0)

    @pytest.mark.parametrize("offset", [4, 20, 200])
    def test_beyond_threshold_follows_cursor(self, qapp, offset):
        """越过阈值后窗口精确跟随，不是只跟一部分。"""
        assert self._drag([(offset, 0)]) == (offset, 0)

    def test_diagonal_drag(self, qapp):
        assert self._drag([(20, 20)]) == (20, 20)

    def test_progressive_drag_has_no_lag(self, qapp):
        """先小幅移动再大幅拖动：阈值吃掉的那几像素不能变成永久滞后。

        `origin_tl` 是按下时记录的、此后不再更新，所以越过阈值的那一刻窗口会
        立刻追上光标 —— 若哪天改成「越过阈值时重置起点」，这条会红。
        """
        assert self._drag([(1, 0), (2, 0), (3, 0), (20, 0)]) == (20, 0)
