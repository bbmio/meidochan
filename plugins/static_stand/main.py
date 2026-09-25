import os
from pathlib import Path

def get_stand_image():
    current_dir = Path(__file__).parent
    stand_img = current_dir / "stand.png"
    if stand_img.exists():
        return str(stand_img)
    else:
        print(" [static_stand] 未找到 stand.png，请放一张图！")
        return None