"""妹抖酱桌面界面（PySide6）。

分层：
- theme.py           设计令牌 + QSS
- media/             媒体抽象层（M1 只实现 IMAGE）
- stage_view.py      素材舞台（三层合成）+ 舞台侧控制条
- chat_view.py       聊天区（气泡 / Markdown / 思考折叠 / 输入区）
- sidebar.py         导航侧栏（工作空间 / 会话 / 角色）
- settings_dialog.py 设置与首次引导
- main_window.py     无边框主窗口 + 事件装配
- app.py             引擎桥（后台线程流式）+ 应用启动
"""
