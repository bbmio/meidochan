"""妹抖酱 - 入口（PySide6 桌面版）"""
import sys
from pathlib import Path

# 把「应用目录」放到 sys.path 最前，这样无论 cwd 在哪，core/ ui_qt/ plugins/ 都能导入。
# ⚠️ 这里必须自己算一次 APP_DIR（与 core/paths.py 同逻辑），因为 core 还没导入；
#    打包后 __file__ 指向 _MEIPASS（内部临时目录），**不能**用它来定位外置的 plugins/。
if getattr(sys, "frozen", False):
    _APP_DIR = Path(sys.executable).resolve().parent
else:
    _APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_APP_DIR))


def _run() -> int:
    """真正的启动流程。

    整体包成函数是为了让下面 `__main__` 的兜底能覆盖**全部**导入 ——
    少一个依赖、写错一个路径，都能被兜底接住并显示出来，而不是静默退出。
    """
    # 后台调试窗口：按 config/bot.toml 的 [logging] show_console 决定是否分配控制台。
    # ⚠️ 必须早于 core.logging_utils 的导入 —— 那个模块在导入时就执行 setup_logging()，
    #    并依据「当时有没有控制台」决定要不要把 print 接管进日志文件。
    from core.console_window import describe_state, ensure_console
    ensure_console()

    from core.logging_utils import guard_console_streams, log, setup_logging

    # 无控制台模式（--noconsole）下 sys.stdout 为 None，先兜住再谈编码
    guard_console_streams()

    # 输出编码兜底：模型回复/群聊内容里可能含 emoji，GBK 控制台下 print 会抛
    # UnicodeEncodeError（实测复现：'\U0001f4ad'）。这里只放宽错误处理、不改编码，
    # 保证中文仍正常显示，遇到无法编码的字符退化为 '?' 而不是让程序崩。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")
        except Exception:
            pass

    # 文件日志（data/logs/meido.log）+ 控制台日志，尽早生效以捕获启动期异常
    setup_logging()
    log.info("[启动] %s", describe_state())

    from ui_qt.app import main
    return main()


if __name__ == "__main__":
    try:
        sys.exit(_run())
    except SystemExit:
        raise
    except BaseException:
        # 无控制台模式（打包版默认 / 源码版 pythonw）下崩溃会让程序「一闪而过」，
        # 用户看不到任何原因。这里强制开一个窗口把 traceback 摆出来并指路日志文件 ——
        # 崩溃时多一个窗口，比安静地死掉有用得多。
        import traceback

        text = traceback.format_exc()
        try:
            from core.console_window import force_console, log_file_hint
            shown = force_console()
        except Exception:
            shown = False
            log_file_hint = None            # noqa: F811

        if shown:
            print(text, flush=True)
            hint = log_file_hint() if log_file_hint else None
            if hint is not None:
                print(f"\n完整日志：{hint}", flush=True)
            try:
                input("按回车关闭…")
            except Exception:
                pass
        else:
            try:
                sys.stderr.write(text)
                sys.stderr.flush()
            except Exception:
                pass
        sys.exit(1)
