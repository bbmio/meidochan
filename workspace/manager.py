"""工作空间管理器 — CRUD + 切换"""
import re
from pathlib import Path
from typing import List, Optional

from core.paths import app_path

from .models import Workspace
from .storage import WorkspaceStorage


def _sanitize_id(name: str) -> str:
    """从名称生成合法的 ASCII ID：仅保留小写字母 + 数字 + 连字符/下划线"""
    # 中文  保留拼音的首字母映射（极简方案：用 hash 的短前缀避免纯数字）
    import hashlib
    ascii_part = re.sub(r"[^a-zA-Z0-9_-]", "", name.lower().replace(" ", "_"))
    if ascii_part and len(ascii_part) >= 3:
        return ascii_part[:32]
    # 纯中文名  用名字的短 hash 生成稳定 ID
    short_hash = hashlib.md5(name.encode()).hexdigest()[:6]
    return f"ws_{short_hash}"


# 特殊名称映射（确保常用名称有友好 ID）
_FRIENDLY_IDS = {
    "默认空间": "default",
}


class WorkspaceManager:
    """管理多工作空间的创建、切换、删除"""

    def __init__(self, root_dir: str | None = None):
        self._root_dir = (Path(root_dir) if root_dir else app_path("workspaces")).resolve()
        self._storage = WorkspaceStorage(str(self._root_dir))
        self._current: Optional[Workspace] = None
        self._state_file = self._root_dir / ".current"

    # ── 当前工作空间 ──

    @property
    def current(self) -> Optional[Workspace]:
        return self._current

    def _save_current_state(self):
        """将当前活跃工作空间的 ID 写入状态文件"""
        self._root_dir.mkdir(parents=True, exist_ok=True)
        if self._current:
            self._state_file.write_text(self._current.id, encoding="utf-8")

    def _load_current_state(self) -> Optional[str]:
        """从状态文件读取上一次活跃的工作空间 ID"""
        if self._state_file.exists():
            ws_id = self._state_file.read_text(encoding="utf-8").strip()
            if ws_id and self._storage.exists(ws_id):
                return ws_id
        return None

    # ── CRUD ──

    def create(
        self,
        name: str,
        persona_prompt: str = "",
        plugin_enabled: Optional[List[str]] = None,
    ) -> Workspace:
        """创建新工作空间"""
        ws_id = _FRIENDLY_IDS.get(name, _sanitize_id(name))

        # ID 冲突处理：追加数字后缀
        if self._storage.exists(ws_id):
            suffix = 2
            while self._storage.exists(f"{ws_id}_{suffix}"):
                suffix += 1
            ws_id = f"{ws_id}_{suffix}"

        workspace = Workspace(
            id=ws_id,
            name=name,
            persona_prompt=persona_prompt,
            plugin_enabled=plugin_enabled or [],
        )
        self._storage.save(workspace)
        print(f"[OK] Workspace created: [{ws_id}] {name}")
        return workspace

    def switch(self, workspace_id: str) -> Workspace:
        """切换到指定工作空间"""
        if workspace_id == self._current.id if self._current else False:
            print(f"[INFO] Already in workspace [{workspace_id}]")
            return self._current

        workspace = self._storage.load(workspace_id)
        if workspace is None:
            raise ValueError(f"Workspace [{workspace_id}] not found")

        self._current = workspace
        self._save_current_state()
        print(f"[SWITCH] Switched to workspace: [{workspace.id}] {workspace.name}")
        return workspace

    def delete(self, workspace_id: str) -> bool:
        """Delete workspace (cannot delete the currently active one)"""
        if self._current and self._current.id == workspace_id:
            raise RuntimeError(
                f"Cannot delete active workspace [{workspace_id}], switch to another first"
            )
        if not self._storage.exists(workspace_id):
            print(f"[WARN] Workspace [{workspace_id}] not found")
            return False

        if not self._storage.delete(workspace_id):
            print(f"[WARN] Workspace [{workspace_id}] 删除失败：目录可能仍被占用（向量库句柄未释放）")
            return False
        print(f"[DEL] Workspace deleted: [{workspace_id}]")
        return True

    def list_all(self) -> List[Workspace]:
        """列出所有工作空间"""
        return self._storage.list_all()

    def get(self, workspace_id: str) -> Optional[Workspace]:
        """按 ID 获取工作空间"""
        return self._storage.load(workspace_id)

    # ── 生命周期 ──

    def start(self):
        """Start workspace manager: restore last active space or auto-create default"""
        saved_id = self._load_current_state()
        if saved_id:
            self._current = self._storage.load(saved_id)
            if self._current:
                print(f"[READY] WorkspaceManager active: [{self._current.id}] {self._current.name}")
                return

        # No state file or space gone -> check existing
        existing = self._storage.list_all()
        if existing:
            self._current = existing[0]
            self._save_current_state()
            print(f"[READY] WorkspaceManager active: [{self._current.id}] {self._current.name}")
            return

        # First launch -> create default space
        self._current = self.create(name="默认空间")
        self._save_current_state()
        print(f"[READY] WorkspaceManager active: [{self._current.id}] {self._current.name}")
