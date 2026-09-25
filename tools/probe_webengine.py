"""QtWebEngine 环境自检（临时诊断工具，确认完可以删）。

为什么需要它：Live2D 依赖 QtWebEngine 渲染。开发环境里 Chromium 沙箱无法启动，
必须加 `--no-sandbox` 才行；但这可能只是开发容器的限制，你的正常桌面环境未必如此。
所以这里把几种组合各起一个**独立子进程**（Chromium 开关只在进程启动时读取一次，
同进程内改不了），把结论一次性列出来。

运行方式：双击项目根目录的「测试WebEngine.bat」。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 以脚本方式运行时 sys.path[0] 是 tools/，不是项目根 —— 补上才能 import core
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.live2d_assets import scan_model_assets  # noqa: E402

MODEL_DIR = ROOT / "assets" / "live2d" / "DS鲸鱼娘"
VIEWER = ROOT / "assets" / "live2d" / "viewer" / "index.html"
MODEL_JSON = MODEL_DIR / "c_0120.model3.json"

# (标签, Chromium 开关, 是否顺便试加载 Live2D viewer)
# 只保留必要的 3 项：file:// 的 fetch 限制已实测不存在，不需要 --allow-file-access-from-files。
CASES = [
    ("不带任何开关（只测能否加载页面）", "", False),
    ("--no-sandbox（只测能否加载页面）", "--no-sandbox", False),
    ("--no-sandbox（实测 Live2D 模型）", "--no-sandbox", True),
]

PAGE_WAIT = 12       # 页面要么几秒内加载完，要么就是失败，不必等 25 秒
CASE_TIMEOUT = 75    # 单个子进程上限


def build_manifest() -> str:
    # 动作/表情清单走 core/live2d_assets 的单一出处（与播放器、设置界面同一份）
    motions, exprs = scan_model_assets(MODEL_DIR)
    payload = json.dumps({"motions": motions, "expressions": exprs},
                         ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def run_worker() -> None:
    """子进程：测 WebEngine 能否加载页面（可选再测 Live2D 模型）。"""
    import time

    from PySide6.QtCore import QUrl, QUrlQuery
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWebEngineWidgets import QWebEngineView

    with_model = "--with-model" in sys.argv
    app = QApplication([])
    view = QWebEngineView()
    view.resize(600, 700)

    if with_model:
        rel = os.path.relpath(MODEL_JSON, VIEWER.parent).replace("\\", "/")
        url = QUrl.fromLocalFile(str(VIEWER))
        query = QUrlQuery()
        query.addQueryItem("model", rel)
        query.addQueryItem("manifest", build_manifest())
        url.setQuery(query)
    else:
        url = QUrl("about:blank")

    loaded: list = []
    view.loadFinished.connect(lambda ok: loaded.append(bool(ok)))
    view.load(url)

    def pump(seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            app.processEvents()
            time.sleep(0.1)

    pump(PAGE_WAIT)
    if not loaded:
        print("WORKER|page=timeout", flush=True)
        return
    if not loaded[0]:
        print("WORKER|page=failed", flush=True)
        return

    # 页面能加载 → 再验 JS 是否可执行
    js_result: list = []
    view.page().runJavaScript("1+1", 0, lambda v: js_result.append(v))
    pump(8)
    if not js_result:
        print("WORKER|page=ok|js=none", flush=True)
        return

    if not with_model:
        print(f"WORKER|page=ok|js={js_result[0]}", flush=True)
        return

    # Live2D：轮询 viewer 暴露的状态出口
    state = {}
    for _ in range(30):
        box: list = []
        view.page().runJavaScript(
            "JSON.stringify({ready:!!(window.MeidoLive2D&&MeidoLive2D.ready),"
            "error:(window.MeidoLive2D&&MeidoLive2D.error)||null,"
            "motions:((window.MeidoLive2D&&MeidoLive2D.motions)||[]).length,"
            "expressions:((window.MeidoLive2D&&MeidoLive2D.expressions)||[]).length})",
            0, lambda v: box.append(v))
        pump(1.0)
        if box and box[0]:
            state = json.loads(box[0])
        if state.get("ready") or state.get("error"):
            break
    print("WORKER|page=ok|js=%s|ready=%s|motions=%s|expressions=%s|error=%s" % (
        js_result[0], state.get("ready"), state.get("motions"),
        state.get("expressions"),
        str(state.get("error"))[:160].replace("\n", " ") or "无"), flush=True)


def precheck() -> bool:
    """先确认当前解释器能 import QtWebEngine，否则后面全是无意义的失败。"""
    print("-- 环境预检")
    try:
        import PySide6  # noqa: F401
    except ImportError:
        print("   [X] 当前 Python 没有安装 PySide6")
        print(f"     解释器：{sys.executable}")
        print("     请先执行：python -m pip install -r requirements.txt")
        return False
    try:
        from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
    except Exception as exc:
        print(f"   [X] PySide6 已装，但 QtWebEngine 不可用：{exc}")
        print("     可能是安装的是 PySide6-Essentials，需要 PySide6-Addons。")
        return False
    import PySide6
    print(f"   [OK] PySide6 {PySide6.__version__}，QtWebEngine 可用")
    print(f"     解释器：{sys.executable}")
    print()
    return True


def main() -> int:
    print("=" * 68)
    print("QtWebEngine 环境自检")
    print("=" * 68)
    print(f"Python : {sys.executable}")
    print(f"项目    : {ROOT}")
    print()

    if not precheck():
        return 2

    passed = []
    total = len(CASES)
    for index, (label, flags, with_model) in enumerate(CASES, start=1):
        env = dict(os.environ)
        if flags:
            env["QTWEBENGINE_CHROMIUM_FLAGS"] = flags
        else:
            env.pop("QTWEBENGINE_CHROMIUM_FLAGS", None)

        args = [sys.executable, str(Path(__file__).resolve()), "--worker"]
        if with_model:
            args.append("--with-model")
        print(f"[{index}/{total}] {label}")
        print(f"        测试中，这一项最长约 {CASE_TIMEOUT} 秒…", flush=True)
        try:
            proc = subprocess.run(args, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  env=env, timeout=CASE_TIMEOUT, cwd=str(ROOT))
            line = next((l for l in (proc.stdout or "").splitlines()
                         if l.startswith("WORKER|")), "")
            if line:
                print(f"        结果：{line.replace('WORKER|', '')}")
                if "page=ok" in line:
                    passed.append(label)
            else:
                print("        结果：子进程没有回报结果")
                # 把子进程的报错原样带出来，否则用户无从下手
                tail = ((proc.stderr or "") + (proc.stdout or "")).strip()
                for raw_line in tail.splitlines()[-6:]:
                    if raw_line.strip():
                        print(f"        | {raw_line.strip()[:150]}")
                if not tail.strip():
                    print(f"        | 子进程无任何输出，退出码 {proc.returncode}")
        except subprocess.TimeoutExpired:
            print(f"        结果：超时（>{CASE_TIMEOUT}s）")
        except Exception as exc:
            print(f"        结果：启动失败 {type(exc).__name__}: {exc}")
        print(flush=True)

    print("=" * 68)
    if not passed:
        print("结论：QtWebEngine 在本机**完全无法加载页面**，Live2D 走 WebEngine 这条路不可行。")
    else:
        print("结论：以下组合可以加载页面 ——")
        for label in passed:
            print(f"   [OK] {label}")
        if not any("不带任何开关" in p for p in passed):
            print()
            print("注意：不带开关时无法加载，说明本机必须加 --no-sandbox，")
            print("      即 WebEngine 的 Chromium 沙箱在此环境下无法启动。")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    if "--worker" in sys.argv:
        run_worker()
    else:
        sys.exit(main())
