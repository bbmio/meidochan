import os
import json
import difflib
import fnmatch
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime
import mimetypes

from core.paths import APP_DIR, app_path

# 初始化 mimetypes
mimetypes.init()

# ── 配置 ──
# 默认行数（当 /view 未指定行数时读取的行数）
DEFAULT_VIEW_LINES = 100
# 完整读取时的最大字符数（防止极端大文件撑爆上下文）
MAX_FULL_CONTENT = 100000

# ── 白名单加载 ──
def load_whitelist():
    manifest_path = Path(__file__).parent / "manifest.json"
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    config = manifest.get("config", {})
    whitelist_raw = config.get("whitelist", [])
    
    resolved = []
    for p in whitelist_raw:
        # 展开环境变量；${PROJECT_DIR} 指妹抖酱自己的项目目录（与本插件所在目录同级）
        expanded = os.path.expandvars(p).replace("${PROJECT_DIR}", str(APP_DIR))
        path = Path(expanded)
        # 相对路径  基于插件目录解析
        if not path.is_absolute():
            path = (Path.home() / path).resolve()
        else:
            path = path.resolve()
        if path.exists():
            resolved.append(path)
        else:
            print(f" [file_explorer] 白名单路径不存在，已忽略: {path}")
    return resolved

WHITELIST = load_whitelist()

def is_allowed(target: Path) -> bool:
    """检查目标路径是否在白名单内"""
    target = target.resolve()
    for allowed in WHITELIST:
        try:
            target.relative_to(allowed)
            return True
        except ValueError:
            continue
    return False

# 常见文本扩展名
TEXT_EXTENSIONS = {
    ".txt", ".py", ".json", ".yaml", ".yml", ".md", ".markdown",
    ".log", ".ini", ".cfg", ".conf", ".toml", ".xml", ".html", ".htm",
    ".css", ".js", ".ts", ".csv", ".rst", ".tex", ".sh", ".bat", ".ps1",
    ".gitignore", ".env", ".dockerfile", ".makefile", ".cmake",
}

def is_text_file(file_path: Path) -> bool:
    """判断文件是否为可读文本"""
    mime_type, _ = mimetypes.guess_type(str(file_path))
    if mime_type and mime_type.startswith("text/"):
        return True
    return file_path.suffix.lower() in TEXT_EXTENSIONS

# ── 命令实现 ──

def list_directory(arg: str = "") -> str:
    """列出目录内容，无参数时使用白名单第一个目录"""
    if not WHITELIST:
        return " 未配置任何可访问目录，请联系管理员。"
    
    if not arg.strip() or arg == ".":
        base = WHITELIST[0]
    else:
        base = Path(arg)
        if not base.is_absolute():
            base = Path.cwd() / base
        base = base.resolve()

    if not is_allowed(base):
        return " 权限不足：该路径不在允许访问的白名单内。"
    if not base.exists():
        return f" 路径不存在: {base}"
    if not base.is_dir():
        return f" 不是文件夹: {base}"

    try:
        items = sorted(base.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except PermissionError:
        return f" 没有权限访问文件夹: {base}"

    if not items:
        return f" {base}\n（空文件夹）"

    lines = [f" {base}"]
    for item in items:
        stat = item.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
        size = "-"
        if item.is_file():
            size_bytes = stat.st_size
            if size_bytes < 1024:
                size = f"{size_bytes} B"
            elif size_bytes < 1024**2:
                size = f"{size_bytes/1024:.1f} KB"
            else:
                size = f"{size_bytes/(1024**2):.1f} MB"
        icon = "" if item.is_dir() else ""
        lines.append(f"  {icon} {item.name}  ({size}, {mtime})")
    return "\n".join(lines)


def get_file_info(arg: str) -> str:
    """显示文件详细信息，行数统计采用流式累加避免大文件卡死"""
    file = Path(arg).resolve()
    if not is_allowed(file):
        return " 权限不足。"
    if not file.exists():
        return f" 文件不存在: {file}"
    if file.is_dir():
        return " 请提供文件路径，文件夹请用 /ls"

    stat = file.stat()
    mime_type, _ = mimetypes.guess_type(str(file))
    mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    info = f""" 文件信息: {file.name}
路径: {file}
类型: {mime_type or '未知'}
大小: {stat.st_size:,} 字节
修改时间: {mtime}"""

    # 文本文件统计行数（流式累加）
    if is_text_file(file):
        try:
            line_count = 0
            with open(file, "r", encoding="utf-8") as f:
                while True:
                    chunk = f.readlines(100000)
                    if not chunk:
                        break
                    line_count += len(chunk)
            info += f"\n行数: {line_count}"
        except Exception as e:
            info += f"\n行数统计失败: {e}"
    return info


def preview_text(arg: str, default_lines: int = DEFAULT_VIEW_LINES) -> str:
    """
    预览文本文件。用法：/view <文件路径> [行数|all|start-end]
    支持自动截断并提示续读。
    """
    arg = arg.strip()
    if not arg:
        return " 请提供文件路径。"

    file_path_str = ""
    line_spec = None
    quote_char = None

    # 解析带引号路径和行参数
    if arg.startswith('"') or arg.startswith("'"):
        quote_char = arg[0]
        end_idx = arg.find(quote_char, 1)
        if end_idx != -1:
            file_path_str = arg[1:end_idx]
            rest = arg[end_idx+1:].strip()
            if rest:
                parts = rest.split()
                if parts[0].lower() == "all" or parts[0].isdigit() or \
                   ('-' in parts[0] and parts[0].replace('-', '').isdigit()):
                    line_spec = parts[0]
        else:
            file_path_str = arg
    else:
        parts = arg.rsplit(maxsplit=1)
        if len(parts) == 2:
            candidate = parts[1].lower()
            if candidate == "all" or candidate.isdigit() or \
               ('-' in candidate and candidate.replace('-', '').isdigit()):
                file_path_str = parts[0]
                line_spec = parts[1]
            else:
                file_path_str = arg
        else:
            file_path_str = arg

    # 解析行数/范围
    start_line = None
    end_line = None
    is_full = False
    if line_spec is None:
        # 未指定，使用默认行数
        start_line = 1
        end_line = default_lines
    elif line_spec.lower() == "all":
        start_line = 1
        end_line = -1
        is_full = True
    elif '-' in line_spec:
        parts_range = line_spec.split('-')
        if len(parts_range) == 2 and parts_range[0].isdigit() and parts_range[1].isdigit():
            start_line = int(parts_range[0])
            end_line = int(parts_range[1])
            if start_line > end_line:
                return " 行数范围错误：起始行不能大于结束行。"
        else:
            return " 行数范围格式错误，请使用如 100-200。"
    elif line_spec.isdigit():
        start_line = 1
        end_line = int(line_spec)
    else:
        return " 行数参数错误，只支持数字、'all' 或范围如 100-200。"

    # 路径解析
    file = Path(file_path_str).resolve()
    if not file.exists() and quote_char is None and line_spec is not None:
        file = Path(arg).resolve()
        start_line = 1
        end_line = default_lines
        if not file.exists():
            return f" 文件不存在: {file}"

    if not is_allowed(file):
        return " 权限不足。"
    if not file.exists():
        return f" 文件不存在: {file}"
    if file.is_dir():
        return " 请提供文件路径，文件夹请用 /ls"

    if not is_text_file(file):
        mime_type, _ = mimetypes.guess_type(str(file))
        return f" 不是可读取的文本文件，类型: {mime_type or '未知'}"

    try:
        with open(file, "r", encoding="utf-8") as f:
            # 先获取总行数（轻量统计，但可能稍慢）
            total_lines = 0
            for _ in f:
                total_lines += 1

            # 重新定位读取指定范围
            f.seek(0)
            if end_line == -1:
                selected_lines = f.readlines()
            else:
                selected_lines = []
                for i, line in enumerate(f, start=1):
                    if i < start_line:
                        continue
                    if i > end_line:
                        break
                    selected_lines.append(line)
            content = "".join(selected_lines)

            # 截断处理
            is_truncated = False
            if not is_full:
                # 指定行数或默认行数，检查是否到达文件末尾
                if end_line < total_lines:
                    is_truncated = True
            else:
                # 完整读取，受字符数硬限制
                if len(content) > MAX_FULL_CONTENT:
                    content = content[:MAX_FULL_CONTENT]
                    is_truncated = True

            info = f" {file.name} (行 {start_line}-{end_line if end_line != -1 else total_lines}，共 {total_lines} 行):\n```\n{content}\n```"

            if is_truncated:
                if not is_full:
                    info += f"\n\n[已读取前 {end_line} 行，共 {total_lines} 行。如需继续，请指定行范围（如 {end_line+1}-{end_line+100}）或说“完整读取”。]"
                else:
                    info += f"\n\n[文件过大，已截断显示前 {MAX_FULL_CONTENT} 字符。建议分段读取或指定行范围。]"
        return info
    except Exception as e:
        return f" 读取失败: {e}"


# ═══════════════════════════════════════════════════════════
# 写入能力（对标 CodeBuddy / zcode 的文件修改工具）
#
# 安全模型（配置见 manifest.json → config.write）：
#   1. 白名单：写前必须 resolve() 后过 is_allowed()，防 ../ 穿越与软链接
#   2. 备份：覆盖/删除前一律备份到 .meido_backups/，滚动保留 max_backups 份
#   3. 自身代码保护：命中 protected 的目标，写入后跑 verify_command，
#      失败即自动回滚（先备份，测试不坏才落定）
#   4. 两段式确认：require_confirm=true 时先给 diff，需再次调用带 confirm=true
#   5. 审计：每次写入记 data/audit/file_edits.jsonl
# ═══════════════════════════════════════════════════════════

MAX_DIFF_CHARS = 6000          # diff 回显上限，避免把上下文撑爆
_PENDING_CONFIRM: dict = {}    # 两段式确认：key(sha256) -> 预览时间


def _write_cfg() -> dict:
    """读取 manifest.json 的 config.write（缺失则用保守默认值）。"""
    try:
        manifest = _load_manifest()
        cfg = manifest.get("config", {}).get("write", {})
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _load_manifest() -> dict:
    with open(Path(__file__).parent / "manifest.json", "r", encoding="utf-8") as f:
        return json.load(f)


def _resolve_write_target(raw):
    """解析写入目标：成功返回 Path，失败返回错误文本(str)。"""
    raw = (raw or "").strip().strip('"').strip("'")
    if not raw:
        return " 请提供文件路径。"
    path = Path(os.path.expandvars(raw).replace("${PROJECT_DIR}", str(APP_DIR)))
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        path = path.resolve()
    except Exception as exc:
        return f" 路径无法解析（{raw}）：{exc}"

    if not is_allowed(path):
        listed = "\n".join(f"  - {w}" for w in WHITELIST) or "  （白名单为空）"
        return (f" 权限不足：`{path}` 不在允许访问的白名单内。\n"
                f"当前白名单：\n{listed}\n"
                f"如需放开，改 plugins/file_explorer/manifest.json 的 config.whitelist。")
    if path.exists() and path.is_dir():
        return f" 目标是目录，请给出具体文件路径：{path}"
    return path


def _is_protected(path: Path) -> bool:
    """是否命中「妹抖酱自身代码」（需要备份 + 测试校验 + 失败回滚）。"""
    patterns = _write_cfg().get("protected") or []
    try:
        rel = path.resolve().relative_to(APP_DIR).as_posix()
    except ValueError:
        return False
    for pattern in patterns:
        rule = str(pattern).strip().strip("/")
        if rule.endswith("/**"):
            base = rule[:-3]
            if rel == base or rel.startswith(base + "/"):
                return True
        elif fnmatch.fnmatch(rel, rule):
            return True
    return False


def _backup(path: Path):
    """备份文件到 .meido_backups/，返回备份路径；失败返回 None（不阻断写入）。"""
    cfg = _write_cfg()
    try:
        rel = path.resolve().relative_to(APP_DIR).as_posix()
    except ValueError:
        rel = path.name
    safe = rel.replace("/", "__")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target_dir = APP_DIR / str(cfg.get("backup_dir", ".meido_backups")) / safe
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{path.name}.{stamp}.bak"
        shutil.copy2(path, target)
        _prune_backups(target_dir, int(cfg.get("max_backups", 5)))
        return target
    except Exception as exc:
        print(f" [file_explorer] 备份失败（仍继续写入）：{exc}")
        return None


def _prune_backups(directory: Path, keep: int) -> None:
    files = sorted(directory.glob("*.bak"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in files[max(1, keep):]:
        try:
            stale.unlink()
        except OSError:
            pass


def _atomic_write(path: Path, content: str) -> None:
    """原子写：临时文件 + os.replace，避免写一半留下坏文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".meido-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _restore(path: Path, backup) -> bool:
    if not backup or not Path(backup).exists():
        return False
    try:
        shutil.copy2(backup, path)
        return True
    except Exception as exc:
        print(f" [file_explorer] 回滚失败：{exc}")
        return False


def _read_text(path: Path):
    """读取已存在文件；非文本或读取失败返回 None。"""
    if not path.exists():
        return ""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
        if b"\x00" in raw:
            return None
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    except Exception:
        return None


def _diff(old: str, new: str, path: Path) -> str:
    try:
        rel = path.resolve().relative_to(APP_DIR).as_posix()
    except ValueError:
        rel = path.name
    text = "".join(difflib.unified_diff(
        (old or "").splitlines(keepends=True),
        (new or "").splitlines(keepends=True),
        fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3))
    if not text.strip():
        return "（内容无变化）"
    if len(text) > MAX_DIFF_CHARS:
        text = text[:MAX_DIFF_CHARS] + f"\n… diff 过长已截断（>{MAX_DIFF_CHARS} 字符）"
    return text.rstrip()


def _verify():
    """跑验证命令（默认 pytest）。返回 (是否通过, 输出尾部)。"""
    cfg = _write_cfg()
    command = str(cfg.get("verify_command") or "").strip()
    if not command:
        return True, "（未配置 verify_command，跳过校验）"
    timeout = int(cfg.get("verify_timeout", 180))
    try:
        proc = subprocess.run(
            command, cwd=str(APP_DIR), shell=True,
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"校验超时（>{timeout}s）"
    except Exception as exc:
        return False, f"校验命令执行失败：{exc}"
    output = "\n".join(((proc.stdout or "") + (proc.stderr or "")).splitlines()[-15:])
    return proc.returncode == 0, output or f"（无输出，退出码 {proc.returncode}）"


def _audit(entry: dict) -> None:
    try:
        directory = app_path("data", "audit")
        directory.mkdir(parents=True, exist_ok=True)
        entry = {"time": datetime.now().isoformat(timespec="seconds"), **entry}
        with open(directory / "file_edits.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f" [file_explorer] 审计日志写入失败：{exc}")


def _pending_key(path: Path, new_content: str) -> str:
    digest = hashlib.sha256(f"{path.resolve()}|{new_content}".encode("utf-8"))
    return digest.hexdigest()[:16]


def _confirm_gate(path: Path, new_content: str, old_content: str, action: str, confirm: bool):
    """两段式确认：需要确认且未确认时返回预览文本；可继续则返回 None。"""
    cfg = _write_cfg()
    if not bool(cfg.get("require_confirm", False)):
        return None
    key = _pending_key(path, new_content)
    if confirm and key in _PENDING_CONFIRM:
        _PENDING_CONFIRM.pop(key, None)
        return None
    _PENDING_CONFIRM[key] = datetime.now().isoformat(timespec="seconds")
    return (
        f" 即将{action} `{path}`\n"
        "当前配置 require_confirm=true，需要二次确认才会落盘：\n"
        f"```diff\n{_diff(old_content, new_content, path)}\n```\n"
        "确认无误后，请**再次调用同一个工具并带上 confirm=true**。"
    )


def _size_guard(content: str):
    max_bytes = int(_write_cfg().get("max_bytes", 524288))
    size = len(content.encode("utf-8"))
    if size > max_bytes:
        return f" 拒绝写入：内容 {size} 字节，超过上限 {max_bytes} 字节（config.write.max_bytes）。"
    return None


def _commit(path: Path, new_content: str, old_content: str, action: str):
    """统一的落盘流程：备份 → 写入 →（自身代码）校验/回滚 → 审计 → 返回报告。"""
    protected = _is_protected(path)
    backup = _backup(path) if path.exists() else None

    try:
        _atomic_write(path, new_content)
    except Exception as exc:
        _audit({"op": action, "path": str(path), "ok": False, "error": str(exc)})
        return f" 写入失败：{exc}"

    lines = [f" 已{action} `{path}`（{len(new_content)} 字符）"]
    if backup:
        lines.append(f"备份：{backup}")

    verified = None
    rolled_back = False
    if protected:
        ok, output = _verify()
        verified = ok
        if ok:
            lines.append("命中自身代码 → 校验通过：\n" + output)
        else:
            rolled_back = _restore(path, backup)
            lines.append("命中自身代码 → 校验**未通过**，已"
                         + ("自动回滚到备份" if rolled_back else "尝试回滚但失败，请手动检查")
                         + "：\n" + output)

    lines.append("```diff\n" + _diff(old_content, new_content, path) + "\n```")
    _audit({
        "op": action, "path": str(path), "ok": not rolled_back,
        "protected": protected, "verified": verified,
        "rolled_back": rolled_back, "backup": str(backup) if backup else "",
        "bytes": len(new_content.encode("utf-8")),
    })
    return "\n".join(lines)


def write_file(path: str = "", content: str = "", confirm: bool = False, **_kwargs) -> str:
    """写入（新建或整体覆盖）一个文本文件，返回 diff 报告。"""
    cfg = _write_cfg()
    if not cfg.get("enabled", True):
        return " 文件写入已禁用（manifest.json → config.write.enabled=false）。"

    target = _resolve_write_target(path)
    if isinstance(target, str):
        return target
    if not is_text_file(target):
        return f" 拒绝写入：{target.name} 不是可编辑的文本类型（config 中未登记该扩展名）。"

    guard = _size_guard(content or "")
    if guard:
        return guard

    old = _read_text(target) if target.exists() else ""
    if old is None:
        return f" 拒绝写入：已存在的 `{target}` 不是 UTF-8 文本（可能是二进制）。"

    gate = _confirm_gate(target, content or "", old, "写入", confirm)
    if gate:
        return gate
    return _commit(target, content or "", old, "写入")


def edit_file(path: str = "", old: str = "", new: str = "",
              replace_all: bool = False, confirm: bool = False, **_kwargs) -> str:
    """精确片段替换：old 必须唯一（除非 replace_all=true）。"""
    cfg = _write_cfg()
    if not cfg.get("enabled", True):
        return " 文件写入已禁用（manifest.json → config.write.enabled=false）。"

    target = _resolve_write_target(path)
    if isinstance(target, str):
        return target
    if not target.exists():
        return f" 文件不存在：{target}（要新建文件请用 write_file）"
    if not is_text_file(target):
        return f" 拒绝修改：{target.name} 不是可编辑的文本类型。"

    current = _read_text(target)
    if current is None:
        return f" 拒绝修改：`{target}` 不是 UTF-8 文本（可能是二进制）。"

    if not old:
        return " old 为空：请把要替换的原文片段原样给出（含缩进与换行）。"

    count = current.count(old)
    if count == 0:
        return (" 没找到要替换的原文片段。请检查缩进/换行是否完全一致，"
                f"或先用 view 读取原文：{target}")
    if count > 1 and not replace_all:
        return (f" old 在文件中出现了 {count} 次，无法确定改哪一处。\n"
                "请把 old 写长一点以唯一确定位置，或传 replace_all=true 替换全部。")

    new_content = current.replace(old, new) if replace_all else current.replace(old, new, 1)
    guard = _size_guard(new_content)
    if guard:
        return guard

    gate = _confirm_gate(target, new_content, current, "修改", confirm)
    if gate:
        return gate
    return _commit(target, new_content, current, "修改")


def delete_file(path: str = "", confirm: bool = False, **_kwargs) -> str:
    """删除文件（默认禁用，需 config.write.allow_delete=true）。"""
    cfg = _write_cfg()
    if not cfg.get("allow_delete", False):
        return (" 删除功能当前禁用。如需开启，改 manifest.json → "
                "config.write.allow_delete=true（删除前仍会自动备份）。")

    target = _resolve_write_target(path)
    if isinstance(target, str):
        return target
    if not target.exists():
        return f" 文件不存在：{target}"

    old = _read_text(target) or ""
    gate = _confirm_gate(target, "", old, "删除", confirm)
    if gate:
        return gate

    backup = _backup(target)
    try:
        target.unlink()
    except Exception as exc:
        _audit({"op": "删除", "path": str(target), "ok": False, "error": str(exc)})
        return f" 删除失败：{exc}"
    _audit({"op": "删除", "path": str(target), "ok": True, "backup": str(backup) if backup else ""})
    return f" 已删除 `{target}`\n备份：{backup}"


# ═══════════════════════════════════════════════════════════
# 打开文件 / 目录（用系统默认程序）
#
# 安全模型（配置见 manifest.json → config.open）：
#   1. 白名单：与读/写共用同一份 config.whitelist（改一处即全生效，不重复定义）
#   2. 可执行类扩展名走「首次确认 + 信任记录」：
#        首次打开 → 先给出审查报告（大小 / 时间 / SHA256 / 数字签名），需 confirm=true；
#        再次打开 → 指纹（大小 + 纳秒时间）一致就直接开；
#                   指纹变了再比 SHA256，内容确实变了才算「大变动」，重新走确认。
#   3. 非可执行类（文档 / 图片 / 压缩包 / 目录）直接打开
#   4. 每次打开都记 data/audit/file_edits.jsonl
# ═══════════════════════════════════════════════════════════

# 需要「首次确认」的扩展名：能直接或间接拉起程序的类型。
# 桌面上的游戏快捷方式是 .url，也在此列（首次确认一次，之后免问）。
DEFAULT_CONFIRM_EXTENSIONS = (
    ".exe", ".bat", ".cmd", ".com", ".scr", ".pif", ".cpl",
    ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".hta",
    ".msi", ".msp", ".reg", ".jar",
    ".lnk", ".url",
)

_CONFIRM_HINT = ("\n\n若确认要打开，请再次调用本工具并带上 confirm=true。"
                 "确认后会写入信任记录，之后只要文件没变化就直接打开。")


def _open_cfg() -> dict:
    """读取 manifest.json 的 config.open（缺失则用保守默认值）。"""
    try:
        manifest = _load_manifest()
        cfg = manifest.get("config", {}).get("open", {})
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _confirm_extensions(cfg: dict) -> set:
    raw = cfg.get("confirm_extensions")
    items = raw if isinstance(raw, list) else DEFAULT_CONFIRM_EXTENSIONS
    return {str(e).lower() for e in items if str(e).strip()}


def _resolve_open_target(raw: str) -> Path | str:
    """解析打开目标：成功返回 Path，失败返回错误文本(str)。

    与 _resolve_write_target 的区别：**允许目录**（用户要能打开文件夹）。
    """
    raw = (raw or "").strip().strip('"').strip("'")
    if not raw:
        return " 请提供要打开的文件或目录路径。"
    path = Path(os.path.expandvars(raw).replace("${PROJECT_DIR}", str(APP_DIR)))
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        path = path.resolve()
    except Exception as exc:
        return f" 路径无法解析（{raw}）：{exc}"

    if not is_allowed(path):
        listed = "\n".join(f"  - {w}" for w in WHITELIST) or "  （白名单为空）"
        return (f" 权限不足：`{path}` 不在允许访问的白名单内。\n"
                f"当前白名单：\n{listed}\n"
                f"如需放开，改 plugins/file_explorer/manifest.json 的 config.whitelist。")
    if not path.exists():
        return f" 路径不存在：{path}"
    return path


def _which_whitelist(target: Path) -> str:
    """目标落在白名单的哪一条上（审查报告里给用户看）。"""
    for allowed in WHITELIST:
        try:
            target.relative_to(allowed)
            return str(allowed)
        except ValueError:
            continue
    return "未知"


# ── 信任记录 ──

def _trust_file_path() -> Path:
    cfg = _open_cfg()
    raw = str(cfg.get("trust_file") or "data/trusted_paths.json")
    path = Path(os.path.expandvars(raw))
    return path if path.is_absolute() else app_path(*path.parts)


def _load_trust() -> dict:
    path = _trust_file_path()
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f" [file_explorer] 信任记录读取失败，按空处理：{exc}")
    return {}


def _save_trust(data: dict) -> None:
    cfg = _open_cfg()
    limit = int(cfg.get("max_trust_records", 200) or 0)
    try:
        if limit > 0 and len(data) > limit:
            # 超限按最后打开时间淘汰，避免文件无限膨胀
            ordered = sorted(data.items(),
                             key=lambda kv: kv[1].get("last_opened_at", ""),
                             reverse=True)[:limit]
            data = dict(ordered)
        path = _trust_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f" [file_explorer] 信任记录写入失败：{exc}")


def _fingerprint(target: Path) -> dict:
    """快速指纹：大小 + 纳秒级修改时间（O(1)，不算哈希）。"""
    st = target.stat()
    return {"size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns)}


def _sha256_of(target: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(target, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _safe_sha256(target: Path) -> str:
    """算哈希；失败返回空串（不阻断流程，但报告里会缺这一项）。"""
    try:
        return _sha256_of(target)
    except Exception as exc:
        print(f" [file_explorer] 哈希计算失败：{exc}")
        return ""


def _signature_of(target: Path, timeout: int = 10) -> str:
    """读 Windows 数字签名。读不到就如实说明，不假装安全。"""
    try:
        script = ("$s = Get-AuthenticodeSignature -LiteralPath $env:MEIDO_SIG_PATH;"
                  "if ($s.Status -eq 'Valid') { 'VALID|' + $s.SignerCertificate.Subject }"
                  " else { $s.Status.ToString() }")
        env = dict(os.environ, MEIDO_SIG_PATH=str(target))
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env)
        out = (proc.stdout or "").strip()
        if not out:
            return "无法读取（powershell 无输出）"
        if out.startswith("VALID|"):
            return f"有效 · 签名者：{out.split('|', 1)[1].strip()[:80]}"
        return f"未签名或无效（{out}）"
    except subprocess.TimeoutExpired:
        return f"无法读取（超时 >{timeout}s）"
    except Exception as exc:
        return f"无法读取（{type(exc).__name__}）"


def _fmt_time(mtime_ns) -> str:
    try:
        return datetime.fromtimestamp(int(mtime_ns) / 1e9).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "未知"


def _review_report(target: Path, fp: dict, reason: str,
                   record: dict | None = None, digest: str = "") -> str:
    """打开前的来源审查报告。"""
    cfg = _open_cfg()
    size = fp["size"]
    lines = [
        "【打开前审查】",
        f"原因：{reason}",
        f"路径：{target}",
        f"类型：可执行类（{target.suffix.lower()}）",
        f"大小：{size} 字节（{size / 1024 / 1024:.2f} MB）",
        f"修改时间：{_fmt_time(fp['mtime_ns'])}",
    ]
    if digest:
        lines.append(f"SHA256：{digest[:32]}…")
    if record:
        lines.append(f"上次记录：{record.get('size')} 字节 / "
                     f"{_fmt_time(record.get('mtime_ns'))}")
        old_hash = record.get("sha256") or ""
        if old_hash:
            lines.append(f"上次 SHA256：{old_hash[:32]}…")
    if cfg.get("check_signature", True):
        lines.append("数字签名：" + _signature_of(
            target, int(cfg.get("signature_timeout", 10) or 10)))
    lines.append(f"位置：白名单内（{_which_whitelist(target)}）")
    return "\n".join(lines)


def _launch(target: Path, kind: str, trust_hit: bool, trust: dict | None = None,
            key: str = "", fp: dict | None = None, digest: str = "",
            note: str = "") -> str:
    """真正调用系统默认程序打开，并更新信任记录与审计日志。"""
    if not hasattr(os, "startfile"):
        return " 当前系统不支持 os.startfile，打开功能仅在 Windows 可用。"
    try:
        os.startfile(str(target))   # noqa: S606 (目标已过白名单校验，可执行类已确认)
    except Exception as exc:
        _audit({"action": "open", "path": str(target), "kind": kind,
                "result": "failed", "error": f"{type(exc).__name__}: {exc}"})
        return f" 打开失败：{type(exc).__name__}: {exc}"

    if trust is not None and key:
        now = datetime.now().isoformat(timespec="seconds")
        entry = trust.get(key) or {}
        entry.update(fp or {})
        if digest:
            entry["sha256"] = digest
        entry.setdefault("first_approved_at", now)
        entry["last_opened_at"] = now
        entry["open_count"] = int(entry.get("open_count") or 0) + 1
        trust[key] = entry
        _save_trust(trust)

    _audit({"action": "open", "path": str(target), "kind": kind,
            "trust_hit": trust_hit, "result": "ok"})
    prefix = "已打开（信任记录命中，未再次确认）" if trust_hit else "已打开"
    tail = f"\n{note}" if note else ""
    return f" {prefix}：`{target}`{tail}"


def _approve_and_open(target: Path, fp: dict, trust: dict, key: str,
                      note: str, digest: str = "") -> str:
    """用户已确认：用哈希建档，然后打开。digest 已算好则复用，避免重复读盘。"""
    if not digest:
        digest = _safe_sha256(target)
    return _launch(target, kind="可执行", trust_hit=False, trust=trust, key=key,
                   fp=fp, digest=digest, note=note)


def open_path(path: str = "", confirm: bool = False, **_kwargs) -> str:
    """用系统默认程序打开文件或目录。

    可执行类扩展名首次打开需确认（先给审查报告）；确认后写入信任记录，
    之后只要指纹没变就直接打开，指纹变了（大变动）才重新确认。
    """
    cfg = _open_cfg()
    if not cfg.get("enabled", True):
        return " 打开能力已在 manifest.json 的 config.open.enabled 中关闭。"

    target = _resolve_open_target(path)
    if isinstance(target, str):
        return target

    is_dir = target.is_dir()
    suffix = target.suffix.lower()

    # 目录与非可执行类：直接打开
    if is_dir or suffix not in _confirm_extensions(cfg):
        return _launch(target, kind="目录" if is_dir else "文件", trust_hit=False)

    # ── 可执行类：首次确认 + 信任记录 ──
    try:
        fp = _fingerprint(target)
    except Exception as exc:
        return f" 无法读取文件信息，不予打开：{exc}"

    trust = _load_trust()
    key = str(target)
    record = trust.get(key)

    if record is None:
        # 首次打开：算一次哈希放进报告，便于拿去 VirusTotal 之类核查
        digest = _safe_sha256(target)
        if not confirm:
            return (_review_report(target, fp, "首次打开，尚未建立信任记录",
                                   digest=digest) + _CONFIRM_HINT)
        return _approve_and_open(target, fp, trust, key,
                                 "首次确认通过，已记入信任记录", digest)

    # 快速路径：指纹一致，直接开（不算哈希）
    if record.get("size") == fp["size"] and record.get("mtime_ns") == fp["mtime_ns"]:
        return _launch(target, kind="可执行", trust_hit=True, trust=trust, key=key, fp=fp)

    # 指纹变了：用哈希区分「时间戳被动过」和「内容真的变了」
    digest = _safe_sha256(target)
    if not digest:
        return " 文件信息有变化，但无法计算哈希，为安全起见不予打开。"

    if digest == record.get("sha256"):
        return _launch(target, kind="可执行", trust_hit=True, trust=trust, key=key,
                       fp=fp, digest=digest,
                       note="时间戳有变化，但内容哈希一致，视为同一文件")

    if not confirm:
        return (_review_report(target, fp, "文件内容已变化（大变动），需要重新确认",
                               record=record, digest=digest) + _CONFIRM_HINT)
    return _approve_and_open(target, fp, trust, key,
                             "已按新内容重新确认，信任记录已更新", digest)


# ── 插件注册 ──
def register_commands():
    return {
        "/ls": list_directory,
        "/info": get_file_info,
        "/view": preview_text,
        "/write_file": write_file,
        "/edit_file": edit_file,
        "/delete_file": delete_file,
        "/open_path": open_path,
    }