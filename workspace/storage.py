"""工作空间存储层 — Repository 模式

接口：save() / load() / list_all() / delete()
后端：TOML 文件，路径 workspaces/<id>/workspace.toml
"""
import gc
import json
import logging
import time
from pathlib import Path
from typing import List, Optional

from .models import Workspace

logger = logging.getLogger(__name__)


def _ensure_toml():
    """兼容 Python 3.10 和 3.11+（仅读取）"""
    try:
        import tomllib
        return tomllib
    except ImportError:
        import tomli
        return tomli


_toml = _ensure_toml()


def _toml_dumps(data: dict) -> str:
    """将 dict 序列化为 TOML 字符串。

    优先使用 tomli_w；未安装时退化为内置序列化器并给出告警。
    """
    try:
        import tomli_w
        # 统一包一层 [workspace]：与内置序列化器、与磁盘上已有文件格式保持一致
        return tomli_w.dumps({"workspace": data})
    except ImportError:
        # Python 3.11+ 有 tomllib 但只读；tomli 也只读 → 只能降级
        logger.warning("未安装 tomli_w，已退化为内置 TOML 序列化器（建议：pip install tomli_w）")
        return _simple_toml_dumps(data)


def _simple_toml_dumps(data: dict) -> str:
    """内置 TOML 序列化器（无第三方依赖时的降级方案）。

    字符串一律使用 JSON 转义：JSON 的字符串转义规则是 TOML 基本字符串（basic string）
    的合法子集，因此生成的内容一定能被 TOML 解析器原样读回。
    （旧实现在字符串里直接拼双引号、换行用三引号，导致 persona 中含英文双引号时
    写出非法 TOML，下次启动解析即崩溃。）
    """
    lines = ["[workspace]"]
    for k, v in data.items():
        # 注意：bool 必须先于 int 判断（bool 是 int 的子类）
        if isinstance(v, bool):
            lines.append(f"{k} = {str(v).lower()}")
        elif isinstance(v, (int, float)):
            lines.append(f"{k} = {v}")
        elif isinstance(v, str):
            lines.append(f"{k} = {json.dumps(v, ensure_ascii=False)}")
        elif isinstance(v, list):
            items = ", ".join(json.dumps(item, ensure_ascii=False) for item in v)
            lines.append(f"{k} = [{items}]")
        else:
            logger.warning("内置 TOML 序列化器跳过不支持的类型：%s (%s)", k, type(v).__name__)
    return "\n".join(lines) + "\n"


class WorkspaceStorage:
    """工作空间持久化存储（Repository 模式）"""

    def __init__(self, root_dir: str):
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def _get_config_path(self, workspace_id: str) -> Path:
        """获取工作空间的配置文件路径"""
        return self.root_dir / workspace_id / "workspace.toml"

    def _ensure_dirs(self, workspace: Workspace):
        """为工作空间创建必要的子目录"""
        ws_dir = self.root_dir / workspace.id
        ws_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "history").mkdir(exist_ok=True)
        (ws_dir / "kb").mkdir(exist_ok=True)

        # 注入派生路径
        workspace.root_dir = str(ws_dir)
        workspace.history_dir = str(ws_dir / "history")
        workspace.kb_dir = str(ws_dir / "kb")
        workspace.memory_file = str(ws_dir / "memory.json")

    def save(self, workspace: Workspace) -> Workspace:
        """保存工作空间配置（创建或更新）"""
        self._ensure_dirs(workspace)
        config_path = self._get_config_path(workspace.id)
        toml_str = _toml_dumps(workspace.to_dict())
        config_path.write_text(toml_str, encoding="utf-8")
        return workspace

    def load(self, workspace_id: str) -> Optional[Workspace]:
        """加载指定工作空间的配置。

        配置文件损坏时**不抛异常**（否则一个坏文件会让整个程序起不来）：
        备份坏文件 → 用默认值重建 → 打出可读告警。
        """
        config_path = self._get_config_path(workspace_id)
        if not config_path.exists():
            return None

        try:
            with open(config_path, "rb") as f:
                data = _toml.load(f)
        except Exception as e:
            backup = config_path.parent / f"{config_path.name}.bak-{int(time.time())}"
            try:
                config_path.replace(backup)
                logger.error(
                    "工作空间 [%s] 配置解析失败（%s: %s），已备份为 %s 并用默认值重建",
                    workspace_id, type(e).__name__, e, backup.name,
                )
            except OSError:
                logger.error(
                    "工作空间 [%s] 配置解析失败（%s: %s），且备份失败",
                    workspace_id, type(e).__name__, e,
                )
            data = {}

        ws_dict: dict = {}
        if isinstance(data, dict):
            raw = data.get("workspace", data)
            if isinstance(raw, dict):
                ws_dict = raw
        if not ws_dict.get("id"):
            # 文件被写坏或内容缺失：用目录名兜底，避免造出 id 为空的工作空间
            logger.error("工作空间 [%s] 配置缺少有效 id，改用目录名重建", workspace_id)
            ws_dict = {"id": workspace_id, "name": workspace_id}

        try:
            workspace = Workspace.from_dict(ws_dict)
        except Exception as e:
            logger.error(
                "工作空间 [%s] 字段异常（%s: %s），改用默认值",
                workspace_id, type(e).__name__, e,
            )
            workspace = Workspace(id=workspace_id, name=workspace_id)

        self._ensure_dirs(workspace)
        return workspace

    def list_all(self) -> List[Workspace]:
        """列出所有已存在的工作空间"""
        workspaces = []
        if not self.root_dir.exists():
            return workspaces
        for entry in sorted(self.root_dir.iterdir()):
            if entry.is_dir():
                config_path = entry / "workspace.toml"
                if config_path.exists():
                    ws = self.load(entry.name)
                    if ws:
                        workspaces.append(ws)
        return workspaces

    def delete(self, workspace_id: str) -> bool:
        """删除工作空间（包括所有数据）。

        Windows 下若目录内有文件仍被占用（典型：向量库 chroma.sqlite3 句柄未释放），
        rmtree 会失败。这里做几次重试并**优雅降级为返回 False**（不抛异常），
        由调用方给出可读提示，避免"点删除按钮直接报错弹栈"。
        """
        import shutil

        ws_dir = self.root_dir / workspace_id
        if not ws_dir.exists():
            return False

        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                shutil.rmtree(ws_dir)
                return True
            except OSError as e:
                last_error = e
                logger.warning(
                    "删除工作空间 [%s] 第 %d 次失败：%s", workspace_id, attempt + 1, e
                )
                gc.collect()
                time.sleep(0.3)

        logger.error("删除工作空间 [%s] 失败：%s（目录仍被占用）", workspace_id, last_error)
        return False

    def exists(self, workspace_id: str) -> bool:
        """检查工作空间是否存在"""
        return self._get_config_path(workspace_id).exists()
