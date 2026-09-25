"""路径层（ARCHITECTURE_V3 §4.2，M2 前置，风险登记册 R1）。

- `APP_DIR`：用户数据 / 配置 / 插件 / 素材所在目录，**始终在 exe（或项目根）旁边**
  → 便携、可编辑、可迁移。
- `RESOURCE_DIR`：打包进 exe 的只读资源（本项目的 config/plugins/assets 等一律外置，
  所以正常运行时两者相同）。

铁律：`config/` `workspaces/` `plugins/` `assets/` `data/` 一律走 `app_path()`；
打包内的只读资源走 `resource_path()`。禁止再出现 `Path("config")` 这类相对路径。
"""
import sys
from pathlib import Path

IS_FROZEN = getattr(sys, "frozen", False)

# 用户数据 / 配置 / 插件 / 素材：始终在 exe（或项目根）旁边 → 可迁移、可编辑
APP_DIR: Path = (Path(sys.executable).parent if IS_FROZEN
                 else Path(__file__).resolve().parent.parent)

# 只读资源（打包进 exe 的静态资源）
RESOURCE_DIR: Path = Path(getattr(sys, "_MEIPASS", APP_DIR))


def app_path(*parts: str) -> Path:
    return APP_DIR.joinpath(*parts)


def resource_path(*parts: str) -> Path:
    return RESOURCE_DIR.joinpath(*parts)


def to_app_path(value: str | Path, *parts: str) -> Path:
    """把配置里声明的路径解析为绝对路径（M2 新增的辅助函数）。

    - 绝对路径：原样返回（用户显式指定绝对位置时尊重其选择）
    - 相对路径：按 `APP_DIR` 解析（打包后即 exe 同级目录）

    §4.2 只规定了上面 4 个符号；真实项目里 `config/*.toml` 会写 `conversations = "…"`
    这类相对值，需要一个统一的解析入口，否则调用方会各自拼接。行为保持"相对即 app 相对"。
    """
    path = Path(value)
    if path.is_absolute():
        return path.joinpath(*parts) if parts else path
    return app_path(*path.parts, *parts)
