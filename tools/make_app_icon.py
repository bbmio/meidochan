"""从静态立绘生成应用图标（`assets/app.ico`）。

## 为什么要裁切

`plugins/static_stand/stand.png` 是 1100x1800 的**全身**立绘。直接拿它当图标
（旧 `_app_icon()` 就是这么干的）缩到 16x16 只会糊成一团色块 —— 看不清是什么。

所以这里裁出一个**以脸为中心的正方形**：脸的辨识度最高，小尺寸下也认得出。
裁切参数是实测挑出来的（试过 600/640/720/800/1040 五档，720 居中最好）。

## 用法

    python tools/make_app_icon.py            # 生成 assets/app.ico
    python tools/make_app_icon.py --preview  # 额外输出 data/_icon_preview.png 便于肉眼确认

换立绘后重跑一次即可。若换了构图差异较大的立绘，`CROP` 需要重新调。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "plugins" / "static_stand" / "stand.png"
TARGET = ROOT / "assets" / "app.ico"
PREVIEW = ROOT / "data" / "_icon_preview.png"

#: 裁切框（源图像素坐标）。以脸为中心的正方形，实测挑出的最佳构图。
CROP = (203, 20, 1023, 840)

#: .ico 内嵌的尺寸。Windows 会按显示场景挑：16/24 任务栏与标题栏，
#: 32 资源管理器，48+ 大图标视图，256 是「超大图标」与商店展示。
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main() -> int:
    if not SOURCE.exists():
        print(f"找不到源立绘：{SOURCE}")
        return 1

    src = Image.open(SOURCE).convert("RGB")
    left, top, right, bottom = CROP
    if right > src.width or bottom > src.height:
        print(f"裁切框超出图像范围：{CROP} vs {src.size}")
        return 1

    # 先裁到 256（ico 的最大档）再交给 Pillow 逐档缩放。
    # 保持 RGB：图标不需要透明，源图本身有完整背景。
    base = src.crop(CROP).resize((256, 256), Image.LANCZOS)

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    base.save(TARGET, format="ICO", sizes=SIZES)

    print(f"已生成 {TARGET}")
    print(f"  源图   {src.size[0]}x{src.size[1]}")
    print(f"  裁切框 {CROP}  ({right - left}x{bottom - top})")
    print(f"  内嵌档 {', '.join(f'{w}x{h}' for w, h in SIZES)}")
    print(f"  体积   {TARGET.stat().st_size / 1024:.1f} KB")

    if "--preview" in sys.argv:
        PREVIEW.parent.mkdir(parents=True, exist_ok=True)
        base.save(PREVIEW)
        print(f"  预览   {PREVIEW}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
