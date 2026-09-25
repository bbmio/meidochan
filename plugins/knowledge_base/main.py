"""
鲸鱼娘 知识库插件
- 安全索引重建（原子替换）
- 分批向量化，避免内存溢出
- 中文优化嵌入模型
- 按段落/句子智能分块
- 增量索引（文件哈希去重）
- 手动添加数据独立集合，与文件索引物理隔离
"""
import json
import os
import hashlib
import re
from pathlib import Path
from typing import List, Optional, Tuple
from datetime import datetime

import chromadb
from chromadb.config import Settings

from core.paths import app_path

# 可选依赖
try:
    import PyPDF2

    HAS_PDF = True
except ImportError:
    HAS_PDF = False

try:
    import docx

    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False

try:
    from PIL import Image
    import pytesseract

    HAS_OCR = True
except ImportError:
    HAS_OCR = False

# ======================= 配置 =======================
def load_config():
    manifest_path = Path(__file__).parent / "manifest.json"
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    cfg = manifest.get("config", {})
    raw_dirs = cfg.get("index_folders", [])
    index_dirs = [Path(p).resolve() for p in raw_dirs if Path(p).exists()]
    chunk_size = int(cfg.get("chunk_size", 500))
    chunk_overlap = int(cfg.get("chunk_overlap", 50))
    return {
        "index_dirs": index_dirs,
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
    }


CONFIG = load_config()


# ======================= 文本提取器 =======================
def extract_text_from_txt(file_path: Path) -> Optional[str]:
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"   读取文本失败 {file_path}: {e}")
        return None


def extract_text_from_pdf(file_path: Path) -> Optional[str]:
    if not HAS_PDF:
        print("   PyPDF2 未安装，无法处理 PDF")
        return None
    try:
        text = []
        with open(file_path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text.append(page_text)
        return "\n".join(text)
    except Exception as e:
        print(f"   PDF 提取失败 {file_path}: {e}")
        return None


def extract_text_from_docx(file_path: Path) -> Optional[str]:
    if not HAS_DOCX:
        print("   python-docx 未安装，无法处理 Word")
        return None
    try:
        doc = docx.Document(str(file_path))
        text = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n".join(text)
    except Exception as e:
        print(f"   DOCX 提取失败 {file_path}: {e}")
        return None


def extract_text_from_image(file_path: Path) -> Optional[str]:
    if not HAS_OCR:
        print("   pytesseract 或 Pillow 未安装，无法 OCR")
        return None
    try:
        img = Image.open(file_path)
        text = pytesseract.image_to_string(img, lang="chi_sim+eng")
        return text.strip()
    except Exception as e:
        print(f"   OCR 失败 {file_path}: {e}")
        return None


EXTRACTORS = {
    ".txt": extract_text_from_txt,
    ".md": extract_text_from_txt,
    ".py": extract_text_from_txt,
    ".json": extract_text_from_txt,
    ".yaml": extract_text_from_txt,
    ".yml": extract_text_from_txt,
    ".log": extract_text_from_txt,
    ".pdf": extract_text_from_pdf,
    ".docx": extract_text_from_docx,
    ".doc": extract_text_from_docx,
    ".png": extract_text_from_image,
    ".jpg": extract_text_from_image,
    ".jpeg": extract_text_from_image,
    ".bmp": extract_text_from_image,
}


def extract_text(file_path: Path) -> Optional[str]:
    ext = file_path.suffix.lower()
    if ext in EXTRACTORS:
        return EXTRACTORS[ext](file_path)
    return None


# ======================= 智能文本分块 =======================
def split_sentences(text: str) -> List[str]:
    """简单的中英文句子分割"""
    return re.split(r'(?<=[.!?。！？])\s+', text)


def split_text(text: str, chunk_size: int = 500, chunk_overlap: int = 50) -> List[str]:
    if not text:
        return []

    paragraphs = text.split('\n\n')
    chunks = []
    for para in paragraphs:
        if not para.strip():
            continue
        if len(para) <= chunk_size:
            chunks.append(para)
        else:
            sentences = split_sentences(para)
            current = ""
            for sent in sentences:
                if len(current) + len(sent) <= chunk_size:
                    current += sent
                else:
                    if current:
                        chunks.append(current)
                    while len(sent) > chunk_size:
                        chunks.append(sent[:chunk_size])
                        sent = sent[chunk_size - chunk_overlap:]
                    current = sent
            if current:
                chunks.append(current)
    return chunks


# ======================= ChromaDB 管理 =======================
class KnowledgeBase:
    def __init__(self, persist_directory: str = None):
        if persist_directory is None:
            persist_directory = str(Path(__file__).parent / "chroma_db")
        self.client = chromadb.PersistentClient(
            path=persist_directory,
            settings=Settings(anonymized_telemetry=False)
        )

        self.collection_name = "knowledge_base"
        self._init_or_migrate_collection(self.collection_name)

        self.manual_collection_name = "knowledge_base_manual"
        self._init_or_migrate_collection(self.manual_collection_name)

        self.collection = self.client.get_collection(self.collection_name)
        self.manual_collection = self.client.get_collection(self.manual_collection_name)

        from core.history_retrieval import get_embedding_model
        self.embedder = get_embedding_model()
        if self.embedder is None:
            raise RuntimeError("embedding 模型加载失败，知识库不可用")
        self._ensure_embedding_dimension(self.collection_name)
        self._ensure_embedding_dimension(self.manual_collection_name)
        self.collection = self.client.get_collection(self.collection_name)
        self.manual_collection = self.client.get_collection(self.manual_collection_name)

    def _ensure_embedding_dimension(self, name: str):
        """让集合的向量维度与当前 embedding 模型保持一致；不一致时自动重建。

        - 空集合：无法采样比对维度，但旧 HNSW 索引可能仍锁在 768 维（日后一查询就报错），
          而重建空集合没有任何数据损失 → 直接重建
        - 非空集合：采样一条比对维度，不一致则用当前模型整体重嵌入（内容不丢）
        """
        collection = self.client.get_collection(name)

        # 1) 空集合：按当前 embedding 维度重建（零数据损失）
        if collection.count() == 0:
            try:
                self.client.delete_collection(name)
            except Exception as e:
                print(f"  集合 {name} 为空但删除失败: {e}")
                return
            self.client.create_collection(name=name, metadata={"hnsw:space": "cosine"})
            print(f"  集合 {name} 为空，已按当前 embedding 维度重建")
            return

        # 2) 非空集合：与当前模型实际输出维度比对（不再硬编码 1024）
        sample = collection.get(limit=1, include=["embeddings", "documents", "metadatas"])
        embeddings = sample.get("embeddings")
        if embeddings is None or len(embeddings) == 0:
            return
        current_dim = len(self.embedder.encode(["."])[0])
        if len(embeddings[0]) == current_dim:
            return

        print(f"  集合 {name} 使用旧 embedding 维度（{len(embeddings[0])} → {current_dim}），正在用当前模型重建...")
        data = collection.get(include=["documents", "metadatas"])
        documents = data.get("documents") or []
        if not documents:
            return
        new_embeddings = self.embedder.encode(documents).tolist()
        self.client.delete_collection(name)
        rebuilt = self.client.create_collection(name=name, metadata={"hnsw:space": "cosine"})
        rebuilt.add(
            ids=data["ids"],
            documents=documents,
            metadatas=data.get("metadatas"),
            embeddings=new_embeddings,
        )
        print(f"  集合 {name} 已重建：{len(documents)} 条")

    def _init_or_migrate_collection(self, name: str):
        try:
            existing = self.client.get_collection(name)
            space = (existing.metadata or {}).get("hnsw:space")
            if space is not None and space != "cosine":
                print(f" 集合 '{name}' 距离度量非 cosine，将重建以启用余弦距离")
                self.client.delete_collection(name)
                self.client.create_collection(
                    name,
                    metadata={"hnsw:space": "cosine"}
                )
        except Exception:
            # get_collection 失败（集合不存在或已在上方被删除）
            try:
                self.client.create_collection(
                    name,
                    metadata={"hnsw:space": "cosine"}
                )
                print(f" 已创建余弦距离集合: {name}")
            except Exception as e:
                # 可能已存在（并发竞争、状态恢复等），尝试直接获取
                print(f" 集合 {name} 创建失败，尝试读取已有集合: {e}")

    # ==================== 文件索引 ====================

    def build_index(self, progress_callback=None) -> Tuple[int, int]:
        TEMP_COLLECTION = "knowledge_base_temp"

        try:
            self.client.delete_collection(TEMP_COLLECTION)
        except Exception:
            pass

        temp_collection = self.client.create_collection(
            TEMP_COLLECTION,
            metadata={"hnsw:space": "cosine"}
        )

        files_processed, total_added = self._scan_and_index(temp_collection, progress_callback)

        try:
            self.client.delete_collection(self.collection_name)
            new_collection = self.client.create_collection(
                self.collection_name,
                metadata={"hnsw:space": "cosine"}
            )
            temp_data = temp_collection.get(
                include=["documents", "metadatas", "embeddings"]
            )
            if temp_data["ids"]:
                new_collection.add(
                    ids=temp_data["ids"],
                    documents=temp_data["documents"],
                    metadatas=temp_data["metadatas"],
                    embeddings=temp_data["embeddings"]
                )
            self.collection = new_collection
        except Exception as e:
            print(f" 索引替换失败，已保留旧数据: {e}")
            raise
        finally:
            try:
                self.client.delete_collection(TEMP_COLLECTION)
            except Exception:
                pass

        return files_processed, total_added

    def _scan_and_index(self, collection, progress_callback=None) -> Tuple[int, int]:
        hash_file = Path(__file__).parent / "file_hashes.json"
        file_hashes = {}
        if hash_file.exists():
            try:
                with open(hash_file, "r", encoding="utf-8") as f:
                    file_hashes = json.load(f)
            except Exception:
                pass

        total_added = 0
        files_processed = 0
        new_hashes = {}

        for directory in CONFIG["index_dirs"]:
            if not directory.exists():
                continue
            for file_path in directory.rglob("*"):
                if not file_path.is_file():
                    continue
                ext = file_path.suffix.lower()
                if ext not in EXTRACTORS:
                    continue

                try:
                    with open(file_path, "rb") as f:
                        file_md5 = hashlib.md5(f.read()).hexdigest()
                except Exception:
                    file_md5 = None

                if file_md5 and str(file_path) in file_hashes and file_hashes[str(file_path)] == file_md5:
                    print(f"  ⏭ 跳过未修改: {file_path}")
                    continue

                print(f"   正在处理: {file_path}")
                text = extract_text(file_path)
                if text:
                    added = self._add_chunks_to(collection, file_path, text)
                    total_added += added
                    files_processed += 1
                    new_hashes[str(file_path)] = file_md5

                if progress_callback:
                    progress_callback(file_path, files_processed, total_added)

        try:
            with open(hash_file, "w", encoding="utf-8") as f:
                json.dump(new_hashes, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f" 保存哈希记录失败: {e}")

        return files_processed, total_added

    def _add_chunks_to(self, collection, file_path: Path, text: str) -> int:
        chunks = split_text(text, CONFIG["chunk_size"], CONFIG["chunk_overlap"])
        if not chunks:
            return 0

        ids = []
        metadatas = []
        for i, chunk in enumerate(chunks):
            chunk_id = hashlib.md5(f"{str(file_path)}_{i}".encode()).hexdigest()
            ids.append(chunk_id)
            metadatas.append({"source": str(file_path), "chunk_index": i, "type": "file"})

        BATCH_SIZE = 32
        total_added = 0
        for start in range(0, len(chunks), BATCH_SIZE):
            batch_chunks = chunks[start:start + BATCH_SIZE]
            batch_ids = ids[start:start + BATCH_SIZE]
            batch_metadatas = metadatas[start:start + BATCH_SIZE]
            batch_embeddings = self.embedder.encode(batch_chunks).tolist()
            collection.add(
                ids=batch_ids,
                documents=batch_chunks,
                metadatas=batch_metadatas,
                embeddings=batch_embeddings
            )
            total_added += len(batch_chunks)
        return total_added

    # ==================== 手动添加 ====================

    def add_text(self, text: str, source_name: str = "manual", allow_duplicate: bool = False) -> dict:
        chunks = split_text(text, CONFIG["chunk_size"], CONFIG["chunk_overlap"])
        if not chunks:
            return {"added": 0, "skipped": 0, "status": "unchanged"}

        skipped = 0
        if not allow_duplicate:
            existing = self.manual_collection.get(include=["documents"])
            text_hashes = set()
            for doc in existing.get("documents", []):
                text_hashes.add(hashlib.md5(doc.encode()).hexdigest())

            deduped_chunks = []
            for chunk in chunks:
                chunk_hash = hashlib.md5(chunk.encode()).hexdigest()
                if chunk_hash not in text_hashes:
                    deduped_chunks.append(chunk)
            skipped = len(chunks) - len(deduped_chunks)
            chunks = deduped_chunks

        if not chunks:
            return {"added": 0, "skipped": skipped, "status": "unchanged"}

        ids = []
        metadatas = []
        now = datetime.utcnow().isoformat()
        for i, chunk in enumerate(chunks):
            content_hash = hashlib.md5(chunk.encode()).hexdigest()
            chunk_id = f"manual_{source_name}_{content_hash}_{i}"
            ids.append(chunk_id)
            metadatas.append({
                "source": source_name,
                "chunk_index": i,
                "added_at": now,
                "type": "manual"
            })

        embeddings = self.embedder.encode(chunks).tolist()
        self.manual_collection.add(
            ids=ids,
            documents=chunks,
            metadatas=metadatas,
            embeddings=embeddings
        )

        return {"added": len(chunks), "skipped": skipped, "status": "new"}

    # ==================== 搜索 ====================

    def search(self, query: str, n_results: int = 3, include_manual: bool = True) -> List[Tuple[str, float, dict]]:
        query_emb = self.embedder.encode([query]).tolist()
        all_results = []

        if self.collection.count() > 0:
            file_results = self.collection.query(
                query_embeddings=query_emb,
                n_results=n_results,
                include=["documents", "distances", "metadatas"]
            )
            for doc, dist, meta in zip(
                file_results["documents"][0],
                file_results["distances"][0],
                file_results["metadatas"][0]
            ):
                all_results.append((doc, dist, meta))

        if include_manual and self.manual_collection.count() > 0:
            manual_results = self.manual_collection.query(
                query_embeddings=query_emb,
                n_results=n_results,
                include=["documents", "distances", "metadatas"]
            )
            for doc, dist, meta in zip(
                manual_results["documents"][0],
                manual_results["distances"][0],
                manual_results["metadatas"][0]
            ):
                all_results.append((doc, dist, meta))

        all_results.sort(key=lambda x: x[1])
        return all_results[:n_results]

    def search_context(self, query: str, top_k: int = 3) -> str:
        results = self.search(query, n_results=top_k)
        if not results:
            return ""
        lines = ["【相关文件片段】"]
        for i, (doc, dist, meta) in enumerate(results, 1):
            source = meta.get("source", "未知文件")
            lines.append(f"[{i}] 来自文件: {source}\n{doc}\n")
        return "\n".join(lines)

    def clear(self):
        self.client.delete_collection(self.collection_name)
        self.collection = self.client.create_collection(
            self.collection_name,
            metadata={"hnsw:space": "cosine"}
        )


# ==================== 全局单例 ====================
_kb_instance: Optional[KnowledgeBase] = None
_bm25_instance = None
_hybrid_instance = None
_relation_graph_instance = None


def get_kb() -> KnowledgeBase:
    global _kb_instance
    if _kb_instance is None:
        persist_dir = str(Path(__file__).parent / "chroma_db")
        _kb_instance = KnowledgeBase(persist_directory=persist_dir)
    return _kb_instance


def get_bm25() -> "BM25Indexer":
    global _bm25_instance
    if _bm25_instance is None:
        from .bm25_indexer import BM25Indexer
        storage = Path(__file__).parent / "bm25_data" / "index.json"
        storage.parent.mkdir(exist_ok=True)
        _bm25_instance = BM25Indexer(storage_path=storage)
        _bm25_instance.load(storage)
    return _bm25_instance


def get_hybrid_searcher() -> "HybridSearcher":
    global _hybrid_instance
    if _hybrid_instance is None:
        from .hybrid_search import HybridSearcher
        _hybrid_instance = HybridSearcher(get_kb(), get_bm25())
    return _hybrid_instance


def get_relation_graph() -> "RelationGraph":
    global _relation_graph_instance
    if _relation_graph_instance is None:
        from .relation_graph import RelationGraph
        _relation_graph_instance = RelationGraph()
    return _relation_graph_instance


# ======================= 命令注册 =======================
def register_commands():
    return {
        "/kb_rebuild": lambda _=None: rebuild_index(),
        "/kb_search": lambda q: search_knowledge(q),
        "/kb_status": lambda _=None: kb_status(),
        "/kb_add": lambda text, source="用户提供的资料": add_to_memory(text, source),
        "/kb_hybrid": lambda q: hybrid_search_command(q),
        "/kb_related": lambda p: related_files_command(p),
        "/kb_mcp": lambda _=None: start_mcp_server(),
    }


def search_knowledge(query: str) -> str:
    kb = get_kb()
    results = kb.search(query, n_results=5)
    if not results:
        return " 未找到相关片段。"
    lines = [f" 搜索: {query}\n"]
    for i, (doc, dist, meta) in enumerate(results, 1):
        similarity = 1 - dist
        lines.append(f"--- 结果 {i} (相似度: {similarity:.2f}) ---")
        lines.append(f"来源: {meta.get('source', '未知')}")
        lines.append(doc)
        lines.append("")
    return "\n".join(lines)


def kb_status() -> str:
    kb = get_kb()
    try:
        file_count = kb.collection.count()
        manual_count = kb.manual_collection.count()
        total = file_count + manual_count
        return f" 当前知识库包含 {total} 个文本块（文件: {file_count}，手动: {manual_count}）。"
    except Exception:
        return " 知识库未初始化或数据损坏。"


def add_to_memory(text: str, source: str = "用户提供的资料") -> str:
    kb = get_kb()
    try:
        result = kb.add_text(text, source)
        if result["status"] == "unchanged":
            return f" 内容已存在（跳过 {result['skipped']} 个重复块），未添加新内容。"
        msg = f" 已将内容加入长期记忆，共 {result['added']} 个文本块。"
        if result["skipped"] > 0:
            msg += f"（跳过 {result['skipped']} 个重复块）"
        return msg
    except Exception as e:
        return f" 加入长期记忆失败：{e}"


def inject_knowledge_context(user_message: str) -> str:
    kb = get_kb()
    if kb.collection.count() == 0 and kb.manual_collection.count() == 0:
        return ""
    return kb.search_context(user_message, top_k=3)


# ======================= 新工具：混合检索 / 依赖图 / MCP =======================

def hybrid_search_command(query: str) -> str:
    """混合检索：BM25 + 语义向量"""
    if not query or not query.strip():
        return " 请输入搜索关键词，用法：/kb_hybrid <关键词>"
    hs = get_hybrid_searcher()
    results = hs.search(query.strip(), top_k=5)
    if not results:
        return " 未找到相关片段。"
    lines = [f" 混合搜索: {query}\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"--- [{i}] 总分: {r.final_score:.2f} (BM25={r.bm25_score:.2f}, Vector={r.vector_score:.2f}) ---")
        lines.append(f"来源: {r.source}")
        lines.append(r.content[:400])
        lines.append("")
    return "\n".join(lines)


def related_files_command(file_path: str) -> str:
    """查询文件依赖关系"""
    if not file_path or not file_path.strip():
        return " 请输入文件路径，用法：/kb_related <文件路径>"
    graph = get_relation_graph()
    project_root = app_path()
    if not graph._dependencies:
        graph.build_from_directory(project_root)
    result = graph.get_related(file_path.strip())
    lines = [f" 依赖关系: {file_path}\n"]
    if result["depends_on"]:
        lines.append("依赖的文件:")
        for f in result["depends_on"]:
            lines.append(f"   {f}")
    if result["used_by"]:
        lines.append("被哪些文件使用:")
        for f in result["used_by"]:
            lines.append(f"   {f}")
    if not result["depends_on"] and not result["used_by"]:
        lines.append("（无依赖关系记录）")
    return "\n".join(lines)


def start_mcp_server() -> str:
    """启动 MCP Server（stdio 协议，供外部 IDE/Agent 连接）"""
    from .mcp_server import run_mcp_server
    import threading
    thread = threading.Thread(target=run_mcp_server, daemon=True)
    thread.start()
    return " MCP Server 已在后台启动（stdio 协议）\n提示：在 Cursor/Claude Code 中添加 stdio MCP 配置即可连接。"


def rebuild_index() -> str:
    kb = get_kb()
    try:
        files, chunks = kb.build_index()
        # 同步重建 BM25 索引
        bm25 = get_bm25()
        if bm25.ready and chunks > 0:
            # 从 ChromaDB 获取所有文档重建 BM25
            try:
                all_docs = kb.collection.get(include=["documents", "metadatas"])
                documents = []
                for doc, meta in zip(all_docs["documents"], all_docs["metadatas"]):
                    doc_id = meta.get("chunk_id", meta.get("source", "unknown"))
                    documents.append((doc_id, doc, meta))
                bm25.build(documents)
                bm25.save(bm25._storage)
            except Exception as e:
                print(f" BM25 索引同步失败: {e}")
        return f" 知识库重建完成！处理了 {files} 个文件，共 {chunks} 个文本块。"
    except Exception as e:
        return f" 重建索引时出错: {e}"