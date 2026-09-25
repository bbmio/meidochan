"""
鲸鱼娘 对话历史管理器
负责会话持久化、导出、会话索引（index.json）、AI 会话摘要、会话上限与删除。

存储结构（每个工作空间的 history 目录下）：
- session_*.json        每轮对话的消息文件 [{"role","content","time"}, ...]
                        time 是**本地字段**：落盘时带上（界面显示与会话索引要用），
                        发给模型前由 core.brain 的 _api_messages 剥离。
- index.json            会话索引：标题 / AI 摘要 / 时间 / 条数（用于左侧列表和检索）
"""
import json
import random
import threading
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple

from core.paths import app_path

MAX_SESSIONS = 30          # 会话数量上限（达到后需手动删除旧会话）
SUMMARY_EVERY = 5          # 每累计 N 轮新消息生成/更新一次会话摘要

# 消息时间戳格式。写盘（core.history / core.engine）与界面解析
# （ui_qt.chat_view.format_time）共用这一份 —— 要改格式只改这里。
STAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def now_stamp() -> str:
    """当前时刻的时间戳字符串。"""
    return datetime.now().strftime(STAMP_FORMAT)


class HistoryManager:
    def __init__(self, history_dir: str | None = None):
        self.history_dir = Path(history_dir) if history_dir else app_path("data", "conversations")
        self.history_dir.mkdir(exist_ok=True)
        self._init_session()
        self._messages_cache = None
        self._index = None
        self._index_lock = threading.RLock()
        self._summary_fn = None  # AI 摘要生成器（engine 注入 brain.quick_chat）

    # ── 会话索引 index.json ──

    @property
    def index_file(self) -> Path:
        return self.history_dir / "index.json"

    @property
    def max_sessions(self) -> int:
        return MAX_SESSIONS

    def set_summary_generator(self, fn):
        """注入 AI 摘要生成器（签名 fn(messages:list, max_tokens:int) -> str）"""
        self._summary_fn = fn

    def _ensure_index(self) -> dict:
        """加载索引；不存在或与目录不一致时重建"""
        with self._index_lock:
            if self._index is not None:
                return self._index
            data = {"max_sessions": MAX_SESSIONS, "sessions": {}}
            if self.index_file.exists():
                try:
                    loaded = json.loads(self.index_file.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict) and "sessions" in loaded:
                        data = loaded
                except Exception:
                    pass
            self._index = data
            self._sync_index_from_disk()
            return self._index

    def _sync_index_from_disk(self):
        """扫描目录：补齐新会话、移除已删除的会话"""
        data = self._ensure_index()
        sessions = data["sessions"]
        existing_files = {f.name for f in self.history_dir.glob("session_*.json")}
        # 移除已不存在的
        changed = False
        for name in list(sessions.keys()):
            if name not in existing_files:
                del sessions[name]
                changed = True
        # 补齐新文件
        for f in sorted(self.history_dir.glob("session_*.json")):
            if f.name not in sessions:
                info = self._scan_session_file(f)
                if info:
                    sessions[f.name] = info
                    changed = True
        if changed:
            self._save_index()

    def _scan_session_file(self, f: Path) -> Optional[dict]:
        """从会话文件提取索引信息（标题/条数/时间）"""
        try:
            msgs = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return None
        title = ""
        created_at = ""
        updated_at = ""
        for m in msgs:
            t = m.get("time")
            if t:
                if not created_at:
                    created_at = t
                updated_at = t
            if not title and m.get("role") == "user" and isinstance(m.get("content"), str):
                t = m["content"].strip().replace("\n", " ")
                title = t[:30]
        return {
            "file": f.name,
            "title": title or f.stem.replace("session_", ""),
            "summary": "",
            "count": len(msgs),
            "created_at": created_at,
            "updated_at": updated_at,
        }

    def _save_index(self):
        with self._index_lock:
            if self._index is None:
                return
            self.index_file.write_text(
                json.dumps(self._index, ensure_ascii=False, indent=2), encoding="utf-8")

    def _update_index(self, file_name: str, **fields):
        data = self._ensure_index()
        with self._index_lock:
            sessions = data["sessions"]
            if file_name not in sessions:
                sessions[file_name] = {
                    "file": file_name, "title": "", "summary": "",
                    "count": 0, "created_at": "", "updated_at": "",
                }
            # 空标题不覆盖（保留时间戳标题，如新会话）
            if "title" in fields and not fields["title"]:
                fields.pop("title")
            sessions[file_name].update(fields)
            sessions[file_name]["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save_index()

    def _remove_from_index(self, file_name: str):
        data = self._ensure_index()
        with self._index_lock:
            data["sessions"].pop(file_name, None)
            self._save_index()

    # ── 会话文件管理 ──

    def _init_session(self):
        existing = sorted(self.history_dir.glob("session_*.json"), reverse=True)
        if existing:
            self.current_file = existing[0]
        else:
            self.current_file = self._new_session_file()
        self._repair_file(self.current_file)
        self._messages_cache = None

    @staticmethod
    def _clean_duplicates(messages: list) -> list:
        """
        清洗旧版本遗留的重复消息：
        相邻且 role 相同、content 相同的 user/assistant 消息合并为一条
        （旧代码 save_message + save_api_state 双写导致每条用户消息存了两遍）。
        """
        cleaned = []
        for m in messages:
            if (cleaned
                    and m.get("role") in ("user", "assistant")
                    and m.get("role") == cleaned[-1].get("role")
                    and m.get("content") == cleaned[-1].get("content")):
                # 优先保留带 time 的那条
                if m.get("time") and not cleaned[-1].get("time"):
                    cleaned[-1] = m
                continue
            cleaned.append(m)
        return cleaned

    def _repair_file(self, f: Path):
        """读取并清洗会话文件，有变化则写回（自愈历史脏数据）"""
        try:
            with open(f, "r", encoding="utf-8") as fd:
                messages = json.load(fd)
            cleaned = self._clean_duplicates(messages)
            if len(cleaned) != len(messages):
                with open(f, "w", encoding="utf-8") as fd:
                    json.dump(cleaned, fd, ensure_ascii=False, indent=2)
                self._messages_cache = None
        except Exception:
            pass

    def set_directory(self, new_dir: str):
        """切换工作空间时更新历史目录（保留旧会话，在新目录下开始）"""
        self.history_dir = Path(new_dir)
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self._index = None
        self._init_session()

    def _new_session_file(self) -> Path:
        # 毫秒时间戳 + 随机后缀，避免同一秒内连续新建会话文件名冲突覆盖
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S") + f"_{random.randint(100, 999)}"
        return self.history_dir / f"session_{stamp}.json"

    def _read_current(self) -> list:
        if self._messages_cache is not None:
            return self._messages_cache
        if self.current_file.exists():
            with open(self.current_file, "r", encoding="utf-8") as f:
                self._messages_cache = json.load(f)
        else:
            self._messages_cache = []
        return self._messages_cache

    @staticmethod
    def _stamp_missing_time(messages: list) -> list:
        """给还没有 time 的消息补上时间戳。

        这是兜底：正常路径由 engine 在消息**产生的时刻**就打好时间
        （那才是真实时刻，写盘时刻会晚一整轮）。这里只保证「谁漏了就补上」，
        免得历史文件里出现没有时间的消息。

        已有 time 的原样保留 —— 反复保存不会把旧时间刷成新时间。
        """
        if not any(isinstance(m, dict) and not m.get("time") for m in messages):
            return messages          # 快路径：一条都不缺
        now = now_stamp()
        return [
            {**m, "time": now} if isinstance(m, dict) and not m.get("time") else m
            for m in messages
        ]

    def _write_current(self, messages: list):
        messages = self._stamp_missing_time(messages)
        with open(self.current_file, "w", encoding="utf-8") as f:
            json.dump(messages, f, ensure_ascii=False, indent=2)
        self._messages_cache = messages
        # 同步索引：条数 / 标题 / 时间
        title = ""
        for m in messages:
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                t = m["content"].strip().replace("\n", " ")
                title = t[:30]
                break
        self._update_index(self.current_file.name, title=title, count=len(messages))

    def save_message(self, role: str, content: str):
        """追加单条消息并落盘。

        注意：**生产路径已不再调用它**（落盘统一走 save_api_state，见
        `_clean_duplicates` 里那段「双写导致重复」的说明）。保留是因为测试
        用它播种历史数据很方便，而且它写的结构是完整正确的（含 time）。
        """
        if not content or not content.strip():
            return
        msg = {
            "role": role,
            "content": content,
            "time": now_stamp()
        }
        messages = self._read_current()
        messages.append(msg)
        self._write_current(messages)

    def save_api_state(self, api_state: list):
        self._write_current(api_state)

    def load_api_state(self) -> list:
        return self._read_current()

    def get_messages(self) -> list:
        """返回当前会话的原始消息（供聊天界面重放）。"""
        return list(self._read_current())

    def load_recent(self, n: int = 20) -> list:
        messages = self._read_current()
        recent = messages[-(n * 2):]
        pairs = []
        i = 0
        while i < len(recent):
            user_msg = None
            bot_msg = None
            if i < len(recent) and recent[i]["role"] == "user":
                user_msg = recent[i]["content"]
                if i + 1 < len(recent) and recent[i + 1]["role"] == "assistant":
                    bot_msg = recent[i + 1]["content"]
                    i += 2
                else:
                    i += 1
                if user_msg:
                    pairs.append([user_msg, bot_msg])
            else:
                i += 1
        return pairs

    def count_messages(self) -> int:
        return len(self._read_current())

    def count_user_turns(self) -> int:
        """当前会话中的用户轮次（用于向量检索的 turn_index）"""
        return sum(1 for m in self._read_current() if m.get("role") == "user")

    def get_last_turn(self) -> Tuple[Optional[str], Optional[str]]:
        """返回当前会话最后一轮 (user_msg, assistant_msg)"""
        msgs = self._read_current()
        user_msg = None
        bot_msg = None
        for m in reversed(msgs):
            if bot_msg is None and m.get("role") == "assistant" and isinstance(m.get("content"), str):
                bot_msg = m["content"]
            elif m.get("role") == "user" and isinstance(m.get("content"), str):
                user_msg = m["content"]
                break
        return user_msg, bot_msg

    # ── 会话列表 / 切换 / 删除 ──

    def list_sessions(self) -> list:
        """返回按更新时间倒序的会话索引列表（含 title/summary）"""
        self._ensure_index()
        self._sync_index_from_disk()
        with self._index_lock:
            sessions = list(self._index["sessions"].values())
        sessions.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
        return sessions

    def get_session_summary(self, file_name: str) -> str:
        data = self._ensure_index()
        with self._index_lock:
            s = data["sessions"].get(file_name, {})
            return s.get("summary", "") or ""

    def switch_to_session(self, file_path: str) -> list:
        """切换到指定会话文件，返回其消息（Gradio 6 格式）。顺带清洗历史脏数据。"""
        target = Path(file_path)
        if not target.is_absolute():
            target = self.history_dir / target
        if not target.exists():
            return []
        try:
            with open(target, "r", encoding="utf-8") as f:
                messages = json.load(f)
        except Exception:
            return []
        cleaned = self._clean_duplicates(messages)
        if len(cleaned) != len(messages):
            try:
                with open(target, "w", encoding="utf-8") as f:
                    json.dump(cleaned, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
        self.current_file = target
        self._messages_cache = cleaned
        return self.to_gradio_format(cleaned)

    def delete_session(self, file_path: str) -> str:
        """删除会话文件并更新索引。删除当前会话时自动切到最新剩余会话。"""
        target = Path(file_path)
        if not target.is_absolute():
            target = self.history_dir / target
        if not target.exists():
            return "会话不存在，可能已被删除"
        file_name = target.name
        target.unlink()
        self._remove_from_index(file_name)
        if self.current_file == target:
            existing = sorted(self.history_dir.glob("session_*.json"), reverse=True)
            if existing:
                self.current_file = existing[0]
            else:
                self.current_file = self._new_session_file()
            self._messages_cache = None
        return f"已删除会话 {file_name.replace('session_', '').replace('.json', '')}"

    def new_session(self) -> str:
        """新建会话；达到上限时拒绝并提示删除旧会话"""
        self.list_sessions()
        with self._index_lock:
            total = len(self._index["sessions"])
        if total >= MAX_SESSIONS:
            return (f" 已达会话数量上限（{MAX_SESSIONS} 个），无法开启更多对话。"
                    f"请删除不需要的旧对话后重试。")
        old_count = self.count_messages()
        self.current_file = self._new_session_file()
        self._messages_cache = None
        if not self.current_file.exists():
            self._write_current([])  # 立即创建空文件，出现在会话列表
        return f" 新会话已开始（旧会话已保存，共 {old_count} 条消息）"

    # ── AI 会话摘要（后台线程） ──

    def schedule_session_summary(self):
        """
        当前会话每累计 SUMMARY_EVERY 轮新消息后，
        在后台线程中用 AI 生成/更新该会话摘要（写入 index.json）。
        """
        file_name = self.current_file.name
        messages = self._read_current()
        turns = self.count_user_turns()
        if turns < SUMMARY_EVERY or turns % SUMMARY_EVERY != 0:
            return
        if self._summary_fn is None:
            return
        thread = threading.Thread(
            target=self._run_summary,
            args=(file_name, messages),
            daemon=True,
        )
        thread.start()

    def _run_summary(self, file_name: str, messages: list):
        try:
            pairs = [m for m in messages if m.get("role") in ("user", "assistant")]
            if not pairs:
                return
            system_content = (
                "你是一个对话记录总结助手。请将以下对话浓缩成一段简洁的会话摘要（150字以内），"
                "保留关键主题、已解决的问题、用户的偏好和重要结论。"
                "用中文，只输出摘要正文，不要任何前缀。"
            )
            prompt_messages = [
                {"role": "system", "content": system_content},
                {"role": "user", "content": "对话记录：\n" + json.dumps(pairs[-40:], ensure_ascii=False, indent=2)},
            ]
            summary = self._summary_fn(prompt_messages, max_tokens=300, temperature=0)
            if summary and summary.strip():
                self._update_index(file_name, summary=summary.strip()[:500])
                print(f" 会话摘要已更新 [{file_name}] ({len(summary)} 字)")
        except Exception as e:
            print(f" 会话摘要生成异常: {e}")

    # ── 导出 / 格式化 ──

    def to_gradio_format(self, messages: list) -> list:
        """
        将历史消息 ({"role","content"}) 转为 Gradio 6 Chatbot 格式。
        Gradio 6 要求 content 为数组: [{"text": ..., "type": "text"}]
        """
        result = []
        for m in messages:
            role = m.get("role", "")
            if role not in ("user", "assistant"):
                continue
            content = m.get("content", "")
            if not isinstance(content, str):
                content = ""
            result.append({
                "role": role,
                "content": [{"text": content, "type": "text"}],
            })
        return result

    def export(self, fmt: str = "markdown") -> str:
        messages = self._read_current()
        if not messages:
            return " 当前会话为空，无需导出。"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if fmt == "json":
            content = json.dumps(messages, ensure_ascii=False, indent=2)
            filename = f"export_{stamp}.json"
        elif fmt == "txt":
            lines = []
            for m in messages:
                if m.get("role") not in ("user", "assistant"):
                    continue
                prefix = "用户" if m["role"] == "user" else "鲸鱼娘"
                lines.append(f"[{m.get('time', '')}] {prefix}:\n{m['content']}\n")
            content = "\n".join(lines)
            filename = f"export_{stamp}.txt"
        else:
            lines = ["# 鲸鱼娘对话记录\n"]
            for m in messages:
                if m.get("role") not in ("user", "assistant"):
                    continue
                if m["role"] == "user":
                    lines.append(f"## 用户（{m.get('time', '')}）\n\n{m['content']}\n")
                else:
                    lines.append(f"## 鲸鱼娘（{m.get('time', '')}）\n\n{m['content']}\n")
            content = "\n".join(lines)
            filename = f"export_{stamp}.md"
        export_path = self.history_dir / filename
        with open(export_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f" 已导出到：{export_path}"
