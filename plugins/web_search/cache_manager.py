"""
搜索缓存管理器
- 独立 collection: search_cache
- 复用 knowledge_base 的 embedding 模型
- 后台线程写入，不阻塞搜索返回
- 支持相似度阈值、14天过期、URL去重
- 余弦距离 + 乐观锁并发写入
"""
import threading
import time
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

import chromadb
from chromadb.config import Settings

from plugins.knowledge_base.main import get_kb

CACHE_DIR = Path(__file__).parent / "cache_db"
CACHE_COLLECTION = "search_cache"
SIMILARITY_THRESHOLD = 0.8
CACHE_DAYS = 14


class SearchCacheManager:
    def __init__(self):
        CACHE_DIR.mkdir(exist_ok=True)
        self.client = chromadb.PersistentClient(
            path=str(CACHE_DIR),
            settings=Settings(anonymized_telemetry=False)
        )

        try:
            self.collection = self.client.get_collection(CACHE_COLLECTION)
            if self.collection.metadata.get("hnsw:space") != "cosine":
                print(" 缓存集合距离度量非 cosine，将重建")
                self.client.delete_collection(CACHE_COLLECTION)
                self.collection = self.client.create_collection(
                    CACHE_COLLECTION,
                    metadata={"hnsw:space": "cosine"}
                )
        except Exception:
            try:
                self.collection = self.client.create_collection(
                    CACHE_COLLECTION,
                    metadata={"hnsw:space": "cosine"}
                )
                print(" 已创建余弦距离缓存集合")
            except Exception as e:
                print(f" 缓存集合创建失败，尝试读取已有集合: {e}")

        self.embedder = get_kb().embedder
        # 缓存集合的维度必须与当前 embedding 模型一致，否则查询直接报维度错 → 自愈重建
        self._ensure_cache_dimension()

    def _ensure_cache_dimension(self) -> None:
        """缓存是纯派生数据：维度与当前 embedding 模型不一致就直接清空重建。

        背景：嵌入模型从 768 维换成 bge-m3（1024 维）后，旧缓存集合被旧维度锁死，
        查询会抛 "Collection expecting embedding with dimension of 768, got 1024"。
        """
        try:
            if self.collection.count() == 0:
                self.client.delete_collection(CACHE_COLLECTION)
                self.collection = self.client.create_collection(
                    CACHE_COLLECTION, metadata={"hnsw:space": "cosine"}
                )
                print("  缓存集合为空，已按当前 embedding 维度重建")
                return
            sample = self.collection.get(limit=1, include=["embeddings"])
            embs = sample.get("embeddings")
            if embs is None or len(embs) == 0:
                return
            current_dim = len(self.embedder.encode(["."])[0])
            if len(embs[0]) != current_dim:
                print(f"  缓存集合维度不匹配（{len(embs[0])} → {current_dim}），已清空重建")
                self.client.delete_collection(CACHE_COLLECTION)
                self.collection = self.client.create_collection(
                    CACHE_COLLECTION, metadata={"hnsw:space": "cosine"}
                )
        except Exception as e:
            print(f"  缓存集合维度检查失败（不影响搜索）: {e}")

    def search_cache(self, query: str, top_k: int = 3) -> Optional[str]:
        cutoff = (datetime.utcnow() - timedelta(days=CACHE_DAYS)).isoformat()
        query_emb = self.embedder.encode([query]).tolist()

        results = self.collection.query(
            query_embeddings=query_emb,
            n_results=top_k * 2,
            include=["documents", "distances", "metadatas"]
        )
        if not results["documents"][0]:
            return None

        valid = []
        for doc, dist, meta in zip(
            results["documents"][0],
            results["distances"][0],
            results["metadatas"][0]
        ):
            if meta.get("cached_at", "0") < cutoff:
                continue
            similarity = 1 - dist
            if similarity < SIMILARITY_THRESHOLD:
                continue
            valid.append((doc, dist, meta))

        if not valid:
            return None

        valid.sort(key=lambda x: x[1])
        lines = [f" 来自缓存 (相似度 {1 - valid[0][1]:.2f})"]
        for i, (doc, _, meta) in enumerate(valid[:top_k], 1):
            lines.append(f"{i}. **{meta.get('title', '无标题')}**")
            lines.append(f"   {doc}")
            lines.append(f"    {meta.get('source_url', '')}")
            lines.append("")
        return "\n".join(lines)

    def add_to_cache(self, results: list[dict], max_retries: int = 3):
        def _write():
            for attempt in range(max_retries):
                try:
                    ids, documents, metadatas = [], [], []
                    now = datetime.utcnow().isoformat()

                    url_ids = [self._url_to_id(r.get("url", "")) for r in results if r.get("url")]
                    existing = self.collection.get(ids=url_ids) if url_ids else {"ids": []}
                    existing_ids = set(existing["ids"])

                    for r in results:
                        url = r.get("url", "")
                        doc_id = self._url_to_id(url)
                        if doc_id in existing_ids:
                            continue
                        content = f"标题: {r.get('title', '')}\n摘要: {r.get('snippet', '')}"
                        ids.append(doc_id)
                        documents.append(content)
                        metadatas.append({
                            "source_url": url,
                            "title": r.get("title", "无标题"),
                            "cached_at": now
                        })

                    if ids:
                        embeddings = self.embedder.encode(documents).tolist()
                        self.collection.add(
                            ids=ids,
                            documents=documents,
                            metadatas=metadatas,
                            embeddings=embeddings
                        )
                        print(f" 后台已写入缓存：{len(ids)} 条新记录")
                    return

                except Exception as e:
                    if attempt == max_retries - 1:
                        print(f" 缓存写入失败，已达最大重试次数: {e}")
                    else:
                        time.sleep(0.1 * (attempt + 1))

        thread = threading.Thread(target=_write, daemon=True)
        thread.start()

    def _is_url_cached(self, url: str) -> bool:
        existing = self.collection.get(ids=[self._url_to_id(url)])
        return len(existing["ids"]) > 0

    @staticmethod
    def _url_to_id(url: str) -> str:
        import hashlib
        return hashlib.md5(url.encode()).hexdigest()

    def clean_expired(self):
        cutoff = (datetime.utcnow() - timedelta(days=CACHE_DAYS)).isoformat()
        all_data = self.collection.get(include=["metadatas"])
        expired_ids = [
            id_ for id_, meta in zip(all_data["ids"], all_data["metadatas"])
            if meta.get("cached_at", "0") < cutoff
        ]
        if expired_ids:
            self.collection.delete(ids=expired_ids)
            print(f" 清理过期缓存：移除 {len(expired_ids)} 条")


_cache_manager: Optional[SearchCacheManager] = None


def get_cache_manager() -> SearchCacheManager:
    global _cache_manager
    if _cache_manager is None:
        _cache_manager = SearchCacheManager()
        _cache_manager.clean_expired()
    return _cache_manager