"""统一日志工具（ARCHITECTURE_V3 §2 日志 / §6-M2）。

- 文件日志：`data/logs/meido.log`（RotatingFileHandler，2MB × 5 份，UTF-8）
- 控制台日志：仅在有控制台时添加。控制台可能是用户终端，也可能是
  `core/console_window.py` 按 `config/bot.toml` 的 `show_console` 分配的调试窗口
- 配置在 **root logger** 上，所以 `logging.getLogger(__name__)` 的第三方/模块日志
  也会一并落盘，排障时一个文件看全

用法：
    from core.logging_utils import log, log_file
    log.info("消息")
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from core import console_window
from core.app_info import LOG_FILE_PREFIX
from core.paths import app_path

LOGGER_NAME = "meido"
_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 5

# 第三方库日志过于啰嗦：只保留 WARNING 以上，避免把日志文件刷满
_NOISY_LOGGERS = ("chromadb", "httpx", "httpcore", "urllib3", "openai", "asyncio", "PIL")

_logger = logging.getLogger(LOGGER_NAME)
_log_file: Path = app_path("data", "logs", f"{LOG_FILE_PREFIX}.log")
_configured = False
_no_console = False      # True = 打包后的 --noconsole 运行（sys.stdout 为 None）


class _LogStream:
    """把 `print` 的输出转成日志行。

    背景：项目里还有大量历史 `print`，在无控制台形态下会静默丢失，打包后完全无法排障。
    这里把 stdout/stderr 接到 logger，print 的输出就跟着日志一起落盘、
    也会经控制台 StreamHandler 显示出来。

    **不要**再往控制台直写一份：那会让 `print` 在控制台上出现两次
    （镜像一次、日志 handler 一次）。统一走 logger 是唯一出口。

    安装时机（见 `setup_logging` 末尾）：
    - 本来就没有控制台（打包版 `--noconsole`、源码版 `pythonw`）
    - 控制台由 `core/console_window.py` 分配（`show_console = true`）

    用户自己的终端（源码模式 `python main.py`）**不装** ——
    把真 TTY 换成非 TTY 的包装对象会影响第三方库的进度条/颜色判断。

    注意：控制台 StreamHandler 持有的是**原始**流，不是 sys.stdout，
    否则会形成「日志 → 控制台 → 日志」的递归。
    """

    def __init__(self, logger: logging.Logger, level: int = logging.INFO) -> None:
        self._logger = logger
        self._level = level
        self._buffer = ""

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self._logger.log(self._level, line)
        return len(text)

    def flush(self) -> None:
        if self._buffer.strip():
            self._logger.log(self._level, self._buffer)
        self._buffer = ""

    def reconfigure(self, **kwargs) -> None:
        """兼容 main.py 的输出编码兜底调用。"""
        return None

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise OSError("日志流没有文件描述符")


def guard_console_streams() -> None:
    """无控制台模式（PyInstaller --noconsole）下 sys.stdout/stderr 为 None，
    此时任何 `print` 都会抛 AttributeError。这里接到 devnull 保证 print 不致命；
    随后 setup_logging() 会把它们换成写日志文件的 _LogStream。"""
    global _no_console
    if getattr(sys, "stdout", None) is None or getattr(sys, "stderr", None) is None:
        _no_console = True
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except OSError:
                pass


def log_file() -> Path:
    """当前日志文件路径（可能尚未创建）。"""
    return _log_file


def setup_logging(level: str = "INFO") -> Path:
    """初始化 root logger（幂等）。返回日志文件路径。"""
    global _configured
    if _configured:
        return _log_file

    guard_console_streams()

    root = logging.getLogger()
    root.setLevel(_to_level(level))

    file_formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")
    console_formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    try:
        _log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            _log_file, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8")
        file_handler.setFormatter(file_formatter)
        root.addHandler(file_handler)
    except OSError as exc:
        # 例如装在只读盘：退化为仅控制台，不影响程序运行
        print(f"[WARN] 日志文件不可写（{exc}），本次仅输出到控制台")

    # 控制台流的判定必须在替换 sys.stdout 之前做，且要排除 guard_console_streams()
    # 塞进来的 devnull —— 那是「没有控制台」的占位，不是真控制台。
    raw_console = None if _no_console else getattr(sys, "stdout", None)

    if raw_console is not None:
        # 用原始流建 handler：sys.stdout 之后会被换成 _LogStream，
        # 拿它当 handler 会形成「日志 → 控制台 → 日志」的递归
        console_handler = logging.StreamHandler(raw_console)
        console_handler.setFormatter(console_formatter)
        root.addHandler(console_handler)

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    # print 接管：只在「本来就没有控制台」或「控制台是我们分配的」时装。
    # 用户自己的终端保持原样 —— 换成非 TTY 包装流会影响第三方库的行为。
    # 判断依据是 `_no_console` 而不是「sys.stdout 是否为 None」：
    # 后者在 Git Bash 的管道下也是非 None，但那种情况用户本来就看得见输出。
    if _no_console or console_window.allocated_by_us():
        sys.stdout = _LogStream(logging.getLogger("stdout"))
        sys.stderr = _LogStream(logging.getLogger("stderr"), logging.WARNING)

    _configured = True
    return _log_file


def set_log_level(level: str) -> None:
    """按配置（config/bot.toml 的 [logging] level）调整日志级别。"""
    logging.getLogger().setLevel(_to_level(level))


def _to_level(level: str) -> int:
    return getattr(logging, str(level or "INFO").upper(), logging.INFO)


class _Log:
    """便捷门面：log.info / log.warning / log.error / log.debug。

    ⚠️ `warn` 是 `warning` 的别名，两个都留着。历史上这里**只有 `warn`**，
    而 `ui_qt/main_window.py` 的自检告警写的是 `log.warning` —— 结果自检一发现问题
    就在这一行抛 `AttributeError`，**侧栏状态与警告框全都没显示**，用户根本不知道
    自己哪里配错了。别再把标准级别名删掉。
    """

    def info(self, *args, **kwargs):
        _logger.info(*args, **kwargs)

    def warning(self, *args, **kwargs):
        _logger.warning(*args, **kwargs)

    #: 别名，等价于 warning（`logging` 里 warn 是已废弃写法，这里保留以兼容老调用）
    warn = warning

    def error(self, *args, **kwargs):
        _logger.error(*args, **kwargs)

    def debug(self, *args, **kwargs):
        _logger.debug(*args, **kwargs)


log = _Log()

# 导入即生效：保证任何入口（main.py / 测试）都有可用的日志落盘
setup_logging()
