"""设置对话框（模型 / 外观 / 日志）+ 首次启动引导（ARCHITECTURE_V3 §5.1 / §5.5）。

首次引导（原 Gradio 版的「向导」）在 §5 未给出具体形态，这里按最小改动保留原功能：
写入 workspaces/.initialized、boot_config.json，并向 __boot__ 记忆库播种自我描述。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.app_info import APP_DISPLAY, APP_VERSION
from core.config.models import (
    LOCAL_PROVIDERS,
    IdentityConfig,
    ModelSite,
    PersonaConfig,
    PluginsConfig,
)
from core.live2d_assets import asset_names, duplicate_names
from core.logging_utils import log_file
from core.paths import app_path, to_app_path

from . import theme
from .theme import APPEARANCE_PRESETS
from .theme_editor import ThemeEditorDialog

SELF_DESC = "# {name}\n我是{name}，一个 AI 助手。{persona}"
TROUBLESHOOT = "# {name} 故障排查\nAPI Key: 设置面板 → 模型 → 填入 → 保存。\n切换模型: /model flash 或 /model pro。"

PROVIDER_LABELS = [
    ("DeepSeek 云端", "deepseek"),
    ("Ollama 本地", "ollama"),
    ("LM Studio 本地", "lmstudio"),
    ("自定义", "custom"),
]


def seed_boot_memory(name: str, persona: str) -> None:
    """向知识库 __boot__ 集合播种「自我描述 + 故障排查」。失败不抛，仅返回。"""
    import chromadb
    from core.history_retrieval import get_embedding_model

    path = app_path("plugins", "knowledge_base", "chroma_db")
    path.mkdir(parents=True, exist_ok=True)
    embedder = get_embedding_model()
    client = chromadb.PersistentClient(path=str(path))
    try:
        client.delete_collection("__boot__")
    except Exception:
        pass
    collection = client.create_collection(
        name="__boot__", metadata={"hnsw:space": "cosine"})
    doc_id = f"boot_{datetime.now().strftime('%Y%m%d')}"
    docs = [SELF_DESC.format(name=name, persona=persona), TROUBLESHOOT.format(name=name)]
    collection.add(
        documents=docs,
        embeddings=embedder.encode(docs).tolist(),
        metadatas=[{"type": "self"}, {"type": "troubleshoot"}],
        ids=[f"{doc_id}_s", f"{doc_id}_t"],
    )


class OnboardingDialog(QDialog):
    """首次启动引导：填写名称与人设。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"欢迎使用 {APP_DISPLAY}")
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        title = QLabel(f"## 欢迎使用 {APP_DISPLAY}")
        title.setTextFormat(Qt.TextFormat.MarkdownText)
        layout.addWidget(title)

        form = QFormLayout()
        self.name_edit = QLineEdit("鲸鱼娘")
        self.persona_edit = QTextEdit()
        self.persona_edit.setPlainText("说话带点口癖，喜欢在句尾加 '噗咕～'，语气温柔俏皮。")
        self.persona_edit.setFixedHeight(70)
        form.addRow("名称", self.name_edit)
        form.addRow("人设", self.persona_edit)
        layout.addLayout(form)

        hint = QLabel("启动后可在「设置 → 模型」中填入 API Key。")
        hint.setProperty("class", "status")
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("开始使用")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("稍后")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple:
        return (self.name_edit.text().strip(),
                self.persona_edit.toPlainText().strip())


class SettingsDialog(QDialog):
    """设置面板：模型 / 外观 / 日志。"""

    appearance_changed = Signal(dict)
    theme_changed = Signal(dict, str)          # (完整令牌表, 预设名或「自定义」)
    model_saved = Signal()
    # 保存了哪一类配置（persona / identity / plugins / live2d），
    # 由主窗口据此触发对应重载
    config_saved = Signal(str)

    def __init__(self, engine, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._engine = engine
        # 主题状态由主窗口持有真值，这里只做镜像（打开面板时 sync_theme 灌进来）
        self._theme_tokens: dict = dict(theme.LIGHT_TOKENS)
        self._theme_preset: str = theme.DEFAULT_THEME_NAME
        self.setWindowTitle(f"设置 — {APP_DISPLAY}")
        self.setMinimumSize(520, 420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        self._build_model_tab()
        self._build_persona_tab()
        self._build_identity_tab()
        self._build_plugins_tab()
        self._build_live2d_tab()
        self._build_appearance_tab()
        self._build_log_tab()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    # ── 模型 ──

    def _build_model_tab(self) -> None:
        self._editing_index: Optional[int] = None    # 正在编辑的站点下标（改名时原地替换）
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        # ── 已添加的站点 / 模型 ──
        site_title = QLabel("已添加的站点 / 模型")
        site_title.setProperty("class", "section")
        layout.addWidget(site_title)

        self.site_list = QListWidget()
        self.site_list.setMinimumHeight(110)
        self.site_list.currentItemChanged.connect(lambda *_: self._on_site_picked())
        layout.addWidget(self.site_list, 1)

        btn_row = QHBoxLayout()
        self.site_new_btn = QPushButton("新增")
        self.site_use_btn = QPushButton("设为当前")
        self.site_del_btn = QPushButton("删除")
        for btn in (self.site_new_btn, self.site_use_btn, self.site_del_btn):
            btn.setProperty("class", "tight")
        self.site_del_btn.setObjectName("danger")
        self.site_new_btn.clicked.connect(self._site_new)
        self.site_use_btn.clicked.connect(self._site_apply)
        self.site_del_btn.clicked.connect(self._site_delete)
        btn_row.addWidget(self.site_new_btn)
        btn_row.addWidget(self.site_use_btn)
        btn_row.addWidget(self.site_del_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        # ── 单个站点的详细配置 ──
        form = QFormLayout()
        form.setSpacing(6)

        self.name_edit = QLineEdit()
        self.provider_combo = QComboBox()
        for label, value in PROVIDER_LABELS:
            self.provider_combo.addItem(label, value)

        self.base_url_edit = QLineEdit()
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText(
            "sk-…（本地服务商留空；云端密钥不会明文写进 model.toml）")

        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setMinimumWidth(220)
        self.probe_btn = QPushButton("探测")
        self.probe_btn.setProperty("class", "tight")
        self.probe_btn.setToolTip("从 Base URL 拉取可用模型列表（本地 Ollama / LM Studio 适用）")
        self.probe_btn.clicked.connect(self._probe_models)
        model_row = QHBoxLayout()
        model_row.addWidget(self.model_combo, 1)
        model_row.addWidget(self.probe_btn)

        self.thinking_check = QCheckBox("开启思考链")
        self.effort_combo = QComboBox()
        for level in ("low", "medium", "high"):
            self.effort_combo.addItem(level, level)

        form.addRow("名称", self.name_edit)
        form.addRow("服务商", self.provider_combo)
        form.addRow("Base URL", self.base_url_edit)
        form.addRow("API Key", self.api_key_edit)
        form.addRow("模型", model_row)
        form.addRow("思考", self.thinking_check)
        form.addRow("推理强度", self.effort_combo)
        layout.addLayout(form)

        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)

        self.save_btn = QPushButton("保存并应用")
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(self._save_model)
        layout.addWidget(self.save_btn)

        self.model_status = QLabel("")
        self.model_status.setProperty("class", "status")
        self.model_status.setWordWrap(True)
        layout.addWidget(self.model_status)

        self.runtime_label = QLabel("")
        self.runtime_label.setProperty("class", "status")
        self.runtime_label.setWordWrap(True)
        layout.addWidget(self.runtime_label)

        layout.addStretch(1)
        self.tabs.addTab(page, "模型")
        self._refresh_sites()

    # ── 站点列表 ──

    def _refresh_sites(self) -> None:
        mc = self._engine.model_config
        self.site_list.blockSignals(True)
        self.site_list.clear()
        for site in mc.sites:
            mark = "● " if site.name == mc.active_site else "○ "
            text = f"{mark}{site.name} · {site.provider} · {site.model or '（未填模型）'} · {site.key_display()}"
            self.site_list.addItem(text)
        self.site_list.blockSignals(False)
        if mc.sites:
            row = next((i for i, s in enumerate(mc.sites) if s.name == mc.active_site), 0)
            self.site_list.setCurrentRow(row)
        self._update_status_line()

    def _current_site(self) -> Optional[ModelSite]:
        mc = self._engine.model_config
        row = self.site_list.currentRow()
        if 0 <= row < len(mc.sites):
            return mc.sites[row]
        return mc.sites[0] if mc.sites else None

    def _on_site_picked(self) -> None:
        site = self._current_site()
        if site is None:
            return
        self._editing_index = self.site_list.currentRow()
        self.name_edit.setText(site.name)
        index = self.provider_combo.findData(site.provider)
        self.provider_combo.setCurrentIndex(index if index >= 0 else 0)
        self.base_url_edit.setText(site.base_url)
        self.api_key_edit.setText(site.api_key or "")
        self.model_combo.clear()
        if site.model:
            self.model_combo.addItem(site.model)
        self.model_combo.setCurrentText(site.model or "")
        self.thinking_check.setChecked(site.thinking)
        self.effort_combo.setCurrentText(site.reasoning_effort or "high")

    def _on_provider_changed(self) -> None:
        """切到本地服务商时，若 Base URL 还没填就自动套用预设端点。"""
        preset = LOCAL_PROVIDERS.get(self.provider_combo.currentData(), {})
        if preset.get("base_url") and not self.base_url_edit.text().strip():
            self.base_url_edit.setText(preset["base_url"])

    def _site_new(self) -> None:
        mc = self._engine.model_config
        name = "新站点"
        suffix = 2
        while any(s.name == name for s in mc.sites):
            name = f"新站点{suffix}"
            suffix += 1
        mc.sites.append(ModelSite(
            name=name, provider="ollama",
            base_url=LOCAL_PROVIDERS["ollama"]["base_url"]))
        self._refresh_sites()
        self.site_list.setCurrentRow(len(mc.sites) - 1)
        self.model_status.setText("已新增站点，填好后点「保存并应用」")

    def _site_delete(self) -> None:
        site = self._current_site()
        if site is None:
            return
        mc = self._engine.model_config
        if len(mc.sites) <= 1:
            self.model_status.setText("至少保留一个站点")
            return
        answer = QMessageBox.question(self, "删除站点", f"确定删除站点「{site.name}」吗？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        mc.sites = [s for s in mc.sites if s.name != site.name]
        if mc.active_site == site.name:
            mc.apply_site(mc.sites[0])
        self._persist(mc, mc.active_site, f"已删除站点「{site.name}」")

    def _site_apply(self) -> None:
        """把选中的站点设为当前并立即落盘。"""
        site = self._current_site()
        if site is None:
            return
        mc = self._engine.model_config
        mc.apply_site(site)
        mc.api_key = self._engine.config.expand_env(site.api_key) or site.api_key
        self._persist(mc, site.name, f"已切换到站点「{site.name}」（{site.model or '未填模型'}）")

    def _probe_models(self) -> None:
        base_url = self.base_url_edit.text().strip()
        try:
            models = self._engine.brain.list_local_models(base_url)
        except Exception as exc:
            self.model_status.setText(f"探测失败：{exc}")
            return
        if not models:
            self.model_status.setText("没探测到模型：确认服务已启动、Base URL 正确")
            return
        current = self.model_combo.currentText()
        self.model_combo.clear()
        self.model_combo.addItems(models)
        self.model_combo.setCurrentText(current)
        self.model_status.setText(f"探测到 {len(models)} 个模型，选定后点「保存并应用」")

    def _update_status_line(self) -> None:
        try:
            self.runtime_label.setText(self._engine.brain.get_status())
        except Exception:
            self.runtime_label.setText("")

    # ── 保存 ──

    def _save_model(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            self.model_status.setText("请填写站点名称")
            return

        mc = self._engine.model_config
        provider = self.provider_combo.currentData()
        preset = LOCAL_PROVIDERS.get(provider, {})

        # 密钥：没改就保留原值；云端密钥真值写 apikey.local，toml 里只留 ${VAR}
        raw_key = self.api_key_edit.text().strip()
        existing = next((s for s in mc.sites if s.name == name), None)
        if not raw_key and existing is not None:
            raw_key = existing.api_key
        stored_key = self._engine.config.store_api_key(name, provider, raw_key)

        site = ModelSite(
            name=name,
            provider=provider,
            base_url=self.base_url_edit.text().strip() or preset.get("base_url", ""),
            api_key=stored_key or preset.get("api_key", ""),
            model=self.model_combo.currentText().strip() or preset.get("default_model", ""),
            thinking=self.thinking_check.isChecked(),
            reasoning_effort=self.effort_combo.currentData() or "high",
            max_tokens=mc.max_tokens,
            aliases=dict(existing.aliases) if existing else {},
        )

        if not self._upsert_site(mc, site):
            return
        mc.apply_site(site)
        mc.api_key = self._engine.config.expand_env(site.api_key) or site.api_key

        key_note = ""
        if stored_key.startswith("${"):
            key_note = "（密钥真值存在 config/apikey.local，未写入 model.toml）"
        self._persist(mc, name,
                      f"已保存并应用：{site.name}（{site.provider} · {site.model}）{key_note}")

    def _upsert_site(self, mc, site: ModelSite) -> bool:
        """写入站点：改名时原地替换（避免留下旧名副本），否则按名字去重后追加。

        返回 False 表示重名冲突（已把原因写进状态栏）。
        """
        index = self._editing_index
        if index is not None and 0 <= index < len(mc.sites):
            old = mc.sites[index]
            if old.name != site.name and any(s.name == site.name for s in mc.sites):
                self.model_status.setText(f"已存在同名站点：{site.name}")
                return False
            mc.sites[index] = site
            return True
        mc.sites = [s for s in mc.sites if s.name != site.name]
        mc.sites.append(site)
        return True

    def _persist(self, mc, active_name: str, message: str) -> None:
        """写盘 + 重新配置客户端 + 刷新界面；失败给可读提示，不抛栈。"""
        try:
            self._engine.config.save_model_config(mc, active_name)
            self._engine.brain.reconfigure_client(mc.api_key, mc.base_url)
            self._engine.brain.current_model = mc.default_model
            self._engine.brain.thinking_enabled = mc.thinking_default
            self._engine.brain.reasoning_effort = mc.reasoning_effort
        except Exception as exc:
            self.model_status.setText(f"保存失败：{exc}")
            return
        self._refresh_sites()
        self.model_status.setText(message + "\n已写入 config/model.toml，重启后仍生效")
        self.model_saved.emit()

    # ── 人设 ──

    def _build_persona_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        form = QFormLayout()
        form.setSpacing(8)

        self.persona_name_edit = QLineEdit()
        self.persona_greeting_edit = QLineEdit()
        self.persona_prompt_edit = QTextEdit()
        self.persona_prompt_edit.setMinimumHeight(170)
        self.persona_prompt_edit.setPlaceholderText(
            "全局人设（system prompt）。工作空间的 persona_prompt 非空时会覆盖这里。")

        form.addRow("名称", self.persona_name_edit)
        form.addRow("问候语", self.persona_greeting_edit)
        form.addRow("全局人设", self.persona_prompt_edit)
        layout.addLayout(form, 1)

        note = QLabel(
            "工作空间可单独覆盖人设。\n"
            "长期记忆（概览卡）由对话自动抽取并注入，无需在这里配置。")
        note.setProperty("class", "status")
        note.setWordWrap(True)
        layout.addWidget(note)

        row = QHBoxLayout()
        save_btn = QPushButton("保存人设")
        save_btn.setObjectName("primary")
        save_btn.clicked.connect(self._save_persona)
        reload_btn = QPushButton("重新载入")
        reload_btn.clicked.connect(self._load_persona_into_form)
        row.addWidget(save_btn)
        row.addWidget(reload_btn)
        row.addStretch(1)
        layout.addLayout(row)

        self.persona_status = QLabel("")
        self.persona_status.setProperty("class", "status")
        self.persona_status.setWordWrap(True)
        layout.addWidget(self.persona_status)

        self.tabs.addTab(page, "人设")
        self._load_persona_into_form()

    def _load_persona_into_form(self) -> None:
        try:
            cfg = self._engine.config.get_persona_config()
        except Exception as exc:
            self.persona_status.setText(f"读取失败：{exc}")
            return
        self.persona_name_edit.setText(cfg.name)
        self.persona_greeting_edit.setText(cfg.greeting)
        self.persona_prompt_edit.setPlainText(cfg.system_prompt)
        self.persona_status.setText("")

    def _save_persona(self) -> None:
        name = self.persona_name_edit.text().strip()
        if not name:
            self.persona_status.setText("请填写名称")
            return
        cfg = PersonaConfig(
            name=name,
            greeting=self.persona_greeting_edit.text().strip(),
            system_prompt=self.persona_prompt_edit.toPlainText(),
        )
        try:
            self._engine.config.save_persona_config(cfg)
        except Exception as exc:
            self.persona_status.setText(f"保存失败：{exc}")
            return
        self.persona_status.setText("已写入 config/persona.toml")
        self.config_saved.emit("persona")

    # ── 身份与偏好 ──

    def _build_identity_tab(self) -> None:
        self._pins_draft: List[dict] = []
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        form = QFormLayout()
        form.setSpacing(8)

        self.identity_name_edit = QLineEdit()
        self.identity_city_edit = QLineEdit()
        self.identity_notes_edit = QTextEdit()
        self.identity_notes_edit.setFixedHeight(56)

        self.identity_lang_combo = QComboBox()
        self.identity_lang_combo.addItem("中文", "zh")
        self.identity_lang_combo.addItem("English", "en")
        self.identity_style_combo = QComboBox()
        for label, value in (("专业严谨", "professional"), ("简洁直接", "concise"),
                             ("轻松随和", "casual")):
            self.identity_style_combo.addItem(label, value)
        self.identity_code_combo = QComboBox()
        for label, value in (("Python", "python"), ("JavaScript", "javascript"),
                             ("TypeScript", "typescript"), ("Go", "go")):
            self.identity_code_combo.addItem(label, value)

        form.addRow("姓名", self.identity_name_edit)
        form.addRow("城市", self.identity_city_edit)
        form.addRow("备注", self.identity_notes_edit)
        form.addRow("语言", self.identity_lang_combo)
        form.addRow("回答风格", self.identity_style_combo)
        form.addRow("代码风格", self.identity_code_combo)
        layout.addLayout(form)

        pin_title = QLabel("固定规则（pins）")
        pin_title.setProperty("class", "section")
        layout.addWidget(pin_title)

        pin_note = QLabel("固定规则会注入到所有 system prompt，优先级最高。")
        pin_note.setProperty("class", "status")
        pin_note.setWordWrap(True)
        layout.addWidget(pin_note)

        self.pin_list = QListWidget()
        self.pin_list.setMinimumHeight(80)
        layout.addWidget(self.pin_list, 1)

        add_row = QHBoxLayout()
        self.pin_edit = QLineEdit()
        self.pin_edit.setPlaceholderText("输入一条规则，回车即添加")
        self.pin_edit.returnPressed.connect(self._add_pin_local)
        self.pin_scope_combo = QComboBox()
        for label, value in (("全局", "global"), ("编码", "coding"),
                             ("对话", "chat"), ("审查", "review")):
            self.pin_scope_combo.addItem(label, value)
        add_btn = QPushButton("添加")
        add_btn.clicked.connect(self._add_pin_local)
        del_btn = QPushButton("删除选中")
        del_btn.clicked.connect(self._remove_pin_local)
        add_row.addWidget(self.pin_edit, 1)
        add_row.addWidget(self.pin_scope_combo)
        add_row.addWidget(add_btn)
        add_row.addWidget(del_btn)
        layout.addLayout(add_row)

        row = QHBoxLayout()
        save_btn = QPushButton("保存身份")
        save_btn.setObjectName("primary")
        save_btn.clicked.connect(self._save_identity)
        reload_btn = QPushButton("重新载入")
        reload_btn.clicked.connect(self._load_identity_into_form)
        row.addWidget(save_btn)
        row.addWidget(reload_btn)
        row.addStretch(1)
        layout.addLayout(row)

        self.identity_status = QLabel("")
        self.identity_status.setProperty("class", "status")
        self.identity_status.setWordWrap(True)
        layout.addWidget(self.identity_status)

        self.tabs.addTab(page, "身份")
        self._load_identity_into_form()

    @staticmethod
    def _select_combo(combo: QComboBox, value: str) -> None:
        """按 data 选中；配置里的值不在候选里时补一项，避免静默丢失。"""
        index = combo.findData(value)
        if index < 0 and value:
            combo.addItem(value, value)
            index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _refresh_pin_list(self) -> None:
        self.pin_list.clear()
        for pin in self._pins_draft:
            self.pin_list.addItem(f"[{pin.get('scope', 'global')}] {pin.get('rule', '')}")

    def _add_pin_local(self) -> None:
        rule = self.pin_edit.text().strip()
        if not rule:
            return
        base = f"pin_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        existing = {p.get("id") for p in self._pins_draft}
        pin_id, suffix = base, 2
        while pin_id in existing:          # 同一秒内连加多条，避免 ID 撞车
            pin_id = f"{base}_{suffix}"
            suffix += 1
        self._pins_draft.append({
            "id": pin_id,
            "rule": rule,
            "scope": self.pin_scope_combo.currentData() or "global",
            "created_at": datetime.now().isoformat(),
        })
        self.pin_edit.clear()
        self._refresh_pin_list()

    def _remove_pin_local(self) -> None:
        row = self.pin_list.currentRow()
        if 0 <= row < len(self._pins_draft):
            self._pins_draft.pop(row)
            self._refresh_pin_list()

    def _load_identity_into_form(self) -> None:
        try:
            cfg = self._engine.context_engine.get_identity_config()
        except Exception as exc:
            self.identity_status.setText(f"读取失败：{exc}")
            return
        self.identity_name_edit.setText(cfg.user_name)
        self.identity_city_edit.setText(cfg.user_city)
        self.identity_notes_edit.setPlainText(cfg.user_notes)
        self._select_combo(self.identity_lang_combo, cfg.language)
        self._select_combo(self.identity_style_combo, cfg.response_style)
        self._select_combo(self.identity_code_combo, cfg.code_style)
        self._pins_draft = [dict(p) for p in cfg.pins]
        self._refresh_pin_list()
        self.identity_status.setText("")

    def _save_identity(self) -> None:
        cfg = IdentityConfig(
            user_name=self.identity_name_edit.text().strip(),
            user_city=self.identity_city_edit.text().strip(),
            user_notes=self.identity_notes_edit.toPlainText().strip(),
            language=self.identity_lang_combo.currentData() or "zh",
            response_style=self.identity_style_combo.currentData() or "professional",
            code_style=self.identity_code_combo.currentData() or "python",
            pins=[dict(p) for p in self._pins_draft],
        )
        try:
            self._engine.context_engine.save_identity_config(cfg)
        except Exception as exc:
            self.identity_status.setText(f"保存失败：{exc}")
            return
        self.identity_status.setText("已写入 config/identity.toml")
        self.config_saved.emit("identity")

    # ── 插件 ──

    def _discover_plugins(self) -> List[tuple]:
        """扫描 plugins/ 下带 manifest.json 的目录，返回 [(名称, 描述)]。"""
        found: List[tuple] = []
        root = app_path("plugins")
        if not root.is_dir():
            return found
        for child in sorted(root.iterdir()):
            manifest = child / "manifest.json"
            if not child.is_dir() or not manifest.exists():
                continue
            desc = ""
            try:
                desc = str(json.loads(manifest.read_text(encoding="utf-8"))
                           .get("description", ""))
            except Exception:
                desc = ""
            found.append((child.name, desc))
        return found

    def _build_plugins_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        title = QLabel("插件开关")
        title.setProperty("class", "section")
        layout.addWidget(title)

        note = QLabel("取消勾选 = 该插件不再加载（写入 plugins.toml 的 disabled 列表）。")
        note.setProperty("class", "status")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.plugin_box = QGroupBox("已安装插件")
        self.plugin_box_layout = QVBoxLayout(self.plugin_box)
        layout.addWidget(self.plugin_box)

        param_title = QLabel("插件参数")
        param_title.setProperty("class", "section")
        layout.addWidget(param_title)

        param_form = QFormLayout()
        param_form.setSpacing(8)
        self.ws_proxy_http_edit = QLineEdit()
        self.ws_proxy_https_edit = QLineEdit()
        self.ip_comfy_edit = QLineEdit()
        self.ip_output_edit = QLineEdit()
        self.ip_timeout_spin = QSpinBox()
        self.ip_timeout_spin.setRange(5, 3600)
        self.ip_timeout_spin.setSuffix(" 秒")
        param_form.addRow("web_search · HTTP 代理", self.ws_proxy_http_edit)
        param_form.addRow("web_search · HTTPS 代理", self.ws_proxy_https_edit)
        param_form.addRow("image_processor · ComfyUI 地址", self.ip_comfy_edit)
        param_form.addRow("image_processor · 输出目录", self.ip_output_edit)
        param_form.addRow("image_processor · 生成超时", self.ip_timeout_spin)
        layout.addLayout(param_form)

        hint = QLabel(
            "web_search 真正生效的代理在 plugins/web_search/config.yaml 的 proxy 段，"
            "这里两项仅为兼容保留，改完请保持两处一致。")
        hint.setProperty("class", "status")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        row = QHBoxLayout()
        save_btn = QPushButton("保存插件配置")
        save_btn.setObjectName("primary")
        save_btn.clicked.connect(self._save_plugins)
        reload_btn = QPushButton("重新载入")
        reload_btn.clicked.connect(self._load_plugins_into_form)
        row.addWidget(save_btn)
        row.addWidget(reload_btn)
        row.addStretch(1)
        layout.addLayout(row)

        self.plugins_status = QLabel("")
        self.plugins_status.setProperty("class", "status")
        self.plugins_status.setWordWrap(True)
        layout.addWidget(self.plugins_status)
        layout.addStretch(1)

        self.tabs.addTab(page, "插件")
        self._load_plugins_into_form()

    def _load_plugins_into_form(self) -> None:
        try:
            cfg = self._engine.config.get_plugins_config()
        except Exception as exc:
            self.plugins_status.setText(f"读取失败：{exc}")
            return
        disabled = {str(n) for n in (cfg.disabled or [])}
        per_plugin = cfg.per_plugin or {}

        while self.plugin_box_layout.count():
            item = self.plugin_box_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.plugin_checks: Dict[str, QCheckBox] = {}
        for name, desc in self._discover_plugins():
            params = per_plugin.get(name)
            enabled = name not in disabled
            if isinstance(params, dict) and params.get("enabled") is False:
                enabled = False
            check = QCheckBox(f"{name} — {desc}" if desc else name)
            check.setChecked(enabled)
            self.plugin_box_layout.addWidget(check)
            self.plugin_checks[name] = check
        if not self.plugin_checks:
            self.plugin_box_layout.addWidget(
                QLabel("（plugins/ 下没有找到带 manifest.json 的插件）"))

        ws = per_plugin.get("web_search") or {}
        ip = per_plugin.get("image_processor") or {}
        self.ws_proxy_http_edit.setText(str(ws.get("proxy_http", "")))
        self.ws_proxy_https_edit.setText(str(ws.get("proxy_https", "")))
        self.ip_comfy_edit.setText(str(ip.get("comfyui_url", "")))
        self.ip_output_edit.setText(str(ip.get("output_dir", "")))
        try:
            self.ip_timeout_spin.setValue(int(ip.get("generation_timeout", 120)))
        except (TypeError, ValueError):
            self.ip_timeout_spin.setValue(120)
        self.plugins_status.setText("")

    def _save_plugins(self) -> None:
        try:
            cfg = self._engine.config.get_plugins_config()
        except Exception as exc:
            self.plugins_status.setText(f"读取失败：{exc}")
            return

        per_plugin = {k: dict(v) for k, v in (cfg.per_plugin or {}).items()
                      if isinstance(v, dict)}
        # 勾选状态同时写进 disabled 列表和各插件的 enabled 字段：
        # 两者都会被 engine._disabled_plugins() 汇总，保持一致才不会互相打架
        disabled: List[str] = []
        for name, check in self.plugin_checks.items():
            on = check.isChecked()
            per_plugin.setdefault(name, {})["enabled"] = on
            if not on:
                disabled.append(name)

        ws = per_plugin.setdefault("web_search", {})
        ws["proxy_http"] = self.ws_proxy_http_edit.text().strip()
        ws["proxy_https"] = self.ws_proxy_https_edit.text().strip()

        ip = per_plugin.setdefault("image_processor", {})
        ip["comfyui_url"] = self.ip_comfy_edit.text().strip()
        ip["output_dir"] = self.ip_output_edit.text().strip()
        ip["generation_timeout"] = int(self.ip_timeout_spin.value())

        cfg.disabled = disabled
        cfg.per_plugin = per_plugin
        try:
            self._engine.config.save_plugins_config(cfg)
        except Exception as exc:
            self.plugins_status.setText(f"保存失败：{exc}")
            return
        self.plugins_status.setText("已写入 config/plugins.toml")
        self.config_saved.emit("plugins")

    # ── Live2D ──

    _LIVE2D_STATES = (
        ("idle", "空闲 / 本轮结束"),
        ("thinking", "正在想（推理流、等首 token）"),
        ("tool", "正在调工具"),
        ("found", "工具都返回了"),
        ("writing", "正文在流"),
        ("error", "出错"),
    )

    def _build_live2d_tab(self) -> None:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        layout = QVBoxLayout(inner)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        model_title = QLabel("模型")
        model_title.setProperty("class", "section")
        layout.addWidget(model_title)

        model_form = QFormLayout()
        model_form.setSpacing(8)
        self.l2d_dir_edit = QLineEdit()
        self.l2d_definition_edit = QLineEdit()
        self.l2d_viewer_edit = QLineEdit()
        self.l2d_fallback_check = QCheckBox("模型加载不出来时回退静态立绘")
        model_form.addRow("模型目录", self.l2d_dir_edit)
        model_form.addRow("定义文件", self.l2d_definition_edit)
        model_form.addRow("播放器页面", self.l2d_viewer_edit)
        model_form.addRow("回退", self.l2d_fallback_check)
        layout.addLayout(model_form)

        path_hint = QLabel("路径写相对项目根的形式，如 assets/live2d/DS鲸鱼娘")
        path_hint.setProperty("class", "status")
        path_hint.setWordWrap(True)
        layout.addWidget(path_hint)

        behavior_title = QLabel("行为与交互")
        behavior_title.setProperty("class", "section")
        layout.addWidget(behavior_title)

        behavior_form = QFormLayout()
        behavior_form.setSpacing(8)
        self.l2d_clear_expr_check = QCheckBox("进入 idle 时清掉当前表情（回到默认脸）")
        # 点击轮换表情：用**可滚动的勾选列表**，不再是逗号分隔的文本框 ——
        # 44 个表情靠手打名字不现实（与下面「状态映射」两列同一个理由）。
        # 勾选顺序 = 点击立绘时的轮换顺序。
        self.l2d_click_expr_list = QListWidget()
        self.l2d_click_expr_list.setMaximumHeight(150)
        self.l2d_click_expr_list.setToolTip("勾选顺序 = 点击立绘时的轮换顺序")
        self.l2d_click_expr_list.itemChanged.connect(self._on_click_expr_toggled)
        #: 已选表情的**有序**真值。列表控件只管勾选状态，顺序由这里维护 ——
        #: 而且配置里可能有当前模型目录里没有的名字（换过模型），那些也必须留住，
        #: 否则一保存就被悄悄删掉了。
        self._click_selected: List[str] = []
        self.l2d_click_expr_status = QLabel("")
        self.l2d_click_expr_status.setProperty("class", "status")
        self.l2d_click_expr_status.setWordWrap(True)
        self.l2d_click_revert_spin = QDoubleSpinBox()
        self.l2d_click_revert_spin.setRange(0.0, 60.0)
        self.l2d_click_revert_spin.setSingleStep(0.5)
        self.l2d_click_revert_spin.setDecimals(1)
        self.l2d_click_revert_spin.setSuffix(" 秒（0 = 不恢复）")
        self.l2d_focus_gain_spin = QDoubleSpinBox()
        self.l2d_focus_gain_spin.setRange(0.0, 1.0)
        self.l2d_focus_gain_spin.setSingleStep(0.05)
        self.l2d_focus_gain_spin.setDecimals(2)
        self.l2d_focus_rest_spin = QDoubleSpinBox()
        self.l2d_focus_rest_spin.setRange(0.0, 60.0)
        self.l2d_focus_rest_spin.setSingleStep(0.1)
        self.l2d_focus_rest_spin.setDecimals(1)
        self.l2d_focus_rest_spin.setSuffix(" 秒（0 = 不回正）")
        behavior_form.addRow("idle 表情", self.l2d_clear_expr_check)
        behavior_form.addRow("点击轮换表情", self.l2d_click_expr_list)
        behavior_form.addRow("", self.l2d_click_expr_status)
        behavior_form.addRow("点击后恢复", self.l2d_click_revert_spin)
        behavior_form.addRow("视线跟随系数", self.l2d_focus_gain_spin)
        behavior_form.addRow("视线回正", self.l2d_focus_rest_spin)
        layout.addLayout(behavior_form)

        state_title = QLabel("对话状态 → 动作 / 表情")
        state_title.setProperty("class", "section")
        layout.addWidget(state_title)

        # ⚠️ QLabel 不渲染 Markdown —— 这里不要写 ** 或反引号，否则会原样显示出来
        state_note = QLabel(
            "从下拉框直接选动作 / 表情，不用去翻文件。\n"
            "填的是「基名」：motions/自拍.motion3.json → 自拍，播放器按这个名字解析。\n"
            "留空 = 不播动作 / 回到模型默认脸；清单里没有的名字也可以手输。")
        state_note.setProperty("class", "status")
        state_note.setWordWrap(True)
        layout.addWidget(state_note)

        scan_row = QHBoxLayout()
        rescan_btn = QPushButton("重新扫描模型目录")
        rescan_btn.setProperty("class", "tight")
        rescan_btn.setToolTip("模型目录里增删了动作 / 表情文件后点这里刷新候选")
        rescan_btn.clicked.connect(self._refresh_live2d_asset_lists)
        scan_row.addWidget(rescan_btn)
        self.l2d_assets_status = QLabel("")
        self.l2d_assets_status.setProperty("class", "status")
        self.l2d_assets_status.setWordWrap(True)
        scan_row.addWidget(self.l2d_assets_status, 1)
        layout.addLayout(scan_row)

        grid = QGridLayout()
        grid.setSpacing(6)
        grid.addWidget(QLabel("状态"), 0, 0)
        grid.addWidget(QLabel("动作"), 0, 1)
        grid.addWidget(QLabel("表情"), 0, 2)
        self.l2d_state_edits: Dict[str, tuple] = {}
        for row_index, (state, label) in enumerate(self._LIVE2D_STATES, start=1):
            motion_combo = self._make_asset_combo("留空 = 不播动作")
            expr_combo = self._make_asset_combo("留空 = 默认脸")
            grid.addWidget(QLabel(f"{state} — {label}"), row_index, 0)
            grid.addWidget(motion_combo, row_index, 1)
            grid.addWidget(expr_combo, row_index, 2)
            self.l2d_state_edits[state] = (motion_combo, expr_combo)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        layout.addLayout(grid)

        # 模型目录改了要重扫 —— 否则下拉框还列着上一个模型的动作名
        self.l2d_dir_edit.editingFinished.connect(self._refresh_live2d_asset_lists)

        row = QHBoxLayout()
        save_btn = QPushButton("保存 Live2D 配置")
        save_btn.setObjectName("primary")
        save_btn.clicked.connect(self._save_live2d)
        reload_btn = QPushButton("重新载入")
        reload_btn.clicked.connect(self._load_live2d_into_form)
        row.addWidget(save_btn)
        row.addWidget(reload_btn)
        row.addStretch(1)
        layout.addLayout(row)

        self.live2d_status = QLabel("")
        self.live2d_status.setProperty("class", "status")
        self.live2d_status.setWordWrap(True)
        layout.addWidget(self.live2d_status)
        layout.addStretch(1)

        self.tabs.addTab(page, "Live2D")
        self._load_live2d_into_form()

    @staticmethod
    def _as_float(value, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    # ── Live2D 资源候选（动作 / 表情下拉框） ──

    @staticmethod
    def _make_asset_combo(placeholder: str) -> QComboBox:
        """动作 / 表情选择框。

        做成**可编辑**的：清单里没有的名字（换了模型、文件还没扫到）仍然能手输，
        不至于把用户卡死。候选多于 `setMaxVisibleItems` 时弹出框自带滚动条 ——
        本项目的模型有 44 个表情，不能滚动根本没法选。
        """
        combo = QComboBox()
        combo.setEditable(True)
        # 回车不要把刚手输的内容塞进候选列表，否则列表会越用越脏
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        combo.setMaxVisibleItems(14)
        combo.addItem("")                      # 空 = 不指定
        combo.lineEdit().setPlaceholderText(placeholder)
        return combo

    @staticmethod
    def _fill_asset_combo(combo: QComboBox, names: List[str]) -> None:
        """重填候选，**保留框里已有的值**。

        保留是必要的：配置里可能是清单里没有的名字（换了模型就会这样），
        或者用户已经手输了一半 —— 刷新不该把它们抹掉。
        """
        current = combo.currentText().strip()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("")
        combo.addItems(names)
        combo.setCurrentText(current)          # 可编辑框接受任意文本
        combo.blockSignals(False)

    def _fill_click_expr_list(self, expressions: List[str]) -> None:
        """重建「点击轮换表情」的勾选列表。

        列表顺序**保持稳定**（模型顺序），不因为勾选而跳动 —— 44 项里勾一下就把
        该项弹到顶部，会让人找不到刚才的位置。已选内容由下方那行状态文字按顺序列出。

        配置里出现、但当前模型目录里没有的名字，也会作为一项加进来并标注 ——
        不加就等于「一保存就把它悄悄删掉」。
        """
        unknown = [n for n in self._click_selected if n not in expressions]
        self.l2d_click_expr_list.blockSignals(True)
        self.l2d_click_expr_list.clear()
        for name in list(expressions) + unknown:
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked
                               if name in self._click_selected
                               else Qt.CheckState.Unchecked)
            if name in unknown:
                item.setText(f"{name}　（模型里没有，按原配置保留）")
                item.setToolTip(
                    "这个名字在当前模型目录里找不到，可能不会生效；取消勾选即可移除")
            self.l2d_click_expr_list.addItem(item)
        self.l2d_click_expr_list.blockSignals(False)
        self._refresh_click_expr_status()

    def _on_click_expr_toggled(self, item: QListWidgetItem) -> None:
        """勾选 / 取消一个表情，维护有序真值。"""
        name = item.data(Qt.ItemDataRole.UserRole) or item.text()
        if item.checkState() == Qt.CheckState.Checked:
            if name not in self._click_selected:
                self._click_selected.append(name)      # 勾选顺序 = 轮换顺序
        elif name in self._click_selected:
            self._click_selected.remove(name)
        self._refresh_click_expr_status()

    def _refresh_click_expr_status(self) -> None:
        """把已选按**顺序**列出来 —— 列表本身保持稳定，不靠位置表达顺序。"""
        if not self._click_selected:
            self.l2d_click_expr_status.setText(
                "未选择：点击立绘不会换表情。勾选顺序 = 轮换顺序。")
            return
        self.l2d_click_expr_status.setText(
            f"已选 {len(self._click_selected)} 个，按此顺序轮换："
            + " → ".join(self._click_selected))

    def _live2d_model_dir(self) -> Path:
        """把「模型目录」输入框的值解析成绝对路径（空值 → 空 Path）。"""
        raw = (self.l2d_dir_edit.text() or "").strip()
        if not raw:
            return Path()
        try:
            return to_app_path(raw)
        except Exception:
            return Path(raw)

    def _refresh_live2d_asset_lists(self) -> None:
        """按当前「模型目录」重扫动作 / 表情，刷新所有候选。

        扫描走 `core.live2d_assets` —— 与播放器 manifest、自检工具**同一份**定义，
        所以下拉框里能选到的名字，播放器一定认得出。
        """
        model_dir = self._live2d_model_dir()
        motions, expressions = asset_names(model_dir)
        for motion_combo, expr_combo in self.l2d_state_edits.values():
            self._fill_asset_combo(motion_combo, motions)
            self._fill_asset_combo(expr_combo, expressions)
        self._fill_click_expr_list(expressions)

        if not str(model_dir) or not model_dir.is_dir():
            self.l2d_assets_status.setText(
                "⚠ 模型目录不存在，候选为空（仍可手输名字）")
            return
        text = f"已读到 {len(motions)} 个动作、{len(expressions)} 个表情"
        dup_motions, dup_expressions = duplicate_names(model_dir)
        if dup_motions or dup_expressions:
            text += (f"；⚠ 有重名 —— 动作 {dup_motions}、表情 {dup_expressions}"
                     "（播放器按基名注册，重名只能取到其中一个）")
        self.l2d_assets_status.setText(text)

    def _load_live2d_into_form(self) -> None:
        try:
            cfg = self._engine.config.get_live2d_config()
        except Exception as exc:
            self.live2d_status.setText(f"读取失败：{exc}")
            return
        model = cfg.get("model") or {}
        behavior = cfg.get("behavior") or {}
        interaction = cfg.get("interaction") or {}
        state_map = cfg.get("state_map") or {}

        self.l2d_dir_edit.setText(str(model.get("dir", "")))
        self.l2d_definition_edit.setText(str(model.get("definition", "")))
        self.l2d_viewer_edit.setText(str(model.get("viewer", "")))
        self.l2d_fallback_check.setChecked(bool(model.get("fallback_to_static", True)))

        self.l2d_clear_expr_check.setChecked(
            bool(behavior.get("clear_expression_on_idle", True)))
        # 「点击轮换表情」的有序真值必须先灌进去，再刷候选 ——
        # 顺序反了，_fill_click_expr_list 就不知道哪些该勾上
        self._click_selected = [str(e) for e in
                                (interaction.get("click_expressions") or []) if str(e)]
        self.l2d_click_revert_spin.setValue(
            self._as_float(interaction.get("click_revert_seconds"), 2.0))
        self.l2d_focus_gain_spin.setValue(
            self._as_float(interaction.get("focus_gain"), 0.35))
        self.l2d_focus_rest_spin.setValue(
            self._as_float(interaction.get("focus_rest_seconds"), 1.2))

        # 先按模型目录刷候选，再灌值 —— 顺序反了会被 _fill_asset_combo 的
        # 「保留原值」逻辑盖掉
        self._refresh_live2d_asset_lists()
        for state, (motion_combo, expr_combo) in self.l2d_state_edits.items():
            entry = state_map.get(state) or {}
            motion_combo.setCurrentText(str(entry.get("motion", "")))
            expr_combo.setCurrentText(str(entry.get("expression", "")))
        self.live2d_status.setText("")

    def _save_live2d(self) -> None:
        # 直接用有序真值：勾选顺序就是轮换顺序，列表控件的位置不参与
        expressions = list(self._click_selected)
        state_map = {
            state: {
                "motion": motion_combo.currentText().strip(),
                "expression": expr_combo.currentText().strip(),
            }
            for state, (motion_combo, expr_combo) in self.l2d_state_edits.items()
        }
        payload = {
            "model": {
                "dir": self.l2d_dir_edit.text().strip(),
                "definition": self.l2d_definition_edit.text().strip(),
                "viewer": self.l2d_viewer_edit.text().strip(),
                "fallback_to_static": self.l2d_fallback_check.isChecked(),
            },
            "behavior": {
                "clear_expression_on_idle": self.l2d_clear_expr_check.isChecked(),
            },
            "interaction": {
                "click_expressions": [e for e in expressions if e],
                "click_revert_seconds": self.l2d_click_revert_spin.value(),
                "focus_gain": self.l2d_focus_gain_spin.value(),
                "focus_rest_seconds": self.l2d_focus_rest_spin.value(),
            },
            "state_map": state_map,
        }
        try:
            self._engine.config.save_live2d_config(payload)
        except Exception as exc:
            self.live2d_status.setText(f"保存失败：{exc}")
            return
        message = "已写入 config/live2d.toml，立绘正在按新配置重新装配（约 1~3 秒）"
        unknown = self._unknown_live2d_names(state_map)
        if unknown:
            # 只提示不阻止：名字对不上可能是换了模型，也可能是有意为之。
            # 但静默保存的话，用户会对着「配了却没反应」发呆 —— 这正是本页
            # 改成下拉框要消灭的那类困惑。
            message += ("\n⚠ 这些名字在模型目录里没找到，可能不会生效："
                        + "、".join(unknown))
        self.live2d_status.setText(message)
        self.config_saved.emit("live2d")

    def _unknown_live2d_names(self, state_map: dict) -> List[str]:
        """挑出 `state_map` 里在模型目录中找不到的动作 / 表情名。"""
        model_dir = self._live2d_model_dir()
        if not str(model_dir) or not model_dir.is_dir():
            return []                     # 扫不到目录就不下结论，避免误报
        known_motions, known_expressions = asset_names(model_dir)
        unknown: List[str] = []
        for state, entry in state_map.items():
            motion = (entry or {}).get("motion", "")
            expression = (entry or {}).get("expression", "")
            if motion and motion not in known_motions:
                unknown.append(f"{state} 的动作「{motion}」")
            if expression and expression not in known_expressions:
                unknown.append(f"{state} 的表情「{expression}」")
        return unknown

    # ── 外观 ──

    def _build_appearance_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        # ── 颜色主题 ──
        theme_form = QFormLayout()
        theme_form.setSpacing(8)

        self.theme_combo = QComboBox()
        for name in theme.THEMES:
            self.theme_combo.addItem(name)
        self.theme_combo.addItem(theme.CUSTOM_THEME_NAME)
        self.theme_combo.currentTextChanged.connect(self._on_theme_combo)

        self.theme_custom_btn = QPushButton("自定义…")
        self.theme_custom_btn.setToolTip("打开色轮面板，逐个调整 18 个语义色")
        self.theme_custom_btn.clicked.connect(self._open_theme_editor)

        theme_row = QHBoxLayout()
        theme_row.setSpacing(8)
        theme_row.addWidget(self.theme_combo, 1)
        theme_row.addWidget(self.theme_custom_btn)
        theme_form.addRow("颜色主题", theme_row)
        layout.addLayout(theme_form)

        theme_note = QLabel(
            "「自定义…」打开色轮面板：左侧选色位、右侧色轮调色，"
            "下方实时预览并做对比度检查。改过颜色后主题记为「自定义」并随配置保存。")
        theme_note.setProperty("class", "status")
        theme_note.setWordWrap(True)
        layout.addWidget(theme_note)

        # ── 视觉特效 ──
        form = QFormLayout()
        form.setSpacing(8)

        self.preset_combo = QComboBox()
        for name in APPEARANCE_PRESETS:
            self.preset_combo.addItem(name, name)
        self.preset_combo.setCurrentIndex(1)
        self.preset_combo.currentIndexChanged.connect(self._apply_preset)

        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(80, 100)
        self.opacity_slider.setValue(98)
        self.blur_slider = QSlider(Qt.Orientation.Horizontal)
        self.blur_slider.setRange(0, 20)
        self.depth_slider = QSlider(Qt.Orientation.Horizontal)
        self.depth_slider.setRange(0, 20)

        for slider in (self.opacity_slider, self.blur_slider, self.depth_slider):
            slider.valueChanged.connect(self._emit_appearance)

        form.addRow("外观预设", self.preset_combo)
        form.addRow("窗口不透明度", self.opacity_slider)
        form.addRow("背景虚化", self.blur_slider)
        form.addRow("立绘景深", self.depth_slider)
        layout.addLayout(form)

        note = QLabel(
            "毛玻璃（Acrylic）在 M1 未接入，见 docs/ARCHITECTURE_V3 §5.5；"
            "拖动滑杆为近似渲染，松手后精算，避免 4K 背景掉帧。")
        note.setProperty("class", "status")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)

        self.tabs.addTab(page, "外观")

    def _apply_preset(self, _index: int) -> None:
        preset = APPEARANCE_PRESETS.get(self.preset_combo.currentData())
        if not preset:
            return
        for slider, key in (
            (self.opacity_slider, "window_opacity"),
            (self.blur_slider, "backdrop_blur"),
            (self.depth_slider, "stand_depth"),
        ):
            slider.blockSignals(True)
            value = preset[key]
            slider.setValue(int(round(value * 100 if key == "window_opacity" else value)))
            slider.blockSignals(False)
        self._emit_appearance()

    def _emit_appearance(self) -> None:
        self.appearance_changed.emit({
            "window_opacity": self.opacity_slider.value() / 100.0,
            "backdrop_blur": float(self.blur_slider.value()),
            "stand_depth": float(self.depth_slider.value()),
        })

    def sync_appearance(self, appearance: dict) -> None:
        """外部（舞台侧滑杆）改动后回填本面板，保持两处一致。"""
        for slider, key, scale in (
            (self.opacity_slider, "window_opacity", 100),
            (self.blur_slider, "backdrop_blur", 1),
            (self.depth_slider, "stand_depth", 1),
        ):
            if key in appearance:
                slider.blockSignals(True)
                slider.setValue(int(round(appearance[key] * scale)))
                slider.blockSignals(False)

    # ── 主题 ──

    def sync_theme(self, tokens: dict, preset: str) -> None:
        """打开面板时把主窗口的主题状态灌进来（下拉 + 内部镜像）。"""
        self._theme_tokens = dict(tokens or theme.LIGHT_TOKENS)
        self._theme_preset = preset
        self._sync_theme_combo()

    def _sync_theme_combo(self) -> None:
        """下拉跟随当前主题；名字不认识时回落到「自定义」。"""
        self.theme_combo.blockSignals(True)
        self.theme_combo.setCurrentText(self._theme_preset)
        if self.theme_combo.currentText() != self._theme_preset:
            self.theme_combo.setCurrentText(theme.CUSTOM_THEME_NAME)
        self.theme_combo.blockSignals(False)

    def _on_theme_combo(self, name: str) -> None:
        if name not in theme.THEMES:
            return          # 「自定义」不是可选项，只能由色轮面板产生
        self._theme_tokens = theme.tokens_for(name)
        self._theme_preset = name
        self.theme_changed.emit(dict(self._theme_tokens), name)

    def _open_theme_editor(self) -> None:
        dialog = ThemeEditorDialog(self._theme_tokens, self)
        dialog.theme_applied.connect(self._on_theme_edited)
        dialog.exec()

    def _on_theme_edited(self, tokens: dict) -> None:
        """色轮面板点「应用」：色值与某预设完全一致就记预设名，否则记「自定义」。"""
        self._theme_tokens = dict(tokens)
        self._theme_preset = theme.match_preset(tokens) or theme.CUSTOM_THEME_NAME
        self._sync_theme_combo()
        self.theme_changed.emit(dict(self._theme_tokens), self._theme_preset)

    # ── 日志 ──

    def _build_log_tab(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        data_dir = app_path("data").resolve()
        path = log_file()
        size_text = ""
        try:
            if path.exists():
                size_text = f"（{path.stat().st_size / 1024:.0f} KB，轮转上限 2MB × 5 份）"
        except OSError:
            size_text = ""

        info = QLabel(
            f"版本：{APP_DISPLAY}（内部 {APP_VERSION}）\n"
            f"数据目录：{data_dir}\n"
            f"日志文件：{path}{size_text}")
        info.setProperty("class", "status")
        info.setWordWrap(True)
        info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(info)

        row = QHBoxLayout()
        log_btn = QPushButton("查看日志")
        log_btn.setObjectName("primary")
        log_btn.clicked.connect(self._open_log)
        data_btn = QPushButton("打开数据目录")
        data_btn.clicked.connect(lambda: self._open(data_dir))
        row.addWidget(log_btn)
        row.addWidget(data_btn)
        row.addStretch(1)
        layout.addLayout(row)

        # 调试窗口：点一下**立即**分配控制台，不用重启、不改配置。
        # 发布版默认没有黑窗口（GUI 子系统本来就不分配），需要时再 AllocConsole()，
        # 启动路径上零闪烁 —— 见 core/console_window.py。
        console_title = QLabel("调试窗口")
        console_title.setProperty("class", "section")
        layout.addWidget(console_title)

        console_row = QHBoxLayout()
        debug_btn = QPushButton("打开调试窗口")
        debug_btn.setToolTip("立即分配一个控制台窗口，实时看日志与 print。\n"
                             "不用重启，也不改任何配置。")
        debug_btn.clicked.connect(self._open_debug_console)
        console_row.addWidget(debug_btn)
        console_row.addStretch(1)
        layout.addLayout(console_row)

        self.console_status = QLabel("")
        self.console_status.setProperty("class", "status")
        self.console_status.setWordWrap(True)
        layout.addWidget(self.console_status)
        self._refresh_console_status()

        layout.addStretch(1)

        self.tabs.addTab(page, "日志")

    def _open_debug_console(self) -> None:
        """立即分配一个调试控制台（不重启、不改配置）。

        `attach_console()` 是「确保有控制台」：已经有就复用，
        没有才 `AllocConsole()`。所以重复点不会开出第二个窗口。
        """
        try:
            from core.console_window import attach_console
        except Exception as exc:
            QMessageBox.warning(self, "打开调试窗口", f"无法加载控制台模块：{exc}")
            return
        ok = attach_console()
        self._refresh_console_status()
        if not ok:
            QMessageBox.warning(
                self, "打开调试窗口",
                "分配控制台失败。\n\n"
                "· 如果程序是从终端启动的，输出本来就在那个终端里，不需要这个开关；\n"
                "· 非 Windows 平台不支持。\n\n"
                "日志仍然会写进 data/logs/meido.log。")

    def _refresh_console_status(self) -> None:
        """刷新「调试窗口」那行状态。

        ⚠️ 不用 `console_window.describe_state()` —— 它的措辞假定「有控制台
        就等于配置开了 show_console」，那是**启动时**的事实；而这里的按钮
        能在配置为 false 的情况下把窗口开出来，用它会给出错误结论。
        """
        try:
            from core.console_window import allocated_by_us, console_enabled
            opened = allocated_by_us()
            always = console_enabled()
        except Exception:
            opened, always = False, False

        state = "已打开" if opened else "未打开"
        if always:
            tail = "启动时会自动打开（config/bot.toml 的 [logging] show_console = true）"
        else:
            tail = ("启动时不会自动打开 —— 想让每次启动都开，"
                    "把 config/bot.toml 的 [logging] show_console 改成 true")
        self.console_status.setText(f"调试窗口：{state}。{tail}")

    def _open_log(self) -> None:
        from .main_window import MainWindow   # 延迟导入，避免循环依赖

        path = log_file()
        if not path.exists():
            QMessageBox.information(
                self, "日志", f"日志文件尚未生成：\n{path}\n\n运行日志会写入 data/logs/。")
            return
        MainWindow.open_path(path)

    @staticmethod
    def _open(path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
