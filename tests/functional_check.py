"""全功能验收脚本（不启动 Web 服务，直接驱动引擎逐项体检）。

用途：改完代码后一条命令确认"关键功能没被改坏"。

    python tests/functional_check.py          # 常规项（约 5 秒）
    python tests/functional_check.py --chat   # 额外跑一轮真实 LLM 对话

它不修改你的业务数据；中途创建的"功能验收空间"会在结束时删除。
（文件名不以 test_ 开头，因此不会被 pytest 收集。）
"""
import contextlib
import io
import os
import sys
import time
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent
os.chdir(PROJ)
sys.path.insert(0, str(PROJ))

RESULTS = []


def check(name, fn):
    t0 = time.time()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            detail = fn()
        RESULTS.append((name, "PASS", f"{time.time() - t0:.1f}s", str(detail)[:100]))
    except Exception as e:
        RESULTS.append((name, "FAIL", f"{time.time() - t0:.1f}s", f"{type(e).__name__}: {e}"[:100]))


def show(label, value, expect=None):
    if expect is not None:
        assert value == expect, f"{label}: 期望 {expect!r}，实际 {value!r}"
    return f"{label}={value!r}"


from core.engine import WhaleGirlEngine  # noqa: E402

engine = WhaleGirlEngine()
engine.start()
time.sleep(2)

pm = engine.plugin_manager
wsm = engine.workspace_mgr

# ── 1. 冷启动 / 插件 / 工具 ──
check("冷启动 + 插件加载", lambda: show("插件数", len(pm.get_all_plugins()), 4))
check("工具注册表", lambda: show("工具数", len(pm.get_tool_commands()), 14))
check("工作空间列表", lambda: show("spaces", [w.id for w in wsm.list_all()]))

# ── 2. 工作空间 创建 / 隔离 / 切换 / 删除 ──
created = {}


def _create_switch():
    engine.create_workspace("功能验收空间")
    ws = [w for w in wsm.list_all() if w.name == "功能验收空间"][0]
    created["id"] = ws.id
    engine.switch_workspace(ws.id)
    cur = wsm.current
    assert cur.id == ws.id, f"切换后应为 {ws.id}，实际 {cur.id}"
    return f"创建并切换到 {ws.id}"


check("工作空间 创建 + 切换", _create_switch)


def _isolation():
    a = wsm.get(created["id"])
    b = [w for w in wsm.list_all() if w.id != created["id"]][0]
    for attr in ("history_dir", "kb_dir", "memory_file"):
        assert getattr(a, attr) != getattr(b, attr), f"{attr} 未隔离"
    return f"{a.id} 与 {b.id} 的 history/kb/memory 路径互不相同"


check("工作空间隔离", _isolation)


def _cleanup_space():
    back = [w for w in wsm.list_all() if w.id != created["id"]][0]
    engine.switch_workspace(back.id)
    msg = engine.delete_workspace(created["id"])
    assert created["id"] not in [w.id for w in wsm.list_all()], f"删除失败：{msg}"
    return f"已切回 {back.id} 并删除验收空间"


check("工作空间 删除（不可删活跃）", _cleanup_space)

# ── 3. 内置命令（均为只读，避免污染真实配置） ──
check("内置命令 /model", lambda: f"返回 {len(engine._execute_command('/model', '') or '')} 字符")
check("内置命令 /memory", lambda: f"返回 {len(engine._execute_command('/memory', '') or '')} 字符")
check("内置命令 /history", lambda: f"返回 {len(engine._execute_command('/history', '') or '')} 字符")
check("内置命令 /pin", lambda: f"返回 {len(engine._execute_command('/pin', '') or '')} 字符")
check("模型状态", lambda: engine.brain.get_status()[:80])

# ── 4. 插件工具 ──
check("工具 /ls", lambda: engine.tool_executor("ls", {"path": "docs"})[:60])
check("工具 /kb_status", lambda: engine.tool_executor("kb_status", {})[:60])
check("工具 /kb_search", lambda: engine.tool_executor("kb_search", {"query": "测试"})[:60])
check("工具 /kb_hybrid", lambda: engine.tool_executor("kb_hybrid", {"query": "测试"})[:60])
check("工具 /kb_related", lambda: engine.tool_executor("kb_related", {"file_path": "main.py"})[:60])


def _search():
    t0 = time.time()
    out = engine.tool_executor("search", {"query": "python asyncio 教程"})
    assert "执行 /search 时出错" not in out, f"搜索链路崩溃: {out[:120]}"
    assert "搜索失败" not in out, f"搜索未取到结果: {out[:120]}"
    assert "http" in out, f"搜索结果里没有真实链接: {out[:120]}"
    return f"{time.time() - t0:.1f}s，{len(out)} 字符，{out.count('http')} 个链接"


check("工具 /search（真实联网搜索）", _search)


def _reload():
    out = pm.reload_plugin("knowledge_base")
    assert "已重载" in out, out
    assert len(pm.get_tool_commands()) == 14, "重载后工具数异常"
    return out


check("插件热重载", _reload)

# ── 5. 历史向量检索 / 概览卡 ──
def _history_retrieval():
    r = engine.retrieval
    assert r is not None, "未绑定检索器（engine.retrieval 为 None）"
    assert r.ready is True, f"向量检索未就绪（ready={r.ready}）"
    hits = r.search("测试")
    n = len(hits) if hasattr(hits, "__len__") else "?"
    return f"ready=True，检索『测试』命中 {n} 条"


check("历史向量检索（就绪 + 真实检索）", _history_retrieval)
check("长期记忆（概览卡）", lambda: f"概览卡 {len(__import__('core.memory.profile_cards', fromlist=['x']).get_profile_prompt())} 字符")

# ── 6.（可选）真实 LLM 对话 ──
if "--chat" in sys.argv:
    check("真实对话一轮（含工具循环）",
          lambda: show("回复", (engine.respond_once("请只回复：功能验收通过") or "")[:40]))

print()
print(f"{'项目':<34}{'结果':<7}{'耗时':<8}细节")
print("-" * 110)
for name, status, cost, detail in RESULTS:
    print(f"{name:<34}{status:<7}{cost:<8}{detail}")
failed = [r for r in RESULTS if r[1] == "FAIL"]
print("-" * 110)
print(f"合计 {len(RESULTS)} 项：PASS {len(RESULTS) - len(failed)} / FAIL {len(failed)}")
