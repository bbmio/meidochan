"""
BM25 倒排索引
基于 jieba 中文分词 + rank_bm25，LocalMind 架构对标
"""
import json
import re
from pathlib import Path
from typing import Dict, List, Tuple, Optional

try:
    import jieba
    import jieba.analyse
    HAS_JIEBA = True
except ImportError:
    HAS_JIEBA = False

try:
    from rank_bm25 import BM25Okapi
    HAS_BM25 = True
except ImportError:
    HAS_BM25 = False


class BM25Indexer:
    """BM25 倒排索引（内存+持久化）"""

    def __init__(self, storage_path: Optional[Path] = None):
        self._corpus: List[str] = []
        self._doc_ids: List[str] = []
        self._doc_metas: List[dict] = []
        self._tokenized: List[List[str]] = []
        self._bm25 = None
        self._storage = storage_path

        if not HAS_JIEBA or not HAS_BM25:
            print(" jieba 或 rank_bm25 未安装，BM25 功能不可用")

    @property
    def ready(self) -> bool:
        return HAS_JIEBA and HAS_BM25 and self._bm25 is not None

    def _tokenize(self, text: str) -> List[str]:
        if HAS_JIEBA:
            # jieba 分词 + 过滤停用词和标点
            words = jieba.lcut(text)
            stop_words = {'的', '了', '在', '是', '我', '有', '和', '就', '不', '人', '都', '一', '一个', '上', '也', '很', '到', '说', '要', '去', '你', '会', '着', '没有', '看', '好', '自己', '这'}
            return [w for w in words if len(w) > 1 and w not in stop_words and not w.isascii()]
        # 无 jieba 时用简单空格分词
        return re.findall(r'\w+', text)

    def build(self, documents: List[Tuple[str, str, dict]]):
        """
        documents: [(doc_id, text, metadata), ...]
        """
        if not HAS_JIEBA or not HAS_BM25:
            return

        self._doc_ids = [d[0] for d in documents]
        self._corpus = [d[1] for d in documents]
        self._doc_metas = [d[2] for d in documents]
        self._tokenized = [self._tokenize(doc) for doc in self._corpus]
        self._bm25 = BM25Okapi(self._tokenized)
        print(f" BM25 索引构建完成：{len(self._corpus)} 个文档")

    def add(self, doc_id: str, text: str, metadata: dict):
        """增量添加单条文档"""
        if not HAS_JIEBA or not HAS_BM25:
            return
        self._doc_ids.append(doc_id)
        self._corpus.append(text)
        self._doc_metas.append(metadata)
        self._tokenized.append(self._tokenize(text))
        self._bm25 = BM25Okapi(self._tokenized)

    def search(self, query: str, top_k: int = 10) -> List[Tuple[str, float, dict]]:
        """
        返回 [(doc_id, score, metadata), ...]
        score 经过 sigmoid 归一化到 0-1
        """
        if not self.ready:
            return []

        tokenized_query = self._tokenize(query)
        scores = self._bm25.get_scores(tokenized_query)

        # 归一化到 0-1
        import math
        max_score = max(scores) if max(scores) > 0 else 1.0
        normalized = [math.tanh(s / max_score) for s in scores]

        # 排序取 top_k
        ranked = sorted(
            zip(self._doc_ids, normalized, self._doc_metas),
            key=lambda x: x[1],
            reverse=True
        )
        return ranked[:top_k]

    def remove(self, doc_id: str):
        if not self.ready:
            return
        try:
            idx = self._doc_ids.index(doc_id)
            self._doc_ids.pop(idx)
            self._corpus.pop(idx)
            self._doc_metas.pop(idx)
            self._tokenized.pop(idx)
            self._bm25 = BM25Okapi(self._tokenized) if self._tokenized else None
        except ValueError:
            pass

    def save(self, path: Path):
        data = {
            "doc_ids": self._doc_ids,
            "corpus": self._corpus,
            "doc_metas": self._doc_metas,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load(self, path: Path) -> bool:
        if not path.exists() or not HAS_JIEBA or not HAS_BM25:
            return False
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self._doc_ids = data["doc_ids"]
        self._corpus = data["corpus"]
        self._doc_metas = data["doc_metas"]
        self._tokenized = [self._tokenize(doc) for doc in self._corpus]
        self._bm25 = BM25Okapi(self._tokenized)
        return True

    @property
    def count(self) -> int:
        return len(self._corpus)
