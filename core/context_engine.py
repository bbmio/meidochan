"""
llmenv 式上下文引擎
管理 identity / persona / pins，构建分层 system prompt

对标 llmenv v2.0:
  identity  全局用户身份和偏好
  persona   基础人设（工作空间可覆盖）
  pins      持久化规则（用户纠正、偏好设置）
"""
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from core.paths import APP_DIR, app_path
from core.config.models import IdentityConfig

logger = logging.getLogger(__name__)


def _toml_scalar(value) -> str:
    """按 TOML 规则序列化单个标量。

    字符串统一走 JSON 转义（JSON 转义规则是 TOML 基本字符串的合法子集），
    保证内容含英文双引号/换行时也能被原样读回。
    """
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


class ContextEngine:
    """上下文引擎：构建分层 system prompt，支持工作空间人设覆盖 + Role 注入"""

    def __init__(self, config_dir: str | None = None):
        self.config_dir = Path(config_dir) if config_dir else app_path("config")
        self._identity_cache: Optional[dict] = None
        self._persona_cache: Optional[dict] = None
        self._workspace_persona: str = ""  # 工作空间级别的人设覆盖
        self._active_role_prompt: str = ""  # 当前激活的 Role 追加 prompt

    def _load_toml(self, path: Path) -> dict:
        try:
            import tomllib
            with open(path, "rb") as f:
                return tomllib.load(f)
        except ImportError:
            try:
                import tomli
                with open(path, "rb") as f:
                    return tomli.load(f)
            except ImportError:
                print(" 需要 tomllib (Python 3.11+) 或 tomli")
                return {}
        except Exception as e:
            print(f" 加载 {path} 失败: {e}")
            return {}

    # ── Identity ──

    def load_identity(self) -> dict:
        if self._identity_cache is not None:
            return self._identity_cache
        path = self.config_dir / "identity.toml"
        self._identity_cache = self._load_toml(path) if path.exists() else {}
        return self._identity_cache

    def get_user_name(self) -> str:
        return self.load_identity().get("user", {}).get("name", "")

    def get_preferences(self) -> dict:
        return self.load_identity().get("preferences", {})

    def get_pins(self) -> List[dict]:
        """读取固定规则。

        identity.toml 里 `[pins]` 可能被写成**空表**（{}）或历史脏数据，
        这里统一归一化成"只含 dict 的列表"，避免调用方拿到 dict 后 .append 崩溃。
        """
        pins = self.load_identity().get("pins", [])
        if isinstance(pins, dict):
            pins = [v for v in pins.values() if isinstance(v, dict)]
        if not isinstance(pins, list):
            return []
        return [p for p in pins if isinstance(p, dict)]

    def add_pin(self, rule: str, scope: str = "global") -> str:
        """添加持久化规则"""
        identity = self.load_identity()
        pins = self.get_pins()
        pin_id = f"pin_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        existing_ids = {p.get("id") for p in pins}
        if pin_id in existing_ids:  # 同一秒内加多条：避免 ID 撞车导致误删
            suffix = 2
            while f"{pin_id}_{suffix}" in existing_ids:
                suffix += 1
            pin_id = f"{pin_id}_{suffix}"
        pins.append({
            "id": pin_id,
            "rule": rule,
            "scope": scope,
            "created_at": datetime.now().isoformat(),
        })
        identity["pins"] = pins
        self._save_identity(identity)
        return f" 规则已固定（{pin_id}）：{rule}"

    def remove_pin(self, pin_id: str) -> str:
        identity = self.load_identity()
        pins = self.get_pins()
        original = len(pins)
        pins = [p for p in pins if p.get("id") != pin_id]
        if len(pins) == original:
            return f" 未找到规则 {pin_id}"
        identity["pins"] = pins
        self._save_identity(identity)
        return f" 规则 {pin_id} 已移除"

    def _save_identity(self, identity: dict):
        """把 identity 写回 TOML。

        旧实现有两个硬伤（已修）：
        1. 字符串直接拼双引号（不转义）→ 规则里打一个引号就让整个文件变成非法 TOML，
           下次启动读不回来，所有固定规则静默消失；
        2. 顶层列表（pins）没有任何写入分支 → 完全丢失。
        """
        path = self.config_dir / "identity.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines: List[str] = []
        for section, values in identity.items():
            if isinstance(values, dict):
                lines.append(f"[{section}]")
                for k, v in values.items():
                    if v is None:
                        continue
                    if isinstance(v, dict):
                        logger.warning(
                            "identity.toml 暂不支持嵌套表 %s.%s，已跳过以避免写坏文件", section, k
                        )
                        continue
                    lines.append(f"{k} = {_toml_scalar(v)}")
                lines.append("")
            elif isinstance(values, list):
                # 数组表：pins = [{...}] → [[pins]] ...
                for item in values:
                    if not isinstance(item, dict):
                        continue
                    lines.append(f"[[{section}]]")
                    for k, v in item.items():
                        if v is None:
                            continue
                        lines.append(f"{k} = {_toml_scalar(v)}")
                    lines.append("")
            elif values is not None:
                lines.append(f"{section} = {_toml_scalar(values)}")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        self._identity_cache = identity

    # ── Identity ↔ 设置界面 ──

    def get_identity_config(self) -> IdentityConfig:
        """把 identity.toml 读成 IdentityConfig（供设置界面回填）。"""
        identity = self.load_identity()
        user = identity.get("user", {})
        prefs = identity.get("preferences", {})
        user = user if isinstance(user, dict) else {}
        prefs = prefs if isinstance(prefs, dict) else {}
        return IdentityConfig(
            user_name=str(user.get("name") or ""),
            user_city=str(user.get("city") or ""),
            user_notes=str(user.get("notes") or ""),
            language=str(prefs.get("language") or "zh"),
            response_style=str(prefs.get("response_style") or "professional"),
            code_style=str(prefs.get("code_style") or "python"),
            pins=[dict(p) for p in self.get_pins()],
        )

    def save_identity_config(self, config: IdentityConfig) -> str:
        """把 IdentityConfig 写回 identity.toml，并同步内存缓存。返回文件路径。

        只覆盖 user / preferences / pins 三段，文件里其余内容原样保留。
        pins 的序列化（`[[pins]]` 表数组 + 引号转义）由 `_save_identity` 负责 ——
        那部分是踩过坑修好的，不要在这里重写一份。
        """
        identity = self.load_identity()
        identity["user"] = {
            "name": config.user_name,
            "city": config.user_city,
            "notes": config.user_notes,
        }
        identity["preferences"] = {
            "language": config.language,
            "response_style": config.response_style,
            "code_style": config.code_style,
        }
        identity["pins"] = [dict(p) for p in (config.pins or []) if isinstance(p, dict)]
        self._save_identity(identity)
        return str(self.config_dir / "identity.toml")

    def invalidate_caches(self) -> None:
        """丢弃 identity / persona 的内存缓存。

        设置界面通过 loader 改写 persona.toml、或直接改写 identity.toml 后，
        必须调用本方法，否则下次构建 system prompt 用的还是旧内容。
        """
        self._identity_cache = None
        self._persona_cache = None

    # ── Persona ──

    def load_persona(self) -> dict:
        if self._persona_cache is not None:
            return self._persona_cache
        path = self.config_dir / "persona.toml"
        self._persona_cache = self._load_toml(path) if path.exists() else {}
        return self._persona_cache

    # ── 工作空间人设覆盖 ──

    def set_workspace_persona(self, prompt: str):
        """设置工作空间级别的人设覆盖（空字符串 = 使用全局默认）"""
        self._workspace_persona = prompt

    # ── Role 系统 ──

    def set_active_role(self, role_prompt: str):
        """设置当前激活的 Role prompt 追加内容"""
        self._active_role_prompt = role_prompt

    def load_role(self, role_file: str) -> Optional[dict]:
        """加载指定 Role 文件，返回 {name, description, category, prompt_content}"""
        roles_dir = app_path("roles")
        path = roles_dir / role_file
        if not path.exists():
            return None
        data = self._load_toml(path)
        prompt = data.get("prompt", {})
        return {
            "name": data.get("name", ""),
            "description": data.get("description", ""),
            "category": data.get("category", ""),
            "prompt_content": prompt.get("content", "") if isinstance(prompt, dict) else "",
        }

    def list_roles(self) -> List[dict]:
        """列出所有可用的 Role 文件"""
        roles_dir = app_path("roles")
        if not roles_dir.exists():
            return []
        result = []
        for f in sorted(roles_dir.glob("*.toml")):
            role = self.load_role(f.name)
            if role:
                role["file"] = f.name
                result.append(role)
        return result

    # ── 构建完整 system prompt ──

    def build_system_prompt(self, profile_cards: str = "") -> str:
        """
        构建分层 system prompt：
        1. 基础人设（工作空间覆盖优先）
        1.5 概览卡（常驻前缀，紧跟人设：称呼/名字、风格、偏好）
        2. Role 追加 prompt
        3. Pins 规则注入
        """
        parts = []

        # 1. 基础人设（工作空间覆盖优先）
        if self._workspace_persona:
            parts.append(self._workspace_persona)
        else:
            persona = self.load_persona()
            base_prompt = persona.get("persona", {}).get("system_prompt", {})
            if isinstance(base_prompt, dict):
                base_prompt = base_prompt.get("text", "")
            if base_prompt:
                parts.append(base_prompt)

        # 1.5 概览卡（常驻，紧跟人设）
        if profile_cards:
            parts.append(f"\n\n[概览卡]\n{profile_cards}")

        # 2. Role 追加 prompt
        if self._active_role_prompt:
            parts.append(f"\n\n[角色指令]\n{self._active_role_prompt}")

        # 3. Pins 规则
        pins = self.get_pins()
        if pins:
            pin_lines = []
            for pin in pins:
                rule = pin.get("rule", "")
                scope = pin.get("scope", "global")
                if scope in ("global", "chat"):
                    pin_lines.append(f"- {rule}")
            if pin_lines:
                parts.append(f"\n\n[固定规则]\n" + "\n".join(pin_lines))

        return "".join(parts)

    # ── 上下文扫描（对标 llmenv scan） ──

    def scan_project(self, project_dir: str | None = None) -> dict:
        """扫描项目结构，返回技术栈和上下文信息"""
        root = Path(project_dir) if project_dir else APP_DIR
        info = {
            "project_name": root.name,
            "python_version": f"{os.sys.version_info.major}.{os.sys.version_info.minor}",
            "has_gradio": (root / "requirements.txt").exists() and "gradio" in (root / "requirements.txt").read_text(),
            "has_chromadb": (root / "requirements.txt").exists() and "chromadb" in (root / "requirements.txt").read_text(),
            "has_comfyui": (root / "plugins" / "comfyui_processor").exists(),
            "plugins_count": len(list((root / "plugins").glob("*/manifest.json"))) if (root / "plugins").exists() else 0,
            "config_files": [f.name for f in (root / "config").glob("*.toml")] if (root / "config").exists() else [],
        }
        return info
