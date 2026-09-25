"""
自我认知模块 — 让 Agent 知道自己是谁、会什么、怎么自查

设计原则（依据用户决策）：
1. 自省 = 用工具看，不是背提示词。提示词只放"项目地图"+"自省协议"
2. 全局记忆库 = 结构化 JSON，跨工作空间共享，可增量更新
3. 不确定先反问，确认用户意图后再执行
4. 指令驱动深度：用户说"读整个文件夹"就递归读，说"看某文件"就看单个
5. 一次最多一轮工具的读取，看完询问是否继续
"""
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from core.paths import APP_DIR, app_path
from core.skills import build_skills_prompt

# ═══════════════════════════════════════════════════════════
# 项目地图（稳定部分 — 目录层级，不写具体文件内容）
# ═══════════════════════════════════════════════════════════

PROJECT_MAP = """
## 我的项目地图（我的代码在哪里）

我的完整代码位于 `{project_dir}`（即工作目录；打包后就是 exe 所在目录），结构如下：

- `main.py` — 入口，启动桌面窗口（PySide6）
- `core/` — 核心引擎
  - `engine.py` — WhaleGirlEngine 调度器（处理消息、路由命令）
  - `brain.py` — Brain（LLM 调用、流式输出、工具循环）
  - `context_engine.py` — ContextEngine（构建分层 system prompt）
  - `history.py` — HistoryManager（对话历史）
  - `paths.py` — 路径层（APP_DIR / RESOURCE_DIR，所有目录一律走它）
  - `config/` — 配置加载器（loader.py + models.py）
  - `plugins/manager.py` — 插件管理器
  - `memory/` — 长期记忆（预留）
  - `self_knowledge.py` — 就是本文件，我的自我认知模块
- `ui_qt/` — 桌面界面（main_window / chat_view / sidebar / stage_view / settings_dialog / theme / media）
- `workspace/` — 工作空间管理器（models/storage/manager）
- `workspaces/` — 工作空间数据（每个空间独立历史/记忆/人设；`workspace.toml` 的 `persona_prompt` 非空时会覆盖全局人设）
- `plugins/` — 插件目录（每个子目录有 main.py + manifest.json）
- `config/` — TOML 配置（model.toml 模型、persona.toml 全局人设、identity.toml 用户身份与固定规则 pins）
- `roles/` — 角色文件（*.toml，每个文件一个角色）
- `skills/` — Skills 能力包（每个子目录一个 SKILL.md，按需加载）
"""

# 用户希望回答"先浅后深"，这里提供分层指引
SELF_CHECK_PROTOCOL = """
## 我的自省协议（如何正确认识自己）

当用户问关于"你自己"的问题时（你能做什么 / 你的架构 / 你的配置 / 如何修改等），遵循以下协议：

### 第一步：先查记忆，再决定是否读文件
- 先看我的全局记忆库（`workspaces/_global/self_knowledge.json`），里面已有我对代码的认知
- 如果记忆库已有答案  直接回答
- 如果没有或用户明确要求最新信息  用工具现场查看

### 第二步：用工具看，不要背
- 查看文件列表用 `/ls` 工具
- 查看文件内容用 `/view` 工具（支持分段：`/view 文件 100-200` 或 `/view 文件 all`）
- 查看插件用 `/ls plugins` + `/view plugins/<名字>/manifest.json`

### 第三步：不确定就反问
如果用户的指令模糊（例如只说"看看你的代码"，没说全量还是某个文件），
必须先反问确认意图，例如：
- "您是想让我通读整个项目，还是只看某个模块（比如 core/ 或 config/）？"
- "您是希望我理解架构，还是要我修改某段代码？"
不要盲目执行。

### 第四步：一次一轮，看完询问
- 每次最多连续读取约 8 个文件（或一轮工具调用）
- 读完一批后，向用户汇报"我已了解这些内容"，并询问是否继续
- 得到用户确认后再读取下一批
- 单个大文件用 `/view 文件 开始行-结束行` 分段读完

### 回答风格
- 先浅层回答（一句话总结），用户追问再深入细节
- 不隐瞒：读到什么说什么，读不到的如实告知
"""

TOOL_LIST_TEMPLATE = """
### 我的工具清单（动态生成 — 来自插件注册表）
当用户明确要求执行以下操作时，通过函数调用执行：
{tools}
"""

# ═══════════════════════════════════════════════════════════
# 全局记忆库（结构化 JSON，跨工作空间共享）
# ═══════════════════════════════════════════════════════════

GLOBAL_MEMORY_FILE = "self_knowledge.json"


def _global_memory_path() -> Path:
    return app_path("workspaces", "_global", GLOBAL_MEMORY_FILE)


def load_global_memory() -> dict:
    """加载全局记忆库（不存在则返回空结构）"""
    path = _global_memory_path()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_global_memory(data: dict):
    """保存全局记忆库"""
    path = _global_memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def record_file_knowledge(file_path: str, summary: str, category: str = ""):
    """
    记录对某个文件的理解，增量更新全局记忆。
    file_path: 相对项目根目录的路径，如 "core/engine.py"
    summary: 对该文件功能的一句话理解
    """
    data = load_global_memory()
    files = data.setdefault("files", {})
    files[file_path] = {
        "path": file_path,
        "category": category,
        "summary": summary,
        "updated_at": datetime.now().isoformat(),
    }
    data["last_updated"] = datetime.now().isoformat()
    save_global_memory(data)
    return data


def build_memory_prompt() -> str:
    """构建记忆库内容摘要，注入 system prompt"""
    data = load_global_memory()
    files = data.get("files", {})
    if not files:
        return "（我的全局记忆库尚为空，尚未通读过自己的代码。用户可以让我'看看你的代码'来建立认知。）"
    lines = [f"- `{fp}`：{info.get('summary', '')}" for fp, info in files.items()]
    return "我对自己代码的记忆：\n" + "\n".join(lines)


def clear_global_memory() -> str:
    """清空全局记忆库"""
    save_global_memory({})
    return "全局记忆库已清空"


# ═══════════════════════════════════════════════════════════
# 动态工具列表（从插件注册表生成，保证与实际一致）
# ═══════════════════════════════════════════════════════════

def get_plugin_tool_text(plugin_manager) -> str:
    """从插件管理器动态生成工具列表（真实能力）"""
    tools = plugin_manager.get_tool_definitions()
    lines = []
    for t in tools:
        name = t.get("function", {}).get("name", "")
        desc = t.get("function", {}).get("description", "").replace("\n", " ")[:120]
        if name:
            lines.append(f"- `{name}`：{desc}")
    if not lines:
        lines = ["- （当前没有可用的插件工具）"]
    return "\n".join(lines)


def build_self_knowledge_prompt(plugin_manager, memory_summary: str = "") -> str:
    """
    构建完整的自我认知提示词，注入 system prompt。
    memory_summary: 全局记忆库的内容摘要（读过的文件记忆）
    """
    tool_text = get_plugin_tool_text(plugin_manager)
    skills_text = build_skills_prompt(str(app_path("skills")))

    parts = [
        "\n\n##  自我认知（我是谁，我会什么）",
        PROJECT_MAP.format(project_dir=APP_DIR),
        SELF_CHECK_PROTOCOL,
        TOOL_LIST_TEMPLATE.format(tools=tool_text),
    ]
    if skills_text:
        parts.append(skills_text)
    if memory_summary:
        parts.append(f"\n{memory_summary}")
    return "\n".join(parts)
