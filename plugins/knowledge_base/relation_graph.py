"""
文件依赖关系图谱
解析 Python import / 文件引用，构建有向图，支持邻域查询
对标 LocalMind 的 import/link graph
"""
import re
import ast
from pathlib import Path
from typing import Dict, List, Set, Optional
from collections import defaultdict


class RelationGraph:
    """文件依赖关系图谱"""

    def __init__(self):
        # adjacency: file  set of files it depends on
        self._dependencies: Dict[str, Set[str]] = defaultdict(set)
        # reverse: file  set of files that depend on it
        self._dependents: Dict[str, Set[str]] = defaultdict(set)
        # alias map: module_name  file_path
        self._module_map: Dict[str, str] = {}

    def add_file(self, file_path: Path):
        """扫描单个文件，提取依赖关系"""
        fp = str(file_path)
        if not file_path.exists():
            return

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            return

        if file_path.suffix == ".py":
            self._parse_python_imports(fp, content)
        elif file_path.suffix in (".md", ".txt"):
            self._parse_markdown_links(fp, content)

    def _parse_python_imports(self, file_path: str, content: str):
        """解析 Python import 语句"""
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name
                    self._add_dependency(file_path, module)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    self._add_dependency(file_path, node.module)

    def _add_dependency(self, file_path: str, module: str):
        """尝试将模块名映射到项目中的文件"""
        # 将 module.name 转为可能的文件路径
        parts = module.split(".")
        # 尝试匹配项目中的文件
        project_root = Path(file_path).parent
        for i in range(len(parts), 0, -1):
            candidate = project_root / "/".join(parts[:i])
            for ext in (".py", "/__init__.py"):
                p = Path(str(candidate) + ext)
                if p.exists():
                    resolved = str(p)
                    self._dependencies[file_path].add(resolved)
                    self._dependents[resolved].add(file_path)
                    self._module_map[module] = resolved
                    return

    def _parse_markdown_links(self, file_path: str, content: str):
        """解析 Markdown 中的文件引用 [text](path)"""
        links = re.findall(r'\[([^\]]+)\]\(([^)]+)\)', content)
        project_root = Path(file_path).parent
        for text, link in links:
            if link.startswith(("http://", "https://", "#", "mailto:")):
                continue
            target = (project_root / link).resolve()
            if target.exists():
                resolved = str(target)
                self._dependencies[file_path].add(resolved)
                self._dependents[resolved].add(file_path)

    def get_related(self, file_path: str, depth: int = 2) -> Dict[str, List[str]]:
        """获取与指定文件相关的所有文件（依赖+被依赖）"""
        deps = self._bfs(self._dependencies, file_path, depth)
        dependents = self._bfs(self._dependents, file_path, depth)
        return {
            "depends_on": sorted(deps),
            "used_by": sorted(dependents),
        }

    def _bfs(self, graph: Dict[str, Set[str]], start: str, max_depth: int) -> Set[str]:
        if start not in graph:
            return set()
        visited = {start}
        queue = [(start, 0)]
        result = set()
        while queue:
            node, depth = queue.pop(0)
            if depth >= max_depth:
                continue
            for neighbor in graph.get(node, set()):
                if neighbor not in visited:
                    visited.add(neighbor)
                    result.add(neighbor)
                    queue.append((neighbor, depth + 1))
        return result

    def build_from_directory(self, directory: Path, extensions: Set[str] = None):
        """扫描目录构建完整依赖图"""
        if extensions is None:
            extensions = {".py", ".md", ".txt"}
        count = 0
        for ext in extensions:
            for f in directory.rglob(f"*{ext}"):
                if f.is_file():
                    self.add_file(f)
                    count += 1
        print(f" 依赖图构建完成：{count} 个文件，{len(self._dependencies)} 个节点")

    def clear(self):
        self._dependencies.clear()
        self._dependents.clear()
        self._module_map.clear()

    @property
    def stats(self) -> dict:
        return {
            "files_tracked": len(self._dependencies),
            "total_edges": sum(len(v) for v in self._dependencies.values()),
        }
