"""应用身份常量（产品显示名 / 版本 / 日志前缀）。

按 ARCHITECTURE_V3 §1：产品显示名与版本**只在本文件定义一次**，
配置文件（config/app.toml）不再重复书写，避免两处不一致。
角色名（人设名，如「鲸鱼娘」）属于 persona.toml，与产品名无关。
"""

APP_NAME = "妹抖酱"
APP_VERSION = "0.1.0"
APP_DISPLAY = f"{APP_NAME} v0.1"      # 标题栏 / 托盘 / About 用
LOG_FILE_PREFIX = "meido"             # 日志文件名前缀（M2 文件日志启用）
BUILD_NAME = "meido"                  # PyInstaller 产物名，交付时改名为「妹抖酱v0.1.exe」
