"""
鲸鱼娘 AI 大脑（重构版）
从 ai_brain.py 提取，全局变量转为实例属性。
"""
import openai
import os
import sys
import json
import time
import socket

# 输出编码兜底（库级）：本模块会把模型原始输出直接 print 到控制台，而模型回复常带
# emoji（实测 '💙' 触发 UnicodeEncodeError，把整条生成流程打断）。这里只放宽错误处理、
# 不改编码：中文照常显示，无法编码的字符退化为 '?'。放在库内是为了覆盖所有入口
# （main.py / 各类脚本），避免"只有某一个入口加了保护"。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass
import hashlib
import re
import traceback
import threading
from pathlib import Path
from typing import Generator, List, Dict, Any, Optional, Callable

from core.config.models import ModelConfig, PersonaConfig
from core.config.loader import ConfigLoader
from core.paths import app_path


def _load_user_id(config_loader: ConfigLoader) -> str:
    """user_id 优先级：环境变量 > user_config.json > 主机名"""
    env_id = os.getenv("DEEPSEEK_USER_ID")
    if env_id:
        print(f" user_id 来源: 环境变量 DEEPSEEK_USER_ID")
        return env_id
    config_path = app_path("user_config.json")
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                uid = cfg.get("user_id")
                if uid:
                    print(f"[INFO] user_id from: user_config.json")
                    return uid
        except Exception:
            print("[WARN] Failed to read user_config.json, will use hostname")
    host = socket.gethostname()
    print(f"[INFO] user_id from: hostname ({host})")
    return host


BUILTIN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_status",
            "description": "查看当前模型配置（服务商、模型名、思考模式、推理强度、本地/云端）。当用户问\"现在用的什么模型\"或需确认配置时使用。只读、不改配置、瞬时返回。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "model",
            "description": "切换对话模型。切换后立即影响后续对话。用户要求\"换模型\"\"切到xx\"时使用。瞬时生效。",
            "parameters": {
                "type": "object",
                "properties": {"model_name": {"type": "string", "description": "目标模型名：flash（快速）、pro（更强推理），或本地模型名如 qwen3:14b"}},
                "required": ["model_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "think",
            "description": "切换思考模式开关（开启后先输出思考链再回复）。仅在用户明确要求开启/关闭思考时调用，绝不主动推测用户意图。用户说\"think on/off\"\"开思考\"\"别思考了\"时使用。瞬时生效。",
            "parameters": {
                "type": "object",
                "properties": {"enabled": {"type": "boolean", "description": "true=开启思考模式，false=关闭"}},
                "required": ["enabled"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "effort",
            "description": "设置推理强度。仅复杂推理时调高。用户要求\"认真点\"\"深度思考\"时使用。",
            "parameters": {
                "type": "object",
                "properties": {"level": {"type": "string", "enum": ["high", "max"], "description": "high（默认，较快）、max（最强推理，更慢更耗 token）"}},
                "required": ["level"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory",
            "description": "管理长期记忆。clear 会清空记忆、不可逆，必须先向用户确认。用户说\"记住什么了\"\"清空记忆\"时使用。",
            "parameters": {
                "type": "object",
                "properties": {"action": {"type": "string", "enum": ["view", "clear"], "description": "view=查看记忆摘要，clear=清空记忆（不可逆）"}},
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "history",
            "description": "管理对话历史。用户说\"导出对话\"\"新建会话\"时使用。瞬时返回。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["view", "export", "new"], "description": "view=会话列表，export=导出，new=新建会话"},
                    "format": {"type": "string", "enum": ["markdown", "json", "txt"], "description": "导出格式，仅 export 时用到"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plugin_reload",
            "description": "重新扫描并加载 plugins/ 下所有插件。插件被修改、新增或加载失败需重试时使用。会重新初始化全部插件工具，耗时数秒。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": "加载指定 skill 的完整说明（SKILL.md 全文）。任务涉及某 skill 领域、需其详细指引时先调本工具，再按其内容执行。找不到会返回可用 skill 列表。",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "skill 名称，见 system prompt 的\"我的 Skills\"清单"}},
                "required": ["name"],
            },
        },
    },
]


# ═══════════════════════════════════════════════════════════
# 手动停止（防思考死循环）
# UI「停止」按钮调用 request_stop() 设置事件，流式循环检查该事件以中断生成。
# ═══════════════════════════════════════════════════════════

_stop_event = threading.Event()


def request_stop():
    """请求停止当前生成（供 UI「停止」按钮调用）。"""
    _stop_event.set()


def _clear_stop():
    """新对话开始前清空停止标志。"""
    _stop_event.clear()


def _get_reasoning(delta) -> str:
    """从流式 delta 取思考内容（兼容不同 SDK 版本和字段名：reasoning_content / reasoning / thinking）。"""
    for field in ("reasoning_content", "reasoning", "thinking"):
        rc = getattr(delta, field, None)
        if rc:
            return rc
    dump = getattr(delta, "model_dump", None)
    if dump:
        try:
            d = dump()
        except Exception:
            d = {}
        for field in ("reasoning_content", "reasoning", "thinking"):
            if d.get(field):
                return d[field]
    extra = getattr(delta, "model_extra", None) or {}
    for field in ("reasoning_content", "reasoning", "thinking"):
        if extra.get(field):
            return extra[field]
    return ""


# 只存在于本地、不能发给模型的字段。
# `time` 服务于界面显示与会话索引（core.history 落盘时写、ui_qt 回放时读），
# 但 OpenAI 兼容接口只认 role / content / tool_calls 等字段，多带字段有被拒的风险。
_LOCAL_ONLY_KEYS = ("time",)


def _api_messages(messages: list) -> list:
    """剥掉本地字段后再交给模型 —— 发请求前的**唯一**出口过滤点。

    chat_stream 是 api_state 唯一会被送进模型的地方，所以在它开头过滤一次即可：
    工具循环里新造的消息本来就不带本地字段，不会重新混进来。
    """
    if not any(isinstance(m, dict) and "time" in m for m in messages):
        return messages          # 快路径：绝大多数请求本来就没有本地字段
    return [
        {k: v for k, v in m.items() if k not in _LOCAL_ONLY_KEYS}
        if isinstance(m, dict) else m
        for m in messages
    ]


class Brain:
    """AI 大脑：LLM 调用 + 工具循环 + 流式对话"""

    def __init__(self, model_config: ModelConfig, persona_config: PersonaConfig, config_loader: ConfigLoader):
        self.model_config = model_config
        self.persona_config = persona_config
        self.config_loader = config_loader

        api_key = model_config.api_key
        if not api_key:
            api_key = os.getenv("DEEPSEEK_API_KEY", "")

        self._api_key = api_key
        # 本地 provider（ollama/lmstudio）无需真实 key，占位即可；云端需要真实 key
        is_local = model_config.is_local()
        if api_key or is_local:
            # timeout/max_retries 限制：避免 API 挂起时请求无限等待阻塞对话
            self._client = openai.OpenAI(
                api_key=api_key or "local", base_url=model_config.base_url,
                timeout=120.0, max_retries=1,
            )
        else:
            self._client = None
            print("[WARN] 未配置 API Key — AI 响应已禁用，请在侧栏设置中配置")

        self._user_id = _load_user_id(config_loader)

        # 运行时状态（必须在此初始化，reconfigure_client 只改 client 不重建状态）
        self.current_model = self.model_config.default_model
        self.thinking_enabled = self.model_config.thinking_default
        # 本地思考模型（qwen3 等）默认开启思考，让思考链可见；云端用 thinking_default
        if self.model_config.is_local():
            self.thinking_enabled = True
        self.reasoning_effort = self.model_config.reasoning_effort

    def _ensure_client(self):
        """确保 client 已初始化，否则抛出友好错误"""
        if self._client is None:
            raise RuntimeError(
                'API Key 未配置。请在侧边栏 -> 设置 -> API Key 中输入你的密钥，然后点击保存配置。'
            )

    def reconfigure_client(self, api_key: str, base_url: str = ""):
        """运行时重新配置 API client"""
        self._api_key = api_key
        self.model_config.api_key = api_key
        if base_url:
            self.model_config.base_url = base_url
        self._client = openai.OpenAI(
            api_key=api_key, base_url=base_url or self.model_config.base_url,
            timeout=120.0, max_retries=1,
        )

    @property
    def available_models(self) -> dict:
        return self.model_config.available_models

    # ── 本地模型探测 ──
    _local_models_cache = (0.0, [])   # (探测时间戳, 模型列表)，10 秒缓存避免重复阻塞

    def list_local_models(self, base_url: str = "http://localhost:11434/v1") -> list:
        """探测 Ollama 已安装的模型，返回模型名列表（失败返回空）。带 10 秒缓存。"""
        import time
        now = time.time()
        cached_at, cached = self._local_models_cache
        if now - cached_at < 10 and cached is not None:
            return cached
        try:
            import urllib.request, json
            req = urllib.request.Request(
                base_url.rstrip("/") + "/models",
                headers={"Authorization": "Bearer ollama"},
            )
            # 短超时：Ollama 跑大模型时也可能忙，探测最多等 2.5 秒，不阻塞页面
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            names = []
            for m in data.get("data", []):
                name = m.get("id") or m.get("model")
                if name:
                    names.append(name)
            self._local_models_cache = (time.time(), names)
            print(f"  [模型] 探测到本地 Ollama 模型: {', '.join(names) or '(无)'}")
            return names
        except Exception as e:
            self._local_models_cache = (time.time(), [])
            print(f"  [模型] 本地 Ollama 探测失败（2.5s 超时，不影响页面）: {e}")
            return []

    # ── 本地模型生命周期（Ollama keep_alive 控制） ──
    def _ollama_api(self, payload: dict) -> bool:
        """调用 Ollama 原生 API /api/generate（用于 keep_alive 控制）"""
        try:
            import urllib.request, json
            base = self.model_config.base_url.rstrip("/")
            # base_url 形如 http://localhost:11434/v1，原生 API 在 http://localhost:11434/api/generate
            if base.endswith("/v1"):
                base = base[:-3]
            req = urllib.request.Request(
                base + "/api/generate",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=300) as resp:
                resp.read()
            return True
        except Exception as e:
            print(f"  [模型] Ollama API 调用失败: {e}")
            return False

    def keep_model_loaded(self, model: str):
        """预加载本地模型并常驻显存（keep_alive=-1）"""
        print(f"  [模型] 预加载 {model} 并常驻显存...")
        if self._ollama_api({"model": model, "prompt": "", "stream": False, "keep_alive": -1}):
            print(f"  [模型] {model} 已常驻显存（Forever）")
        else:
            print(f"  [模型] {model} 预加载失败（模型可能已删除，或 Ollama 未运行），不影响对话")

    def unload_model(self, model: str):
        """卸载本地模型，释放显存（keep_alive=0）"""
        print(f"  [模型] 卸载 {model}，释放显存...")
        if self._ollama_api({"model": model, "prompt": "", "stream": False, "keep_alive": 0}):
            print(f"  [模型] {model} 已卸载")
        else:
            print(f"  [模型] {model} 卸载失败（模型可能已删除）")

    # ── provider / 模型切换（WebUI 动态切换，无需重启） ──
    def switch_provider(self, provider: str, model: str = "", base_url: str = "") -> str:
        """动态切换模型服务商与模型，重建客户端。本地/云端通用。
        模型生命周期：切入本地 → 常驻显存；切走 → 卸载旧本地模型释放显存。
        """
        import threading
        from core.config.models import LOCAL_PROVIDERS
        # 记录旧状态（用于切换后卸载旧本地模型）
        old_provider = self.model_config.provider
        old_model = self.model_config.default_model
        preset = LOCAL_PROVIDERS.get(provider, {})
        # 解析端点：显式传入 > 预设 > 当前
        url = base_url or preset.get("base_url", "") or self.model_config.base_url
        # 占位 key：本地用预设占位，云端保留当前
        if provider in ("ollama", "lmstudio"):
            key = preset.get("api_key", "local")
        else:
            key = self.model_config.api_key or ""
        # 更新配置
        self.model_config.provider = provider
        self.model_config.base_url = url
        self.model_config.api_key = key
        if model:
            self.model_config.default_model = model
            self.current_model = model
            if provider in ("ollama", "lmstudio"):
                self.model_config.available_models = {
                    "flash": model, "pro": model, "chat": model}
        # 切到本地思考模型 → 默认开启思考；切到云端 → 恢复 thinking_default
        if provider in ("ollama", "lmstudio"):
            self.thinking_enabled = True
        else:
            self.thinking_enabled = self.model_config.thinking_default
        # 重建客户端
        self.reconfigure_client(key, url)

        # 模型生命周期管理（同步执行，保证切换完成后立即可用）：
        # 1) 从本地模型切走 → 卸载旧本地模型释放显存
        # 2) 切入本地模型 → 同步预加载并常驻显存（首次加载需等待，
        #    避免「切换后立即发消息但模型还没就绪」导致的报错）
        model_changed = (provider != old_provider) or (model and model != old_model)
        if old_provider == "ollama" and old_model and model_changed:
            self.unload_model(old_model)
        if provider == "ollama" and model:
            self.keep_model_loaded(model)

        return f" 已切换到 {provider} → {self.current_model}"

    # ── 模型控制 ──
    def set_model(self, model_name: str) -> str:
        key = model_name.lower().strip()
        if key in self.available_models:
            self.current_model = self.available_models[key]
            # 深度思考模型自动开启思考（仅 DeepSeek 支持；本地模型忽略）
            if self.model_config.supports_thinking and key == "pro":
                self.thinking_enabled = True
            return f" 已切换至模型：{self.current_model}（{self.model_config.provider}），思考模式：{'开启' if self.thinking_enabled else '关闭'}"
        return f" 未知模型 '{model_name}'，可用：{', '.join(self.available_models)}"

    def set_thinking(self, enabled: bool) -> str:
        # 本地模型（Qwen3 等）通过 think 参数同样支持思考，不再忽略
        self.thinking_enabled = bool(enabled)
        return f" 思考模式已{'开启' if enabled else '关闭'}（{self.current_model}）"

    def set_effort(self, level: str) -> str:
        level = level.lower().strip()
        if level in ("high", "max"):
            self.reasoning_effort = level
            return f" 推理强度已设为 {self.reasoning_effort}"
        return " 推理强度仅支持 high 或 max"

    def get_status(self) -> str:
        return (
            f"当前模型：{self.current_model}，"
            f"思考模式：{'开启' if self.thinking_enabled else '关闭'}，"
            f"推理强度：{self.reasoning_effort}"
        )

    # ── API 参数构建 ──
    def _get_extra_body(self) -> dict:
        body = {}
        # user_id / thinking 是 DeepSeek 私有参数；custom 走纯 OpenAI 标准协议，不传私有参数
        if self.model_config.provider == "deepseek":
            body["user_id"] = self._user_id
            if self.thinking_enabled:
                body["thinking"] = {"type": "enabled"}
        return body

    def _build_api_kwargs(self, stream=False, max_tokens=None, tools=None, tool_choice=None,
                          temperature=None, thinking=None):
        """
        thinking 参数：
          None  = 跟随 self.thinking_enabled（主对话）
          True  = 强制开启思考
          False = 强制关闭思考（摘要等快速调用）
        """
        if max_tokens is None:
            max_tokens = self.model_config.max_tokens
        thinking_on = self.thinking_enabled if thinking is None else bool(thinking)
        kwargs = {
            "model": self.current_model,
            "stream": stream,
            "max_tokens": max_tokens,
        }
        # DeepSeek：原生思考参数（thinking / reasoning_effort / user_id）
        if self.model_config.supports_thinking:
            extra = self._get_extra_body()
            if extra:
                kwargs["extra_body"] = extra
            if thinking_on:
                kwargs["reasoning_effort"] = self.reasoning_effort
        # 本地模型：通过 think 参数控制思考（Qwen3 支持，跟随用户开关）
        # 需放进 extra_body（SDK 校验严格，顶层参数会报 unexpected keyword）
        if self.model_config.is_local():
            extra_body = dict(kwargs.get("extra_body") or {})
            extra_body["think"] = thinking_on
            kwargs["extra_body"] = extra_body
            # 思考时不限制 max_tokens（给足思考链 + 正文空间；防死循环交给手动截断按钮，而不是截断 token）
            if thinking_on:
                kwargs["max_tokens"] = 32768
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice
        if temperature is not None:
            kwargs["temperature"] = temperature
        return kwargs

    # ── 重试装饰器 ──
    def _retry_on_failure(self, func):
        max_retries = self.model_config.max_retries
        delay = self.model_config.retry_delay_seconds

        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    print(f" API调用失败 (尝试 {attempt+1}/{max_retries+1}): {e}")
                    if attempt == max_retries:
                        raise
                    time.sleep(delay)
            raise last_exc
        return wrapper

    def quick_chat(self, messages: list, max_tokens=None, temperature=0) -> Optional[str]:
        func = self._retry_on_failure(lambda: self._do_quick_chat(messages, max_tokens, temperature))
        return func()

    def _do_quick_chat(self, messages, max_tokens, temperature):
        self._ensure_client()
        # 后台快速调用（摘要等）强制不思考：避免耗时长 + 思考耗尽 token 导致正文为空
        kwargs = self._build_api_kwargs(
            max_tokens=max_tokens or self.model_config.max_tokens,
            temperature=temperature, thinking=False)
        kwargs["messages"] = messages
        response = self._client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content
        if content is None:
            print(" quick_chat: API 返回 content=None")
            return None
        return content

    # ── 文本生成三级回退（非流式 → 流式 → 补充请求） ──
    def _generate_text(self, messages, prefer_non_stream=True, on_chunk=None, on_reasoning=None):
        """
        生成回复文本，带三级回退，返回 (full_text, reasoning)。
        1) 非流式（prefer_non_stream=True 时优先）
        2) 流式（分片经 on_chunk / on_reasoning 回调传出）
        3) 有思考链但无正文时，追加思考链再补一次请求
        """
        full_text = ""
        reasoning = ""

        # 1) 非流式
        if prefer_non_stream:
            try:
                nsk = self._build_api_kwargs(stream=False, tools=None, tool_choice=None)
                nsk["messages"] = messages
                resp = self._client.chat.completions.create(**nsk)
                full_text = resp.choices[0].message.content or ""
                reasoning = getattr(resp.choices[0].message, "reasoning_content", "") or ""
                if full_text:
                    return full_text, reasoning
            except Exception as e:
                print(f" 非流式生成失败: {e}，回退流式...")

        # 2) 流式
        try:
            skw = self._build_api_kwargs(stream=True, tools=None, tool_choice=None)
            skw["messages"] = messages
            stream_response = self._client.chat.completions.create(**skw)
            for chunk in stream_response:
                delta = chunk.choices[0].delta
                if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                    if not reasoning:
                        # 本函数供摘要/概览卡等**后台任务**使用，加前缀避免与主对话输出混淆
                        print("\n[摘要] 后台生成·思考链：", end="")
                    reasoning += delta.reasoning_content
                    print(delta.reasoning_content, end="", flush=True)
                    if on_reasoning:
                        on_reasoning(delta.reasoning_content)
                if delta.content:
                    full_text += delta.content
                    if on_chunk:
                        on_chunk(delta.content)
            if reasoning:
                print()
        except Exception as e:
            print(f" 流式生成失败: {e}")

        # 3) 补充请求：有思考链但无正文
        if not full_text and reasoning:
            print(" 有思考链但无回复文本，请求补充回复...")
            try:
                fb_msgs = messages.copy()
                fb_msgs.append({"role": "assistant", "content": None, "reasoning_content": reasoning})
                fb_msgs.append({"role": "user", "content": "请基于你的思考输出最终回复。"})
                resp = self._client.chat.completions.create(
                    model=self.current_model,
                    messages=fb_msgs,
                    extra_body=self._get_extra_body(),
                )
                full_text = resp.choices[0].message.content or ""
            except Exception as e:
                print(f" 补充请求失败: {e}")

        return full_text, reasoning

    # ── 消息完整性维护 ──
    def _ensure_complete_tool_calls(self, messages):
        cleaned = []
        skip_next_tools = 0
        for i, msg in enumerate(messages):
            if skip_next_tools > 0:
                if msg.get("role") == "tool":
                    skip_next_tools -= 1
                    continue
                else:
                    skip_next_tools = 0

            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                tool_calls = msg["tool_calls"]
                required_ids = [tc["id"] for tc in tool_calls if "id" in tc]
                next_msgs = messages[i + 1:]
                actual_tools = []
                for nxt in next_msgs:
                    if nxt.get("role") != "tool":
                        break
                    actual_tools.append(nxt)
                actual_ids = [t.get("tool_call_id") for t in actual_tools]
                if len(required_ids) != len(actual_ids) or any(
                    req != act for req, act in zip(required_ids, actual_ids)
                ):
                    skip_next_tools = len(actual_tools)
                    continue

            if msg.get("role") == "tool":
                if i == 0 or messages[i - 1].get("role") != "assistant" or not messages[i - 1].get("tool_calls"):
                    continue
            cleaned.append(msg)
        return cleaned

    def _trim_messages(self, messages, max_messages=40):
        if len(messages) <= max_messages:
            return messages
        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]
        if len(non_system) <= max_messages:
            return system_msgs + non_system

        start_idx = len(non_system) - max_messages
        trimmed = non_system[start_idx:]
        for _ in range(20):
            if start_idx == 0:
                break
            first_role = trimmed[0].get("role")
            if first_role == "tool":
                found = False
                for i in range(start_idx - 1, -1, -1):
                    if non_system[i].get("role") == "assistant" and non_system[i].get("tool_calls"):
                        trimmed = non_system[i:]
                        start_idx = i
                        found = True
                        break
                if not found:
                    if len(trimmed) > 1:
                        trimmed = trimmed[1:]
                        start_idx += 1
                    else:
                        break
                continue
            elif first_role == "assistant" and trimmed[0].get("tool_calls"):
                tc_ids = [tc["id"] for tc in trimmed[0]["tool_calls"] if "id" in tc]
                found_ids = set()
                for j in range(1, len(trimmed)):
                    if trimmed[j]["role"] == "tool":
                        found_ids.add(trimmed[j].get("tool_call_id"))
                    else:
                        break
                if set(tc_ids) != found_ids:
                    if len(trimmed) > 1:
                        trimmed = trimmed[1:]
                        start_idx += 1
                    else:
                        break
                    continue
            break
        if len(system_msgs) + len(trimmed) < len(messages):
            print(f" 消息列表已裁剪：{len(messages)}  {len(system_msgs) + len(trimmed)} 条")
        return system_msgs + trimmed

    def _safe_json_loads(self, arguments_str: str) -> dict:
        try:
            return json.loads(arguments_str)
        except json.JSONDecodeError:
            fixed = re.sub(r'(:\s*)(\d+-\d+)\s*([,}])', r'\1"\2"\3', arguments_str)
            return json.loads(fixed)

    # ── 系统提示 ──
    def _build_system_prompt(self) -> str:
        prompt = self.persona_config.system_prompt
        if not prompt:
            prompt = (
                "你是一只诚实、可靠的助手。执行任务（编程、分析、检索、写文档等）时直接、专业、简洁，"
                "不带角色扮演或口癖；仅日常闲聊可偶尔使用'噗咕～'作为口癖。"
            )
        return prompt

    # ── 流式工具调用循环 ──
    def chat_stream(
        self,
        messages: List[Dict],
        tool_executor: Callable[[str, dict], str],
        max_iterations: int = 8,
        system_prompt: str = "",
        plugin_tools: List[Dict] = None,
    ) -> Generator[Dict, None, None]:
        """流式对话 + 工具调用循环。"""
        self._ensure_client()
        _clear_stop()
        system_content = system_prompt or self._build_system_prompt()
        # 无条件追加工具调用指令
        system_content += (
            "\n\n## 工具调用指令\n"
            "你有多种工具可通过函数调用直接执行。\n"
            "当需要工具时直接调用函数，不要先客套或预告。\n"
            "当用户要求你录入、记住、保存、搜索或查看内容时，必须调用对应工具实际执行并报告结果，"
            "禁止只说\"好的\"\"我会记住的\"而不调用工具。\n"
            "工具执行完根据结果自然总结回复，不要重复工具参数。"
        )
        if not any(m["role"] == "system" for m in messages):
            messages.insert(0, {"role": "system", "content": system_content})

        # 剥离本地字段（time）后再进工具循环：api_state 带着 time 落盘，
        # 但模型接口不认这个字段
        messages = _api_messages(messages)

        messages = self._ensure_complete_tool_calls(messages)

        _plugin_tools = plugin_tools or []
        all_tools = _plugin_tools + BUILTIN_TOOLS
        builtin_names = {t["function"]["name"] for t in BUILTIN_TOOLS}

        SAFE_REPEAT_TOOLS = {"get_status"}
        last_fingerprint = ""
        consecutive_identical = 0
        MAX_IDENTICAL = 3
        force_finish = False

        # ── 阶段上报 ──
        # UI 侧的表情由这个驱动，**不再从展示文本反推**。展示文本是给人看的：
        # 分支一变就失准（`正在生成最终回复` 那个 yield 就是死代码 —— 只有工具循环
        # 跑满 max_iterations 才到得了，正常对话在「无工具调用」分支就 return 了）。
        # 阶段：thinking / tool / found / writing
        _last_phase = None

        def _phase_event(name: str):
            nonlocal _last_phase
            if name == _last_phase:
                return None
            _last_phase = name
            return {"type": "phase", "content": name}

        for iteration in range(max_iterations):
            if force_finish:
                break
            if _stop_event.is_set():
                print(" [用户停止] 生成被手动中断")
                yield {"type": "done", "content": "（已停止生成）", "reasoning": ""}
                return

            messages = self._trim_messages(messages, max_messages=40)
            kwargs = self._build_api_kwargs(tools=all_tools, tool_choice="auto", stream=True)
            kwargs["messages"] = messages
            # DEBUG：thinking=DeepSeek 思考参数，think=Ollama 本地思考参数（都显示）
            extra = kwargs.get("extra_body", {})
            thinking_cfg = extra.get("thinking", "未设置")
            local_think = extra.get("think", "未设置")
            print(f" [DEBUG] 第{iteration+1}轮: model={kwargs['model']}, "
                  f"thinking(DeepSeek)={thinking_cfg}, think(本地)={local_think}, "
                  f"thinking_enabled={self.thinking_enabled}, "
                  f"tools数={len(kwargs.get('tools',[]))}, tool_choice={kwargs.get('tool_choice')}")

            ev = _phase_event("thinking")
            if ev:
                yield ev

            try:
                stream_response = self._client.chat.completions.create(**kwargs)
            except Exception as e:
                print(f" API 调用失败 (第 {iteration+1} 轮): {e}")
                err = str(e).lower()
                if "not found" in err or ("model" in err and ("no such" in err or "404" in err)):
                    hint = f" 模型 {kwargs.get('model')} 不存在或已被删除，请在上方重新选择模型。"
                elif "connection" in err or "connect" in err or "timed out" in err or "timeout" in err:
                    hint = f" 连接模型服务失败：请确认服务正在运行（本地 Ollama 已启动？）、或云端网络可用。"
                else:
                    hint = f" AI 调用出错：{e}"
                yield {"type": "done", "content": hint}
                return

            # ── 流式累加 content / reasoning / tool_calls ──
            acc_content = ""
            acc_reasoning = ""
            acc_tool_calls = []

            try:
                for chunk in stream_response:
                    if _stop_event.is_set():
                        break
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta is None:
                        continue
                    rc = _get_reasoning(delta)
                    if rc:
                        acc_reasoning += rc
                        print(rc, end="", flush=True)  # 思考链实时打印到 cmd
                        yield {"type": "reasoning", "content": rc}
                        ev = _phase_event("thinking")
                        if ev:
                            yield ev
                    if delta.content:
                        if not acc_content and acc_reasoning:
                            # 思考链与正文之间补一个分隔，避免控制台里两段粘连
                            print("\n[正文] ", end="", flush=True)
                        acc_content += delta.content
                        print(delta.content, end="", flush=True)  # 正文实时打印到 cmd
                        yield {"type": "text", "content": delta.content}
                        ev = _phase_event("writing")
                        if ev:
                            yield ev
                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            idx = tc.index
                            while len(acc_tool_calls) <= idx:
                                acc_tool_calls.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                            if tc.id:
                                acc_tool_calls[idx]["id"] = tc.id
                            if tc.function and tc.function.name:
                                acc_tool_calls[idx]["function"]["name"] += tc.function.name
                            if tc.function and tc.function.arguments:
                                acc_tool_calls[idx]["function"]["arguments"] += tc.function.arguments
            except Exception as e:
                # 流式响应中途断连：用已生成的部分内容收尾，不抛 traceback
                print(f" 流式响应中断: {e}")

            # 流式结束：只补一行元信息。
            # 注意：思考链与正文在上面已**逐字实时打印过**，这里不能再整段回打，
            # 否则控制台会出现两份一模一样的内容（曾被误认为"模型重复思考"）。
            if acc_reasoning:
                print(f"\n[思考链 #{iteration+1} 完整 {len(acc_reasoning)} 字]")
            if acc_content:
                print(f"[正文 #{iteration+1} 完整 {len(acc_content)} 字]")

            if _stop_event.is_set():
                # 用户手动停止：用已生成的部分内容收尾
                print(" [用户停止] 生成被手动中断")
                full_text = acc_content or "（已停止生成）"
                assistant_final = {"role": "assistant", "content": full_text}
                if acc_reasoning:
                    assistant_final["reasoning_content"] = acc_reasoning
                messages.append(assistant_final)
                yield {"type": "done", "content": full_text, "reasoning": acc_reasoning}
                return

            if not acc_tool_calls:
                # ── 无工具调用：纯文本回复 ──
                full_text = acc_content
                stream_reasoning = acc_reasoning
                if not full_text:
                    full_text = "（思考中...噗咕～）"

                assistant_final = {"role": "assistant", "content": full_text}
                if stream_reasoning:
                    assistant_final["reasoning_content"] = stream_reasoning
                messages.append(assistant_final)
                yield {"type": "done", "content": full_text, "reasoning": stream_reasoning}
                return

            # ── 有工具调用 ──
            assistant_msg = {
                "role": "assistant",
                "content": acc_content or "",
                "tool_calls": acc_tool_calls,
            }
            if acc_reasoning:
                assistant_msg["reasoning_content"] = acc_reasoning
            messages.append(assistant_msg)

            for tc in acc_tool_calls:
                func_name = tc["function"]["name"]
                arguments = self._safe_json_loads(tc["function"]["arguments"])

                yield {"type": "status", "content": f" 正在调用 {func_name}..."}
                ev = _phase_event("tool")
                if ev:
                    yield ev
                print(f" [鲸鱼娘] 调用工具: {func_name}({arguments})")

                if func_name not in SAFE_REPEAT_TOOLS:
                    arg_fp = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
                    current_fp = f"{func_name}:{arg_fp}"
                    if current_fp == last_fingerprint:
                        consecutive_identical += 1
                    else:
                        consecutive_identical = 1
                        last_fingerprint = current_fp
                else:
                    consecutive_identical = 0
                    last_fingerprint = ""

                # 工具执行放后台线程，主流程轮询停止事件：耗时工具（crawl/deep/kb_rebuild 等）
                # 也能被「停止」按钮立即中断响应（工具线程 daemon，停止后自然结束）
                result_box = {}

                def _run_tool():
                    try:
                        result_box["result"] = tool_executor(func_name, arguments)
                    except Exception as e:
                        result_box["error"] = f" 工具执行异常: {e}"
                        print(f" 工具 {func_name} 执行错误: {e}")
                        traceback.print_exc()

                t = threading.Thread(target=_run_tool, daemon=True)
                t.start()
                result = ""
                while t.is_alive():
                    if _stop_event.is_set():
                        result = "（用户已停止，工具执行被中断）"
                        break
                    time.sleep(0.05)
                if not result:
                    result = result_box.get("result", result_box.get("error", ""))

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": str(result),
                })

                if _stop_event.is_set():
                    print(f" [用户停止] 工具 {func_name} 执行被中断，结束本轮")
                    break

            # 这一轮的工具全部返回了 —— 「哦，拿到了」的真实时刻。
            # 原来挂在 `正在生成最终回复` 那个状态行上，但那是死代码，所以
            # 「拿到结果」这个表情从来没出现过。
            ev = _phase_event("found")
            if ev:
                yield ev

            if _stop_event.is_set():
                print(" [用户停止] 结束工具循环")
                yield {"type": "done", "content": "（已停止生成）", "reasoning": ""}
                return

            if consecutive_identical >= MAX_IDENTICAL:
                print(f" 检测到重复调用 {last_fingerprint} 共 {consecutive_identical} 次，强制结束")
                force_finish = True

        # ── 工具调用结束，生成最终回复 ──
        yield {"type": "status", "content": " 正在生成最终回复..."}
        ev = _phase_event("writing")
        if ev:
            yield ev

        messages.append({
            "role": "user",
            "content": "以上是你要用到的所有信息和工具返回结果。请根据已获得的信息，用自然语言直接回答，不要再调用任何工具。",
        })
        messages = self._trim_messages(messages, max_messages=30)

        print(" 请求最终总结...")
        chunks = []
        full_text, stream_reasoning = self._generate_text(
            messages, prefer_non_stream=False, on_chunk=chunks.append)
        for c in chunks:
            yield {"type": "text", "content": c}

        if not full_text:
            full_text = " 咱已经完成操作啦，但回复生成出了点问题，可以再说一遍吗噗咕～"

        assistant_final = {"role": "assistant", "content": full_text}
        if stream_reasoning:
            assistant_final["reasoning_content"] = stream_reasoning
        messages.append(assistant_final)
        yield {"type": "done", "content": full_text, "reasoning": stream_reasoning}
