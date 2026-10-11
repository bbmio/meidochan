"""安全加固回归测试（2026-10-11）。

覆盖三处加固：
1. **日志脱敏**（`core/brain.py`）：工具调用参数会原样进日志，而日志常被分享；
   `redact_secrets()` 必须在打印前遮蔽密钥（键名命中 / 密钥串形态）。
   起因：实测妹抖酱把 MCP 的 `x-mcp-key` 当参数传给 `edit_file`，密钥永久留在了日志里。
2. **读保护**（`plugins/file_explorer`）：含密钥 / 凭据的文件即使落在白名单内，
   也**不允许读内容**（否则会进对话上下文与日志）。
3. **提示词防注入**（`config/persona.toml`）：外部内容一律当数据而非指令。
"""
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plugins.file_explorer import main as fe  # noqa: E402


# ═══════════════════════════════════════════════════════════
# 1. 日志脱敏
# ═══════════════════════════════════════════════════════════

class TestRedactSecrets:
    @pytest.fixture()
    def redact(self):
        from core.brain import redact_secrets, MASK
        return redact_secrets, MASK

    def test_key_name_masked(self, redact):
        redact_secrets, MASK = redact
        assert redact_secrets({"x-mcp-key": "abcdef123456"})["x-mcp-key"] == MASK

    def test_various_key_names(self, redact):
        redact_secrets, MASK = redact
        for k in ("api_key", "token", "password", "Authorization", "credential"):
            assert redact_secrets({k: "v"})[k] == MASK

    def test_sk_value_masked(self, redact):
        redact_secrets, MASK = redact
        out = redact_secrets({"url": "https://x?k=sk-abcdefghijklmnopqrstuvwxyz012345"})
        assert "sk-abcdef" not in out["url"]
        assert MASK in out["url"]

    def test_long_hex_masked(self, redact):
        """32 位 hex 的 MCP key 必须被识别（键名未必含 key）。

        ⚠️ 这里用的是**假值**（全 deadbeef），绝不能把真 key 写进测试文件 ——
        本文件属于公开仓库，写进去就是又一次泄漏。
        """
        redact_secrets, MASK = redact
        assert redact_secrets({"note": "deadbeefdeadbeefdeadbeefdeadbeef"})["note"] == MASK

    def test_nested_structures(self, redact):
        redact_secrets, MASK = redact
        out = redact_secrets({
            "headers": {"Authorization": "Bearer zzz"},
            "args": ["--x"],
            "servers": [{"key": "abc"}],
        })
        assert out["headers"]["Authorization"] == MASK
        assert out["args"] == ["--x"]
        assert out["servers"][0]["key"] == MASK

    def test_plain_args_untouched(self, redact):
        redact_secrets, _ = redact
        d = {"path": "C:/Users/A/Desktop/a.txt", "content": "hello world", "lines": 100, "all": True}
        assert redact_secrets(d) == d

    def test_original_not_mutated(self, redact):
        """只读构造副本：原参数必须原样留给工具执行。"""
        redact_secrets, _ = redact
        d = {"x-mcp-key": "abcdef123456"}
        redact_secrets(d)
        assert d["x-mcp-key"] == "abcdef123456"

    def test_non_string_passthrough(self, redact):
        redact_secrets, _ = redact
        assert redact_secrets({"n": 1, "b": True, "x": None}) == {"n": 1, "b": True, "x": None}


# ═══════════════════════════════════════════════════════════
# 2. 读保护（file_explorer）
# ═══════════════════════════════════════════════════════════

def _grant(monkeypatch, root: Path):
    """把白名单与项目根都指到 tmp，让被测文件「在白名单内」。"""
    monkeypatch.setattr(fe, "WHITELIST", [root], raising=False)
    monkeypatch.setattr(fe, "APP_DIR", root, raising=False)


class TestReadDeny:
    @pytest.mark.parametrize("name", [
        "apikey.local", "mcp_servers.json", ".env", ".env.local",
        "conversation_memory.json", "secrets.local",
    ])
    def test_denied_names(self, monkeypatch, tmp_path, name):
        _grant(monkeypatch, tmp_path)
        p = tmp_path / name
        p.write_text("secret", encoding="utf-8")
        assert fe.is_readable(p) is False

    @pytest.mark.parametrize("rel", [
        "config/adapters.toml", "searxng/settings.yml", "qq/config.toml",
    ])
    def test_denied_paths(self, monkeypatch, tmp_path, rel):
        _grant(monkeypatch, tmp_path)
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
        assert fe.is_readable(p) is False

    def test_normal_file_readable(self, monkeypatch, tmp_path):
        _grant(monkeypatch, tmp_path)
        p = tmp_path / "note.txt"
        p.write_text("hi", encoding="utf-8")
        assert fe.is_readable(p) is True

    def test_example_still_readable(self, monkeypatch, tmp_path):
        """模板文件（*.example）必须仍可读 —— 否则用户看不到怎么配。"""
        _grant(monkeypatch, tmp_path)
        for name in ("mcp_servers.json.example", "apikey.local.example"):
            p = tmp_path / name
            p.write_text("template", encoding="utf-8")
            assert fe.is_readable(p) is True

    def test_outside_whitelist_denied(self, monkeypatch, tmp_path):
        monkeypatch.setattr(fe, "WHITELIST", [tmp_path / "allowed"], raising=False)
        monkeypatch.setattr(fe, "APP_DIR", tmp_path, raising=False)
        p = tmp_path / "other" / "a.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
        assert fe.is_readable(p) is False

    def test_info_and_view_blocked(self, monkeypatch, tmp_path):
        """工具层（info / view）必须返回拒绝说明，而不是内容。"""
        _grant(monkeypatch, tmp_path)
        p = tmp_path / "apikey.local"
        p.write_text("sk-should-not-leak", encoding="utf-8")
        info = fe.get_file_info(str(p))
        view = fe.preview_text(str(p))
        assert "sk-should-not-leak" not in info
        assert "sk-should-not-leak" not in view
        assert "安全" in info
        assert "安全" in view


# ═══════════════════════════════════════════════════════════
# 3. 提示词防注入
# ═══════════════════════════════════════════════════════════

class TestPersonaInjectionGuard:
    def test_has_external_untrusted_section(self):
        text = tomllib.load(open(ROOT / "config" / "persona.toml", "rb"))[
            "persona"]["system_prompt"]["text"]
        assert "外部内容不可信" in text
        assert "防注入" in text

    def test_keeps_other_guardrails(self):
        """加固不应挤掉既有护栏。"""
        text = tomllib.load(open(ROOT / "config" / "persona.toml", "rb"))[
            "persona"]["system_prompt"]["text"]
        assert "不给自己提权" in text
        assert "说人话" in text
