"""后台调试窗口（控制台）开关的测试。

对应需求「分包版本的后台调试窗口可选开启」。

分两层：

1. `core/console_window.py` —— 配置解析与「是否该分配控制台」的判定。
   判定逻辑用 monkeypatch 控制 Win32 探测结果，所以不依赖当前测试进程
   到底有没有控制台。
2. `core/logging_utils.py` 的 `_LogStream` —— print 接管。
   这里有一条**负向断言**守着已经修过的坑：print 只能经 logger 出一个出口，
   不能再往控制台直写一份（否则控制台上每条 print 都会出现两次）。

真实的 AllocConsole 行为无法在 pytest 里安全验证（会弹窗口、会替换全局 sys.stdout），
已用 pythonw + cmd `start` 的独立进程验证过，见 `docs/` 与本次改动的说明。
"""
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import console_window  # noqa: E402
from core.logging_utils import _LogStream  # noqa: E402


@pytest.fixture()
def cfg_dir(tmp_path, monkeypatch) -> Path:
    """把 console_window 眼里的 config/ 指到临时目录。"""
    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setattr(console_window, "app_path",
                        lambda *parts: tmp_path.joinpath(*parts))
    return config


def _write_bot_toml(config: Path, text: str) -> None:
    (config / "bot.toml").write_text(text, encoding="utf-8")


class TestShowConsoleParsing:
    def test_missing_file_defaults_to_false(self, cfg_dir):
        """没有 bot.toml 时不开控制台 —— 配置缺失不该意外弹窗。"""
        assert console_window.console_enabled() is False

    def test_missing_section_defaults_to_false(self, cfg_dir):
        _write_bot_toml(cfg_dir, '[bot]\nname = "妹抖酱"\n')
        assert console_window.console_enabled() is False

    def test_explicit_false(self, cfg_dir):
        _write_bot_toml(cfg_dir, '[logging]\nlevel = "INFO"\nshow_console = false\n')
        assert console_window.console_enabled() is False

    def test_explicit_true(self, cfg_dir):
        _write_bot_toml(cfg_dir, '[logging]\nlevel = "INFO"\nshow_console = true\n')
        assert console_window.console_enabled() is True

    def test_string_value_is_rejected(self, cfg_dir):
        """写成字符串 "true" 不算数 —— 只认 TOML 布尔，避免误开窗口。"""
        _write_bot_toml(cfg_dir, '[logging]\nshow_console = "true"\n')
        assert console_window.console_enabled() is False

    def test_number_value_is_rejected(self, cfg_dir):
        _write_bot_toml(cfg_dir, "[logging]\nshow_console = 1\n")
        assert console_window.console_enabled() is False

    def test_malformed_toml_defaults_to_false(self, cfg_dir):
        """写坏的 TOML 不能把程序带崩，按关闭处理。"""
        _write_bot_toml(cfg_dir, "[logging\nshow_console = true\n")
        assert console_window.console_enabled() is False

    def test_logging_section_not_a_table(self, cfg_dir):
        """`logging = "x"` 这种写法也不能崩。"""
        _write_bot_toml(cfg_dir, 'logging = "x"\n')
        assert console_window.console_enabled() is False


class TestConsoleDecision:
    def test_ensure_console_does_nothing_when_disabled(self, cfg_dir, monkeypatch):
        """配置关闭时绝不能去分配控制台（默认观感靠这条守住）。"""
        _write_bot_toml(cfg_dir, "[logging]\nshow_console = false\n")
        calls = []
        monkeypatch.setattr(console_window, "attach_console",
                            lambda *a, **k: calls.append(a) or True)
        monkeypatch.setattr(console_window, "_console_hwnd", lambda: 0)

        assert console_window.ensure_console() is False
        assert calls == [], "配置关闭时不应调用 attach_console"

    def test_ensure_console_attaches_when_enabled(self, cfg_dir, monkeypatch):
        _write_bot_toml(cfg_dir, "[logging]\nshow_console = true\n")
        calls = []
        monkeypatch.setattr(console_window, "attach_console",
                            lambda *a, **k: calls.append(a) or True)

        assert console_window.ensure_console() is True
        assert len(calls) == 1

    def test_ensure_console_reports_existing_console(self, cfg_dir, monkeypatch):
        """用户本来就开着终端时，返回 True 但不去分配。"""
        _write_bot_toml(cfg_dir, "[logging]\nshow_console = false\n")
        calls = []
        monkeypatch.setattr(console_window, "attach_console",
                            lambda *a, **k: calls.append(a) or True)
        monkeypatch.setattr(console_window, "_console_hwnd", lambda: 12345)

        assert console_window.ensure_console() is True
        assert calls == []

    def test_attach_console_reuses_existing_window(self, monkeypatch):
        """已有控制台（用户终端）→ 直接复用，不分配、也不标记成「我们的」。"""
        monkeypatch.setattr(console_window, "_console_hwnd", lambda: 999)
        monkeypatch.setattr(console_window, "_allocated", False)

        assert console_window.attach_console() is True
        assert console_window.allocated_by_us() is False

    def test_force_console_ignores_config(self, cfg_dir, monkeypatch):
        """崩溃兜底不看配置：该亮出来就得亮出来。"""
        _write_bot_toml(cfg_dir, "[logging]\nshow_console = false\n")
        calls = []
        monkeypatch.setattr(console_window, "attach_console",
                            lambda *a, **k: calls.append(a) or True)
        assert console_window.force_console() is True
        assert len(calls) == 1


class TestDescribeState:
    def test_no_console(self, monkeypatch):
        monkeypatch.setattr(console_window, "_IS_WINDOWS", True)
        monkeypatch.setattr(console_window, "_allocated", False)
        monkeypatch.setattr(console_window, "_console_hwnd", lambda: 0)
        assert "无" in console_window.describe_state()
        assert "show_console = false" in console_window.describe_state()

    def test_allocated(self, monkeypatch):
        monkeypatch.setattr(console_window, "_IS_WINDOWS", True)
        monkeypatch.setattr(console_window, "_allocated", True)
        assert "由程序分配" in console_window.describe_state()

    def test_user_terminal(self, monkeypatch):
        monkeypatch.setattr(console_window, "_IS_WINDOWS", True)
        monkeypatch.setattr(console_window, "_allocated", False)
        monkeypatch.setattr(console_window, "_console_hwnd", lambda: 42)
        assert "用户终端" in console_window.describe_state()

    def test_non_windows(self, monkeypatch):
        monkeypatch.setattr(console_window, "_IS_WINDOWS", False)
        assert "非 Windows" in console_window.describe_state()


class TestLogFileHint:
    def test_points_at_a_log_file(self):
        hint = console_window.log_file_hint()
        assert hint is not None
        assert hint.suffix == ".log"
        assert hint.parent.name == "logs"


class TestLogStream:
    """print 接管流。"""

    @pytest.fixture()
    def records(self):
        logger = logging.getLogger("test._LogStream")
        logger.propagate = False
        logger.setLevel(logging.INFO)
        collected = []

        class _Handler(logging.Handler):
            def emit(self, record):
                collected.append(record.getMessage())

        handler = _Handler()
        logger.addHandler(handler)
        yield logger, collected
        logger.removeHandler(handler)

    def test_complete_line_becomes_one_record(self, records):
        logger, collected = records
        stream = _LogStream(logger)
        stream.write("hello\n")
        assert collected == ["hello"]

    def test_partial_line_is_buffered(self, records):
        logger, collected = records
        stream = _LogStream(logger)
        stream.write("par")
        assert collected == []
        stream.write("tial\n")
        assert collected == ["partial"]

    def test_multiple_lines_in_one_write(self, records):
        logger, collected = records
        stream = _LogStream(logger)
        stream.write("a\nb\nc\n")
        assert collected == ["a", "b", "c"]

    def test_blank_lines_are_dropped(self, records):
        """print() 空行不该刷进日志。"""
        logger, collected = records
        stream = _LogStream(logger)
        stream.write("\n\n  \n")
        assert collected == []

    def test_flush_emits_tail_without_newline(self, records):
        logger, collected = records
        stream = _LogStream(logger)
        stream.write("no newline")
        assert collected == []
        stream.flush()
        assert collected == ["no newline"]

    def test_write_returns_length(self, records):
        """print 依赖 write 的返回值，不能返回 None。"""
        logger, _ = records
        stream = _LogStream(logger)
        assert stream.write("abc\n") == 4

    def test_exactly_one_record_per_line(self, records):
        """负向断言：print 只能出一个出口。

        曾经的设计给 _LogStream 加过 mirror（同时直写控制台），
        结果是控制台 handler 再写一遍 —— 每条 print 在窗口里出现两次。
        这里钉住「一行只产生一条记录」。
        """
        logger, collected = records
        stream = _LogStream(logger)
        stream.write("only once\n")
        assert len(collected) == 1

    def test_stream_has_no_console_mirror(self):
        """负向断言：接管流不该再有直写控制台的通道。"""
        logger = logging.getLogger("test._LogStream.nomirror")
        stream = _LogStream(logger)
        assert not hasattr(stream, "_mirror")

    def test_stderr_variant_uses_given_level(self, records):
        logger, _ = records
        stream = _LogStream(logger, logging.WARNING)
        assert stream._level == logging.WARNING

    def test_isatty_is_false(self, records):
        logger, _ = records
        assert _LogStream(logger).isatty() is False

    def test_fileno_raises(self, records):
        logger, _ = records
        with pytest.raises(OSError):
            _LogStream(logger).fileno()

    def test_reconfigure_is_noop(self, records):
        """main.py 会对 stdout 调 reconfigure(errors=...)，不能炸。"""
        logger, _ = records
        assert _LogStream(logger).reconfigure(errors="replace") is None
