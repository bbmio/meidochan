"""
MCP Server — 知识库插件
对标 LocalMind，通过 stdio 协议向外部 AI 工具暴露知识库工具
"""
import sys
import json
from typing import Optional
from pathlib import Path

from .main import get_kb, get_hybrid_searcher


def create_mcp_server():
    """创建 MCP Server 实例（FastMCP）"""
    try:
        from fastmcp import FastMCP
    except ImportError:
        print(" fastmcp 未安装。请: pip install fastmcp")
        return None

    mcp = FastMCP("whalegirl-knowledge-base")

    @mcp.tool()
    def search_code(query: str, top_k: int = 5) -> str:
        """在知识库中搜索代码/文档片段（BM25 + 语义混合检索）"""
        hs = get_hybrid_searcher()
        results = hs.search(query, top_k=top_k)
        if not results:
            return "未找到相关文档。"
        lines = [f"搜索: {query}\n"]
        for i, r in enumerate(results, 1):
            lines.append(f"--- [{i}] 得分: {r.final_score:.2f} (BM25={r.bm25_score:.2f}, Vector={r.vector_score:.2f}) ---")
            lines.append(f"来源: {r.source}")
            lines.append(r.content[:500])
            lines.append("")
        return "\n".join(lines)

    @mcp.tool()
    def ask_codebase(question: str) -> str:
        """基于知识库内容回答问题（带引用）"""
        hs = get_hybrid_searcher()
        context = hs.search_context(question, top_k=5)
        if not context:
            return "知识库中未找到相关信息。"
        return f"基于知识库的上下文：\n\n{context}\n\n请基于以上上下文回答问题。"

    @mcp.tool()
    def find_related_files(file_path: str, depth: int = 2) -> str:
        """查找与指定文件有依赖关系的文件"""
        from .relation_graph import RelationGraph
        kb = get_kb()
        # 使用当前项目根目录构建依赖图
        graph = RelationGraph()
        graph.build_from_directory(Path(kb._persist_dir).parent.parent)
        result = graph.get_related(file_path, depth)
        lines = [f"文件依赖关系: {file_path}\n"]
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

    @mcp.tool()
    def explain_file(file_path: str, line_start: int = 1, line_end: int = 50) -> str:
        """解释指定文件的内容"""
        p = Path(file_path)
        if not p.exists():
            return f"文件不存在: {file_path}"
        try:
            with open(p, "r", encoding="utf-8") as f:
                lines = f.readlines()
            selected = lines[line_start - 1:line_end]
            return f"文件 {p.name} 第 {line_start}-{line_end} 行:\n```\n{''.join(selected)}\n```"
        except Exception as e:
            return f"读取失败: {e}"

    @mcp.tool()
    def index_status() -> str:
        """查看知识库索引状态"""
        kb = get_kb()
        file_count = kb.collection.count()
        manual_count = kb.manual_collection.count()
        return f"知识库: 文件块={file_count}, 手动块={manual_count}, 总计={file_count + manual_count}"

    @mcp.tool()
    def reindex(full: bool = False) -> str:
        """重建知识库索引（增量或全量）"""
        from .main import rebuild_index
        return rebuild_index()

    return mcp


def run_mcp_server():
    """启动 MCP Server（stdio 协议）"""
    mcp = create_mcp_server()
    if mcp is None:
        sys.exit(1)
    print(" 知识库 MCP Server 启动 (stdio)...")
    mcp.run()
