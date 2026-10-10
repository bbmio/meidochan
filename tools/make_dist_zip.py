"""把 `dist/meido` 打成「开包即用」的 zip —— 边打包边剔除个人数据。

## 为什么不能直接 zip 整个 dist/meido

`dist/meido/` 是**开发机跑出来的**，里面带着大量只属于本机的东西：

| 路径 | 内容 |
|:---|:---|
| `config/apikey.local` | **真 API key** |
| `config/mcp.toml` | 若配了 HTTP MCP，headers 里有**真 key** |
| `data/` | 日志（会记下 key）、`trusted_paths.json`（本机路径）、audit、tool_outputs… |
| `workspaces/` | **聊天记录**（向量库里也可能有 key） |
| `conversations/` | 对话导出 |
| `.meido_backups/` | 文件编辑器的备份（可能含被改过的私人文件） |

所以**绝不能**直接压缩整目录 —— 本脚本在写入 zip 时逐条跳过，
并在内存里把 `config/mcp.toml` 的 key 换成占位符（**不改动 dist 里的原文件**）。

## 用法

    python tools/make_dist_zip.py                  # 输出到桌面
    python tools/make_dist_zip.py <输出.zip 路径>   # 指定输出
"""
from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DIST = PROJECT_ROOT / "dist" / "meido"
#: zip 里的顶层目录名（ASCII，避免某些解压工具的中文乱码）
INNER = "meido-S"

#: 整目录剔除（个人数据 / 开发残留 / 运行期产物）
SKIP_DIRS = ("data", "workspaces", "conversations", ".meido_backups")
#: 单独剔除的文件（含真密钥）
SKIP_FILES = {"config/apikey.local"}
#: 这些文件里的密钥会被换成占位符
SCRUB_FILES = {"config/mcp.toml"}

#: ⚠️ **不要把具体密钥写在这里** —— 本文件是公开仓库的一部分，写死密钥等于把它发布出去。
#: 改成按「键名」擦除：任何叫 x-mcp-key / api_key / token / secret 的字段，值一律换成占位符。
_SECRET_RE = re.compile(
    r"(?i)(x-mcp-key|api[_-]?key|access[_-]?token|secret|password)"
    r'(["\']?\s*[=:]\s*["\']?)'
    r"([A-Za-z0-9_\-\.]{12,})"
)
PLACEHOLDER = "在此填入你的密钥"


def _scrub(text: str) -> str:
    """把敏感字段的值换成占位符（保留键名与引号，方便用户照着填）。"""
    return _SECRET_RE.sub(lambda m: m.group(1) + m.group(2) + PLACEHOLDER, text)

README = """妹抖酱 v0.1 · 使用说明
================================

【怎么用】
  解压后双击 meido.exe 即可。首次启动稍慢（要初始化，10~30 秒），之后正常。

【第一次用要做的】
  1. 打开「设置 → 模型」，填入你的 API Key（DeepSeek 等）。
     —— 本包**不含任何密钥**，需要你自己填。
  2. 可选：设置 → 外观 / Live2D 调外观。

【文件说明】
  meido.exe      主程序
  _internal/     运行库（别删、别改名）
  config/        配置（可编辑；改完重启生效）
  plugins/       插件
  assets/        Live2D 模型与素材
  data/          运行数据（日志、缓存，会自动生成）
  workspaces/    对话记录（会自动生成）

【注意】
  - 这是 onedir 打包，exe 必须和 _internal/ 等目录放在一起，单独拷走 exe 跑不起来。
  - 移动/备份时请整个文件夹一起搬。
"""


def build(out: Path) -> int:
    if not DIST.is_dir():
        print(f"[错误] 找不到 {DIST}（先跑一次打包）")
        return 2
    if out.exists():
        out.unlink()

    n_files = n_skipped = 0
    raw = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in sorted(DIST.rglob("*")):
            rel = path.relative_to(DIST).as_posix()
            if rel.split("/", 1)[0] in SKIP_DIRS or rel in SKIP_FILES or path.is_dir():
                if not path.is_dir():
                    n_skipped += 1
                continue
            if rel in SCRUB_FILES:
                data = _scrub(path.read_text(encoding="utf-8")).encode("utf-8")
            else:
                data = path.read_bytes()
            z.writestr(f"{INNER}/{rel}", data)
            n_files += 1
            raw += len(data)

        z.writestr(f"{INNER}/config/apikey.local.example",
                   'MEIDO__KEY = "sk-在此填入你的-API-Key"\n')
        z.writestr(f"{INNER}/使用说明.txt", README)

    print(f"写入 {n_files} 个文件（跳过 {n_skipped} 个私密条目），原始 {raw/1024/1024:.0f} MB")
    print(f"zip：{out}  （{out.stat().st_size/1024/1024:.0f} MB）")
    return 0


def main(argv: list[str]) -> int:
    if argv:
        out = Path(argv[0]).resolve()
    else:
        out = Path.home() / "Desktop" / "妹抖酱v0.1-开包即用版.zip"
    return build(out)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
