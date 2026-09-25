"""日志门面 `_Log` 的接口契约测试。

## 为什么有这组用例

`core/logging_utils._Log` 是一个**手写门面**，只暴露它自己定义的那几个方法 ——
调一个不存在的方法会抛 `AttributeError`，不像 `logging.Logger` 那样有兜底。

2026-09-25 实测踩到：门面只有 `warn`，而 `ui_qt/main_window.py` 的自检告警写的是
`log.warning` —— 于是**自检一旦发现问题就在那一行崩掉**，后面的侧栏状态与警告框
全都没执行，用户根本不知道自己哪里配错了。

这个 bug 藏得久，是因为它只在「自检发现问题」时才走到 —— 而平时自检都是通过的，
启动日志里只会看到一句 `[自检] 启动自检通过`，看起来一切正常。

所以这里不只测「有没有 warning」，而是**扫全仓的调用点**：
门面缺哪个方法，就红哪条用例。以后新增调用忘了补方法，会立刻被发现。
"""
import logging
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.logging_utils import _Log, log  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("core", "ui_qt", "plugins", "tools")
SCAN_FILES = ("main.py",)

#: `log.xxx(` —— `\b` 保证不会误匹配 `dialog.warning(` / `logging.warn(` / `log_file(`
_CALL_RE = re.compile(r"\blog\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\(")


def _iter_call_sites():
    """产出 (相对路径, 行号, 方法名)。"""
    targets = []
    for name in SCAN_DIRS:
        targets.extend((REPO_ROOT / name).rglob("*.py"))
    for name in SCAN_FILES:
        p = REPO_ROOT / name
        if p.exists():
            targets.append(p)
    for path in targets:
        if "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for match in _CALL_RE.finditer(line):
                yield path.relative_to(REPO_ROOT).as_posix(), lineno, match.group(1)


class TestFacadeSurface:
    @pytest.mark.parametrize("name", ["info", "warning", "warn", "error", "debug"])
    def test_standard_levels_exist(self, name):
        assert callable(getattr(log, name, None)), f"_Log 缺少 {name}()"

    def test_warn_is_alias_of_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="meido"):
            log.warn("别名走 warning 级别")
        assert any(r.levelno == logging.WARNING for r in caplog.records)

    def test_warning_reaches_the_logger(self, caplog):
        with caplog.at_level(logging.WARNING, logger="meido"):
            log.warning("测试 %s", "告警")
        messages = [r.getMessage() for r in caplog.records]
        assert "测试 告警" in messages

    def test_error_reaches_the_logger(self, caplog):
        with caplog.at_level(logging.ERROR, logger="meido"):
            log.error("测试 %s", "错误")
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    def test_facade_is_a_log_object(self):
        assert isinstance(log, _Log)


class TestEveryCallSiteExists:
    """核心用例：全仓每个 `log.xxx(` 的 xxx 都必须真的存在于门面上。"""

    def test_scan_finds_call_sites(self):
        """先确认扫描本身有效 —— 否则下面那条用例会「空集通过」。"""
        sites = list(_iter_call_sites())
        assert len(sites) >= 10, f"只扫到 {len(sites)} 处调用，扫描逻辑可能失效"

    def test_no_call_to_missing_method(self):
        missing = {}
        for rel, lineno, name in _iter_call_sites():
            if not callable(getattr(log, name, None)):
                missing.setdefault(name, []).append(f"{rel}:{lineno}")
        assert not missing, (
            "这些调用点用了门面上不存在的方法（运行时会抛 AttributeError）：\n"
            + "\n".join(f"  log.{name}() ← {', '.join(locs)}"
                        for name, locs in sorted(missing.items())))

    def test_does_not_match_unrelated_receivers(self, tmp_path):
        """确认正则不会把 `dialog.warning(` / `logging.warn(` 当成门面调用。"""
        sample = "dialog.warning('x')\nlogging.warn('y')\nlog_file()\n"
        assert list(_CALL_RE.finditer(sample)) == []
