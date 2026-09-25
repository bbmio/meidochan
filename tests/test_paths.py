"""路径层测试（ARCHITECTURE_V3 §8.8：路径解析必须有 pytest；对应风险 R1）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.paths import APP_DIR, RESOURCE_DIR, app_path, resource_path, to_app_path


def test_app_path_is_absolute_and_under_app_dir():
    p = app_path("config", "bot.toml")
    assert p.is_absolute()
    assert p.parent.parent == APP_DIR
    assert p.name == "bot.toml"


def test_resource_path_falls_back_to_app_dir_when_not_frozen():
    # 未打包时 RESOURCE_DIR == APP_DIR（config/plugins 等在源码树里）
    assert resource_path("x").is_absolute()
    assert RESOURCE_DIR.exists()


def test_to_app_path_relative_resolves_against_app_dir():
    assert to_app_path("data/conversations") == APP_DIR / "data" / "conversations"
    assert to_app_path("conversations") == APP_DIR / "conversations"


def test_to_app_path_absolute_is_kept():
    absolute = Path("C:/tmp/meido-test") if sys.platform == "win32" else Path("/tmp/meido-test")
    assert to_app_path(str(absolute)) == absolute
    # 绝对路径 + 追加部分
    assert to_app_path(str(absolute), "child") == absolute / "child"


def test_to_app_path_accepts_path_objects():
    assert to_app_path(Path("plugins")) == APP_DIR / "plugins"
