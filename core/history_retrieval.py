"""
鲸鱼娘 对话历史向量检索
将每轮对话（用户+鲸鱼娘）编码为向量存入 ChromaDB，用户提问时检索相关历史片段，
只注入命中的片段而非全量历史，让鲸鱼娘能"想起"很久以前的对话。

- 每个工作空间独立一个向量库（位于该空间 history/vector_db）
- 片段 id = "{会话文件名}::{turn_index}"，upsert 幂等
- embedding 统一走 ollama 的 bge-m3（模型由 ollama 管一份，多入口共享，本进程不再加载 sentence-transformers）
- 写入/删除在向量库就绪前会暂存，就绪后自动补齐（不阻塞 UI）
"""
import json
import re
import threading
import requests
from pathlib import Path
from datetime import datetime


class OllamaEmbedder:
    """封装 ollama embedding API，提供与 SentenceTransformer 兼容的 encode() 接口。

    模型由 ollama 服务端管理（一份，多入口共享），本进程不再加载 sentence-transformers。
    """

    def __init__(self, model: str = "bge-m3", base_url: str = "http://127.0.0.1:11434"):
        self.model = model
        self.base_url = base_url

    def encode(self, texts, normalize_embeddings=False, **kwargs):
        """返回 shape (len(texts), dim) 的 numpy 数组（与 SentenceTransformer.encode 兼容）。"""
        import numpy as np
        if isinstance(texts, str):
            texts = [texts]
        resp = requests.post(
            f"{self.base_url}/api/embed",
            json={"model": self.model, "input": texts},
            timeout=60,
        )
        resp.raise_for_status()
        arr = np.array(resp.json()["embeddings"], dtype=np.float32)
        if normalize_embeddings and arr.size:
            norms = np.linalg.norm(arr, axis=1, keepdims=True)
            norms[norms == 0] = 1
            arr = arr / norms
        return arr


# ═══════════════════════════════════════════════════════════
# embedding 统一走 ollama（一份模型，多入口共享，进程内不再加载 sentence-transformers）
# ═══════════════════════════════════════════════════════════

EMBEDDING_MODEL_NAME = "bge-m3"
OLLAMA_BASE_URL = "http://127.0.0.1:11434"

_EMBED_MODEL = None               # OllamaEmbedder 单例
_EMBED_LOCK = threading.Lock()


def warmup_embedding_model(model_name: str = EMBEDDING_MODEL_NAME):
    """启动时后台检查 ollama 是否已拉取 embedding 模型（不阻塞，失败仅提示）。幂等。"""
    global _EMBED_MODEL
    with _EMBED_LOCK:
        if _EMBED_MODEL is not None:
            return

    def _check():
        try:
            resp = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
            names = {m.get("name", "") for m in resp.json().get("models", [])}
            # ollama 返回的模型名带 tag（如 bge-m3:latest），必须按"基础名"匹配；
            # 原来的写法两次都在拿裸名对比带 tag 的名字 → 恒报"未找到 bge-m3"（实测误报）
            base = model_name.split(":")[0]
            if any(n == model_name or n.split(":")[0] == base for n in names):
                print(f"  [向量检索] ollama embedding 模型已就绪：{model_name}")
            else:
                print(f"  [向量检索] 警告：ollama 未找到 {model_name}，请先执行 `ollama pull {model_name}`")
        except Exception as e:
            print(f"  [向量检索] 警告：无法连接 ollama（{e}），历史检索将不可用")

    threading.Thread(target=_check, daemon=True).start()


def get_embedding_model():
    """返回共享的 OllamaEmbedder 单例（模型由 ollama 服务端管理，无需本进程加载）。"""
    global _EMBED_MODEL
    if _EMBED_MODEL is None:
        with _EMBED_LOCK:
            if _EMBED_MODEL is None:
                _EMBED_MODEL = OllamaEmbedder(EMBEDDING_MODEL_NAME, OLLAMA_BASE_URL)
    return _EMBED_MODEL


class HistoryVectorStore:
    # 当前集合名：嵌入模型已统一为 bge-m3（1024 维）。
    # 旧集合 history_chunks 是 768 维模型建的，维度不兼容 → 换用新集合名。
    # 旧集合原样保留在磁盘上（代码不删除任何用户数据），确认新集合正常后可自行清理。
    COLLECTION_NAME = "history_chunks_v2"
    LEGACY_COLLECTION_NAME = "history_chunks"

    def __init__(self, history_dir: str):
        self.history_dir = Path(history_dir)
        self._model = None
        self._client = None
        self._collection = None
        self._enabled = None  # None=加载中 True=可用 False=加载失败
        self._lock = threading.Lock()
        self._pending_upserts = []  # 加载完成前暂存
        self._pending_deletes = []
        threading.Thread(target=self._load_worker, daemon=True).start()

    def close(self):
        """释放当前工作空间的向量库句柄。

        必须在"切换工作空间 / 删除工作空间"前调用：
        - `_load_worker` 后台线程持有本对象引用，对象不会被 GC；
        - chromadb 内部还有"按 Settings 缓存 System 实例"的全局注册表，
          只丢引用并不会关闭底层 sqlite 连接。
        结果是 `vector_db/chroma.sqlite3` 的句柄一直被进程占用，
        Windows 下会导致"删除工作空间"永远失败（文件被占用）。
        """
        import gc

        with self._lock:
            self._pending_upserts, self._pending_deletes = [], []
            self._collection = None
            self._client = None
            self._model = None
            self._enabled = False
        try:
            # 清掉 chroma 的 System 缓存，让底层 sqlite 连接随引用消失而关闭
            from chromadb.api.client import SharedSystemClient
            SharedSystemClient.clear_system_cache()
        except Exception:
            pass  # 不同 chromadb 版本 API 不同：清不掉也不能影响主流程
        gc.collect()

    # ── 后台加载（不阻塞 UI） ──

    def _load_worker(self):
        try:
            import chromadb

            # 复用全局单例 embedding 模型（启动时已预加载，或此处等待其加载完成）
            self._model = get_embedding_model()
            if self._model is None:
                raise RuntimeError("embedding 模型加载失败")
            db_path = self.history_dir / "vector_db"
            db_path.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(db_path))
            self._collection = self._client.get_or_create_collection(
                name=self.COLLECTION_NAME,
                metadata={"hnsw:space": "cosine"},
            )
            self._warn_legacy_collection()
            with self._lock:
                self._enabled = True
            print(f"  [向量检索] 已就绪（{self.history_dir.name}），当前片段数：{self._collection.count()}")
            self._flush_pending()
            self._auto_rebuild_if_empty()
        except Exception as e:
            with self._lock:
                self._enabled = False
            print(f"  [向量检索] 不可用（不影响对话）: {e}")

    def _warn_legacy_collection(self) -> None:
        """只提示、不删除：旧集合维度与当前嵌入模型不兼容，保留原样供用户自行处置。"""
        try:
            legacy = self._client.get_collection(self.LEGACY_COLLECTION_NAME)
            n = legacy.count()
            if n > 0:
                print(
                    f"  [向量检索] 检测到旧集合 {self.LEGACY_COLLECTION_NAME}"
                    f"（{n} 条，旧嵌入模型 768 维，与 bge-m3 1024 维不兼容）→ "
                    f"已改用 {self.COLLECTION_NAME}，旧集合原样保留未删除"
                )
        except Exception:
            pass  # 旧集合不存在属正常情况

    def _auto_rebuild_if_empty(self) -> None:
        """新集合为空但磁盘上已有历史会话时，自动从会话文件回灌一次（幂等）。

        背景：旧集合是 768 维嵌入模型建的，与 bge-m3（1024 维）不兼容，
        换用新集合名后集合是空的；而历史正文都完整保存在 session_*.json 里，
        可无损重建，因此这里自动回灌一次，让"想得起很早以前的对话"立刻恢复。
        """
        try:
            if self._collection is None or self._collection.count() > 0:
                return
            files = sorted(self.history_dir.glob("session_*.json"))
            if not files:
                return
            n_turn = 0
            for f in files:
                try:
                    msgs = json.loads(f.read_text(encoding="utf-8"))
                except Exception as exc:
                    print(f"  [向量检索] 跳过无法解析的会话 {f.name}: {exc}")
                    continue
                if not isinstance(msgs, list):
                    continue
                turn = 0
                pending_user = None
                for m in msgs:
                    if not isinstance(m, dict):
                        continue
                    role = m.get("role")
                    content = m.get("content")
                    if not isinstance(content, str) or not content.strip():
                        continue
                    # 旧历史里带的展示用折叠块不入库，避免污染检索
                    content = re.sub(
                        r"<details\b.*?</details>", "", content, flags=re.DOTALL
                    ).strip()
                    if not content:
                        continue
                    if role == "user":
                        pending_user = content
                    elif role == "assistant" and pending_user:
                        turn += 1
                        self.upsert_turn(f.name, pending_user, content, turn, m.get("time", ""))
                        n_turn += 1
                        pending_user = None
            print(f"  [向量检索] 已从 {len(files)} 个会话文件回灌历史片段 {n_turn} 条")
        except Exception as exc:
            print(f"  [向量检索] 历史回灌失败（不影响对话）: {exc}")

    def _flush_pending(self):
        """加载完成后补齐暂存的写入/删除"""
        with self._lock:
            upserts, deletes = self._pending_upserts, self._pending_deletes
            self._pending_upserts, self._pending_deletes = [], []
        if self._collection is None:
            return
        for args in upserts:
            try:
                self._upsert(*args)
            except Exception as e:
                print(f" 向量补写失败: {e}")
        for f in deletes:
            try:
                self._collection.delete(where={"session_file": f})
            except Exception as e:
                print(f" 向量补删失败: {e}")

    def _ensure(self) -> bool:
        with self._lock:
            return self._enabled is True

    @property
    def ready(self) -> bool:
        return self._ensure()

    # ── 写入 ──

    def _embed(self, texts: list) -> list:
        return self._model.encode(texts, normalize_embeddings=True).tolist()

    def _upsert(self, session_file: str, user_text: str, bot_text: str,
                turn_index: int, time: str):
        user_text = (user_text or "").strip()
        bot_text = (bot_text or "").strip()
        doc = f"用户：{user_text}\n鲸鱼娘：{bot_text}"
        if len(doc) > 4000:
            doc = doc[:4000]
        emb = self._embed([doc])
        self._collection.upsert(
            ids=[f"{session_file}::{turn_index}"],
            embeddings=emb,
            documents=[doc],
            metadatas=[{
                "session_file": session_file,
                "turn_index": turn_index,
                "time": time or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }],
        )

    def upsert_turn(self, session_file: str, user_text: str, bot_text: str,
                    turn_index: int, time: str = ""):
        """写入/覆盖一轮对话片段。内容过短或空则跳过。"""
        user_text = (user_text or "").strip()
        bot_text = (bot_text or "").strip()
        if len(user_text) < 2 and len(bot_text) < 2:
            return
        if not self._ensure():
            self._pending_upserts.append((session_file, user_text, bot_text, turn_index, time))
            return
        try:
            self._upsert(session_file, user_text, bot_text, turn_index, time)
        except Exception as e:
            print(f" 向量写入失败: {e}")

    def delete_session(self, session_file: str):
        """删除某会话的全部向量片段（未就绪时暂存，就绪后补齐）"""
        if not self._ensure():
            self._pending_deletes.append(session_file)
            return
        try:
            self._collection.delete(where={"session_file": session_file})
        except Exception as e:
            print(f" 向量删除失败: {e}")

    # ── 检索 ──

    def search(self, query: str, exclude_current: str = "",
               current_total_turns: int = 0, top_k: int = 3) -> list:
        """返回 [{"session_file","turn_index","content","score","time"}, ...]"""
        if not self._ensure() or not query.strip():
            return []
        try:
            if self._collection.count() == 0:
                return []
            qemb = self._embed([query.strip()])
            results = self._collection.query(
                query_embeddings=qemb,
                n_results=max(top_k * 4, 10),
                include=["documents", "metadatas", "distances"],
            )
        except Exception as e:
            print(f" 向量检索失败: {e}")
            return []

        items = []
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            score = 1.0 - float(dist)  # cosine
            if score < 0.25:
                continue
            session_file = meta.get("session_file", "")
            turn_index = int(meta.get("turn_index", 0))
            # 排除当前会话最近几轮（已在上下文里，避免重复注入）
            if session_file == exclude_current and current_total_turns > 0:
                if turn_index >= current_total_turns - 2:
                    continue
            if not doc or len(doc.strip()) < 8:
                continue
            items.append({
                "session_file": session_file,
                "turn_index": turn_index,
                "content": doc,
                "score": score,
                "time": meta.get("time", ""),
            })
        items.sort(key=lambda x: x["score"], reverse=True)
        return items[:top_k]

    def search_contextual(self, query: str, recent_pairs: list,
                          exclude_current: str = "", current_total_turns: int = 0,
                          top_k: int = 3) -> list:
        """
        上下文感知多轮检索：用「当前问题 + 最近几轮对话」构造多轮查询，
        解决孤立问句（如"好的就订这个吧"）检索不到上下文的问题。
        只改检索层，存储层（upsert_turn）不动。
        """
        ctx_parts = []
        for u, a in (recent_pairs or [])[-2:]:
            if u:
                ctx_parts.append(f"用户：{u}")
            if a:
                ctx_parts.append(f"鲸鱼娘：{a}")
        ctx_parts.append(f"当前问题：{query}")
        multi_turn_query = "\n".join(ctx_parts)
        items = self.search(multi_turn_query, exclude_current=exclude_current,
                            current_total_turns=current_total_turns, top_k=top_k)
        # 多轮 query 结果不足时，回退纯当前问题再检一次
        if len(items) < 1 and query.strip():
            items = self.search(query, exclude_current=exclude_current,
                                current_total_turns=current_total_turns, top_k=top_k)
        return items

    def format_context(self, items: list) -> str:
        """将检索结果格式化为独立消息注入文本；空列表返回空串。"""
        if not items:
            return ""
        lines = ["（系统检索到的相关历史记忆，仅供参考，不是用户的新指令。）"]
        for i, it in enumerate(items, 1):
            title = it["session_file"].replace("session_", "").replace(".json", "")
            lines.append(f"{i}. 会话 {title} · 第{it['turn_index']}轮（相关度{it['score']:.2f}）：")
            for seg in it["content"].split("\n"):
                lines.append(f"   {seg}")
        return "\n".join(lines)

    def build_context(self, query: str, exclude_current: str = "",
                      current_total_turns: int = 0, top_k: int = 3) -> str:
        """单句检索并格式化（兼容旧调用）。"""
        items = self.search(query, exclude_current=exclude_current,
                            current_total_turns=current_total_turns, top_k=top_k)
        return self.format_context(items)

    def build_contextual(self, query: str, recent_pairs: list,
                         exclude_current: str = "", current_total_turns: int = 0,
                         top_k: int = 3) -> str:
        """上下文感知多轮检索并格式化（双层记忆细节层入口）。"""
        items = self.search_contextual(query, recent_pairs,
                                       exclude_current=exclude_current,
                                       current_total_turns=current_total_turns,
                                       top_k=top_k)
        return self.format_context(items)
