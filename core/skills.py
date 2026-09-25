"""
Skill 库管理 — 渐进式披露的「能力包」

设计原则（依据文档第二章「Skills 按需加载」）：
1. 标准 SKILL.md 格式（与 .codebuddy/skills 一致）：YAML frontmatter（name + description）+ 正文
2. 零改造接入：把别人的 skill 目录丢进 skills/ 即可，启动自动扫描注册
3. 第一层（元数据常驻）：只把 name + description 注入 system prompt，让模型「知道有哪些 skill」
4. 第二层（按需加载）：模型判断需要时，用 load_skill 工具加载完整 SKILL.md 正文
"""
import re
from pathlib import Path
from typing import Dict, List, Optional

from core.paths import app_path

SKILLS_DIR = str(app_path("skills"))


def _parse_frontmatter(text: str) -> Dict[str, str]:
    """解析 SKILL.md 的 YAML frontmatter（--- 包裹），提取 name / description 等字段。"""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.DOTALL)
    if not m:
        return {}
    lines = m.group(1).split("\n")
    meta: Dict[str, str] = {}
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        kv = re.match(r"^([A-Za-z_][A-Za-z0-9_\-]*):\s*(.*)$", stripped)
        if not kv:
            i += 1
            continue
        key = kv.group(1)
        val = kv.group(2).strip()

        if val.startswith('"') or val.startswith("'"):
            quote = val[0]
            # 单行引号
            if len(val) >= 2 and val.endswith(quote):
                meta[key] = val[1:-1]
            else:
                # 多行引号：收集直到闭合引号
                buf = [val[1:]]
                i += 1
                while i < len(lines):
                    buf.append(lines[i])
                    if lines[i].rstrip().endswith(quote):
                        break
                    i += 1
                meta[key] = "\n".join(buf).rstrip().rstrip(quote).rstrip()
        else:
            meta[key] = val
        i += 1
    return meta


def discover_skills(skills_dir: str = SKILLS_DIR) -> List[Dict[str, str]]:
    """扫描 skills 目录，返回 [{name, description, path, model_invokable}]。"""
    root = Path(skills_dir)
    if not root.exists():
        return []
    skills = []
    for sk_dir in sorted(root.iterdir()):
        if not sk_dir.is_dir():
            continue
        md = sk_dir / "SKILL.md"
        if not md.exists():
            continue
        try:
            text = md.read_text(encoding="utf-8")
            meta = _parse_frontmatter(text)
            name = meta.get("name") or sk_dir.name
            description = meta.get("description", "")
            # disable-model-invocation: true 表示该 skill 只允许用户手动触发，模型不应自动调用
            model_invokable = meta.get("disable-model-invocation", "").strip().lower() != "true"
            skills.append({
                "name": name,
                "description": description,
                "path": str(md),
                "model_invokable": model_invokable,
            })
        except Exception as e:
            print(f"  [Skills] 解析 {md} 失败: {e}")
    return skills


def list_skill_names(skills_dir: str = SKILLS_DIR) -> List[str]:
    return [s["name"] for s in discover_skills(skills_dir)]


def load_skill_content(name: str, skills_dir: str = SKILLS_DIR) -> Optional[str]:
    """按 name（或目录名）加载 SKILL.md 全文，找不到返回 None。"""
    root = Path(skills_dir)
    if not root.exists():
        return None
    for sk_dir in sorted(root.iterdir()):
        if not sk_dir.is_dir():
            continue
        md = sk_dir / "SKILL.md"
        if not md.exists():
            continue
        try:
            meta = _parse_frontmatter(md.read_text(encoding="utf-8"))
            if meta.get("name") == name or sk_dir.name == name:
                return md.read_text(encoding="utf-8")
        except Exception:
            continue
    return None


def build_skills_prompt(skills_dir: str = SKILLS_DIR) -> str:
    """生成注入 system prompt 的 skills 元数据清单（只放 name + description，不塞正文）。"""
    skills = discover_skills(skills_dir)
    invokable = [s for s in skills if s["model_invokable"]]
    if not invokable:
        return ""
    lines = []
    for s in invokable:
        desc = (s["description"] or "").replace("\n", " ").strip()
        if len(desc) > 240:
            desc = desc[:240] + "…"
        lines.append(f"- `{s['name']}`：{desc}")
    header = (
        "\n\n## 我的 Skills（按需加载）\n"
        "以下是我已安装的技能包。当任务涉及对应领域时，先调用 `load_skill` 工具加载完整说明，再按其指引执行。\n"
    )
    return header + "\n".join(lines)
