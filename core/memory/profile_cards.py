"""
鲸鱼娘 概览卡（Profile Cards）—— 双层记忆的「概览层」

与「细节层」（history_retrieval 向量检索）构成双层记忆：
- 概览层：少量关键事实（称呼/名字、风格、偏好）结构化常驻上下文，提供随时可见的概览
- 细节层：原始对话按需检索，放消息末尾（KV cache 友好）

特性：
- 结构化 JSON 卡片（区别于 memory.py 的整段合并摘要 / Enhanced Notes 档）
- 字段级合并：同名键更新、新键追加、嵌套 dict 递归合并（不会像整段摘要那样重写丢失）
- 定期抽取（quick_chat 生成 JSON 卡片）+ 会话结束才异步写盘（慢变数据）
"""
import json
import os
import threading
from datetime import datetime
from typing import Any, Dict, Optional

_PROFILE_FILE = ""          # 按工作空间隔离的卡片文件路径（engine 注入）
_EXTRACT_FN = None          # 抽取生成器（engine 注入 brain.quick_chat）

_lock = threading.Lock()
_cards: Dict[str, Any] = {}  # 内存卡片（字段级合并后的最新状态）
_dirty = False               # 是否有未写盘更新
_pending = 0                 # 距离上次抽取的新消息计数
_extracting = False          # 是否正在后台抽取
_saving = False              # 是否正在写盘

THRESHOLD = 4                # 每累计 N 轮新消息抽取一次（内存合并）


def set_extract_generator(fn):
    """注入概览卡抽取生成器（brain.quick_chat）。"""
    global _EXTRACT_FN
    _EXTRACT_FN = fn


def set_profile_file(path: str):
    """切换工作空间时更新卡片文件路径并加载对应卡片。"""
    global _PROFILE_FILE, _cards, _dirty, _pending
    with _lock:
        _PROFILE_FILE = path
        _cards = _load_from_disk(path)
        _dirty = False
        _pending = 0


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


def get_profile_prompt() -> str:
    """生成可注入 system prompt 的概览卡文本；无卡片返回空串。"""
    if not _cards:
        return ""
    lines = []
    c = _cards.get("称呼")
    if isinstance(c, dict):
        parts = []
        if c.get("名字"):
            parts.append(f"称呼：{c['名字']}")
        if c.get("风格"):
            parts.append(f"风格：{c['风格']}")
        if parts:
            lines.append("；".join(parts))
    p = _cards.get("偏好")
    if isinstance(p, dict):
        pref = [f"{k}＝{v}" for k, v in p.items() if v]
        if pref:
            lines.append("偏好：" + "；".join(pref))
    return "\n".join(lines)


def get_cards() -> dict:
    """返回当前概览卡（供 /memory 命令查看）。"""
    with _lock:
        return json.loads(json.dumps(_cards))


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


def schedule_extraction(recent_msgs: list):
    """收到新消息后调用：达到阈值则后台抽取 + 字段级合并到内存（不写盘）。"""
    global _pending, _extracting
    with _lock:
        _pending += 1
        if _pending < THRESHOLD or _extracting:
            return
        _pending = 0
        _extracting = True
    threading.Thread(target=_run_extraction, args=(recent_msgs,), daemon=True).start()


def _run_extraction(recent_msgs: list):
    global _extracting, _dirty
    try:
        new = _extract_cards(recent_msgs)
        if new:
            with _lock:
                _deep_merge(_cards, new)
                _dirty = True
            print(" 概览卡已字段级合并")
    except Exception as e:
        print(f" 概览卡抽取异常: {e}")
    finally:
        with _lock:
            _extracting = False


def finalize_session(recent_msgs: list):
    """会话结束：用最近对话强制抽取一次（同步，不依赖阈值）+ 字段级合并 + 异步写盘。
    确保名字/偏好等关键信息在开新会话前被固化。"""
    global _cards, _dirty
    try:
        new = _extract_cards(recent_msgs)
        if new:
            with _lock:
                _deep_merge(_cards, new)
                _dirty = True
            print(" 概览卡：会话结束强制抽取并合并")
    except Exception as e:
        print(f" 概览卡会话结束抽取异常: {e}")
    flush_to_disk()


def flush_to_disk():
    """会话结束调用：有未写盘更新则异步写盘（慢变数据，不频繁写）。"""
    global _saving
    with _lock:
        if not _dirty or _saving or not _PROFILE_FILE:
            return
        _saving = True
        snapshot = json.loads(json.dumps(_cards))  # 深拷贝，避免写盘期间被并发修改
    threading.Thread(target=_save_worker, args=(snapshot,), daemon=True).start()


def _save_worker(snapshot: dict):
    global _dirty, _saving
    try:
        parent = os.path.dirname(_PROFILE_FILE)
        if parent:
            os.makedirs(parent, exist_ok=True)
        data = {"cards": snapshot, "last_updated": datetime.now().isoformat()}
        with open(_PROFILE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        with _lock:
            _dirty = False
        print(" 概览卡已写入磁盘")
    except Exception as e:
        print(f" 概览卡写盘失败: {e}")
    finally:
        with _lock:
            _saving = False


def clear_cards():
    """清空概览卡（/memory clear）。"""
    global _cards, _dirty
    with _lock:
        _cards = {}
        _dirty = True
