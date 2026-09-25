"""把 release 里的**文档类** .md 改成 .txt，方便用户双击打开。

## 为什么要单独一个脚本

源项目里这几篇是 `.md`（GitHub 渲染 / 编辑器友好），但**分发给用户**时，
Windows 上双击 `.md` 会弹「你要如何打开这个文件？」—— 用户打不开。

⚠️ **每次从源项目同步到 release 之后，都要再跑一次本脚本**：
同步会把 `.md` 拷回来、把改名悄悄撤销（`docs/*.md` 与 `docs/*.txt` 并存，
或直接退回 `.md`）。本脚本是幂等的，多跑几次无害。

用法：
    python tools/release_docs_to_txt.py            # 处理 ..\\meidochanv0.1-release
    python tools/release_docs_to_txt.py <目录>      # 指定 release 目录
    python tools/release_docs_to_txt.py --check     # 只看要做什么，不改动

## 不动 `skills/translate/SKILL.md`

它是**程序文件**，不是文档：`core/skills.py` 按 `SKILL.md` 这个确切名字加载，
改名会让「翻译」技能直接失效。

## 只改 release，不改源项目

源项目里引用这几篇的地方写的仍是 `.md`，那是**对的**（那边文件确实叫 .md）。
本脚本只让 release 内部自洽：改名 + 把 release 里指向它们的引用同步成 `.txt`。
两边各自正确，互不干扰。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

#: 要改成 .txt 的文档（相对 release 根）
DOCS: Tuple[str, ...] = (
    "README.md",
    "docs/ARCHITECTURE_V3.md",
    "docs/TEST_CHECKLIST.md",
    "docs/UPGRADE_PLAN.md",
)

#: 改名后要在文件正文里同步替换的引用
REFERENCE_SUBS: Tuple[Tuple[str, str], ...] = (
    (r"ARCHITECTURE_V3\.md", "ARCHITECTURE_V3.txt"),
    (r"TEST_CHECKLIST\.md", "TEST_CHECKLIST.txt"),
    (r"UPGRADE_PLAN\.md", "UPGRADE_PLAN.txt"),
    (r"README\.md", "README.txt"),
)

#: 这些行不动 —— 它们针对的是**源码仓库**（那边 README 仍是 .md）
SKIP_LINE_MARKERS: Tuple[str, ...] = ("git checkout", "git mv")


def _rename(path: Path, target: Path, dry: bool) -> str:
    """把 `.md` 改成 `.txt`。

    ⚠️ 目标可能**已经存在**：从源项目同步会把 `.md` 拷回来，而上一轮改名的
    `.txt` 还在 —— 这就是本脚本最常见的运行场景。此时以**刚同步来的 `.md` 为准**
    覆盖掉旧 `.txt`（`replace` / `git mv -f` 都是覆盖语义），
    随后 `_fix_references` 会把正文里的引用再改回 `.txt`。

    早期版本直接用 `rename`，在这个场景下会抛 `FileExistsError` —— 也就是说
    脚本恰好在它唯一被需要的时候失效。
    """
    overwrite = target.exists()
    if dry:
        action = "改名(覆盖同名旧文件)" if overwrite else "改名"
        return f"{action} {path.name} -> {target.name}"

    result = subprocess.run(["git", "mv", "-f", path.name, target.name],
                            cwd=path.parent, capture_output=True, text=True)
    if result.returncode != 0:
        path.replace(target)          # replace 是覆盖语义，不会抛 FileExistsError
        return f"改名(普通) {path.name} -> {target.name}"
    return f"改名(git mv) {path.name} -> {target.name}"


def _fix_references(path: Path, dry: bool) -> List[int]:
    """把文件正文里指向这些文档的 `.md` 引用改成 `.txt`。返回改动的行号。"""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    changed: List[int] = []
    for index, line in enumerate(lines, 1):
        if any(marker in line for marker in SKIP_LINE_MARKERS):
            continue
        original = line
        for pattern, replacement in REFERENCE_SUBS:
            line = re.sub(pattern, replacement, line)
        if line != original:
            changed.append(index)
            lines[index - 1] = line
    if changed and not dry:
        path.write_text("".join(lines), encoding="utf-8")
    return changed


def apply(release_dir: Path, dry: bool = False) -> int:
    if not release_dir.is_dir():
        print(f"[错误] 目录不存在：{release_dir}")
        return 2

    print(f"release 目录：{release_dir}")
    for rel in DOCS:
        source = release_dir / rel
        target = source.with_suffix(".txt")
        if source.exists():
            print("  " + _rename(source, target, dry))
        elif target.exists():
            print(f"  已是 .txt：{target.name}")
        else:
            print(f"  [跳过] 两者都不存在：{rel}")
            continue
        changed = _fix_references(target, dry)
        if changed:
            print(f"    更新引用行：{changed}")

    # 收尾检查：还有没有指向 .md 的死引用
    # 排除本脚本（含 release 里的那份副本）—— 它的 DOCS 清单里写的就是 .md
    # 名字，那是输入不是死引用。按**文件名**排除：两份副本的绝对路径并不相同。
    myself_name = Path(__file__).name
    leftovers: List[str] = []
    for pattern, _replacement in REFERENCE_SUBS:
        for path in release_dir.rglob("*"):
            if not path.is_file() or ".git" in path.parts:
                continue
            if path.suffix.lower() not in (".txt", ".py", ".bat", ".spec", ".toml"):
                continue
            if path.name == myself_name:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if any(marker in line for marker in SKIP_LINE_MARKERS):
                    continue
                if re.search(pattern, line):
                    leftovers.append(
                        f"{path.relative_to(release_dir).as_posix()}:{lineno}")

    if leftovers:
        print("\n[注意] 仍有指向 .md 的引用（请确认是否该改）：")
        for item in leftovers:
            print(f"  {item}")
    else:
        print("\n没有残留的 .md 死引用。")
    return 0


def main(argv: List[str]) -> int:
    dry = "--check" in argv
    args = [a for a in argv if not a.startswith("--")]
    if args:
        release_dir = Path(args[0]).resolve()
    else:
        release_dir = (Path(__file__).resolve().parent.parent.parent
                       / "meidochanv0.1-release")
    if dry:
        print("（--check：只预览，不修改）\n")
    return apply(release_dir, dry)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
