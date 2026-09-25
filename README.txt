# 妹抖酱 v0.1

本地运行的 AI Agent：**Python + PySide6（Qt 6）桌面应用**。
项目目录：`meidochanv0.1/`（2026-09-12 从 `WhaleGirl_v2/` 独立出来，遗留旧文件仍留在原处）。
多工作空间隔离（知识库 / 历史 / 记忆 / 人设）+ 插件系统 + 向量知识库 / 历史检索 + 长期记忆。

---

## 1. 它是什么

- **多工作空间**：每个空间有自己独立的人设、对话历史、知识库与长期记忆，互不串味（`workspaces/<id>/`）
- **插件系统**：`plugins/` 下每个目录 = 一个插件（`manifest.json` + 入口 py），支持热重载，不改主程序即可扩展
- **流式回复 + 工具调用循环**：模型可自动调用插件工具（联网搜索、知识库检索、文件浏览）后继续作答
- **历史向量检索**：把历史对话切块存入 ChromaDB（Ollama `bge-m3` 嵌入），跨会话回忆相关内容
- **长期记忆 / 概览卡**：会话结束自动抽取用户特征，压成"概览卡"常驻 system prompt
- **文件修改能力**：AI 可写入/精确修改文本文件（白名单 + 自动备份 + 改自身代码自动跑测试、失败回滚）
- **长代码自动折叠**：回复里的代码块默认折叠（>20 行），点标题栏即可展开/收起，可整体复制

---

## 2. 怎么装

### 环境要求

| 依赖 | 说明 |
|:---|:---|
| Python ≥ 3.11 | 使用标准库 `tomllib` 读取配置（本机实测 3.14.6） |
| [Ollama](https://ollama.com) | 本地模型推理，需 `bge-m3`（向量）+ 一个对话模型 |
| 可选 | Tesseract-OCR（图片识别）、HTTP 代理（联网搜索）、Docker（自建 SearXNG） |

### 安装步骤

```powershell
cd <项目目录>

# 1. Python 依赖
python -m pip install -r requirements.txt

# 2. 向量模型（历史检索 / 知识库必需）
ollama pull bge-m3

# 3. 对话模型（与 config/model.toml 里的 model 保持一致）
ollama pull qwen3.5:9b
```

---

## 3. 怎么跑

```powershell
python main.py
# 或双击  启动妹抖酱.bat
```

启动后会直接打开一个无边框桌面窗口（无需浏览器）：左侧导航侧栏、中间聊天区、右侧素材舞台。

首次启动顺序：窗口立即显示 → 加载插件 → 恢复上次活跃的工作空间（没有则建"默认空间"）→ 预热向量模型 → 拉取立绘与列表（约 10-30 秒）。控制台出现 `[READY] WorkspaceManager active` 后即可开始对话。

---

## 4. 怎么配

| 配置文件 | 作用 |
|:---|:---|
| `config/bot.toml` | 机器人名称/版本、`user_id_source`、输入长度上限 `max_input_length`、路径（conversations/memory/sandbox）、日志级别 `[logging] level`、后台调试窗口 `[logging] show_console` |
| `config/model.toml` | 模型服务商：`provider`（`deepseek` 云端 / `ollama` 本地 / `lmstudio` / `custom`）、`base_url`、默认模型、`thinking`、`reasoning_effort`、`max_tokens`、模型别名、重试 |
| `config/persona.toml` | 全局人设（system prompt） |
| `config/identity.toml` | 用户身份 `[user]`、偏好 `[preferences]`、固定规则 `[[pins]]` |
| `config/plugins.toml` | 插件开关（`disabled` 列表）与插件参数 |
| `config/appearance.toml` | 颜色主题（`preset` = 亮色 / 深色 / 初音 / 自定义，自定义时附 18 个语义色）与视觉特效（不透明度 / 虚化 / 景深） |
| `config/live2d.toml` | 立绘模型（`dir` / `definition` / `viewer`）、`state_map`（对话状态 → 动作 / 表情）、点击轮换表情、视线跟随 |
| `plugins/web_search/config.yaml` | 搜索引擎（`duckduckgo`/`searxng`/`google`/`bing`）、代理 `proxy`、`searxng_url`、结果条数 |
| `workspaces/<id>/workspace.toml` | 单个工作空间：`persona_prompt`（非空时覆盖全局人设）、`plugin_enabled` |

> 人设可以写成多行文本；含英文双引号也没问题（写入时会自动转义）。

### Live2D 的动作 / 表情怎么填

「设置 → Live2D」里两处都要填动作 / 表情名，**都从列表里选**，不用再去翻模型目录手抄文件名：

**① 对话状态 → 动作 / 表情**（两列可编辑下拉框）

- 候选由 `core/live2d_assets` 扫描模型目录得出（自带模型：8 个动作、44 个表情）；
  超过 14 项时弹出框自动滚动；增删了动作文件后点「重新扫描模型目录」刷新
- 填的是**基名**：`motions/自拍.motion3.json` → `自拍`，`吐舌.exp3.json` → `吐舌`
- 清单里没有的名字仍可手输（换了模型、或文件还没扫到）；保存时会提示哪些名字没找到，
  但**不阻止**保存 —— 名字对不上不一定是错，可能是有意为之

**② 点击轮换表情**（可滚动的勾选列表）

- 44 个表情列在一个可滚动的勾选框里，**勾选顺序 = 点击立绘时的轮换顺序**；
  下方一行文字按顺序把已选列出来，不用去列表里找
- 列表顺序保持稳定（不因为勾选而跳动）；要调顺序就取消再重新勾
- 配置里出现、但当前模型目录里没有的名字会**保留并标注**「模型里没有」，
  不会因为重建列表而被悄悄删掉

> 两处的名字都走 `core/live2d_assets` —— 与播放器 manifest、自检工具**同一份定义**
> （见 `assets/live2d/viewer/index.html` 的 `stem()`）。所以列表里选得到的名字，
> 播放器一定认得出。

### 凭据安全（重要）

- 配置支持 `${环境变量}` 展开（见 `core/config/loader.py`），**不要把 API Key / token 明文写进任何配置文件**
- **推荐存法**：把密钥写入 `config/apikey.local`（每行 `KEY = "VALUE"`）。妹抖酱启动时会自动读取并注入环境变量，且该文件已被 `.gitignore` 忽略，明文不进版本库
- 在「设置 → 模型」界面填写密钥时，程序会自动把真值写进 `config/apikey.local`，`model.toml` 里只保留 `${MEIDO_站点名_KEY}` 引用（见 `core/config/loader.py` 的 `store_api_key`）
- 也可以用系统环境变量，优先级高于 `apikey.local`（已存在的同名变量不会被覆盖）
- ⚠️ 妹抖酱**不会自动加载 `.env` 文件**；`.env.example` 仅列出可用环境变量供参考
- 首次使用可从模板复制：`cp config/apikey.local.example config/apikey.local`

---

## 5. 常用命令

在聊天框输入（`/` 开头）：

| 命令 | 作用 |
|:---|:---|
| `/model [名称]` | 查看当前模型与服务商状态 / 切换模型 |
| `/think on\|off` | 开关思考链 |
| `/effort [档位]` | 查看 / 调整推理强度 |
| `/memory` | 查看概览卡（`/memory clear` 清空） |
| `/history` | 会话概况；`/history new` 开新会话；`/history export markdown\|json\|txt` 导出 |
| `/pin [规则]` | 不带参数=查看固定规则；带参数=固定一条规则（注入 system prompt，重启后仍生效） |
| `/remember <内容>` | 直接把内容写进长期记忆（不依赖模型工具调用） |
| `/plugin_reload` | 重新加载全部插件 |
| `/self_scan` `/self_status` | 查看 / 清空"自我认知"记忆库 |
| `/search` `/deep` `/crawl` `/search_config` | 联网搜索 / 深度抓取 / 递归爬取（`web_search` 插件） |
| `/kb_add` `/kb_search` `/kb_hybrid` `/kb_related` `/kb_status` `/kb_rebuild` `/kb_mcp` | 知识库增删查与混合检索（`knowledge_base` 插件） |
| `/ls` `/view` `/info` | 浏览项目文件（`file_explorer` 插件） |

### 让 AI 改文件（`file_explorer` 插件的写入能力）

AI 可以直接改文件，工具为 `write_file`（整体写入）、`edit_file`（精确片段替换）、`delete_file`（默认禁用）。
在聊天里直接说"把 xx.py 的某段改成…"即可，无需手工下命令。

安全约束（配置都在 `plugins/file_explorer/manifest.json`）：

| 机制 | 说明 |
|:---|:---|
| 白名单 | `config.whitelist`，默认 `~/Desktop` + 项目目录；写前 `resolve()` 后校验，防 `../` 穿越 |
| 自动备份 | 覆盖/删除前备份到 `.meido_backups/`，滚动保留 `max_backups` 份 |
| 自身代码保护 | 命中 `config.write.protected`（`core/**`、`ui_qt/**`、`plugins/**`…）时，写入后自动跑 `verify_command`（默认 `pytest`），**失败即回滚** |
| 两段式确认 | `require_confirm=true` 时先给 diff 预览，需再次调用带 `confirm=true` 才落盘 |
| 审计 | 每次写入记 `data/audit/file_edits.jsonl` |
| 删除 | 已开放；删除前自动备份，不想要就在 manifest 里把 `allow_delete` 改回 `false` |

> `edit_file` 要求 `old` 片段唯一；不唯一时会告诉你出现了几次，把片段写长一点即可。

> ⚠️ **`verify_command` 是当 shell 命令执行的**（`subprocess.run(..., shell=True)`，
> 见 `plugins/file_explorer/main.py`）。它读自 `plugins/file_explorer/manifest.json` ——
> 也就是说**改这个字段，等于给后续的受保护写入挂上一条任意命令**。
> 默认值是 `python -m pytest tests -q`。不需要这层保护就把 `config.write.protected` 清空，
> 那样校验根本不会触发。

---

## 6. 目录结构

```
main.py                 启动入口（PySide6 桌面窗口 + 引擎装配）
core/                   引擎、大脑（LLM 调用/工具循环）、上下文、历史、配置、插件管理、记忆
  paths.py              路径层（APP_DIR / RESOURCE_DIR），所有目录一律走它
  selfcheck.py          启动自检（模型服务 / 向量模型 / API Key → 人话指引）
  logging_utils.py      日志（data/logs/meido.log，2MB × 5 轮转）
ui_qt/                  PySide6 界面（main_window / chat_view / sidebar / stage_view /
                          settings_dialog / theme / media 媒体抽象层）
plugins/                file_explorer / knowledge_base / web_search / static_stand
workspace/              工作空间模型、存储、管理器
workspaces/             【数据】每个工作空间的人设/历史/知识库/记忆
config/                 全部配置（toml）
roles/ skills/          角色文件 / Skills 能力包
docs/                   架构（ARCHITECTURE_V3.txt）、测试清单
tests/                  pytest 测试
build/                  打包配置（meido.spec + build.bat）
data/                   【数据】日志、缓存等运行时产物
dist/                   【产物】打包输出（不入库）
```

---

## 7. 打包成 exe

```powershell
# 一键（推荐）
build\build.bat

# 或手动
python -m pip install "pyinstaller==6.22.2"
python -m PyInstaller build\meido.spec --noconfirm
```

- 产物：`dist/meido/meido.exe`（`--onedir`，无控制台），约 **790 MB**
- **外置目录**（必须与 exe 同级，`build.bat` 会自动复制）：
  `config/ plugins/ workspaces/ assets/ roles/ skills/ data/`
- 交付：把 `dist/meido` 整个目录打包成 zip；或把 `meido.exe` 改名为 `妹抖酱v0.1.exe`（建议 exe 路径保持纯 ASCII）
- 排障：默认**不显示**控制台，所有输出（含历史 `print`）都会写进 `data/logs/meido.log`；
  托盘右键 →「查看日志」/「打开数据目录」可直接打开

> **体积为什么这么大**：Live2D 用 QtWebEngine 播放，`Qt6WebEngineCore.dll` 单文件约 195 MB。
> 这是**必需**的——`ui_qt/media/sources/live2d_source.py` 在模块顶层就 import 它，
> 而这条链在**启动路径**上（`main.py` → `ui_qt.app` → `main_window` → `agent_state` → `ui_qt.media`）。
> 请**不要**把 `PySide6.QtWebEngineCore` / `QtWebEngineWidgets` 加回 `meido.spec` 的 `excludes`。

### 源码分发版：文档是 `.txt` 不是 `.md`

分发给用户的源码包里，`README` 与 `docs/` 下三篇（架构 / 测试清单 / 升级计划）是 **`.txt`** ——
Windows 双击 `.md` 会弹「你要如何打开这个文件？」，用户根本打不开。

⚠️ **每次从源项目同步到 release 之后，必须再跑一次**：

```powershell
python tools\release_docs_to_txt.py          # 幂等，多跑无害
python tools\release_docs_to_txt.py --check  # 只看要做什么，不改动
```

否则同步会把 `.md` 拷回来、把改名悄悄撤销（脚本会检测 `.md` 与 `.txt` 并存的情况并以新同步的为准）。

**唯一不动的是 `skills/translate/SKILL.md`** —— 它是**程序文件**不是文档：
`core/skills.py` 按 `SKILL.md` 这个确切名字加载，改名会让「翻译」技能直接失效。

源项目里这几篇**仍是 `.md`**（GitHub 渲染 / 编辑器友好），两边各自自洽、互不干扰。

### 后台调试窗口（可选开启）

发布版默认没有黑窗口。需要实时看日志与 `print` 时，改 `config/bot.toml`：

```toml
[logging]
show_console = true
```

重启后程序会自己 `AllocConsole()` 分配一个控制台窗口，并把输出重定向过去。
**同一个 exe，不用换产物、不用重新打包。**

为什么不是打包时决定（`console=True`）：那样每次启动都会先由系统分配控制台、
再被程序隐藏，用户会看到一次黑窗闪烁。GUI 子系统本来就不分配控制台，
需要时再新建，启动路径上零闪烁。

打包配置里仍保留 `build.bat debug`（`console=True`）这一变体，用途不同：
它保留的是 **PyInstaller bootloader 阶段**的控制台，也就是「Python 还没跑起来就崩了」
（解包失败、`_internal` 缺文件、缺 VC 运行库）时唯一能看到输出的形态。
`AllocConsole()` 只能覆盖 Python 代码开始执行之后的阶段。

源码分发版同理：`启动妹抖酱.bat` 用 `pythonw` 启动（无窗口），读的是同一个开关。

---

## 8. 测试

```powershell
python -m pytest tests -v
```

覆盖：工作空间创建/切换/隔离/持久化、TOML 读写与损坏自愈、固定规则持久化、system prompt 组装、历史落盘/去重、消息时间戳（落盘补 time / 发送前剥离 / 界面格式化）、颜色主题（预设完整性 / WCAG 对比度 / active 令牌切换 / 外观配置往返）、色轮面板（选色位 / 改色 / 对比度提示）、后台调试窗口开关、配置面板回填与保存、路径解析（APP_DIR）、启动自检（Key/Ollama/模型缺失的人话指引）。

### 颜色主题

「设置 → 外观 → 颜色主题」内置三套：

| 主题 | 说明 |
|:---|:---|
| 亮色 | 默认。Radix slate + indigo |
| 深色 | Radix slate dark + indigo dark |
| 初音 | 青绿 `#39C5BB` 系 |

点「**自定义…**」打开**色轮面板**：左侧列出 18 个语义色（底色与层次 / 文字 / 主色 / 危险色 / 滚动条），
右侧是 HSV 色轮（角度 = 色相、半径 = 饱和度）+ 明度滑杆 + 色值输入，下方用**真实控件与同一套 QSS**
渲染实时预览，并做 7 项 WCAG 对比度检查（不达标会红字提示，但**不阻止**你保存）。

改动任一颜色后主题记为「自定义」，18 个色值随 `config/appearance.toml` 保存；
只选预设名时配置文件里只写预设名——这样预设日后改进能自动跟上，而不是被你机器上的旧副本钉死。

> 初音那套的「实色底上的文字」是**深青**而非白色：`#39C5BB` 是中间调，
> 配白字只有 2.0 对比度，达不到 WCAG AA 4.5。

---

## 9. 常见问题

| 现象 | 原因与解法 |
|:---|:---|
| 搜索报"本机代理 127.0.0.1:7897 连不上" | DuckDuckGo / 远程 SearXNG 需要代理。启动 Clash 等代理，或把 `plugins/web_search/config.yaml` 的 `proxy` 改成可用地址；也可自建 SearXNG 并填 `searxng_url` |
| 搜索/知识库提示"集合为空，已按当前 embedding 维度重建" | 更换过嵌入模型导致维度变化，程序会自动切换到新集合并重建（旧集合保留不删），属正常自愈 |
| 启动时提示"未安装 tomli_w" | 走了内置降级序列化器，功能正常但建议 `python -m pip install tomli_w` |
| 想看实时日志（默认没有黑窗口） | 把 `config/bot.toml` 的 `[logging] show_console` 改成 `true` 再重启，会开一个后台调试窗口；不开也能在 `data/logs/meido.log` 或托盘右键 →「查看日志」里看 |
| 调试窗口里看不到回复正文 | 正文会实时打印（思考链打印完会先打一行 `[正文]` 分隔）；若整段都没出现，检查是否被 `config/bot.toml` 的 `[logging] level` 过滤 |
| 控制台出现 emoji 相关报错 | 已在 `main.py` / `core/brain.py` 做了编码兜底；如仍出现请设置环境变量 `PYTHONUTF8=1` |
| 窗口没有立即出现内容 | 窗口会先显示，插件/工作空间/向量模型就绪需要 10-30 秒；开调试窗口后看有无 `[READY]` 与 `[启动]` 日志 |
| 开了调试窗口后程序崩了、窗口一闪而过 | 崩溃时会强制把窗口亮出来并显示 traceback（见 `main.py` 的兜底），按回车关闭；完整日志在 `data/logs/meido.log` |
| Live2D 不显示，日志里有 `[live2d] 页面加载失败` | QtWebEngine 的 Chromium 沙箱在某些环境（开发容器 / 受限会话）起不来，页面根本加载不了 —— 与妹抖酱代码无关。双击 **`测试WebEngine.bat`** 跑自检：它会分别用「默认」和「`--no-sandbox`」加载一次并实测模型。若只有后者成功，就把环境变量 `QTWEBENGINE_CHROMIUM_FLAGS` 设成 `--no-sandbox` 再启动（**会降低浏览器进程隔离，只在确认需要时用**）。模型加载不出来时会按 `config/live2d.toml` 的 `fallback_to_static` 回退到静态立绘，不会崩 |
| 打包版双击后没有窗口，日志只有一行 `[启动] 控制台：…` | 多半是产物**缺模块**（典型：`PySide6.QtWebEngineCore`）。此时 `main.py` 的崩溃兜底会把 traceback 打进一个临时控制台窗口，**日志文件里看不到**。改用 `build\build.bat debug` 构建排障版即可看到真正报错；确认是缺模块就检查 `build/meido.spec` 的 `excludes` 是否误排除了它 |
| 配置文件写坏了（漏引号、`=` 写成 `:` 之类） | 程序**不会崩**。坏掉的那个文件回落到默认值启动，并弹出「启动自检」告诉你**哪个文件、什么原因、怎么改**。原文件一个字节都不会动，首次发现时另存一份 `config/<文件名>.bak-<时间戳>` 备份内容；把语法改好重启即可生效 |
| 主题改了但重启后又变回亮色 | 外观在「停手 800ms 后」才落盘（特效滑杆是连续触发的，逐次写盘会把磁盘敲烂）。确认 `config/appearance.toml` 可写；自定义主题应能看到 `[theme.tokens]` 段 |
| 自定义的配色看不太清 | 色轮面板底部会列出不达标的组合（WCAG AA 4.5:1）。它**只提示不阻止**——你选了完全自由，但得知道代价 |
| 消息旁边没有时间戳 | 时间戳是**本地字段**：新消息从本版本起开始记录。**本版本之前存下的旧会话没有时间戳，回放时就不显示**（不拿别的时刻冒充）。格式：同一天 `HH:MM`，跨天 `MM-DD HH:MM`，跨年带年份；完整时间戳在鼠标悬停提示里 |
| 点 ✕ 窗口消失但进程还在 | 这是「最小化到托盘」的预期行为（`ui_qt/main_window.py`）：点托盘图标恢复，托盘右键可退出 |
| 托盘图标不显示 | 系统托盘不可用时程序会自动跳过（不报错），关闭窗口即退出进程 |
| 首次启动很慢 | 首次要加载对话模型并预热 `bge-m3`，属正常现象 |
| 某个工作空间打不开 | 其 `workspace.toml` 可能被写坏：程序会自动把坏文件备份为 `workspace.toml.bak-<时间戳>` 并用默认值重建，见日志 |
| 图片识别不可用 | `pytesseract` 需要另装 Tesseract-OCR 可执行文件并加入 PATH |
