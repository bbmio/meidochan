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

#: 工具调用约定（默认提示词末尾的通用说明）
TOOL_USAGE_GUIDE = """
### 工具调用约定
- 工具名只能来自上面的清单，不要自己发明；参数按说明填全，缺参数会被拒绝。
- 一次只做一轮工具调用：拿到结果先判断够不够，再决定要不要继续。
- 工具返回里若带了「可用清单」或纠错提示，按它换一个再试，不要重复同样的调用。
- 只读工具（搜索 / 读文件 / 查状态 / 查知识库）可以直接调；
  写入或不可逆操作先说明计划，征得用户同意再执行。
"""

#: 立绘表情工具名（与 plugins/live2d_control 的 TOOL_SET_EXPRESSION 同一份）
LIVE2D_SET_EXPRESSION_TOOL = "set_live2d_expression"

#: 立绘表情自主选择规则 —— 只在「本机有模型、工具已暴露」时才追加
LIVE2D_EXPRESSION_RULE = """
### 我的立绘表情（每轮都要自己做）
桌面上的立绘由你控制，**每一轮回复都自己挑一个表情**（调用 `set_live2d_expression`）：
- 依据是你这一轮的语气和心情（开心 / 害羞 / 认真 / 困惑 / 得意…），不是用户的要求；
- 名字必须来自工具说明里的表情清单，挑最接近的那个，不要造名字；
- 一轮只切一次，先切表情再说话；表情会保持到本轮结束，之后自动恢复默认脸；
- 只有用户明确说「别笑了」「表情收一收」时，才用 `clear_live2d_expression` 清掉；
- 这和「执行任务时不带口癖」不冲突：立绘只是你的表现，回答内容照旧保持专业。
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

def _plugin_tools(plugin_manager, allow_risky_tools: bool) -> List[dict]:
    """取插件工具定义；枚举失败返回空列表（提示词不该因为工具枚举失败而构建不出来）。"""
    try:
        return list(plugin_manager.get_tool_definitions(
            allow_risky_tools=allow_risky_tools) or [])
    except Exception:
        return []


def _format_tool_list(tools: List[dict]) -> str:
    """把 OpenAI 工具定义渲染成提示词里的清单。"""
    lines = []
    for t in tools:
        fn = t.get("function") or {}
        name = fn.get("name", "")
        desc = (fn.get("description") or "").replace("\n", " ")[:120]
        if name:
            lines.append(f"- `{name}`：{desc}")
    return "\n".join(lines) or "- （当前没有可用的插件工具）"


def get_plugin_tool_text(plugin_manager, allow_risky_tools: bool = False) -> str:
    """从插件管理器动态生成工具列表（真实能力）"""
    return _format_tool_list(_plugin_tools(plugin_manager, allow_risky_tools))


def build_self_knowledge_prompt(plugin_manager, memory_summary: str = "",
                                allow_risky_tools: bool = False) -> str:
    """
    构建完整的自我认知提示词，注入 system prompt。
    memory_summary: 全局记忆库的内容摘要（读过的文件记忆）
    allow_risky_tools: 风险工具开关；关闭时高风险工具不进清单（与给模型的实际工具保持一致）
    """
    tools = _plugin_tools(plugin_manager, allow_risky_tools)
    tool_text = _format_tool_list(tools)
    skills_text = build_skills_prompt(str(app_path("skills")))

    parts = [
        "\n\n##  自我认知（我是谁，我会什么）",
        PROJECT_MAP.format(project_dir=APP_DIR),
        SELF_CHECK_PROTOCOL,
        TOOL_LIST_TEMPLATE.format(tools=tool_text),
        TOOL_USAGE_GUIDE,
    ]
    # 立绘表情规则只在工具真的可用时追加 —— 没有模型时不提，免得她反复调一个不存在的工具
    if any((t.get("function") or {}).get("name") == LIVE2D_SET_EXPRESSION_TOOL for t in tools):
        parts.append(LIVE2D_EXPRESSION_RULE)
    if skills_text:
        parts.append(skills_text)
    if memory_summary:
        parts.append(f"\n{memory_summary}")
    return "\n".join(parts)
