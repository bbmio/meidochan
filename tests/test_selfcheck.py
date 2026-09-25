"""启动自检测试（ARCHITECTURE_V3 §6-M2 验收：能识别人话指引）。

覆盖三种缺失场景：Key 未填 / Ollama 未运行 / 模型缺失。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import selfcheck


class _ModelConfig:
    def __init__(self, provider, api_key="", base_url=""):
        self.provider = provider
        self.api_key = api_key
        self.base_url = base_url or "http://localhost:11434/v1"

    def is_local(self):
        return self.provider in ("ollama", "lmstudio")


class _Brain:
    def __init__(self, model):
        self.current_model = model


class _Engine:
    def __init__(self, provider, api_key="", model="qwen3.5:9b", base_url=""):
        self.model_config = _ModelConfig(provider, api_key, base_url)
        self.brain = _Brain(model)
        # run_selfcheck 会读 engine.config.broken_files（配置文件检查项）。
        # 真实 engine 一定有 config，这里补个「没有坏配置」的最小替身。
        self.config = _Config()


class _Config:
    broken_files = {}


def _find(results, name):
    return next(r for r in results if r.name == name)


# ── API Key ──

def test_cloud_without_key_reports_hint():
    result = selfcheck.check_api_key(_Engine("deepseek"))
    assert result.ok is False
    assert "API Key" in result.detail
    assert "设置" in result.hint and "DEEPSEEK_API_KEY" in result.hint


def test_cloud_with_key_passes():
    assert selfcheck.check_api_key(_Engine("deepseek", api_key="sk-x")).ok is True


def test_local_provider_needs_no_key():
    assert selfcheck.check_api_key(_Engine("ollama")).ok is True


# ── Ollama 连通性 ──

def test_ollama_unreachable_gives_actionable_hint(monkeypatch):
    monkeypatch.setattr(selfcheck, "probe_ollama", lambda *a, **k: (False, [], "URLError"))
    results = selfcheck.run_selfcheck(_Engine("ollama"))

    service = _find(results, "Ollama 服务")
    assert service.ok is False
    assert "127.0.0.1:11434" in service.detail
    assert "启动" in service.hint
    # 服务不通时，模型项不应再刷屏报错（标记为跳过）
    assert _find(results, "向量模型 bge-m3").ok is True
    assert _find(results, "对话模型").ok is True
    assert selfcheck.format_report(results).count("启动自检发现问题") == 1


# ── 模型缺失 ──

def test_missing_embedding_model_points_to_ollama_pull(monkeypatch):
    monkeypatch.setattr(selfcheck, "probe_ollama",
                        lambda *a, **k: (True, ["qwen3.5:9b:latest"], "已连接"))
    result = _find(selfcheck.run_selfcheck(_Engine("ollama")), "向量模型 bge-m3")
    assert result.ok is False
    assert "ollama pull bge-m3" in result.hint


def test_missing_chat_model_lists_available(monkeypatch):
    monkeypatch.setattr(selfcheck, "probe_ollama",
                        lambda *a, **k: (True, ["bge-m3:latest"], "已连接"))
    result = _find(selfcheck.run_selfcheck(_Engine("ollama", model="qwen3.5:9b")), "对话模型")
    assert result.ok is False
    assert "qwen3.5:9b" in result.detail
    assert "ollama pull" in result.hint


def test_model_tag_is_ignored_when_matching(monkeypatch):
    """Ollama 返回带 :latest 的 tag，不能因此误报模型缺失。"""
    monkeypatch.setattr(selfcheck, "probe_ollama",
                        lambda *a, **k: (True, ["bge-m3:latest", "qwen3.5:9b:latest"], "已连接"))
    results = selfcheck.run_selfcheck(_Engine("ollama", model="qwen3.5:9b"))
    assert all(r.ok for r in results)
    assert "通过" in selfcheck.format_report(results)


def test_cloud_provider_skips_connectivity_probe():
    results = selfcheck.run_selfcheck(_Engine("deepseek", api_key="sk-x"))
    assert _find(results, "模型服务").ok is True
    assert _find(results, "向量模型 bge-m3").ok is True


def test_run_selfcheck_never_raises(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("探测炸了")

    monkeypatch.setattr(selfcheck, "probe_ollama", boom)
    results = selfcheck.run_selfcheck(_Engine("ollama"))   # 不应抛异常
    assert isinstance(results, list) and results
