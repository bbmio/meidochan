"""
混合检索引擎
BM25 关键词召回 × ChromaDB 语义召回  重排序
"""
from typing import List, Tuple, Optional
from dataclasses import dataclass

from .bm25_indexer import BM25Indexer
from .main import KnowledgeBase


@dataclass
class SearchResult:
    doc_id: str
    content: str
    source: str
    bm25_score: float
    vector_score: float
    final_score: float
    chunk_index: int


class HybridSearcher:
    """BM25 + 向量混合检索"""

    def __init__(self, kb: KnowledgeBase, bm25: BM25Indexer,
                 bm25_weight: float = 0.3, vector_weight: float = 0.7):
        self.kb = kb
        self.bm25 = bm25
        self.bm25_weight = bm25_weight
        self.vector_weight = vector_weight

    def search(self, query: str, top_k: int = 5) -> List[SearchResult]:
        # ── BM25 召回 ──
        bm25_results = {}
        if self.bm25.ready:
            for doc_id, score, meta in self.bm25.search(query, top_k=top_k * 3):
                bm25_results[doc_id] = (score, meta)

        # ── 向量召回 ──
        vector_results = {}
        query_emb = self.kb.embedder.encode([query]).tolist()
        for collection, label in [
            (self.kb.collection, "file"),
            (self.kb.manual_collection, "manual"),
        ]:
            if collection.count() == 0:
                continue
            results = collection.query(
                query_embeddings=query_emb,
                n_results=top_k * 3,
                include=["documents", "distances", "metadatas"],
            )
            for doc, dist, meta in zip(
                results["documents"][0],
                results["distances"][0],
                results["metadatas"][0],
            ):
                doc_id = meta.get("chunk_id", meta.get("source", "unknown"))
                sim = 1.0 - dist  # cosine similarity
                if doc_id not in vector_results or sim > vector_results[doc_id][0]:
                    vector_results[doc_id] = (sim, doc, meta, label)

        # ── 合并重排序 ──
        merged = {}
        for doc_id, (score, meta) in bm25_results.items():
            merged[doc_id] = {
                "bm25_score": score,
                "vector_score": 0.0,
                "content": meta.get("content", ""),
                "source": meta.get("source", "unknown"),
                "chunk_index": meta.get("chunk_index", 0),
            }
        for doc_id, (sim, doc, meta, label) in vector_results.items():
            if doc_id in merged:
                merged[doc_id]["vector_score"] = sim
                if not merged[doc_id]["content"]:
                    merged[doc_id]["content"] = doc
            else:
                merged[doc_id] = {
                    "bm25_score": 0.0,
                    "vector_score": sim,
                    "content": doc,
                    "source": meta.get("source", "unknown"),
                    "chunk_index": meta.get("chunk_index", 0),
                }

        # 加权融合
        for doc_id, item in merged.items():
            item["final_score"] = (
                self.bm25_weight * item["bm25_score"] +
                self.vector_weight * item["vector_score"]
            )

        # 排序取 top_k
        ranked = sorted(merged.items(), key=lambda x: x[1]["final_score"], reverse=True)
        return [
            SearchResult(
                doc_id=doc_id,
                content=item["content"],
                source=item["source"],
                bm25_score=item["bm25_score"],
                vector_score=item["vector_score"],
                final_score=item["final_score"],
                chunk_index=item["chunk_index"],
            )
            for doc_id, item in ranked[:top_k]
        ]

    def search_context(self, query: str, top_k: int = 3) -> str:
        """生成供 system prompt 注入的上下文"""
        results = self.search(query, top_k=top_k)
        if not results:
            return ""
        lines = ["【相关文档片段】"]
        for i, r in enumerate(results, 1):
            lines.append(f"[{i}] 来源: {r.source}")
            lines.append(f"    相关度: {r.final_score:.2f} (BM25={r.bm25_score:.2f}, Vector={r.vector_score:.2f})")
            lines.append(f"    {r.content[:200]}...")
            lines.append("")
        return "\n".join(lines)
