"""鲸鱼娘 概览卡（Profile Cards）—— 双层记忆的「概览层」

与「细节层」（history_retrieval 向量检索）构成双层记忆：
- 概览层：少量关键事实（称呼/名字、风格/偏好）结构化常驻上下文
- 细节层：原始对话按需检索，放消息末尾（KV cache 友好）

并发模型（2026-09-28 修复跨工作空间竞争，见设计规格）：
- 异步抽取绑定「工作空间代次」_epoch：切换空间后，旧任务的结果会被丢弃，
  永远不会合并进新空间的卡片；
- 保存是同步 + 原子替换（临时文件 → os.replace）：函数返回即已落盘，
  退出 / 切换不再依赖 daemon 线程；
- _revision 识别「保存期间产生的新修改」，旧快照写完不会误清 _dirty。
"""
import json
import os
import threading
from datetime import datetime
from typing import Any, Dict, Optional

_PROFILE_FILE = ""          # 按工作空间隔离的卡片文件路径（engine 注入）
_EXTRACT_FN = None          # 抽取生成器（engine 注入 brain.quick_chat）

_lock = threading.Lock()
_epoch = 0                   # 工作空间代次：每次 set_profile_file +1
_cards: Dict[str, Any] = {}  # 内存卡片（字段级合并后的最新状态）
_dirty = False               # 是否有未写盘更新
_revision = 0                # 修改版本号：每次标脏 +1
_pending = 0                 # 距离上次抽取的新消息计数
_extracting_for: Optional[int] = None   # 正在抽取的代次（None=空闲）

THRESHOLD = 4                # 每累计 N 轮新消息抽取一次（内存合并）


def set_extract_generator(fn):
    """注入概览卡抽取生成器（brain.quick_chat）。"""
    global _EXTRACT_FN
    _EXTRACT_FN = fn


def set_profile_file(path: str):
    """切换工作空间时更新卡片文件路径并加载对应卡片。

    递增代次：仍在后台运行的旧空间抽取任务，完成时因代次不符会被丢弃，
    不会把结果合并进新空间的卡片。
    """
    global _PROFILE_FILE, _cards, _dirty, _pending, _revision, _epoch, _extracting_for
    with _lock:
        _epoch += 1
        _PROFILE_FILE = path
        _cards = _load_from_disk(path)
        _dirty = False
        _pending = 0
        _revision = 0
        _extracting_for = None


def _load_from_disk(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("cards"), dict):
            return data["cards"]
    except Exception as e:
        print(f" 加载概览卡失败: {e}")
    return {}


def _snapshot_locked() -> dict:
    """锁内取卡片快照（JSON 往返深拷贝即可 —— 卡片很小）。"""
    return json.loads(json.dumps(_cards))


def get_profile_prompt() -> str:
    """生成可注入 system prompt 的概览卡文本；无卡片返回空串。"""
    with _lock:
        cards = _snapshot_locked()
    if not cards:
        return ""
    lines = []
    c = cards.get("称呼")
    if isinstance(c, dict):
        parts = []
        if c.get("名字"):
            parts.append(f"称呼：{c['名字']}")
        if c.get("风格"):
            parts.append(f"风格：{c['风格']}")
        if parts:
            lines.append("；".join(parts))
    p = cards.get("偏好")
    if isinstance(p, dict):
        pref = [f"{k}＝{v}" for k, v in p.items() if v]
        if pref:
            lines.append("偏好：" + "；".join(pref))
    return "\n".join(lines)


def get_cards() -> dict:
    """返回当前概览卡（供 /memory 命令查看）。"""
    with _lock:
        return _snapshot_locked()


def _deep_merge(base: dict, update: dict) -> dict:
    """字段级合并：同名键更新、新键追加、嵌套 dict 递归合并。"""
    for k, v in update.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _extract_cards(recent_msgs: list) -> Optional[dict]:
    """调用 quick_chat 从对话中抽取结构化 JSON 卡片。"""
    if not recent_msgs or _EXTRACT_FN is None:
        return None
    system_content = (
        "你是记忆卡片抽取助手。从对话中抽取关于用户的长期稳定信息，"
        "输出严格的 JSON 对象（只包含有依据的字段）：\n"
        '- "称呼": {"名字": "用户希望被如何称呼", "风格": "用户偏好的交流风格"}\n'
        '- "偏好": {"偏好项": "具体值", ...}\n'
        "只输出 JSON，不要任何解释或代码块标记。"
    )
    prompt_messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": "对话记录：\n" + json.dumps(recent_msgs[-40:], ensure_ascii=False, indent=2)},
    ]
    try:
        text = _EXTRACT_FN(prompt_messages, max_tokens=300, temperature=0)
        if not text:
            return None
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`").strip()
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].strip()
        data = json.loads(cleaned)
        return data if isinstance(data, dict) else None
    except Exception as e:
        print(f" 概览卡抽取失败: {e}")
        return None


def _mark_dirty_locked() -> None:
    """锁内调用：标记有未落盘修改（版本号用于识别保存期间的新修改）。"""
    global _dirty, _revision
    _dirty = True
    _revision += 1


def schedule_extraction(recent_msgs: list):
    """收到新消息后调用：达到阈值则后台抽取 + 字段级合并到内存（不写盘）。

    任务绑定启动时的代次与消息快照：切换工作空间后该结果会被丢弃。
    """
    global _pending, _extracting_for
    with _lock:
        _pending += 1
        if _pending < THRESHOLD or _extracting_for is not None:
            return
        _pending = 0
        epoch = _epoch
        _extracting_for = epoch
        snapshot = list(recent_msgs)
    threading.Thread(target=_run_extraction, args=(snapshot, epoch),
                     daemon=True).start()


def _run_extraction(recent_msgs: list, epoch: int):
    global _extracting_for
    try:
        new = _extract_cards(recent_msgs)
        if new:
            with _lock:
                if epoch == _epoch:
                    _deep_merge(_cards, new)
                    _mark_dirty_locked()
                    print(" 概览卡已字段级合并")
                else:
                    print(" [概览卡] 抽取完成时工作空间已切换，结果已丢弃")
    except Exception as e:
        print(f" 概览卡抽取异常: {e}")
    finally:
        with _lock:
            # 只释放「自己那一代」的占用；不得清掉新空间已启动的抽取状态
            if _extracting_for == epoch:
                _extracting_for = None


def finalize_session(recent_msgs: list) -> bool:
    """会话结束前：强制抽取一次（同步）+ 合并 + 同步写盘。

    返回 False 表示写盘失败（dirty 保留），调用方应放弃「开新会话」等
    破坏性动作，避免丢掉尚未落盘的记忆。
    """
    with _lock:
        epoch = _epoch
    try:
        new = _extract_cards(recent_msgs)
        if new:
            with _lock:
                if epoch == _epoch:
                    _deep_merge(_cards, new)
                    _mark_dirty_locked()
                    print(" 概览卡：会话结束强制抽取并合并")
                else:
                    print(" [概览卡] 会话结束抽取完成时工作空间已切换，结果已丢弃")
    except Exception as e:
        print(f" 概览卡会话结束抽取异常: {e}")
    return flush_to_disk()


def flush_to_disk() -> bool:
    """把未落盘的卡片同步写入磁盘（临时文件 + os.replace 原子替换）。

    返回 True 表示「磁盘上已是最新」（含本来就没有未落盘修改的情况）；
    False 表示写盘失败：_dirty 保留、可重试，调用方应放弃破坏性动作。
    """
    global _dirty
    with _lock:
        if not _dirty or not _PROFILE_FILE:
            return True
        path = _PROFILE_FILE
        snapshot = _snapshot_locked()
        revision = _revision
    tmp = f"{path}.tmp"
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        data = {"cards": snapshot, "last_updated": datetime.now().isoformat()}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        print(f" 概览卡写盘失败: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False
    with _lock:
        # 写入期间又产生了新修改（revision 变过）：不要误清新修改的 dirty
        if _revision == revision:
            _dirty = False
    print(" 概览卡已写入磁盘")
    return True


def clear_cards():
    """清空概览卡（/memory clear）。"""
    global _cards
    with _lock:
        _cards = {}
        _mark_dirty_locked()
