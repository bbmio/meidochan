"""工具结果落盘（书里的「工具结果预算控制」）。

## 为什么这不是「限制信息」

单次读文件可以吐 100,000 字符（`plugins/file_explorer/main.py` 的
`MAX_FULL_CONTENT`），一次就吃掉大半个上下文预算。但**直接截断会丢信息** ——
模型后面要用到某个细节时，原文已经没了。

所以这里做的不是「删掉」，而是「**换位置**」：原文**逐字节落盘**，上下文里只放
路径 + 行数 + 首尾预览。信息从「在上下文里」变成「可检索」，而不是被销毁。

这也正是它能和「工具结果跨轮保留」共存的原因：不落盘就没法保留（一保留就撑爆），
落了盘才敢把工具结果一直留着。

## 为什么是「首尾预览」而不是「前 N 行」

报错往往在**末尾**（异常栈、退出码、总结行），开头则是「这是什么」。
两头各给一段、中间省略，比只给开头有用得多。

## 替换串必须冻结

书里的原话是「替换决策一旦做出就被冻结」。这里靠「写一次 + 结果直接存进消息里」
实现：调用方拿到什么就持久化什么，**不会二次计算** ——
否则会话恢复后消息序列与缓存里的字节流对不上，prompt cache 全废。
"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Optional

from core.logging_utils import log
from core.paths import app_path

#: 超过这个字符数才落盘。小结果（目录列表、搜索结果）原样保留 ——
#: 几千字符的来回搬运不值得，而且模型读回一次也要花 token。
MAX_INLINE_CHARS = 6_000
#: 预览的首 / 尾各取多少字符（合计约 2.3K 字符 ≈ 1K token）
PREVIEW_HEAD_CHARS = 1_500
PREVIEW_TAIL_CHARS = 800
#: 落盘文件的保留天数
RETENTION_DAYS = 7

_pruned = False


def outputs_dir() -> Path:
    return app_path("data", "tool_outputs")


def _safe_name(tool_call_id: str) -> str:
    """把 tool_call_id 变成安全的文件名。

    id 由服务端生成（形如 `call_abc123`），但**不能**直接当路径用 ——
    万一它带上 `/`、`..` 之类，就是一个目录穿越。只留保守字符集。
    """
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", str(tool_call_id or ""))[:80]
    return cleaned or "unknown"


def path_for(tool_call_id: str, text: str = "") -> Path:
    """该 tool_call_id 对应的落盘路径。

    文件名带上**内容哈希**。id 本身在一次对话里是唯一的，但文件要留 7 天、跨会话 ——
    万一 id 被复用，没有哈希就会把旧文件覆盖掉，而**旧消息里记着的那个路径**
    还在引用它，模型读回来的就是张冠李戴的内容。带上哈希，这种覆盖就不可能发生。

    `text` 省略时只做名字清洗（供调用方检查路径安全性用）。
    """
    base = _safe_name(tool_call_id)
    if not text:
        return outputs_dir() / f"{base}.txt"
    digest = hashlib.sha1(str(text).encode("utf-8")).hexdigest()[:8]
    return outputs_dir() / f"{base}-{digest}.txt"


def _render_preview(text: str, path: Path) -> str:
    lines = text.count("\n") + 1
    head = text[:PREVIEW_HEAD_CHARS]
    tail = text[-PREVIEW_TAIL_CHARS:]
    omitted = len(text) - len(head) - len(tail)
    return (
        f"[内容过大，已落盘]\n"
        f"共 {lines:,} 行 / {len(text):,} 字符。完整内容：\n"
        f"{path}\n"
        f'需要细节时用 view 按行范围读取（如 lines="100-200"），不要凭预览猜。\n'
        f"\n--- 开头 ---\n{head}\n"
        f"\n--- 中间省略 {omitted:,} 字符 ---\n"
        f"\n--- 结尾 ---\n{tail}"
    )


def page_out(tool_call_id: str, text) -> str:
    """超长就落盘并返回预览；不超长原样返回。

    ⚠️ 落盘失败时**原样返回**：宁可多占一点上下文，也不能把内容弄丢。
    """
    text = "" if text is None else str(text)
    if len(text) <= MAX_INLINE_CHARS:
        return text

    path = path_for(tool_call_id, text)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        log.warning("工具结果落盘失败（%s），本次不裁剪：%s", path, exc)
        return text

    _prune_once()
    log.info("[工具结果] %d 字符已落盘 → %s", len(text), path)
    return _render_preview(text, path)


def prune(max_age_days: int = RETENTION_DAYS) -> int:
    """删掉过期的落盘文件，返回删除数量。

    这些是**纯派生数据**：过期了删掉只会让模型需要时重新调一次工具，
    不像用户手写的配置那样删了就没了。
    """
    directory = outputs_dir()
    if not directory.exists():
        return 0
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for entry in directory.iterdir():
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            continue
    if removed:
        log.info("[工具结果] 清理过期落盘文件 %d 个（保留 %d 天）", removed, max_age_days)
    return removed


def _prune_once() -> None:
    """每个进程只清理一次 —— 别每次落盘都遍历一遍目录。"""
    global _pruned
    if _pruned:
        return
    _pruned = True
    try:
        prune()
    except Exception as exc:      # 清理失败绝不能影响主流程
        log.warning("工具结果清理失败（不影响本次对话）：%s", exc)


def _reset_prune_flag() -> None:
    """仅供测试：让下一次 page_out 重新走一遍清理。"""
    global _pruned
    _pruned = False
