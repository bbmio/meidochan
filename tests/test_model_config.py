"""模型配置层测试：站点列表、落盘持久化、密钥外置、向后兼容。

重点锁死"在设置里改完 → 写回 model.toml → 重启后仍在"这个行为。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config.loader import ConfigLoader  # noqa: E402
from core.config.models import ModelSite  # noqa: E402


def _write(tmp_path: Path, text: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "model.toml"
    target.write_text(text, encoding="utf-8")
    return target


# ── 读取 / 向后兼容 ──

def test_legacy_config_synthesizes_one_site(tmp_path):
    """老配置只有 [model]，没有 [[model.sites]] → 合成一个站点。"""
    _write(tmp_path, '[model]\nprovider = "ollama"\n')
    mc = ConfigLoader(str(tmp_path)).get_model_config()
    assert len(mc.sites) == 1
    assert mc.sites[0].provider == "ollama"
    assert mc.active_site == mc.sites[0].name


def test_sites_are_loaded(tmp_path):
    _write(tmp_path, (
        '[model]\nprovider = "deepseek"\n'
        '[[model.sites]]\nname = "A"\nprovider = "ollama"\nmodel = "m1"\n'
        '[[model.sites]]\nname = "B"\nprovider = "deepseek"\nmodel = "m2"\n'))
    mc = ConfigLoader(str(tmp_path)).get_model_config()
    assert [s.name for s in mc.sites] == ["A", "B"]
    assert [s.model for s in mc.sites] == ["m1", "m2"]


def test_invalid_site_entries_are_skipped(tmp_path):
    _write(tmp_path, '[model]\n[[model.sites]]\nprovider = "ollama"\n')  # 没 name
    mc = ConfigLoader(str(tmp_path)).get_model_config()
    assert mc.sites, "缺 name 的坏条目应被跳过并回落到合成站点"
    assert all(site.name for site in mc.sites)


# ── 保存 / 持久化 ──

def test_save_is_persisted_after_reload(tmp_path):
    """核心回归：保存后重新读盘（等价重启）仍生效。"""
    _write(tmp_path, '[model]\nprovider = "ollama"\n')
    loader = ConfigLoader(str(tmp_path))
    mc = loader.get_model_config()

    mc.sites.append(ModelSite(name="DeepSeek 云端", provider="deepseek",
                              base_url="https://api.deepseek.com",
                              model="deepseek-v4-flash"))
    mc.apply_site(mc.sites[-1])
    loader.save_model_config(mc, "DeepSeek 云端")

    fresh = ConfigLoader(str(tmp_path)).get_model_config()   # 模拟重启
    assert fresh.provider == "deepseek"
    assert fresh.default_model == "deepseek-v4-flash"
    assert fresh.active_site == "DeepSeek 云端"
    assert {s.name for s in fresh.sites} >= {"DeepSeek 云端", "本地 Ollama"}
    assert "[[model.sites]]" in (tmp_path / "model.toml").read_text(encoding="utf-8")


# ── 密钥处理 ──

def test_cloud_key_is_stored_outside_toml(tmp_path):
    _write(tmp_path, '[model]\nprovider = "deepseek"\n')
    loader = ConfigLoader(str(tmp_path))
    try:
        stored = loader.store_api_key("DeepSeek 云端", "deepseek", "sk-secret")
    finally:
        os.environ.pop("MEIDO_DEEPSEEK_KEY", None)

    assert stored == "${MEIDO_DEEPSEEK_KEY}"
    assert "sk-secret" not in (tmp_path / "model.toml").read_text(encoding="utf-8")
    local = (tmp_path / "apikey.local").read_text(encoding="utf-8")
    assert "MEIDO_DEEPSEEK_KEY" in local and "sk-secret" in local


def test_local_provider_key_stays_plaintext(tmp_path):
    _write(tmp_path, '[model]\nprovider = "ollama"\n')
    loader = ConfigLoader(str(tmp_path))
    # 本地占位 key 无保密需求，原样返回，不外置
    assert loader.store_api_key("本地 Ollama", "ollama", "ollama") == "ollama"


def test_env_reference_is_expanded_on_reload(tmp_path):
    _write(tmp_path, '[model]\nprovider = "deepseek"\napi_key = "${MEIDO_TEST_KEY}"\n')
    (tmp_path / "apikey.local").write_text('MEIDO_TEST_KEY = "sk-from-local"\n',
                                           encoding="utf-8")
    try:
        mc = ConfigLoader(str(tmp_path)).get_model_config()
    finally:
        os.environ.pop("MEIDO_TEST_KEY", None)
    assert mc.api_key == "sk-from-local"


# ── apply_site ──

def test_apply_site_updates_current_and_aliases():
    from core.config.models import ModelConfig

    mc = ModelConfig(provider="ollama", default_model="qwen3.5:9b")
    site = ModelSite(name="DeepSeek 云端", provider="deepseek",
                     base_url="https://api.deepseek.com", model="deepseek-v4-pro")
    mc.apply_site(site)
    assert mc.provider == "deepseek"
    assert mc.default_model == "deepseek-v4-pro"
    assert mc.available_models["flash"] == "deepseek-v4-flash"

    local = ModelSite(name="本地", provider="ollama",
                      base_url="http://localhost:11434/v1", model="qwen3.5:9b")
    mc.apply_site(local)
    # 本地站点：别名都指向当前模型，避免 /model flash 切到不存在的模型
    assert set(mc.available_models.values()) == {"qwen3.5:9b"}
