"""启动前的中文提示（由 `启动妹抖酱.bat` 调用）。

## 为什么单独放一个文件

`启动妹抖酱.bat` 必须保持**纯 ASCII**。

cmd.exe 用系统 ANSI 代码页（中文 Windows 上是 GBK）解析批处理文件，
文件里出现 UTF-8 中文会被错解 —— 不只是显示乱码，而是**真正的命令行会被吃掉**
（实测：`python -m pip install -r requirements.txt` 被错位成两条不存在的命令，
依赖安装整步静默失效）。项目里 `测试WebEngine.bat` 已经踩过这个坑并留下约定，
这里沿用同一条路：bat 里只写英文，中文由 Python 输出
（配合 bat 的 `chcp 65001`，Python 写 UTF-8 到控制台即可正常显示）。

不要因为「就加一句提示」把中文搬回 bat。

## 这条提示现在讲什么

bat 已经改成**在当前控制台里直接跑 `python main.py`**（有头启动），
所以这个控制台**就是**实时调试窗口 —— 日志与 print 都会打在这里。
（`core/logging_utils.py` 只要发现存在控制台就会挂 `StreamHandler`；
`core/console_window.py` 也绝不会去动不是它自己分配的控制台。）

因此这里不再讲「启动完成」，而是在启动**之前**交代清楚：窗口别关、
日志在哪、想要无控制台就用打包好的 exe。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.console_window import console_enabled  # noqa: E402


def main() -> int:
    print()
    print("=" * 60)
    print("  即将启动妹抖酱（有头模式）")
    print("=" * 60)
    print()
    print("这个控制台窗口就是实时调试窗口 —— 日志与 print 都打在这里。")
    print("启动期间窗口会先出现，插件 / 工作空间 / 向量模型就绪还需几秒。")
    print()
    print("⚠️ 关闭这个窗口会一并结束妹抖酱。想让它后台常驻，请用托盘")
    print("   右键 →「退出」，或者直接双击打包好的 exe（无控制台）。")
    print()

    if console_enabled():
        print("另外：config/bot.toml 的 show_console = true，所以程序还会")
        print("      再分配一个控制台。两者内容相同，关掉一个不影响运行。")
        print()

    print("日志文件：data/logs/meido.log")
    print("          托盘右键 →「查看日志」也可直接打开。")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
