"""配置数据模型"""
from dataclasses import dataclass, field
from typing import Dict, List

from core.paths import app_path

# 本地模型 provider 预设（均可走 OpenAI 兼容端点，无需更换 SDK）
LOCAL_PROVIDERS = {
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "api_key": "ollama",  # Ollama 占位 key
        "aliases": {"flash": "llama3.2", "pro": "qwen2.5:32b", "chat": "llama3.2"},
        "default_model": "llama3.2",
        "note": "Ollama 需先设置 OLLAMA_HOST 与 OPENAI_BASE_URL",
    },
    "lmstudio": {
        "base_url": "http://localhost:1234/v1",
        "api_key": "lm-studio",  # LM Studio 占位 key
        "aliases": {"flash": "local-model", "pro": "local-model", "chat": "local-model"},
        "default_model": "local-model",
        "note": "LM Studio 服务启动后加载模型即暴露 OpenAI 兼容端点",
    },
}


@dataclass
class BotConfig:
    name: str = "妹抖酱"
    version: str = "0.1.0"
    user_id_source: str = "hostname"
    max_input_length: int = 8000
    api_state_trim_length: int = 40
    # 默认值也按 APP_DIR 解析（配置缺失时的兜底，不允许落成相对路径）
    conversation_dir: str = field(default_factory=lambda: str(app_path("data", "conversations")))
    memory_dir: str = field(default_factory=lambda: str(app_path("data", "memory")))
    sandbox_dir: str = field(default_factory=lambda: str(app_path("data", "sandbox")))


# 老配置（只有 [model] 段、没有 [[model.sites]]）合成站点时用的默认名
DEFAULT_SITE_NAMES = {
    "deepseek": "DeepSeek 云端",
    "ollama": "本地 Ollama",
    "lmstudio": "本地 LM Studio",
    "custom": "自定义",
}


@dataclass
class ModelSite:
    """一个已保存的服务商/站点配置（设置界面里可增删改、可设为当前）。

    api_key 既可以是明文，也可以是 `${ENV_VAR}` 引用 —— 后者是推荐写法，
    真值存在 config/apikey.local（已被 .gitignore 忽略），不会进版本库。
    """
    name: str
    provider: str = "custom"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    thinking: bool = False
    reasoning_effort: str = "high"
    max_tokens: int = 384000
    aliases: Dict[str, str] = field(default_factory=dict)

    def is_local(self) -> bool:
        return self.provider in LOCAL_PROVIDERS

    def key_display(self) -> str:
        """给界面展示用的密钥状态（不回显真值）。"""
        if not self.api_key:
            return "未设置"
        if self.api_key.startswith("${"):
            return f"环境变量 {self.api_key}"
        if self.is_local():
            return "占位 key"
        return "已保存（明文）"


@dataclass
class ModelConfig:
    provider: str = "deepseek"       # 模型服务商：deepseek / ollama / lmstudio / custom
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    default_model: str = "deepseek-v4-flash"
    thinking_default: bool = False
    reasoning_effort: str = "high"
    max_tokens: int = 384000
    available_models: Dict[str, str] = field(default_factory=lambda: {
        "flash": "deepseek-v4-flash",
        "pro": "deepseek-v4-pro",
        "chat": "deepseek-v4-flash",
    })
    max_retries: int = 1
    retry_delay_seconds: int = 1
    # 已保存的站点列表 + 当前启用的站点名（设置界面据此展示"已添加的模型和站点"）
    sites: List[ModelSite] = field(default_factory=list)
    active_site: str = ""

    def apply_site(self, site: "ModelSite") -> None:
        """把某个已保存站点设为当前生效配置。"""
        preset = LOCAL_PROVIDERS.get(site.provider, {})
        self.provider = site.provider
        self.base_url = site.base_url or preset.get("base_url", self.base_url)
        self.default_model = site.model or self.default_model
        self.thinking_default = bool(site.thinking)
        self.reasoning_effort = site.reasoning_effort or self.reasoning_effort
        self.max_tokens = int(site.max_tokens or self.max_tokens)
        self.active_site = site.name
        if site.aliases:
            self.available_models = dict(site.aliases)
        elif site.provider == "deepseek":
            self.available_models = {
                "flash": "deepseek-v4-flash",
                "pro": "deepseek-v4-pro",
                "chat": "deepseek-v4-flash",
            }
        else:
            # 本地 / 自定义：别名都指向当前模型，避免 /model flash 切到不存在的模型
            self.available_models = {
                key: (site.model or value)
                for key, value in self.available_models.items()
            }

    @property
    def supports_thinking(self) -> bool:
        """是否支持 DeepSeek 私有思考参数（仅 DeepSeek；custom 走纯 OpenAI 标准协议）"""
        return self.provider == "deepseek"

    def is_local(self) -> bool:
        return self.provider in ("ollama", "lmstudio")


@dataclass
class PluginsConfig:
    directory: str = "plugins"
    disabled: list = field(default_factory=list)
    per_plugin: dict = field(default_factory=dict)


@dataclass
class PersonaConfig:
    name: str = "鲸鱼娘"
    greeting: str = "噗咕～鲸鱼娘来啦！"
    system_prompt: str = ""


@dataclass
class IdentityConfig:
    """用户身份与偏好（config/identity.toml）。

    注意：`identity.toml` 的实际读写由 `core/context_engine.py` 负责
    （那里有 pins 的表数组序列化与引号转义逻辑），本类只作为
    「设置界面 ↔ context_engine」之间的传输对象，不要在 loader 里重复实现一份。
    """
    user_name: str = ""
    user_city: str = ""
    user_notes: str = ""
    language: str = "zh"
    response_style: str = "professional"
    code_style: str = "python"
    pins: List[dict] = field(default_factory=list)


@dataclass
class AppearanceConfig:
    """外观（config/appearance.toml）。

    `preset` 是预设主题名（亮色 / 深色 / 初音）或 "自定义"。
    `tokens` 只在 preset == "自定义" 时有意义 —— 其余情况色值从
    `ui_qt.theme.THEMES` 取，这样预设日后改进能自动跟上，
    而不是被用户机器上的旧副本钉死。

    色值合法性由 `ui_qt.theme.is_hex_color` 单点判定，loader 不重复实现。
    """
    preset: str = "亮色"
    tokens: Dict[str, str] = field(default_factory=dict)
    effects_preset: str = "标准"
    window_opacity: float = 0.98
    backdrop_blur: float = 8.0
    stand_depth: float = 0.0
