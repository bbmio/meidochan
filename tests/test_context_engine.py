"""上下文引擎测试：固定规则(pins)持久化 + system prompt 分层组装。

对应关键功能里的**内置命令 /pin**、**工作空间人设覆盖**、**上下文组装**。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.context_engine import ContextEngine


@pytest.fixture()
def cfg(tmp_path) -> Path:
    """模拟一份最小配置目录（与真实 config/ 结构一致）。"""
    d = tmp_path / "config"
    d.mkdir()
    (d / "identity.toml").write_text(
        '[user]\nname = ""\ncity = ""\n\n[preferences]\nlanguage = "zh"\n\n[pins]\n',
        encoding="utf-8",
    )
    (d / "persona.toml").write_text(
        '[persona.system_prompt]\ntext = "你是鲸鱼娘。"\n', encoding="utf-8"
    )
    return d


class TestPins:
    def test_empty_pins_table_is_normalized(self, cfg):
        """identity.toml 里 [pins] 是空表 → 必须归一化成 list（旧实现 .append 直接崩）。"""
        assert ContextEngine(str(cfg)).get_pins() == []

    def test_add_pin_persists_across_restart(self, cfg):
        ce = ContextEngine(str(cfg))
        ce.add_pin("回答一律用中文")
        assert [p["rule"] for p in ce.get_pins()] == ["回答一律用中文"]

        fresh = ContextEngine(str(cfg))  # 模拟重启：必须从磁盘读回来
        assert [p["rule"] for p in fresh.get_pins()] == ["回答一律用中文"]

    def test_pin_with_quotes_survives_round_trip(self, cfg):
        """规则里含英文双引号时，identity.toml 不能被写坏。"""
        rule = '自称要写成 "鲸鱼娘"'
        ContextEngine(str(cfg)).add_pin(rule)
        assert [p["rule"] for p in ContextEngine(str(cfg)).get_pins()] == [rule]

    def test_two_pins_in_same_second_do_not_collide(self, cfg):
        ce = ContextEngine(str(cfg))
        ce.add_pin("规则一")
        ce.add_pin("规则二")
        ids = [p["id"] for p in ce.get_pins()]
        assert len(set(ids)) == 2  # 同一秒内加的规则 ID 不能撞车

    def test_remove_pin(self, cfg):
        ce = ContextEngine(str(cfg))
        ce.add_pin("规则一")
        ce.add_pin("规则二")
        ce.remove_pin(ce.get_pins()[0]["id"])
        assert [p["rule"] for p in ContextEngine(str(cfg)).get_pins()] == ["规则二"]

    def test_remove_unknown_pin_is_graceful(self, cfg):
        assert "未找到" in ContextEngine(str(cfg)).remove_pin("pin_not_exist")


class TestSystemPrompt:
    def test_all_layers_are_assembled(self, cfg):
        ce = ContextEngine(str(cfg))
        ce.add_pin("不要用 emoji")
        prompt = ce.build_system_prompt(profile_cards="称呼：老板")

        assert "你是鲸鱼娘。" in prompt                              # 基础人设
        assert "[概览卡]" in prompt and "称呼：老板" in prompt          # 概览卡
        assert "[固定规则]" in prompt and "不要用 emoji" in prompt      # pins

    def test_legacy_memory_layer_is_gone(self, cfg):
        """长期记忆摘要层已废弃（被概览卡取代），不应再出现在 system prompt 里。

        曾经的 memory.py 整段摘要 + [persona.memory] 配置 + [长期记忆] 段
        整条链路从未被执行过，已整体移除。这里守住「不要再长回来」。
        """
        ce = ContextEngine(str(cfg))
        prompt = ce.build_system_prompt(profile_cards="称呼：老板")
        assert "[长期记忆]" not in prompt

    def test_workspace_persona_overrides_global(self, cfg):
        ce = ContextEngine(str(cfg))
        ce.set_workspace_persona("你是另一只鲸鱼。")
        prompt = ce.build_system_prompt()
        assert prompt.startswith("你是另一只鲸鱼。")
        assert "你是鲸鱼娘。" not in prompt

    def test_missing_config_files_do_not_crash(self, tmp_path):
        empty = tmp_path / "empty_config"
        empty.mkdir()
        ce = ContextEngine(str(empty))
        assert ce.get_user_name() == ""
        assert isinstance(ce.build_system_prompt(), str)
