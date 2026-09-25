"""Live2D 模型资源清单（动作名 / 表情名）。

## 这是「模型目录里到底有哪些动作和表情」的**唯一出处**

三个地方都要这份清单：

- `ui_qt/media/sources/live2d_source.py` —— 塞进页面 manifest，播放器按名解析
- `ui_qt/settings_dialog.py` —— 「对话状态 → 动作 / 表情」下拉框的候选
- `tools/probe_webengine.py` —— 自检时用的假 manifest

以前前两处各自扫各自的（`rglob("*.motion3.json")` 各写一遍）。设置界面再加一份
就会变成三份 —— 而一旦它们对「动作名」的定义有分歧，就会出现**最难查的那类错配**：
下拉框里选得出、播放器却认不出，界面上还看不出哪里不对。

## 命名规则（与 config/live2d.toml 的注释一致）

- 动作名 = 任意层级的 `*.motion3.json` 去掉扩展名
- 表情名 = 任意层级的 `*.exp3.json` 去掉扩展名
- 都写成**相对模型目录**的 POSIX 路径（`rglob` 会进子目录，所以可能是 `a/b` 形式）
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

MOTION_SUFFIX = ".motion3.json"
EXPRESSION_SUFFIX = ".exp3.json"


def scan_model_assets(model_dir: Path | str) -> Tuple[List[str], List[str]]:
    """扫描模型目录，返回 `(动作路径列表, 表情路径列表)`，均已排序。

    这里给的是**相对模型目录的完整路径**（如 `motions/自拍.motion3.json`），
    给播放器的 manifest 用。

    ⚠️ 但 `config/live2d.toml` 的 `state_map` 里**不能**写这个 ——
    那里要写**基名**（`自拍`）。原因见 `stem_of`。

    目录不存在 / 不可读时返回两个空列表 —— **不抛异常**：调用方（设置界面、
    自检）都可能在没有模型的机器上跑，那里「一个都没有」是正常状态，不是错误。
    """
    base = Path(model_dir)
    motions: List[str] = []
    expressions: List[str] = []
    if not base.is_dir():
        return motions, expressions
    try:
        motions = sorted(p.relative_to(base).as_posix()
                         for p in base.rglob(f"*{MOTION_SUFFIX}"))
        expressions = sorted(p.relative_to(base).as_posix()
                             for p in base.rglob(f"*{EXPRESSION_SUFFIX}"))
    except OSError:
        # 权限不足 / 路径过长等：当作「扫不到」，不让调用方崩
        return [], []
    return motions, expressions


def stem_of(path: str) -> str:
    """路径 → 基名：去目录、去扩展名。

        motions/自拍.motion3.json -> 自拍
        吐舌.exp3.json            -> 吐舌

    **必须与播放器里的 `stem()` 完全一致**
    （`assets/live2d/viewer/index.html`）：播放器用它注册动作与表情的名字，
    而 `state_map` 是按这个名字去查的。两边规则一旦有出入，
    表现就是「配置里填了却不出效果」，且界面上看不出哪里错。
    """
    name = str(path).replace("\\", "/").split("/")[-1]
    for suffix in (MOTION_SUFFIX, EXPRESSION_SUFFIX):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return name


def asset_names(model_dir: Path | str) -> Tuple[List[str], List[str]]:
    """给设置界面下拉框用的候选：`(动作基名, 表情基名)`，去重且已排序。

    去重是必要的：基名相同的两项在下拉框里长得一模一样，选了也没法区分。
    """
    motions, expressions = scan_model_assets(model_dir)
    return (sorted({stem_of(p) for p in motions}),
            sorted({stem_of(p) for p in expressions}))


def duplicate_names(model_dir: Path | str) -> Tuple[List[str], List[str]]:
    """基名重复的动作 / 表情（同一基名对应多个文件）。

    播放器按**基名**注册，所以两个不同目录下的同名文件会撞成一个名字 ——
    只能取到其中一个。当前模型没有这种情况，换模型就可能遇到，
    设置界面据此给出提示（否则用户会发现「我明明选了这个，播的却是另一个」）。
    """
    motions, expressions = scan_model_assets(model_dir)

    def _dups(paths: List[str]) -> List[str]:
        seen: Dict[str, int] = {}
        for p in paths:
            key = stem_of(p)
            seen[key] = seen.get(key, 0) + 1
        return sorted(name for name, count in seen.items() if count > 1)

    return _dups(motions), _dups(expressions)
