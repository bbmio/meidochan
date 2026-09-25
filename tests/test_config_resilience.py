"""配置文件损坏时的启动韧性测试。

## 为什么有这组用例

`ConfigLoader.load()` 以前会把 `TOMLDecodeError` **直接抛出去**，而
`bot.toml` / `model.toml` / `persona.toml` 三个配置是在 `engine.__init__` 里读的：

    engine.__init__
      ├─ self.config.get_bot_config()      → load("bot.toml")
      ├─ self.config.get_model_config()    → load("model.toml")
      └─ self.config.get_persona_config()  → load("persona.toml")

于是**一个手抖的字符 = 启动即崩**，而且崩在窗口出现之前 —— 用户只看到
「一闪而过」，连 traceback 都看不见（会被 main.py 的兜底塞进临时控制台窗口）。

这里守三条：
1. **任何配置文件坏掉都不能让 getter 抛异常** —— 必须回落默认值。
2. **用户手写的文件一个字节都不能动** —— 只另存 `.bak` 备份内容。
   （刻意不学 `workspace/storage.py` 的「改名移走」：那边是程序生成的配置，
   而 `bot.toml` 里存着数据目录路径，移走等于悄悄换掉用户的数据位置。）
3. **回落不能是静默的** —— 必须能从 `broken_files` 查到，并由启动自检告知用户。
   否则用户会以为「我的配置生效了」，实际跑的是默认值。
"""
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config.loader import ConfigLoader  # noqa: E402
from core.config.models import AppearanceConfig  # noqa: E402
from core.selfcheck import (  # noqa: E402
    CheckResult,
    check_config_files,
    format_report,
    run_selfcheck,
)

BROKEN = "这不是 toml {{{\n"

#: 文件名 → 对应的 getter 名。前三个在 engine.__init__ 的启动路径上。
GETTERS = {
    "bot.toml": "get_bot_config",
    "model.toml": "get_model_config",
    "persona.toml": "get_persona_config",
    "live2d.toml": "get_live2d_config",
    "plugins.toml": "get_plugins_config",
    "appearance.toml": "get_appearance_config",
}

#: 这三个是「坏了就启动即崩」的，单独钉住
STARTUP_CRITICAL = ("bot.toml", "model.toml", "persona.toml")


@pytest.fixture()
def cfg_dir(tmp_path):
    d = tmp_path / "config"
    d.mkdir()
    return d


def _broken_loader(cfg_dir, name: str) -> ConfigLoader:
    (cfg_dir / name).write_text(BROKEN, encoding="utf-8")
    return ConfigLoader(str(cfg_dir))


# ── 1. 永不抛异常 ──

class TestNeverRaises:
    @pytest.mark.parametrize("name", list(GETTERS))
    def test_getter_falls_back(self, cfg_dir, name):
        loader = _broken_loader(cfg_dir, name)
        getattr(loader, GETTERS[name])()      # 不抛即通过

    @pytest.mark.parametrize("name", STARTUP_CRITICAL)
    def test_startup_critical_getters_fall_back(self, cfg_dir, name):
        """engine.__init__ 直接调这三个 —— 它们抛异常就是「启动即崩」。"""
        loader = _broken_loader(cfg_dir, name)
        getattr(loader, GETTERS[name])()

    def test_falls_back_to_meaningful_defaults(self, cfg_dir):
        """回落的不只是「不崩」，还得是能用的默认值。"""
        loader = _broken_loader(cfg_dir, "bot.toml")
        assert loader.get_bot_config().name          # 有默认名字
        loader = _broken_loader(cfg_dir, "model.toml")
        assert loader.get_model_config().provider    # 有默认服务商
        loader = _broken_loader(cfg_dir, "appearance.toml")
        assert loader.get_appearance_config() == AppearanceConfig()
        loader = _broken_loader(cfg_dir, "live2d.toml")
        assert loader.get_live2d_config() == {}

    def test_empty_file_also_falls_back(self, cfg_dir):
        """空文件不是合法 TOML 吗？—— 是合法的（空表），这里确认不会误判。"""
        (cfg_dir / "bot.toml").write_text("", encoding="utf-8")
        assert ConfigLoader(str(cfg_dir)).get_bot_config().name
        assert ConfigLoader(str(cfg_dir)).broken_files == {}

    def test_binary_garbage_falls_back(self, cfg_dir):
        (cfg_dir / "persona.toml").write_bytes(b"\x00\x01\xff\xfe garbage")
        assert ConfigLoader(str(cfg_dir)).get_persona_config() is not None


# ── 2. 用户文件非破坏性 ──

class TestNonDestructive:
    @pytest.mark.parametrize("name", list(GETTERS))
    def test_original_file_is_untouched(self, cfg_dir, name):
        """用户手写的文件一个字节都不能动 —— 他要靠它自己修回来。"""
        loader = _broken_loader(cfg_dir, name)
        getattr(loader, GETTERS[name])()
        assert (cfg_dir / name).read_text(encoding="utf-8") == BROKEN

    @pytest.mark.parametrize("name", list(GETTERS))
    def test_backup_is_created(self, cfg_dir, name):
        loader = _broken_loader(cfg_dir, name)
        getattr(loader, GETTERS[name])()
        backups = list(cfg_dir.glob(f"{name}.bak-*"))
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == BROKEN

    def test_backup_is_created_only_once(self, cfg_dir):
        """反复启动不该堆一地 .bak —— 用户要的是「内容别丢」不是「一堆副本」。"""
        for _ in range(3):
            loader = _broken_loader(cfg_dir, "persona.toml")
            loader.get_persona_config()
        assert len(list(cfg_dir.glob("persona.toml.bak-*"))) == 1

    def test_backup_failure_does_not_raise(self, cfg_dir, monkeypatch):
        """备份失败（只读盘等）也必须照常回落，不能因此再崩一次。"""
        import shutil as _shutil

        def boom(*_a, **_k):
            raise OSError("模拟：盘只读")

        monkeypatch.setattr(_shutil, "copy2", boom)
        loader = _broken_loader(cfg_dir, "bot.toml")
        assert loader.get_bot_config().name
        assert "bot.toml" in loader.broken_files


# ── 3. 回落必须可查、可告知 ──

class TestBrokenFilesReport:
    def test_records_name_and_reason(self, cfg_dir):
        loader = _broken_loader(cfg_dir, "persona.toml")
        loader.get_persona_config()
        broken = loader.broken_files
        assert "persona.toml" in broken
        assert "TOMLDecodeError" in broken["persona.toml"]

    def test_good_configs_are_not_recorded(self, cfg_dir):
        (cfg_dir / "bot.toml").write_text("[bot]\nname = \"妹抖酱\"\n", encoding="utf-8")
        loader = ConfigLoader(str(cfg_dir))
        loader.get_bot_config()
        assert loader.broken_files == {}

    def test_missing_file_is_not_broken(self, cfg_dir):
        """文件不存在 ≠ 文件坏了 —— 首次启动本来就没有配置文件。"""
        loader = ConfigLoader(str(cfg_dir))
        loader.get_bot_config()
        assert loader.broken_files == {}

    def test_returns_copy(self, cfg_dir):
        loader = _broken_loader(cfg_dir, "bot.toml")
        loader.get_bot_config()
        loader.broken_files.clear()
        assert loader.broken_files          # 外部改动不该影响内部状态

    def test_logs_an_error(self, cfg_dir, caplog):
        """回落是静默的，日志是最后的兜底渠道 —— 必须真的写进去。"""
        with caplog.at_level(logging.ERROR, logger="core.config.loader"):
            loader = _broken_loader(cfg_dir, "bot.toml")
            loader.get_bot_config()
        # 用 getMessage() 做 %-格式化；直接读 record.message 拿到的是未格式化的模板
        messages = [r.getMessage() for r in caplog.records
                    if r.levelno == logging.ERROR]
        assert any("bot.toml" in m for m in messages), messages
        assert any("TOMLDecodeError" in m for m in messages), messages


class TestSelfcheckIntegration:
    class _FakeEngine:
        def __init__(self, loader):
            self.config = loader

    def test_ok_when_nothing_broken(self, cfg_dir):
        loader = ConfigLoader(str(cfg_dir))
        result = check_config_files(self._FakeEngine(loader))
        assert isinstance(result, CheckResult)
        assert result.ok

    def test_flags_broken_file(self, cfg_dir):
        loader = _broken_loader(cfg_dir, "persona.toml")
        loader.get_persona_config()
        result = check_config_files(self._FakeEngine(loader))
        assert not result.ok
        assert "persona.toml" in result.detail
        assert "TOMLDecodeError" in result.detail      # 带上原因，用户才知道改什么
        assert result.hint

    def test_never_raises_on_bad_engine(self):
        """自检的契约是永不抛异常 —— engine 缺属性也不能炸。"""
        class _NoConfig:
            pass

        assert not check_config_files(_NoConfig()).ok

    def test_run_selfcheck_includes_config_check(self, cfg_dir, monkeypatch):
        """光有 check_config_files 没用，得真的挂进 run_selfcheck 才会被调用。"""
        import core.selfcheck as selfcheck

        class _Engine:
            config = ConfigLoader(str(cfg_dir))

            class model_config:
                provider = "deepseek"
                base_url = "https://example.invalid"
                api_key = "x"

                @staticmethod
                def is_local():
                    return False

            class brain:
                current_model = "m"

        monkeypatch.setattr(selfcheck, "check_service",
                            lambda _e: (CheckResult("模型服务", True), None))
        names = [r.name for r in run_selfcheck(_Engine())]
        assert "配置文件" in names

    def test_report_mentions_the_file(self, cfg_dir):
        loader = _broken_loader(cfg_dir, "bot.toml")
        loader.get_bot_config()
        report = format_report([check_config_files(self._FakeEngine(loader))])
        assert "config/bot.toml" in report
        assert "解决" in report
