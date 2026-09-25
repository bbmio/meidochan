"""首次启动自检（ARCHITECTURE_V3 §6-M2 改动点 3）。

职责：在启动后检查「API Key / 模型服务连通性 / bge-m3 向量模型 / 对话模型」，
把技术错误翻译成**人话指引**。约定：
- 永不抛异常（任何探测失败都转成一条 CheckResult）
- 不替用户做决定：只在界面上提示，不自动改配置
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from typing import List, Optional, Tuple

from core.history_retrieval import EMBEDDING_MODEL_NAME

OLLAMA_HOST = "http://127.0.0.1:11434"
PROBE_TIMEOUT = 2.5


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    hint: str = ""


def _base_name(model: str) -> str:
    """把 `bge-m3:latest` / `qwen3.5:9b` 归一成不带 tag 的名字，便于比对。"""
    return (model or "").strip().split(":")[0].strip().lower()


def _http_json(url: str, timeout: float = PROBE_TIMEOUT) -> dict:
    request = urllib.request.Request(url, headers={"Authorization": "Bearer ollama"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def probe_ollama(timeout: float = PROBE_TIMEOUT) -> Tuple[bool, List[str], str]:
    """探测 Ollama：返回 (是否连通, 模型名列表, 说明)。"""
    try:
        data = _http_json(f"{OLLAMA_HOST}/api/tags", timeout)
        names = [str(m.get("name") or m.get("model") or "")
                 for m in data.get("models", [])]
        names = [n for n in names if n]
        return True, names, "已连接"
    except Exception as exc:
        return False, [], f"{type(exc).__name__}: {exc}"


def _probe_openai_models(base_url: str, timeout: float = PROBE_TIMEOUT) -> Tuple[bool, List[str], str]:
    try:
        data = _http_json(base_url.rstrip("/") + "/models", timeout)
        names = [str(m.get("id") or m.get("model") or "") for m in data.get("data", [])]
        return True, [n for n in names if n], "已连接"
    except Exception as exc:
        return False, [], f"{type(exc).__name__}: {exc}"


# ── 单项检查 ──

def check_api_key(engine) -> CheckResult:
    config = engine.model_config
    if config.is_local():
        return CheckResult("API Key", True, f"{config.provider} 本地服务无需 Key")
    if (config.api_key or "").strip():
        return CheckResult("API Key", True, "已配置")
    return CheckResult(
        "API Key", False,
        detail="云端模型未配置 API Key",
        hint=("点窗口右上角「设置」→ 模型 → 填入 API Key 后点「保存并应用」；"
              "或设置环境变量 DEEPSEEK_API_KEY 后重启。"),
    )


def check_service(engine) -> Tuple[CheckResult, Optional[List[str]]]:
    """检查模型服务连通性，顺带返回可用模型列表（拿不到则 None）。"""
    config = engine.model_config
    if config.provider == "ollama":
        ok, models, detail = probe_ollama()
        if ok:
            return CheckResult("Ollama 服务", True, f"{detail}，共 {len(models)} 个模型"), models
        return CheckResult(
            "Ollama 服务", False,
            detail=f"连接 http://127.0.0.1:11434 失败（{detail}）",
            hint=("① 确认已安装并启动 Ollama（托盘里应有羊驼图标）；"
                  "② 浏览器打开 http://127.0.0.1:11434 应返回提示文本；"
                  "③ 若改过端口，请在「设置 → 模型」里同步 Base URL。"),
        ), None
    if config.provider == "lmstudio":
        ok, models, detail = _probe_openai_models(config.base_url)
        if ok:
            return CheckResult("LM Studio 服务", True, f"{detail}，共 {len(models)} 个模型"), models
        return CheckResult(
            "LM Studio 服务", False,
            detail=f"连接 {config.base_url} 失败（{detail}）",
            hint="启动 LM Studio → 加载模型 → 打开 Local Server（默认 1234 端口）。",
        ), None
    # 云端 / 自定义：不额外探测，避免误报
    return CheckResult("模型服务", True, f"{config.provider}（云端，跳过连通性探测）"), None


def check_embedding_model(models: Optional[List[str]]) -> CheckResult:
    if models is None:
        return CheckResult("向量模型 bge-m3", True, "跳过（服务未连通）")
    target = _base_name(EMBEDDING_MODEL_NAME)
    if any(_base_name(m) == target for m in models):
        return CheckResult("向量模型 bge-m3", True, "已就绪")
    return CheckResult(
        "向量模型 bge-m3", False,
        detail="未找到 bge-m3，历史检索与知识库将不可用",
        hint=f"在命令行执行：ollama pull {EMBEDDING_MODEL_NAME}",
    )


def check_chat_model(engine, models: Optional[List[str]]) -> CheckResult:
    config = engine.model_config
    if models is None:
        return CheckResult("对话模型", True, "跳过（服务未连通）")
    current = (engine.brain.current_model or "").strip()
    if not current:
        return CheckResult(
            "对话模型", False, detail="未设置对话模型",
            hint="在「设置 → 模型」里填写模型名（本地需先 ollama pull）。")
    if any(_base_name(m) == _base_name(current) for m in models):
        return CheckResult("对话模型", True, f"{current} 可用")
    return CheckResult(
        "对话模型", False,
        detail=f"服务里没有模型「{current}」（当前可用：{', '.join(models[:5]) or '无'}）",
        hint=f"在命令行执行：ollama pull {current}；或在「设置 → 模型」换成已有模型。",
    )


def check_config_files(engine) -> CheckResult:
    """检查有没有配置文件解析失败。

    `ConfigLoader.load()` 现在遇到坏 TOML 会回落默认值而不是抛异常 ——
    但**回落是静默的**，用户会以为自己的配置生效了，实际跑的是默认值。
    这一条就是那个告知渠道（配合 `MainWindow._on_selfcheck_done` 的非模态弹窗）。

    背景：`bot/model/persona` 三个配置都在 `engine.__init__` 里读，
    以前一个手抖的字符就等于「启动即崩」，且崩在窗口出现之前。
    """
    try:
        broken = engine.config.broken_files
    except Exception as exc:       # 自检永不抛异常
        return CheckResult("配置文件", False, f"无法检查：{type(exc).__name__}: {exc}")
    if not broken:
        return CheckResult("配置文件", True, "全部可解析")

    detail = "以下配置文件无法解析，本次已改用默认值：" + "、".join(
        f"config/{name}" for name in broken)
    reasons = "\n".join(f"    · {name}：{reason}" for name, reason in broken.items())
    return CheckResult(
        "配置文件", False,
        detail=f"{detail}\n{reasons}",
        hint=("原文件未被改动，按上面的原因修正语法后重启即可生效"
              "（首次发现时会另存一份 config/<文件名>.bak-<时间戳> 备份内容）。\n"
              "    常见的坏法：漏了引号、字符串里有未转义的双引号、把 = 写成了 :、"
              "多行文本忘了用三个引号。"),
    )


# ── 汇总 ──

def run_selfcheck(engine) -> List[CheckResult]:
    """跑一遍自检，返回全部结果（包含通过的项）。永不抛异常。"""
    results: List[CheckResult] = []
    try:
        results.append(check_api_key(engine))
    except Exception as exc:
        results.append(CheckResult("API Key", False, str(exc)))

    models: Optional[List[str]] = None
    try:
        service, models = check_service(engine)
        results.append(service)
    except Exception as exc:
        results.append(CheckResult("模型服务", False, str(exc)))

    try:
        results.append(check_embedding_model(models))
    except Exception as exc:
        results.append(CheckResult("向量模型 bge-m3", False, str(exc)))

    try:
        results.append(check_chat_model(engine, models))
    except Exception as exc:
        results.append(CheckResult("对话模型", False, str(exc)))

    try:
        results.append(check_config_files(engine))
    except Exception as exc:
        results.append(CheckResult("配置文件", False, str(exc)))
    return results


def format_report(results: List[CheckResult]) -> str:
    """把自检结果整理成可读文本（写日志 / 弹窗都用它）。"""
    problems = [r for r in results if not r.ok]
    if not problems:
        return "启动自检通过：模型服务与向量模型均就绪。"
    lines = ["启动自检发现问题："]
    for item in problems:
        lines.append(f"\n• {item.name}：{item.detail}")
        if item.hint:
            lines.append(f"  解决：{item.hint}")
    return "\n".join(lines)
