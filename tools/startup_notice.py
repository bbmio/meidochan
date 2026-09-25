"""启动完成后的中文提示。

## 为什么单独放一个文件

`启动妹抖酱.bat` 必须保持**纯 ASCII**。

cmd.exe 用系统 ANSI 代码页（中文 Windows 上是 GBK）解析批处理文件，
文件里出现 UTF-8 中文会被错解 —— 不只是显示乱码，而是**真正的命令行会被吃掉**
（实测：`python -m pip install -r requirements.txt` 被错位成两条不存在的命令，
依赖安装整步静默失效）。项目里 `测试WebEngine.bat` 已经踩过这个坑并留下约定，
这里沿用同一条路：bat 里只写英文，中文由 Python 输出
（配合 bat 的 `chcp 65001`，Python 写 UTF-8 到控制台即可正常显示）。

不要因为「就加一句提示」把中文搬回 bat。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.console_window import console_enabled  # noqa: E402


def main() -> int:
    print()
    print("=" * 60)
    print("  启动完成！桌面窗口已出现。")
    print("=" * 60)
    print()
    print("提示：桌面窗口会立即出现，但插件 / 工作空间 / 向量模型就绪")
    print("      需要 10-30 秒，稍等片刻即可开始对话。")
    print()

    if console_enabled():
        print("后台调试窗口：已开启（config/bot.toml 的 show_console = true）")
        print("              会另开一个控制台窗口，实时显示日志与 print。")
    else:
        print("后台调试窗口：已关闭（默认）。需要实时看日志时，")
        print("              把 config/bot.toml 的 [logging] show_console 改成 true")
        print("              再重新启动即可，无需换 exe 或重新安装。")

    print()
    print("日志文件：data/logs/meido.log")
    print("          托盘右键 →「查看日志」也可直接打开。")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
