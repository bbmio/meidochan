"""工作空间存储层测试（对应阶段二批次 B2：TOML 序列化与加载容错）

覆盖 Phase 1 体检发现的 P0-2：
1. persona 含英文双引号时，序列化结果必须是**合法** TOML（旧实现在这里写出非法文件，
   导致下次启动解析失败、程序永远起不来）；
2. 配置文件损坏时 load() 不得抛异常（旧实现会把整个启动流程带崩）。

运行：python -m pytest tests -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from workspace.models import Workspace
from workspace.storage import WorkspaceStorage, _simple_toml_dumps, _toml, _toml_dumps

# 容易把"手写序列化器"打穿的真实人设片段
NASTY_TEXTS = [
    'He said "hi"',                 # 英文双引号（P0-2 的触发条件）
    "反斜杠路径 C:\\Users\\A",        # 反斜杠
    "第一行\n第二行",                 # 换行
    '三引号 """ 也要稳',              # 三引号
    "单引号 'ok' 与制表符\t结束",       # 单引号 + 制表符
    "中文括号（）与 emoji 🐳",         # 非 ASCII
]


@pytest.mark.parametrize("dumps", [_toml_dumps, _simple_toml_dumps], ids=["tomli_w", "fallback"])
@pytest.mark.parametrize("text", NASTY_TEXTS)
def test_persona_round_trip(dumps, text):
    """两条序列化路径都必须满足"写出去一定读得回来"。"""
    data = Workspace(id="demo", name="演示", persona_prompt=text).to_dict()
    parsed = _toml.loads(dumps(data))
    assert parsed["workspace"]["persona_prompt"] == text


def test_plugin_list_round_trip():
    """插件名列表里的引号同样不能被漏掉（fallback 路径）。"""
    data = Workspace(id="demo", name="演示", plugin_enabled=["a", 'b"c']).to_dict()
    parsed = _toml.loads(_simple_toml_dumps(data))
    assert parsed["workspace"]["plugin_enabled"] == ["a", 'b"c']


def test_save_then_load_with_quote(tmp_path):
    """P0-2 回归：保存含双引号的人设后重新读取必须成功。"""
    storage = WorkspaceStorage(str(tmp_path / "workspaces"))
    ws = Workspace(id="quote", name="引号测试", persona_prompt='他说"你好"\n第二行')
    storage.save(ws)

    loaded = storage.load("quote")
    assert loaded is not None
    assert loaded.persona_prompt == ws.persona_prompt
    assert loaded.name == ws.name


def test_load_corrupt_file_does_not_raise(tmp_path):
    """损坏的 workspace.toml 不得让启动崩溃，且应留下 .bak 备份。"""
    root = tmp_path / "workspaces"
    bad_dir = root / "broken"
    bad_dir.mkdir(parents=True)
    (bad_dir / "workspace.toml").write_text(
        '[workspace]\nid = "broken"\nname = "未闭合的引号\n', encoding="utf-8"
    )

    storage = WorkspaceStorage(str(root))
    ws = storage.load("broken")  # 旧实现：这里直接抛异常，启动失败

    assert ws is not None
    assert ws.id == "broken"
    assert list(bad_dir.glob("workspace.toml.bak-*")), "应生成备份文件"


def test_list_all_survives_broken_workspace(tmp_path):
    """list_all() 内部会 load()：坏文件夹在好文件中间时，好文件不能被带崩。"""
    root = tmp_path / "workspaces"
    storage = WorkspaceStorage(str(root))
    storage.save(Workspace(id="ok", name="正常空间"))

    bad = root / "bad"
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "workspace.toml").write_text("这不是 TOML = = =\n", encoding="utf-8")

    assert "ok" in [w.id for w in storage.list_all()]
