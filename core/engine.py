"""
妹抖酱 核心引擎
连接 Brain、History、PluginManager、配置、工作空间等所有组件。
"""
import re
import traceback
from pathlib import Path
from typing import Generator, Tuple, List, Any

from core.history import HistoryManager, now_stamp
from core.history_retrieval import HistoryVectorStore, warmup_embedding_model
from core.config.loader import ConfigLoader
from core.paths import app_path
from core.plugins.manager import PluginManager
from core.brain import Brain, BUILTIN_TOOLS
from core.context_engine import ContextEngine
from workspace.manager import WorkspaceManager

MAX_INPUT_LENGTH = 8000


def _clean_xml_tags(text: str) -> str:
    text = re.sub(r"<invoke_tool_calls>.*?</invoke_tool_calls>", "", text, flags=re.DOTALL)
    text = re.sub(r"<invoke_invoke[^>]*>.*?</invoke_invoke>", "", text, flags=re.DOTALL)
    text = re.sub(r"<\s*(invoke|invoke_tool_calls)[^>]*/>", "", text)
    # 剥离模型"模仿"出来的思考折叠块（历史里曾带 <details>，模型照抄了一份）
    text = re.sub(r"<details\b.*?</details>", "", text, flags=re.DOTALL | re.IGNORECASE)
    return text.strip()


def _reasoning_display(reasoning: str, body: str) -> str:
    """把思考链包成折叠块附在正文上方。

    流式阶段和最终展示用同一套格式，界面侧 split_reply 才能一致地拆出来。
    折叠块只用于【界面展示】——写入历史的永远是不含折叠块的干净正文，
    否则模型下一轮会照抄一份（实测出现过"双份思考过程"）。
    """
    if not reasoning:
        return body
    return ("<details><summary>[思考过程]</summary>\n\n"
            + reasoning + "\n\n</details>\n\n" + body)


class WhaleGirlEngine:
    def __init__(self, config_dir: str | None = None):
        # 当前对话阶段（由 brain 上报，UI 侧的表情靠它驱动，不再从展示文本反推）
        self.current_phase = "idle"
        # 相对路径一律按 APP_DIR 解析（打包后 = exe 同级目录，见 §4.2）
        config_dir = str(app_path("config")) if config_dir is None else config_dir
        self.config = ConfigLoader(config_dir)
        self.bot_config = self.config.get_bot_config()

        # 工作空间管理器
        self.workspace_mgr = WorkspaceManager(str(app_path("workspaces")))

        # 历史管理器（延迟绑定到工作空间路径）
        self.history = HistoryManager(self.bot_config.conversation_dir)
        self.retrieval = None  # 历史向量检索（_bind_workspace 时创建）

        self.plugin_manager = PluginManager(str(app_path("plugins")),
                                            disabled=self._disabled_plugins())

        self.model_config = self.config.get_model_config()
        self.persona_config = self.config.get_persona_config()

        self.brain = Brain(self.model_config, self.persona_config, self.config)

        # 概览卡抽取使用 brain.quick_chat（双层记忆的概览层）
        from core.memory.profile_cards import set_extract_generator
        set_extract_generator(self.brain.quick_chat)

        # llmenv 式上下文引擎
        self.context_engine = ContextEngine(config_dir)

        # 运行时引用（保持兼容）
        self.pm = self.plugin_manager
        self.hm = self.history

    def start(self):
        print(" 妹抖酱 引擎启动...")
        self.plugin_manager.discover_and_load()
        print(f" 已加载 {len(self.plugin_manager.get_all_plugins())} 个插件")
        # 启动工作空间管理器（恢复上次空间或创建默认空间）
        self.workspace_mgr.start()
        # 启动即后台预加载 embedding 模型（常驻内存，切工作空间不再重复加载）
        warmup_embedding_model()
        # 将引擎组件绑定到当前工作空间路径
        self._bind_workspace()

    def _bind_workspace(self):
        """将引擎组件绑定到当前工作空间的路径"""
        ws = self.workspace_mgr.current
        if ws is None:
            return
        # 历史目录
        self.history.set_directory(ws.history_dir)
        # 历史向量检索（每个工作空间独立向量库）
        # 先释放上一个工作空间的句柄，否则其 chroma.sqlite3 会被一直占用（Windows 下无法删除该空间）
        if self.retrieval is not None:
            try:
                self.retrieval.close()
            except Exception as e:
                print(f"  [向量检索] 释放旧工作空间句柄失败（不影响对话）: {e}")
        self.retrieval = HistoryVectorStore(ws.history_dir)
        # 会话 AI 摘要使用已配置的 brain client
        self.history.set_summary_generator(self.brain.quick_chat)
        # 概览卡文件（双层记忆的概览层）
        from core.memory.profile_cards import set_profile_file
        set_profile_file(str(Path(ws.memory_file).parent / "profile_cards.json"))
        # 人设覆盖
        self.context_engine.set_workspace_persona(ws.persona_prompt)

    def switch_workspace(self, workspace_id: str) -> str:
        """切换工作空间并重新绑定引擎组件"""
        try:
            # 会话结束：先落盘当前空间概览卡，再切换
            from core.memory import profile_cards
            profile_cards.flush_to_disk()
            ws = self.workspace_mgr.switch(workspace_id)
            self._bind_workspace()
            return f" 已切换到工作空间: [{ws.id}] {ws.name}"
        except ValueError as e:
            return f" {e}"

    def new_session(self) -> str:
        """新会话：先固化概览卡（会话结束强制抽取+写盘），再开新会话。"""
        from core.memory import profile_cards
        profile_cards.finalize_session(self.history.load_api_state())
        return self.history.new_session()

    def create_workspace(self, name: str, persona_prompt: str = "") -> str:
        """创建新工作空间"""
        ws = self.workspace_mgr.create(name=name, persona_prompt=persona_prompt)
        return f" 工作空间已创建: [{ws.id}] {ws.name}"

    def delete_workspace(self, workspace_id: str) -> str:
        """删除工作空间"""
        try:
            ok = self.workspace_mgr.delete(workspace_id)
        except RuntimeError as e:
            return f" {e}"
        if not ok:
            return (
                f" 删除失败：工作空间 [{workspace_id}] 的目录仍被占用"
                f"（多半是向量库文件句柄）。请退出程序后手动删除 workspaces/{workspace_id}。"
            )
        return f" 工作空间 [{workspace_id}] 已删除"

    def _disabled_plugins(self) -> list:
        """汇总 plugins.toml 里被禁用的插件名。

        两个来源合并：`[plugins] disabled = [...]` 列表，
        以及各插件段里显式写的 `enabled = false`。
        PluginManager 依据它跳过加载 —— 否则「设置 → 插件」的开关只是摆设。
        """
        try:
            cfg = self.config.get_plugins_config()
        except Exception:
            return []
        disabled = {str(n) for n in (cfg.disabled or [])}
        for name, params in (cfg.per_plugin or {}).items():
            if isinstance(params, dict) and params.get("enabled") is False:
                disabled.add(str(name))
        return sorted(disabled)

    def reload_plugins(self) -> str:
        """按 plugins.toml 重新发现并加载插件。

        设置界面保存插件开关后调用；`/plugin_reload` 命令走同一条路径。
        必须同步 `self.pm` —— 它是 plugin_manager 的兼容别名，漏掉会让
        别名继续指向被丢弃的旧管理器。
        """
        # 先丢 loader 缓存再读：`_disabled_plugins()` 走的是带缓存的
        # get_plugins_config()，而缓存的失效原本只发生在 save_* 内部。
        # 写文件的人一旦不是 save_*（手工改配置、外部脚本），这里就会读到旧值 ——
        # 表现是「开关点了没反应」。重载本就该以磁盘为准。
        self.config.invalidate_cache()
        self.plugin_manager = PluginManager(str(app_path("plugins")),
                                            disabled=self._disabled_plugins())
        self.pm = self.plugin_manager
        self.plugin_manager.discover_and_load()
        return f" 已重新加载 {len(self.plugin_manager.get_all_plugins())} 个插件"

    def reload_persona(self) -> None:
        """重新读取 persona.toml，并让内存快照与缓存同步失效。

        人设有两个持有者，只改文件不动这两处，界面上保存成功、对话里还是旧人设：
          - `self.persona_config` / `brain.persona_config`：初始化时的快照对象；
          - `context_engine._persona_cache`：每轮 build_system_prompt 实际读的缓存。
        """
        self.config.invalidate_cache()   # 同上：以磁盘为准，不依赖写入方清缓存
        self.persona_config = self.config.get_persona_config()
        self.brain.persona_config = self.persona_config
        self.context_engine.invalidate_caches()

    def _execute_command(self, cmd: str, arg: str) -> str | None:
        try:
            if cmd == "/plugin_reload":
                return self.reload_plugins()
            elif cmd == "/memory":
                from core.memory import profile_cards
                if arg.strip() == "clear":
                    profile_cards.clear_cards()
                    return " 概览卡已清空。"
                else:
                    cards = profile_cards.get_cards()
                    if not cards:
                        return " 概览卡为空。对话中告诉我你的名字、偏好，我会记住。"
                    import json
                    return " 当前概览卡：\n" + json.dumps(cards, ensure_ascii=False, indent=2)
            elif cmd == "/remember":
                # 可靠录入记忆：不依赖模型工具调用，直接写入长期记忆知识库
                text = arg.strip()
                if not text:
                    return " 用法：/remember <要记住的内容>"
                from plugins.knowledge_base.main import add_to_memory
                return add_to_memory(text)
            elif cmd == "/model":
                if not arg.strip():
                    return self.brain.get_status()
                return self.brain.set_model(arg.strip())
            elif cmd == "/pin":
                parts = arg.strip().split(maxsplit=1)
                if not parts or not parts[0].strip():
                    pins = self.context_engine.get_pins()
                    if not pins:
                        return " 暂无固定规则。用法：/pin <规则描述>"
                    return " 固定规则：\n" + "\n".join(f"  [{p.get('scope','')}] {p.get('rule','')}" for p in pins)
                return self.context_engine.add_pin(parts[0].strip())
            elif cmd == "/self_scan":
                from core.self_knowledge import load_global_memory, clear_global_memory
                if arg.strip() == "clear":
                    return clear_global_memory()
                data = load_global_memory()
                files = data.get("files", {})
                if not files:
                    return " 全局记忆库为空。请对我说：'读一下你的全部代码'，我会用 /ls 和 /view 逐段查看并建立认知。"
                return f" 我的自我认知记忆共 {len(files)} 条：\n" + "\n".join(
                    f"  • `{fp}`：{info.get('summary','')}" for fp, info in list(files.items())[:30]
                )
            elif cmd == "/self_status":
                from core.self_knowledge import load_global_memory
                data = load_global_memory()
                files = data.get("files", {})
                last = data.get("last_updated", "从未")
                if not files:
                    return " 自我认知记忆为空。可用 /self_scan 查看，或让我'读你的代码'。"
                return f" 自我认知记忆：{len(files)} 个文件已理解，最后更新 {last}。用 /self_scan 查看详情。"
            elif cmd == "/think":
                a = arg.strip().lower()
                if a in ("on", "true", "1", "启用", "开"):
                    return self.brain.set_thinking(True)
                elif a in ("off", "false", "0", "禁用", "关"):
                    return self.brain.set_thinking(False)
                return " 用法：/think on 或 /think off"
            elif cmd == "/effort":
                if not arg.strip():
                    return self.brain.get_status()
                return self.brain.set_effort(arg.strip())
            elif cmd == "/history":
                parts = arg.strip().split(maxsplit=1)
                sub = parts[0].lower() if parts else ""
                sub_arg = parts[1] if len(parts) > 1 else ""
                if sub == "export":
                    fmt = sub_arg.strip() if sub_arg else "markdown"
                    if fmt not in ("markdown", "json", "txt"):
                        return " 导出格式可选：markdown / json / txt"
                    return self.history.export(fmt)
                elif sub == "new":
                    # 会话结束：固化概览卡（强制抽取+写盘），再开新会话
                    return self.new_session()
                elif sub.isdigit():
                    return " 使用界面按钮加载，或输入 /history 查看帮助"
                else:
                    total = self.history.count_messages()
                    sessions = self.history.list_sessions()
                    lines = [f" 对话历史\n\n 当前会话：{total} 条消息\n 历史会话：{len(sessions)} 个（上限 {self.history.max_sessions}）"]
                    if sessions:
                        lines.append("\n最近会话：")
                        for s in sessions[:5]:
                            title = s.get("title") or s.get("name", "")
                            summary = s.get("summary", "") or ""
                            if summary:
                                lines.append(f"  • {title} ({s['count']}条) — {summary[:50]}")
                            else:
                                lines.append(f"  • {title} ({s['count']}条)")
                    return "\n".join(lines)
            else:
                commands = self.plugin_manager.get_tool_commands()
                if cmd in commands:
                    func, plugin_name = commands[cmd]
                    try:
                        if cmd == "/search_config":
                            parts = arg.strip().split(maxsplit=1) if arg.strip() else []
                            return func(key=parts[0] if parts else "", value=parts[1] if len(parts) > 1 else "")
                        return func(arg) if arg else func()
                    except Exception as e:
                        return f" 执行命令时出错: {e}"
                return None
        except Exception as e:
            return f" 执行 {cmd} 时出错: {e}"

    def builtin_executor(self, tool_name: str, arguments: dict) -> str:
        cmd = f"/{tool_name}"
        if tool_name == "model":
            arg = arguments.get("model_name", "")
        elif tool_name == "think":
            enabled = arguments.get("enabled", None)
            arg = "on" if enabled else "off"
        elif tool_name == "effort":
            arg = arguments.get("level", "")
        elif tool_name == "memory":
            action = arguments.get("action", "")
            if action == "clear":
                return " 请直接在输入框输入 `/memory clear` 来清空记忆。"
            arg = action if action else ""
        elif tool_name == "history":
            action = arguments.get("action", "")
            fmt = arguments.get("format", "")
            arg = action
            if fmt:
                arg += f" {fmt}"
        elif tool_name == "load_skill":
            from core.skills import load_skill_content, list_skill_names
            skills_dir = str(app_path("skills"))
            name = arguments.get("name", "")
            content = load_skill_content(name, skills_dir)
            if content is None:
                avail = ", ".join(list_skill_names(skills_dir))
                return f" 未找到 skill：{name}。可用 skills：{avail}"
            return content
        elif tool_name in ("get_status", "plugin_reload"):
            arg = ""
        else:
            arg = ""
        return self._execute_command(cmd, arg) or f" 已执行 {tool_name}"

    def tool_executor(self, tool_name: str, arguments: dict) -> str:
        builtin_names = {t["function"]["name"] for t in BUILTIN_TOOLS}
        if tool_name in builtin_names:
            return self.builtin_executor(tool_name, arguments)
        return self.plugin_manager.execute_tool(tool_name, arguments)

    def _build_system_prompt(self) -> str:
        """构建分层 system prompt（人设/概览卡/角色/pins + 自我认知），供 respond / respond_once 复用。"""
        from core.memory import profile_cards
        profile_text = profile_cards.get_profile_prompt()
        ctx_prompt = self.context_engine.build_system_prompt(profile_cards=profile_text)
        from core.self_knowledge import build_self_knowledge_prompt, build_memory_prompt
        ctx_prompt = ctx_prompt + "\n" + build_self_knowledge_prompt(self.plugin_manager, build_memory_prompt())
        return ctx_prompt

    def delete_session(self, file_path: str) -> str:
        """删除历史会话（含向量片段），返回状态文本"""
        if not file_path:
            return " 未选择会话"
        try:
            result = self.history.delete_session(file_path)
            if self.retrieval is not None:
                from pathlib import Path
                self.retrieval.delete_session(Path(file_path).name)
            return result
        except Exception as e:
            return f" 删除会话失败: {e}"

    def respond(self, user_msg: str, history: list, api_state: list):
        if len(user_msg) > MAX_INPUT_LENGTH:
            user_msg = user_msg[:MAX_INPUT_LENGTH] + "…\n（内容过长已截断）"

        history = history or []
        api_state = api_state or []
        # 页面刷新后 api_state 为空时，从会话文件恢复最近上下文（最多 10 条）
        if not api_state and self.history.count_messages() > 0:
            saved = self.history.load_api_state()
            if saved:
                api_state = saved[-10:]

        if user_msg.startswith("/"):
            parts = user_msg.split(maxsplit=1)
            cmd = parts[0]
            arg = parts[1] if len(parts) > 1 else ""
            result = self._execute_command(cmd, arg)
            if result is None:
                commands = self.plugin_manager.get_tool_commands()
                available = ", ".join(sorted(commands.keys()))
                result = f" 未知命令: {cmd}\n可用命令: {available}"
            api_state.append({"role": "user", "content": user_msg, "time": now_stamp()})
            api_state.append({"role": "assistant", "content": result, "time": now_stamp()})
            # 覆盖式保存（幂等）：与对话分支一致，避免事件重复触发时命令消息双写
            self.history.save_api_state(api_state)
            # 界面历史只追加本轮，不回灌整个上下文——
            # 否则刷新页面后发第一条命令时，旧会话消息会突然全部出现在界面上
            # 命令是即时出结果的，直接进「正文」阶段 —— 否则立绘会一直停在上一轮的状态
            self.current_phase = "writing"
            yield "", (history or []) + [
                {"role": "user", "content": user_msg},
                {"role": "assistant", "content": result},
            ], api_state
            return

        # 本轮时间戳：user 与 assistant 共用同一个 —— 它们属于同一次交互。
        # 分开取会让两条消息差出整个生成时长，界面回放时看着像跨了时间。
        # 这是本地字段（界面显示 / 会话索引），发给模型前由 brain._api_messages 剥离。
        turn_stamp = now_stamp()
        api_state.append({"role": "user", "content": user_msg, "time": turn_stamp})

        # 构建分层 system prompt（人设/概览卡/角色/pins + 自我认知）
        ctx_prompt = self._build_system_prompt()

        # 上下文感知多轮检索历史片段（作为独立消息注入，不拼进 system prompt）
        retrieval_ctx = ""
        if self.retrieval is not None:
            try:
                recent_pairs = self.history.load_recent(6)
                retrieval_ctx = self.retrieval.build_contextual(
                    user_msg, recent_pairs,
                    exclude_current=self.history.current_file.name,
                    current_total_turns=self.history.count_user_turns(),
                    top_k=3,
                )
            except Exception as e:
                print(f" 历史检索失败: {e}")

        full_reply = ""
        full_reasoning = ""
        try:
            # 用副本承载临时注入（system + 历史检索片段），避免污染持久化的 api_state
            chat_messages = list(api_state)
            if retrieval_ctx:
                chat_messages.append({"role": "user", "content": retrieval_ctx})
            event_iter = self.brain.chat_stream(
                chat_messages,
                self.tool_executor,
                system_prompt=ctx_prompt,
                plugin_tools=self.plugin_manager.get_tool_definitions(),
            )
        except RuntimeError as e:
            # API Key 未配置等情况
            error_msg = str(e)
            api_state.append({"role": "assistant", "content": error_msg, "time": turn_stamp})
            self.history.save_api_state(api_state)
            yield "", history + [{"role": "user", "content": user_msg}, {"role": "assistant", "content": error_msg}], api_state
            return

        self.current_phase = "thinking"
        last_display = ""
        for event in event_iter:
            if event["type"] == "phase":
                # 阶段变化必须**也推一次**：工具返回那一刻（found）没有任何正文，
                # 不推的话 UI 要等到下一段输出才知道，这个阶段就被整段跳过了。
                self.current_phase = event["content"]
                tmp = history + [{"role": "user", "content": user_msg},
                                 {"role": "assistant", "content": last_display}]
                yield "", tmp, api_state
            elif event["type"] == "status":
                display = f"_{event['content']}_"
                last_display = display
                tmp = history + [{"role": "user", "content": user_msg}, {"role": "assistant", "content": display}]
                yield "", tmp, api_state
            elif event["type"] == "reasoning":
                full_reasoning += event["content"]
                # 思考链也要流式推给界面：只累加不 yield 的话，折叠块要等整轮结束
                # 才一次性出现，用户在整个思考期间看不到任何进展
                display = _reasoning_display(full_reasoning,
                                             _clean_xml_tags(full_reply))
                last_display = display
                tmp = history + [{"role": "user", "content": user_msg}, {"role": "assistant", "content": display}]
                yield "", tmp, api_state
            elif event["type"] == "text":
                full_reply += event["content"]
                cleaned = _clean_xml_tags(full_reply)
                if not cleaned:
                    continue
                last_display = cleaned
                tmp = history + [{"role": "user", "content": user_msg}, {"role": "assistant", "content": cleaned}]
                yield "", tmp, api_state
            elif event["type"] == "done":
                if not full_reply:
                    full_reply = event["content"]
                full_reply = _clean_xml_tags(full_reply)
                if event.get("reasoning") and not full_reasoning:
                    full_reasoning = event["reasoning"]

        if not full_reply:
            has_tool = any(m.get("role") == "tool" for m in api_state)
            full_reply = " 工具调用完成，但没有生成文字回复噗咕～" if has_tool else "（思考中...噗咕～）"

        # 思考链以折叠块形式附在回复上方（Gradio allow_tags=True 会渲染 <details>）
        # 注意：折叠块只用于【界面展示】；写入历史的是不含折叠块的干净正文——
        # 否则历史里带上 <details> 后模型下一轮会照抄一份（实测出现"双份思考过程"）
        display_reply = _reasoning_display(full_reasoning, full_reply)

        # 覆盖式保存（幂等）：user 已在开头 append，这里补 assistant 后整体写入
        api_state.append({"role": "assistant", "content": full_reply, "time": turn_stamp})
        self.history.save_api_state(api_state)
        # 概览卡定期抽取（双层记忆概览层：字段级合并到内存，会话结束才写盘）
        from core.memory import profile_cards
        profile_cards.schedule_extraction(api_state)

        # 记录本轮对话到历史向量库（幂等 upsert）
        if self.retrieval is not None:
            try:
                u, b = self.history.get_last_turn()
                if u and b:
                    self.retrieval.upsert_turn(
                        self.history.current_file.name, u, b,
                        self.history.count_user_turns(),
                    )
            except Exception as e:
                print(f" 历史向量记录失败: {e}")
        # 每 N 轮后台生成会话 AI 摘要
        try:
            self.history.schedule_session_summary()
        except Exception as e:
            print(f" 会话摘要调度失败: {e}")

        # 界面展示用带折叠块的版本；历史里存的仍是干净正文
        current = history + [{"role": "user", "content": user_msg}, {"role": "assistant", "content": display_reply}]
        yield "", current, api_state

    def respond_once(self, user_msg: str) -> str:
        """无状态单次回复：构建 system prompt + 单轮对话，不持久化历史/概览卡/检索。

        用于外部适配器的硬触发包装（如"天气/搜索"硬触发命中后，把工具结果交给 LLM 包装），
        避免污染持久化的对话历史。
        """
        if len(user_msg) > MAX_INPUT_LENGTH:
            user_msg = user_msg[:MAX_INPUT_LENGTH] + "…\n（内容过长已截断）"

        ctx_prompt = self._build_system_prompt()
        messages = [{"role": "user", "content": user_msg}]
        full_reply = ""
        try:
            event_iter = self.brain.chat_stream(
                messages,
                self.tool_executor,
                system_prompt=ctx_prompt,
                plugin_tools=self.plugin_manager.get_tool_definitions(),
            )
            for event in event_iter:
                if event["type"] == "text":
                    full_reply += event["content"]
                elif event["type"] == "done" and not full_reply:
                    full_reply = event["content"]
        except RuntimeError as e:
            return f" {e}"

        if not full_reply:
            full_reply = "（思考中...）"
        return _clean_xml_tags(full_reply)
