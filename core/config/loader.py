"""TOML 配置加载器，支持环境变量覆盖。

优先级：环境变量 > config/*.toml > 默认值
环境变量格式：WHALEGIRL_SECTION_KEY（如 WHALEGIRL_MODEL_API_KEY）
"""
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import asdict

from core.paths import APP_DIR, app_path, to_app_path

_logger = logging.getLogger(__name__)

from .models import (
    DEFAULT_SITE_NAMES,
    AppearanceConfig,
    BotConfig,
    IdentityConfig,
    LOCAL_PROVIDERS,
    ModelConfig,
    ModelSite,
    PersonaConfig,
    PluginsConfig,
)


def _ensure_toml():
    """尝试导入 tomllib (3.11+) 或 tomli"""
    try:
        import tomllib
        return tomllib
    except ImportError:
        try:
            import tomli
            return tomli
        except ImportError:
            raise ImportError("需要 tomli 或 tomllib（Python 3.11+）。请: pip install tomli")


MODEL_TOML_HEADER = """# ── 模型服务商配置 ──
# 本文件由「设置 → 模型」界面写入，也可手工编辑（重启后生效）。
#
#   [model]         当前生效的配置（provider / base_url / 默认模型…）
#   [[model.sites]] 已保存的站点与模型列表，界面里可增删改、可设为当前
#
# api_key 支持 ${ENV_VAR} 引用：真值写在 config/apikey.local（已被 .gitignore 忽略），
# 本地服务商（ollama / lmstudio）的占位 key 可直接明文放在这里。
"""

PERSONA_TOML_HEADER = """# ── 人设配置 ──
# 本文件由「设置 → 人设」界面写入，也可手工编辑。
#
#   [persona]                名称与问候语
#   [persona.system_prompt]  text = 全局人设（多行文本）
#
# 工作空间可单独覆盖人设：workspaces/<id>/workspace.toml 的 persona_prompt
# 非空时优先于这里的 text。
# 长期记忆（概览卡）由对话自动抽取，无需在这里配置。
"""

APPEARANCE_TOML_HEADER = """# ── 外观配置 ──
# 本文件由「设置 → 外观」界面写入，也可手工编辑（改完重启妹抖酱生效）。
#
#   [theme]          preset = 亮色 / 深色 / 初音 / 自定义
#   [theme.tokens]   仅当 preset = "自定义" 时读取：18 个语义色的完整色表
#   [effects]        窗口不透明度 / 背景虚化 / 立绘景深
#
# preset 是预设名时，色值从程序内置的预设取（预设日后改进会自动跟上）；
# 在色轮面板里改动任意颜色后，preset 会变成「自定义」，色值写进 [theme.tokens]。
# 令牌键名与含义见 ui_qt/theme.py 的 TOKEN_LABELS。
"""

PLUGINS_TOML_HEADER = """# ── 插件配置 ──
# 本文件由「设置 → 插件」界面写入，也可手工编辑。
#
#   [plugins]          directory = 插件目录；disabled = 禁用的插件名列表
#   [plugins.<名称>]   每个插件的参数（enabled 开关 + 各自配置项）
#
# 改完可用 /plugin_reload 热重载，不必重启。
"""

LIVE2D_TOML_HEADER = """# ── Live2D 立绘配置 ──
# 本文件由「设置 → Live2D」界面写入，也可手工编辑（改完重启妹抖酱生效）。
#
# 动作名 = motions/ 下的 *.motion3.json 去掉扩展名
# 表情名 = 模型目录下的 *.exp3.json 去掉扩展名
#
#   [model]             模型目录 / 定义文件 / 播放器页面 / 回退开关
#   [behavior]          idle 时是否清掉表情
#   [interaction]       点击轮换表情、视线跟随系数
#   [state_map.<阶段>]  对话阶段 → 动作 / 表情
#
# 状态名来自引擎上报的对话阶段：thinking / tool / found / writing / idle / error
# 空字符串 = 清掉表情回到默认脸；想「保持当前表情不动」就把该行整行删掉。
"""


def _toml_value(value) -> str:
    """把 Python 值转成 TOML 字面量（标量 / 内联表 / 内联数组 / 多行字符串）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        inner = ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items())
        return "{" + inner + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    text = str(value)
    # 真正的多行文本（如 persona 的 system_prompt）用字面字符串写出，
    # 比压成一行带 \n 转义的长串可读得多。判据是「去掉首尾换行后仍含换行」：
    # 这样 "\n\n[记忆]：{x}" 这类只是首尾带换行的短串仍走普通转义写法，
    # 不会写出 `'''` 紧跟空行这种别扭形式。
    # 字面字符串不做转义，故内容含 ''' 时一律回退 json 写法。
    if "\n" in text.strip("\n") and "'''" not in text:
        return "'''\n" + text + "'''"
    return json.dumps(text, ensure_ascii=False)


def _toml_emit(data: dict, prefix: str, lines: List[str]) -> None:
    """递归写出：标量 → [表] → [[表数组]]（TOML 要求标量必须在子表之前）。"""
    if prefix:
        lines.append(f"[{prefix}]")
    for key, value in data.items():
        if not isinstance(value, (dict, list)):
            lines.append(f"{key} = {_toml_value(value)}")
        elif isinstance(value, list) and not any(isinstance(i, dict) for i in value):
            # 纯标量数组（如 live2d 的 click_expressions）→ 内联数组。
            # 不单独处理会被下面的表数组分支静默丢弃。
            lines.append(f"{key} = {_toml_value(value)}")
    if prefix:
        lines.append("")

    for key, value in data.items():
        if isinstance(value, dict):
            _toml_emit(value, f"{prefix}.{key}" if prefix else key, lines)

    for key, value in data.items():
        if isinstance(value, list):
            for item in value:
                if not isinstance(item, dict):
                    continue
                lines.append(f"[[{prefix}.{key}]]" if prefix else f"[[{key}]]")
                for item_key, item_value in item.items():
                    if not isinstance(item_value, (dict, list)):
                        lines.append(f"{item_key} = {_toml_value(item_value)}")
                lines.append("")


def _toml_dump(data: dict) -> str:
    """极简 TOML 序列化（够用即可：标量 / 表 / 表数组）。"""
    lines: List[str] = []
    _toml_emit(data, "", lines)
    return "\n".join(lines).rstrip() + "\n"


def _as_float(value, fallback: float) -> float:
    """把配置里的数值转成 float，转不动就回落。

    配置文件是**可以手改的**，所以这里不能假设类型正确 —— 一个手抖的
    `window_opacity = "很透明"` 不该让程序起不来。
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(fallback)


def _to_rel_if_inside(value: str, fallback: str) -> str:
    """把「项目内的绝对路径」转回相对项目根的写法。

    读配置时 `to_app_path()` 会把相对路径解析成绝对路径，写回时必须转回来，
    否则会把本机绝对路径（含用户名，形如 `C:/Users/<用户名>/…`）写进配置文件 ——
    既不可移植，分发时还会泄漏用户名。不在项目内或转换失败时回退 fallback。
    """
    if not value:
        return fallback
    path = Path(value)
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(APP_DIR.resolve()).as_posix()
    except (ValueError, OSError):
        return fallback


class ConfigLoader:
    def __init__(self, config_dir: str | None = None):
        self.config_dir = Path(config_dir) if config_dir else app_path("config")
        self._cache: Dict[str, Dict] = {}
        # 本次启动里解析失败、已回落默认值的配置文件 {文件名: 原因}
        self._broken: Dict[str, str] = {}
        self._toml = _ensure_toml()
        # 先加载本地密钥，后面 ${VAR} 才能在 model.toml 里被正确展开
        self._load_local_keys()

    @property
    def broken_files(self) -> Dict[str, str]:
        """本次启动中解析失败的配置文件 {文件名: 原因}。

        供启动自检展示 —— 回落默认值本身是静默的，用户必须被告知，
        否则他会以为「我的配置生效了」，实际跑的是默认值。
        """
        return dict(self._broken)

    def expand_env(self, value: str) -> str:
        """公开入口：把 `${VAR}` 展开成环境变量值（供 UI 保存后立即生效使用）。"""
        return self._expand_env(value)

    def _expand_env(self, value: str) -> str:
        """展开 ${VAR_NAME} 格式的环境变量，未设置则返回空字符串"""
        if isinstance(value, str):
            def _replacer(m):
                return os.getenv(m.group(1), "")
            return re.sub(r'\$\{(\w+)\}', _replacer, value)
        return value

    def _expand_dict(self, d: dict) -> dict:
        for k, v in d.items():
            if isinstance(v, dict):
                self._expand_dict(v)
            elif isinstance(v, str):
                d[k] = self._expand_env(v)
        return d

    # ── 本地密钥（config/apikey.local，已被 .gitignore 忽略） ──

    @property
    def local_keys_file(self) -> Path:
        return self.config_dir / "apikey.local"

    def _load_local_keys(self) -> None:
        """把 `KEY=VALUE` 形式的本地密钥注入环境变量（不覆盖已存在的环境变量）。

        这样 model.toml 里只写 `${MEIDO_XXX_KEY}`，明文不进版本库。
        """
        path = self.local_keys_file
        if not path.exists():
            return
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        except OSError as exc:
            print(f"[WARN] 读取 {path} 失败：{exc}")

    def save_secret(self, env_name: str, value: str) -> None:
        """把密钥写入 apikey.local（已存在则覆盖），并同步到当前进程环境变量。"""
        path = self.local_keys_file
        path.parent.mkdir(parents=True, exist_ok=True)
        lines: List[str] = []
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()
        replaced = False
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and stripped.split("=", 1)[0].strip() == env_name:
                lines[index] = f'{env_name} = "{value}"'
                replaced = True
                break
        if not replaced:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f'{env_name} = "{value}"')
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)      # 仅当前用户可读写（Windows 上尽力而为）
        except OSError:
            pass
        os.environ[env_name] = value

    def store_api_key(self, site_name: str, provider: str, raw_key: str) -> str:
        """把用户输入的密钥转成"可安全落盘"的形式，返回要写进 model.toml 的值。

        - 空 / 已是 `${VAR}` → 原样返回
        - 本地服务商的占位 key（如 ollama）→ 明文无所谓，原样返回
        - 其余 → 真值写 apikey.local，返回 `${MEIDO_<站点名>_KEY}`
        """
        if not raw_key:
            return ""
        if raw_key.startswith("${"):
            return raw_key
        if provider in LOCAL_PROVIDERS:
            return raw_key
        env_name = "MEIDO_" + re.sub(r"[^A-Za-z0-9]+", "_", site_name).strip("_").upper() + "_KEY"
        self.save_secret(env_name, raw_key)
        return "${" + env_name + "}"

    def load(self, name: str) -> Dict[str, Any]:
        """读取 `config/<name>`。

        ⚠️ **解析失败不抛异常**。这几个 TOML 全在启动路径上
        （`engine.__init__` 直接调 `get_bot_config` / `get_model_config` /
        `get_persona_config`），一个手抖的字符会让程序**启动即崩**，
        而且崩在窗口出现之前 —— 用户只看到「一闪而过」，连报错都看不到。

        这里的处理：**原文件一个字节都不动**，另存一份 `.bak-<时间戳>` 保内容，
        打 ERROR 日志，返回空 dict 让各 getter 用默认值兜底；
        再由启动自检（`core/selfcheck.py` 的 `check_config_files`）把
        「哪个文件坏了、怎么修」摆到界面上。

        为什么不学 `workspace/storage.py` 那样把坏文件**改名移走**：
        那边的配置是程序生成的，重建即可；而 `config/*.toml` 是**用户手写**的，
        而且 `bot.toml` 里存着数据目录路径 —— 移走它等于悄悄换掉用户的数据位置。
        对用户手写的文件，非破坏性优先。
        """
        if name in self._cache:
            return self._cache[name]
        path = self.config_dir / name
        if path.exists():
            try:
                with open(path, "rb") as f:
                    data = self._toml.load(f)
                self._expand_dict(data)
            except Exception as exc:
                self._record_broken(name, exc)
                data = {}
        else:
            data = {}
        self._cache[name] = data
        return data

    def _record_broken(self, name: str, exc: Exception) -> None:
        """登记一个坏掉的配置文件（备份内容 + 记名给启动自检）。"""
        reason = f"{type(exc).__name__}: {exc}"
        self._broken[name] = reason

        path = self.config_dir / name
        backup_note = ""
        # 只备份一次：同名 .bak 已存在就跳过。否则每次启动都堆一个，
        # 而用户真正需要的是「内容别丢」+「有人告诉我坏了」。
        if not any(self.config_dir.glob(f"{name}.bak-*")):
            backup = self.config_dir / f"{name}.bak-{int(time.time())}"
            try:
                shutil.copy2(path, backup)
                backup_note = f"，内容已另存为 {backup.name}"
            except OSError as copy_exc:
                backup_note = f"，备份失败（{copy_exc}）"
        _logger.error(
            "[配置] %s 解析失败（%s），本次改用默认值%s；原文件未改动，修好语法后重启即可生效",
            name, reason, backup_note)

    def get_bot_config(self) -> BotConfig:
        data = self.load("bot.toml")
        bot = data.get("bot", {})
        paths = data.get("paths", {})
        # 配置里可以写相对路径（相对 exe/项目根），这里统一解析成绝对路径
        return BotConfig(
            name=bot.get("name", "妹抖酱"),
            version=bot.get("version", "0.1.0"),
            user_id_source=bot.get("user_id_source", "hostname"),
            max_input_length=bot.get("max_input_length", 8000),
            api_state_trim_length=bot.get("api_state_trim_length", 40),
            conversation_dir=str(to_app_path(paths.get("conversations") or "data/conversations")),
            memory_dir=str(to_app_path(paths.get("memory") or "data/memory")),
            sandbox_dir=str(to_app_path(paths.get("sandbox") or "data/sandbox")),
        )

    def get_model_config(self) -> ModelConfig:
        data = self.load("model.toml")
        m = data.get("model", {})
        defaults = m.get("defaults", {})
        retry = m.get("retry", {})
        provider = m.get("provider", "deepseek")
        # 本地 provider 预设：未显式覆盖时套用 Ollama / LM Studio 默认端点与占位 key
        preset = LOCAL_PROVIDERS.get(provider, {})
        api_key_value = self._expand_env(
            m.get("api_key") or preset.get("api_key") or os.getenv("DEEPSEEK_API_KEY", ""))
        base_url_value = m.get("base_url") or preset.get("base_url") or "https://api.deepseek.com"
        model_value = defaults.get("model") or preset.get("default_model", "deepseek-v4-flash")
        thinking_value = bool(defaults.get("thinking", False))
        effort_value = defaults.get("reasoning_effort", "high")
        max_tokens_value = defaults.get("max_tokens", 384000)
        aliases_value = m.get("aliases") or preset.get("aliases") or {
            "flash": "deepseek-v4-flash",
            "pro": "deepseek-v4-pro",
            "chat": "deepseek-v4-flash",
        }

        # ── 站点列表（设置界面展示"已添加的模型和站点"用） ──
        sites: List[ModelSite] = []
        for raw in m.get("sites", []) or []:
            if not isinstance(raw, dict) or not raw.get("name"):
                continue
            sites.append(ModelSite(
                name=str(raw["name"]),
                provider=str(raw.get("provider", "custom")),
                base_url=str(raw.get("base_url", "")),
                api_key=str(raw.get("api_key", "")),
                model=str(raw.get("model", "")),
                thinking=bool(raw.get("thinking", False)),
                reasoning_effort=str(raw.get("reasoning_effort", effort_value)),
                max_tokens=int(raw.get("max_tokens", max_tokens_value)),
                aliases=raw.get("aliases") or {},
            ))
        active_site = str(m.get("active_site", "") or "")
        if not sites:
            # 向后兼容：老配置没有 [[model.sites]] → 用当前配置合成一个站点
            sites = [ModelSite(
                name=DEFAULT_SITE_NAMES.get(provider, provider),
                provider=provider,
                base_url=base_url_value,
                api_key=str(m.get("api_key") or ""),
                model=model_value,
                thinking=thinking_value,
                reasoning_effort=effort_value,
                max_tokens=max_tokens_value,
                aliases=dict(aliases_value),
            )]
        if not active_site:
            active_site = sites[0].name

        return ModelConfig(
            provider=provider,
            api_key=api_key_value,
            base_url=base_url_value,
            default_model=model_value,
            thinking_default=thinking_value,
            reasoning_effort=effort_value,
            max_tokens=max_tokens_value,
            available_models=aliases_value,
            max_retries=retry.get("max_retries", 1),
            retry_delay_seconds=retry.get("retry_delay_seconds", 1),
            sites=sites,
            active_site=active_site,
        )

    def save_model_config(self, config: ModelConfig, active_name: str = "") -> str:
        """把模型配置（含站点列表）写回 config/model.toml。

        `config.api_key` 里若含 `${VAR}` 引用，原样保存（真值在 apikey.local）；
        本地服务商占位 key 也原样保存。返回写入的文件路径。
        """
        active_name = active_name or config.active_site or (
            config.sites[0].name if config.sites else "")
        payload = {
            "model": {
                "provider": config.provider,
                "api_key": config.api_key,
                "base_url": config.base_url,
                "active_site": active_name,
                "defaults": {
                    "model": config.default_model,
                    "thinking": bool(config.thinking_default),
                    "reasoning_effort": config.reasoning_effort,
                    "max_tokens": int(config.max_tokens),
                },
                "aliases": dict(config.available_models or {}),
                "retry": {
                    "max_retries": int(config.max_retries),
                    "retry_delay_seconds": int(config.retry_delay_seconds),
                },
                "sites": [
                    {
                        "name": site.name,
                        "provider": site.provider,
                        "base_url": site.base_url,
                        "api_key": site.api_key,
                        "model": site.model,
                        "thinking": bool(site.thinking),
                        "reasoning_effort": site.reasoning_effort,
                        "max_tokens": int(site.max_tokens),
                        **({"aliases": site.aliases} if site.aliases else {}),
                    }
                    for site in config.sites
                ],
            }
        }
        path = self.config_dir / "model.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(MODEL_TOML_HEADER + _toml_dump(payload), encoding="utf-8")
        # 让下次 get_model_config() 重新读盘
        self._cache.pop("model.toml", None)
        return str(path)

    def get_plugins_config(self) -> PluginsConfig:
        data = self.load("plugins.toml")
        p = data.get("plugins", {})
        return PluginsConfig(
            directory=str(to_app_path(p.get("directory") or "plugins")),
            disabled=p.get("disabled", []),
            per_plugin={k: v for k, v in p.items() if isinstance(v, dict)},
        )

    def get_persona_config(self) -> PersonaConfig:
        data = self.load("persona.toml")
        persona = data.get("persona", {})
        sp = persona.get("system_prompt", {})
        prompt_text = sp.get("text", "") if isinstance(sp, dict) else str(sp)
        return PersonaConfig(
            name=persona.get("name", "鲸鱼娘"),
            greeting=persona.get("greeting", "噗咕～鲸鱼娘来啦！"),
            system_prompt=prompt_text,
        )

    # ── 人设 ──

    def save_persona_config(self, config: PersonaConfig) -> str:
        """把人设写回 config/persona.toml。返回写入的文件路径。"""
        payload = {
            "persona": {
                "name": config.name,
                "greeting": config.greeting,
                "system_prompt": {"text": config.system_prompt},
            }
        }
        path = self.config_dir / "persona.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(PERSONA_TOML_HEADER + _toml_dump(payload), encoding="utf-8")
        self._cache.pop("persona.toml", None)
        return str(path)

    # ── 外观 ──

    def get_appearance_config(self) -> AppearanceConfig:
        """读取 config/appearance.toml。

        缺失 / 解析失败 / 类型不对一律回落到默认值 —— 外观配置坏了不该
        让程序起不来，最坏也就是回到亮色 + 标准特效。
        解析失败本身由 `load()` 统一接住并登记（见 `broken_files`），这里不再重复处理。
        """
        data = self.load("appearance.toml")
        if not isinstance(data, dict):
            return AppearanceConfig()

        theme = data.get("theme")
        theme = theme if isinstance(theme, dict) else {}
        tokens = theme.get("tokens")
        tokens = {str(k): str(v) for k, v in tokens.items()} \
            if isinstance(tokens, dict) else {}

        effects = data.get("effects")
        effects = effects if isinstance(effects, dict) else {}

        default = AppearanceConfig()
        return AppearanceConfig(
            preset=str(theme.get("preset") or default.preset),
            tokens=tokens,
            effects_preset=str(effects.get("preset") or default.effects_preset),
            window_opacity=_as_float(effects.get("window_opacity"), default.window_opacity),
            backdrop_blur=_as_float(effects.get("backdrop_blur"), default.backdrop_blur),
            stand_depth=_as_float(effects.get("stand_depth"), default.stand_depth),
        )

    def save_appearance_config(self, config: AppearanceConfig) -> str:
        """把外观写回 config/appearance.toml。返回写入的文件路径。

        只在 preset == "自定义" 时写 [theme.tokens] —— 预设名本身已经表达了
        全部信息，存一份副本只会让预设日后的改进传不到用户那儿。

        色值合法性用 `ui_qt.theme.is_hex_color` 单点判定，这里不另写一份。
        延迟 import 是刻意的：`core` 依赖 `ui_qt` 方向上是反的，
        能成立只因为 `ui_qt/theme.py` 是**纯 Python**（只 import typing，
        无 Qt、无副作用）。哪天给它加了 Qt 依赖，这里会立刻炸出来。
        """
        from ui_qt.theme import CUSTOM_THEME_NAME, is_hex_color

        theme: Dict[str, Any] = {"preset": config.preset}
        if config.preset == CUSTOM_THEME_NAME:
            theme["tokens"] = {
                str(k): str(v).upper() for k, v in (config.tokens or {}).items()
                if is_hex_color(v)
            }

        payload = {
            "theme": theme,
            "effects": {
                "preset": config.effects_preset,
                "window_opacity": round(float(config.window_opacity), 3),
                "backdrop_blur": round(float(config.backdrop_blur), 2),
                "stand_depth": round(float(config.stand_depth), 2),
            },
        }
        path = self.config_dir / "appearance.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(APPEARANCE_TOML_HEADER + _toml_dump(payload), encoding="utf-8")
        self._cache.pop("appearance.toml", None)
        return str(path)

    # ── 插件 ──

    def save_plugins_config(self, config: PluginsConfig) -> str:
        """把插件配置写回 config/plugins.toml。返回写入的文件路径。

        `get_plugins_config()` 返回的 directory 是绝对路径，这里统一回落成
        相对项目根的写法（见 `_to_rel_if_inside`）。
        """
        plugins: Dict[str, Any] = {
            "directory": _to_rel_if_inside(config.directory, "plugins"),
            "disabled": list(config.disabled or []),
        }
        for name, params in (config.per_plugin or {}).items():
            if isinstance(params, dict):
                plugins[str(name)] = dict(params)
        path = self.config_dir / "plugins.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(PLUGINS_TOML_HEADER + _toml_dump({"plugins": plugins}),
                        encoding="utf-8")
        self._cache.pop("plugins.toml", None)
        return str(path)

    # ── Live2D ──

    def get_live2d_config(self) -> Dict[str, Any]:
        """读取 config/live2d.toml。

        原先由 `ui_qt/main_window.py` 直读，现收归 loader 统一入口；
        缺失或解析失败一律返回空 dict，由调用方决定回退策略（静态立绘）。
        """
        data = self.load("live2d.toml")
        return data if isinstance(data, dict) else {}

    def save_live2d_config(self, data: Dict[str, Any]) -> str:
        """把 Live2D 配置写回 config/live2d.toml。返回写入的文件路径。

        `model.dir` / `model.viewer` 一律保持相对项目根的写法。
        """
        payload: Dict[str, Any] = {}
        for section, values in (data or {}).items():
            if isinstance(values, dict):
                payload[str(section)] = dict(values)
        model = payload.get("model")
        if isinstance(model, dict):
            model["dir"] = _to_rel_if_inside(model.get("dir", ""), "assets/live2d")
            if model.get("viewer"):
                model["viewer"] = _to_rel_if_inside(
                    model["viewer"], "assets/live2d/viewer/index.html")
        path = self.config_dir / "live2d.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(LIVE2D_TOML_HEADER + _toml_dump(payload), encoding="utf-8")
        self._cache.pop("live2d.toml", None)
        return str(path)

    def invalidate_cache(self):
        self._cache.clear()
