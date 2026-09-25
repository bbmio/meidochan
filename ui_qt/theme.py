"""设计令牌 + QSS 生成（ARCHITECTURE_V3 §5.1 / §5.5）。

规则：界面颜色、圆角、间距**只能**来自本文件，禁止在控件里散落硬编码颜色。

一个「主题」= 一组同名的 TOKENS 字典。加主题只需往 THEMES 里再加一份；
QSS 生成、气泡自绘（`speech_bubble`）、色轮面板（`theme_editor`）都从
`active_tokens()` 取色，不各自持有副本。

取色参考 Radix Colors，按官方 12 步语义映射：
  step2=细微背景 / step4=悬停背景 / step6=细微分隔 / step8=更强边界
  step9=实色底 / step10=悬停实色 / step11=低对比文字 / step12=高对比文字
亮色选 indigo 而非 blue：blue-9 配白字仅 3.26，达不到 WCAG 4.5；
indigo-9 白字 5.21、浅底文字 4.95，两边达标且保持 step9/step10 的官方语义。
"""
from __future__ import annotations

from typing import Dict, List, Tuple

# ── 令牌的分组与中文名（色轮面板按这个渲染，新增令牌必须同时补这两张表） ──
TOKEN_GROUPS: List[Tuple[str, List[str]]] = [
    ("底色与层次", ["bg", "surface", "sidebar-bg", "surface-hover", "code-bg", "border"]),
    ("文字", ["text", "muted", "disabled", "on-solid"]),
    ("主色", ["accent", "accent-hover", "accent-soft", "accent-text"]),
    ("危险色", ["danger", "danger-soft", "danger-text"]),
    ("滚动条", ["scrollbar"]),
]

TOKEN_LABELS: Dict[str, str] = {
    "bg": "应用背景",
    "surface": "细微背景（卡片 / 浮层）",
    "sidebar-bg": "侧栏 / 舞台背景",
    "surface-hover": "悬停背景",
    "code-bg": "代码块背景",
    "border": "分隔线 / 描边",
    "text": "正文",
    "muted": "次要文字",
    "disabled": "禁用文字",
    "on-solid": "实色底上的文字",
    "accent": "主色（实色底 / 描边）",
    "accent-hover": "主色（悬停）",
    "accent-soft": "主色（选中浅底）",
    "accent-text": "主色文字（浅底上）",
    "danger": "危险色（实色底）",
    "danger-soft": "危险色（浅底）",
    "danger-text": "危险色文字",
    "scrollbar": "滚动条",
}

ALL_TOKEN_KEYS: List[str] = [k for _group, keys in TOKEN_GROUPS for k in keys]


# ── 预设主题 ──

# 亮色（Radix slate + indigo）
LIGHT_TOKENS: Dict[str, str] = {
    "bg": "#FFFFFF",            # app 背景
    "surface": "#F9F9FB",       # slate-2  细微背景（卡片 / 浮层）
    "sidebar-bg": "#F0F0F3",    # slate-3  两侧栏：比 surface 再深一档，
                                #          否则与纯白主区只差 3%，边界几乎看不出来
    "surface-hover": "#E8E8EC", # slate-4  悬停背景
    "code-bg": "#F9F9FB",       # slate-2
    "border": "#D9D9E0",        # slate-6  细微分隔线

    "text": "#1C2024",          # slate-12 高对比文字
    "muted": "#60646C",         # slate-11 低对比文字
    "disabled": "#8B8D98",      # slate-9  禁用态（3.14，达 3.0 且与 muted 仍有区分）
    "on-solid": "#FFFFFF",      # 落在实色底上的文字

    "accent": "#3E63DD",        # indigo-9  实色底 / 描边
    "accent-hover": "#3358D4",  # indigo-10 悬停实色
    "accent-soft": "#EDF2FE",   # indigo-3  选中态浅底
    "accent-text": "#3A5BC7",   # indigo-11 浅底上的强调文字

    "danger": "#CE2C31",        # red-11  白字达 4.5，用于实色底
    "danger-soft": "#FEEBEC",   # red-3
    "danger-text": "#CE2C31",   # red-11  浅底上的危险色文字

    "scrollbar": "#D9D9E0",     # slate-6
}

# 深色（Radix slate dark + indigo dark）
DARK_TOKENS: Dict[str, str] = {
    "bg": "#111113",            # slate-1
    "surface": "#18191B",       # slate-2
    "sidebar-bg": "#212225",    # slate-3
    "surface-hover": "#272A2D", # slate-4
    "code-bg": "#18191B",       # slate-2
    "border": "#363A3F",        # slate-6

    "text": "#EDEEF0",          # slate-12
    "muted": "#B0B4BA",         # slate-11
    "disabled": "#696E77",      # slate-9
    "on-solid": "#FFFFFF",

    "accent": "#3E63DD",        # indigo-9
    "accent-hover": "#5472E4",  # indigo-10（深色下「悬停」要更亮，与亮色相反）
    "accent-soft": "#182449",   # indigo-3 dark
    "accent-text": "#9EB1FF",   # indigo-11 dark

    "danger": "#E5484D",        # red-9 dark
    "danger-soft": "#3B1219",   # red-3 dark
    "danger-text": "#FF9592",   # red-11 dark

    "scrollbar": "#363A3F",
}

# 初音（青绿 #39C5BB 系）
# ⚠️ #39C5BB 是中间调：配白字只有 2.0、当浅底上的文字也只有 2.0，两边都不达标。
#    所以这里拆开用 —— `accent` 保留标志色 #39C5BB 但配**深色**文字（9.3），
#    浅底上的文字另给一个更深的青 `accent-text`（5.8）。
#    这也是为什么 QToolTip 不能再用 on-solid 取反色，见 build_qss 里的说明。
MIKU_TOKENS: Dict[str, str] = {
    "bg": "#FBFEFD",
    "surface": "#F1F8F7",
    "sidebar-bg": "#E6F2F0",
    "surface-hover": "#DCEDEA",
    "code-bg": "#F1F8F7",
    "border": "#BFDCD7",

    "text": "#0C2220",
    "muted": "#3F6B66",
    "disabled": "#7FA39E",
    "on-solid": "#06302B",      # 落在 #39C5BB 上的深青文字

    "accent": "#39C5BB",        # 初音标志色
    "accent-hover": "#2AAFA6",
    "accent-soft": "#DDF5F2",
    "accent-text": "#0F6E56",

    "danger": "#C4342B",
    "danger-soft": "#FCEBEA",
    "danger-text": "#B0332A",

    "scrollbar": "#BFDCD7",
}

THEMES: Dict[str, Dict[str, str]] = {
    "亮色": LIGHT_TOKENS,
    "深色": DARK_TOKENS,
    "初音": MIKU_TOKENS,
}

CUSTOM_THEME_NAME = "自定义"
DEFAULT_THEME_NAME = "亮色"

# 向后兼容：仍指向亮色。新代码请用 active_tokens()。
TOKENS: Dict[str, str] = LIGHT_TOKENS

_active: Dict[str, str] = dict(LIGHT_TOKENS)


def active_tokens() -> Dict[str, str]:
    """当前生效的令牌。

    返回**副本**：调用方（QSS 生成、自绘控件）本来就只读，但真被改了
    也不该污染全局主题状态 —— 一个 `active_tokens()["bg"] = x` 就能
    把整个界面的背景改掉，这种坑不值得留。
    """
    return dict(_active)


def set_active_theme(tokens: Dict[str, str]) -> None:
    """切换生效主题。缺键用亮色补齐，非法色值丢弃 —— 坏配置不该让界面变黑。"""
    global _active
    merged = dict(LIGHT_TOKENS)
    for key in ALL_TOKEN_KEYS:
        value = (tokens or {}).get(key)
        if is_hex_color(value):
            merged[key] = value.upper()
    _active = merged


def tokens_for(name: str) -> Dict[str, str]:
    """预设名 → 令牌副本；未知名字回退亮色。"""
    return dict(THEMES.get(name, LIGHT_TOKENS))


def match_preset(tokens: Dict[str, str]) -> str | None:
    """色值与某个预设**完全一致**时返回它的名字，否则 None。

    用来判断「用户只是选了个预设」还是「改过颜色」—— 后者要存成
    `自定义` 并把 18 个色值一起写进配置，前者只存预设名就够了。
    """
    for name, preset in THEMES.items():
        if all(tokens.get(key) == value for key, value in preset.items()):
            return name
    return None


def is_hex_color(value) -> bool:
    """是否是 #RRGGBB（色轮面板与配置读取共用这一处判定）。"""
    if not isinstance(value, str):
        return False
    raw = value.strip()
    if len(raw) != 7 or not raw.startswith("#"):
        return False
    try:
        int(raw[1:], 16)
    except ValueError:
        return False
    return True


# ── 对比度（WCAG 2.1）──
# 色轮面板用它给用户提示「这组颜色看不清」，不阻断保存 —— 用户选了完全自由，
# 但得知道自己在牺牲什么。

def _channel_luminance(value: int) -> float:
    srgb = value / 255.0
    return srgb / 12.92 if srgb <= 0.03928 else ((srgb + 0.055) / 1.055) ** 2.4


def relative_luminance(color: str) -> float:
    """WCAG 相对亮度。非法色值返回 0（调用方先用 is_hex_color 判过）。"""
    if not is_hex_color(color):
        return 0.0
    raw = color.strip().lstrip("#")
    r, g, b = (int(raw[i:i + 2], 16) for i in (0, 2, 4))
    return (0.2126 * _channel_luminance(r)
            + 0.7152 * _channel_luminance(g)
            + 0.0722 * _channel_luminance(b))


def contrast_ratio(fg: str, bg: str) -> float:
    """前景/背景对比度，1.0 ~ 21.0。"""
    l1, l2 = relative_luminance(fg), relative_luminance(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


# (说明, 前景键, 背景键, 最低要求)
CONTRAST_CHECKS: List[Tuple[str, str, str, float]] = [
    ("正文 / 应用背景", "text", "bg", 4.5),
    ("正文 / 卡片背景", "text", "surface", 4.5),
    ("次要文字 / 应用背景", "muted", "bg", 4.5),
    ("实色底上的文字 / 主色", "on-solid", "accent", 4.5),
    ("主色文字 / 应用背景", "accent-text", "bg", 4.5),
    ("主色文字 / 选中浅底", "accent-text", "accent-soft", 4.5),
    ("危险色文字 / 应用背景", "danger-text", "bg", 4.5),
]


def contrast_report(tokens: Dict[str, str]) -> List[Tuple[str, float, float, bool]]:
    """逐项算对比度。返回 [(说明, 实测, 要求, 是否达标)]。"""
    out = []
    for label, fg_key, bg_key, minimum in CONTRAST_CHECKS:
        ratio = contrast_ratio(tokens.get(fg_key, ""), tokens.get(bg_key, ""))
        out.append((label, ratio, minimum, ratio >= minimum))
    return out

# 圆角阶：xl=窗口 / lg=气泡 / md=输入框与卡片 / sm=按钮
RADIUS = {"sm": 8, "md": 10, "lg": 14, "xl": 16}
SPACING = (4, 8, 12, 16)                 # 统一节奏
# 字族只写**确实存在**的现代字体。
# 注意：Qt 不认 CSS 的 `system-ui` 关键字 —— 实测它会被模糊匹配成 Tahoma
# （为小字号屏幕优化的老字体，字形紧凑、hinting 强，高 DPI 下观感发糙）。
# 另外 Segoe UI 不含中文字形，单独用它会让中文回退到宋体，所以 YaHei UI 排第一。
FONT_FAMILY = '"Microsoft YaHei UI", "Segoe UI", "Noto Sans SC", sans-serif'
FONT_SIZE = 14

TITLEBAR_HEIGHT = 44    # 顶栏高
INPUTAREA_HEIGHT = 64   # 输入区高
SIDEBAR_WIDTH = 232
STAGE_WIDTH = 208
WINDOW_MARGIN = 6       # 无边框窗口的可缩放边缘宽度

# ── 外观预设（§5.5 表格；Acrylic 毛玻璃未在 M1 接入，见阶段回报） ──
APPEARANCE_PRESETS: Dict[str, Dict[str, float]] = {
    "清晰": {"window_opacity": 1.00, "backdrop_blur": 0.0, "stand_depth": 0.0},
    "标准": {"window_opacity": 0.98, "backdrop_blur": 8.0, "stand_depth": 0.0},
    "沉浸": {"window_opacity": 0.92, "backdrop_blur": 16.0, "stand_depth": 2.0},
}


def _rgba(color: str, alpha: float) -> str:
    """把设计令牌里的 #RRGGBB 转成 rgba()，用于半透明面板。

    QSS 不支持给十六进制颜色单独指定透明度，只能写成 rgba。
    """
    raw = (color or "").lstrip("#")
    if len(raw) != 6:
        return color
    try:
        r, g, b = (int(raw[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return color
    return f"rgba({r}, {g}, {b}, {max(0.0, min(1.0, alpha)):.2f})"


def build_qss(tokens: Dict[str, str] | None = None) -> str:
    """生成全局 QSS。

    不传 tokens 就用**当前生效主题**（`active_tokens()`）—— 换肤时先
    `set_active_theme()` 再重新调用本函数即可。
    传 tokens 时以当前主题为底、用传入值覆盖，供色轮面板做**实时预览**。
    """
    t = dict(active_tokens())
    if tokens:
        t.update(tokens)
    r_sm, r_md, r_lg, r_xl = (RADIUS["sm"], RADIUS["md"],
                              RADIUS["lg"], RADIUS["xl"])
    rgba = _rgba

    return f"""
* {{
    font-family: {FONT_FAMILY};
    font-size: {FONT_SIZE}px;
    color: {t['text']};
}}
QWidget#root {{
    background: {t['bg']};
    border: 1px solid {t['border']};
    border-radius: {r_xl}px;
}}
QWidget#titlebar {{ background: transparent; }}
QLabel#brand {{ font-size: 14px; font-weight: 600; }}
QLabel#brand-sub {{ font-size: 11px; color: {t['muted']}; }}

/* ── 侧栏 ── */
QWidget#sidebar {{
    background: {t['sidebar-bg']};
    border-right: 1px solid {t['border']};
    border-top-left-radius: {r_xl}px;
    border-bottom-left-radius: {r_xl}px;
}}
QLabel[class="section"] {{
    font-size: 11px; font-weight: 600; color: {t['muted']};
    letter-spacing: 0.5px; padding: 8px 2px 2px 2px;
}}
QLabel[class="status"] {{ font-size: 11px; color: {t['muted']}; }}
QLabel#session-note {{ font-size: 11px; color: {t['muted']}; }}

/* ── 聊天区 ── */
QWidget#chatpanel {{ background: {t['bg']}; }}
QScrollArea#chatscroll {{ background: {t['bg']}; border: none; }}

/* 消息：assistant 全宽扁平（靠留白分组），user 用低饱和浅底靠右 */
QLabel[class="bubble"] {{ padding: 10px 14px; border-radius: {r_lg}px; }}
QLabel#bubble-user {{
    background: {t['accent-soft']}; color: {t['text']}; border: none;
}}
QLabel#bubble-assistant {{
    background: transparent; border: none; padding: 0; border-radius: 0;
}}
QLabel#bubble-status {{ color: {t['muted']}; font-size: 12px; padding: 2px 12px; }}

/* 消息时间戳：常显，跟着气泡那一侧对齐（旧会话无 time 时不显示） */
QLabel#msg-time {{ font-size: 11px; color: {t['muted']}; padding: 0 2px; }}

/* 空状态（铺在消息区上的浮层） */
QLabel#empty-hint {{ color: {t['muted']}; font-size: 13px; padding: 0 32px; }}

/* 回到最新（贴滚动区右下角的浮层按钮） */
QPushButton#jumpbtn {{
    background: {t['bg']}; color: {t['accent-text']};
    border: 1px solid {t['border']}; border-radius: {r_lg}px;
    padding: 5px 14px; font-size: 12px;
}}
QPushButton#jumpbtn:hover {{
    background: {t['accent-soft']}; border-color: {t['accent']};
}}

/* 流式光标：生成期间在正文末尾闪动 */
QLabel#stream-cursor {{ color: {t['accent']}; font-size: 13px; padding: 0 2px; }}

/* 代码块复制按钮（贴在代码块头部右侧） */
QPushButton#code-copy {{
    border: none; background: transparent; color: {t['muted']};
    font-size: 11px; padding: 5px 10px;
    border-radius: 0; border-top-right-radius: {r_md}px;
}}
QPushButton#code-copy:hover {{ background: {t['surface']}; color: {t['accent-text']}; }}

/* 消息级操作（复制 / 重新生成，悬停时出现） */
QPushButton#msg-action {{
    border: none; background: transparent; color: {t['muted']};
    font-size: 11px; padding: 2px 8px; border-radius: 6px;
}}
QPushButton#msg-action:hover {{
    background: {t['surface-hover']}; color: {t['accent-text']};
}}

/* ── 桌宠配套的迷你对话框 ── */
QWidget#desktopchat {{
    background: {t['bg']};
    border: 1px solid {t['border']};
    border-radius: {r_lg}px;
}}
QWidget#chatbar {{
    background: {t['sidebar-bg']};
    border-top-left-radius: {r_lg}px;
    border-top-right-radius: {r_lg}px;
}}
QLabel#chatbar-title {{ color: {t['text']}; font-size: 12px; font-weight: 600; }}
QPushButton#chatbar-btn {{
    border: none; background: transparent; color: {t['muted']};
    font-size: 11px; padding: 2px 8px; border-radius: {r_sm}px;
}}
QPushButton#chatbar-btn:hover {{ background: {t['surface-hover']}; color: {t['accent-text']}; }}
QPlainTextEdit#chattranscript {{
    background: {t['bg']}; border: none; color: {t['text']};
    font-size: 12px; padding: 8px 10px;
}}
QLabel#chatstatus {{ color: {t['muted']}; font-size: 11px; padding: 2px 10px; }}
QPlainTextEdit#chatinput {{
    background: {t['surface']}; border: 1px solid {t['border']};
    border-radius: {r_md}px; color: {t['text']}; font-size: 12px;
    padding: 6px; margin: 0 8px;
}}
QPlainTextEdit#chatinput:focus {{ border-color: {t['accent']}; }}
QPushButton#chatsend {{
    background: {t['accent']}; color: {t['on-solid']}; border: none;
    border-radius: {r_md}px; padding: 5px 16px; font-size: 12px;
}}
QPushButton#chatsend:hover {{ background: {t['accent-hover']}; }}
QPushButton#chatsend:disabled {{ background: {t['disabled']}; }}
QPushButton#chatstop {{
    background: {t['surface']}; color: {t['text']};
    border: 1px solid {t['border']};
    border-radius: {r_md}px; padding: 5px 14px; font-size: 12px;
}}
QPushButton#chatstop:hover {{ background: {t['surface-hover']}; }}
QPushButton#chatstop:disabled {{ color: {t['disabled']}; }}

/* ── 桌宠立绘下方的输入条（闲时半透明，聚焦/悬停变实） ── */
QWidget#petinput {{ background: transparent; }}
QLineEdit#petinput-edit {{
    background: {rgba(t['surface'], 0.55)};
    border: 1px solid {rgba(t['border'], 0.55)};
    border-radius: {r_md}px;
    color: {t['text']};
    padding: 3px 10px;
    font-size: 12px;
}}
QLineEdit#petinput-edit:focus {{
    background: {rgba(t['surface'], 0.97)};
    border-color: {t['accent']};
}}
QWidget#petinput[tone="active"] QLineEdit#petinput-edit {{
    background: {rgba(t['surface'], 0.97)};
    border-color: {rgba(t['border'], 0.95)};
}}
QWidget#petinput[tone="active"] QLineEdit#petinput-edit:focus {{
    border-color: {t['accent']};
}}
QLineEdit#petinput-edit:disabled {{
    background: {rgba(t['surface'], 0.35)};
    color: {t['muted']};
}}
QPushButton#petinput-toggle {{
    background: {rgba(t['surface'], 0.55)};
    border: 1px solid {rgba(t['border'], 0.55)};
    border-radius: {r_md}px;
    color: {t['muted']};
    font-size: 12px;
    padding: 0;
}}
QPushButton#petinput-toggle:hover {{
    background: {rgba(t['accent'], 0.90)};
    border-color: {t['accent']};
    color: {t['on-solid']};
}}
QWidget#petinput[tone="active"] QPushButton#petinput-toggle {{
    background: {rgba(t['surface'], 0.95)};
    border-color: {rgba(t['border'], 0.95)};
    color: {t['text']};
}}

/* 思考折叠块 */
QToolButton#think-toggle {{
    border: 1px solid transparent; border-radius: {r_sm}px;
    background: transparent; color: {t['muted']};
    font-size: 11px; padding: 3px 8px; text-align: left;
}}
QToolButton#think-toggle:hover {{
    background: {t['surface']}; color: {t['accent-text']};
    border-color: {t['border']};
}}
QLabel#think-body {{
    background: {t['surface']}; border: 1px solid {t['border']};
    border-radius: {r_md}px; color: {t['muted']};
    font-size: 12px; padding: 8px 12px;
}}

/* 可折叠代码块 */
QFrame[class="codeblock"] {{
    background: {t['bg']}; border: 1px solid {t['border']};
    border-radius: {r_md}px;
}}
QToolButton#code-toggle {{
    border: none; background: transparent; color: {t['muted']};
    font-size: 11px; padding: 5px 10px; text-align: left;
    border-top-left-radius: {r_md}px; border-top-right-radius: {r_md}px;
}}
QToolButton#code-toggle:hover {{ background: {t['surface']}; color: {t['accent-text']}; }}
QPlainTextEdit#code-body {{
    border: none; border-top: 1px solid {t['border']};
    border-radius: 0; background: {t['code-bg']}; color: {t['text']};
    font-family: Consolas, "Cascadia Mono", Menlo, monospace; font-size: 12px;
    padding: 8px 12px; selection-background-color: {t['accent']};
}}

/* ── 舞台 ── */
QWidget#stagepanel {{
    background: {t['sidebar-bg']};
    border-left: 1px solid {t['border']};
    border-top-right-radius: {r_xl}px;
    border-bottom-right-radius: {r_xl}px;
}}
QWidget#stage {{ background: transparent; border-radius: {r_md}px; }}

/* ── 输入区 / 通用控件 ── */
QLineEdit, QTextEdit, QPlainTextEdit, QComboBox {{
    background: {t['bg']}; border: 1px solid {t['border']};
    border-radius: {r_md}px; padding: 8px 12px;
    selection-background-color: {t['accent']};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{
    border: 1.5px solid {t['accent']};
}}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background: {t['bg']}; border: 1px solid {t['border']};
    border-radius: {r_sm}px;
    selection-background-color: {t['accent-soft']}; selection-color: {t['text']};
    outline: none;
}}
QPushButton {{
    background: {t['bg']}; color: {t['muted']};
    border: 1px solid {t['border']}; border-radius: {r_sm}px;
    padding: 7px 14px;
}}
QPushButton:hover {{
    background: {t['surface']}; color: {t['accent-text']};
    border-color: {t['accent']};
}}
QPushButton:disabled {{
    color: {t['disabled']}; border-color: {t['border']}; background: {t['surface']};
}}
QPushButton#primary {{
    background: {t['bg']}; color: {t['accent-text']};
    border: 1.5px solid {t['accent']};
}}
QPushButton#primary:hover {{
    background: {t['accent-hover']}; color: {t['on-solid']};
    border-color: {t['accent-hover']};
}}
QPushButton#danger:hover {{
    background: {t['danger-soft']}; color: {t['danger-text']};
    border-color: {t['danger-text']};
}}
QPushButton[class="tight"] {{ padding: 2px 8px; font-size: 12px; }}

QCheckBox {{ spacing: 6px; }}
QSlider::groove:horizontal {{
    height: 4px; background: {t['border']}; border-radius: 2px;
}}
QSlider::handle:horizontal {{
    width: 12px; height: 12px; margin: -5px 0; border-radius: 6px;
    background: {t['accent']};
}}
QSlider::sub-page:horizontal {{ background: {t['accent']}; border-radius: 2px; }}

QScrollBar:vertical {{ background: transparent; width: 6px; margin: 2px 0; }}
QScrollBar::handle:vertical {{ background: {t['scrollbar']}; border-radius: 3px; min-height: 32px; }}
QScrollBar::handle:vertical:hover {{ background: {t['muted']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollBar:horizontal {{ background: transparent; height: 6px; margin: 0 2px; }}
QScrollBar::handle:horizontal {{ background: {t['scrollbar']}; border-radius: 3px; min-width: 32px; }}

/* ── 标题栏按钮 ── */
QPushButton#winbtn {{
    border: none; background: transparent; color: {t['muted']};
    padding: 4px 10px; border-radius: {r_sm}px;
}}
QPushButton#winbtn:hover {{ background: {t['surface']}; color: {t['text']}; }}
QPushButton#winbtn-close:hover {{ background: {t['danger']}; color: {t['on-solid']}; }}
QPushButton#pinbtn {{
    border: none; background: transparent; color: {t['muted']};
    padding: 4px 10px; border-radius: {r_sm}px;
}}
QPushButton#pinbtn:hover {{ background: {t['surface']}; color: {t['text']}; }}
QPushButton#pinbtn:checked {{ background: {t['accent-soft']}; color: {t['accent-text']}; }}

/* ── 会话行 ── */
QFrame[class="session-row"] {{
    background: transparent; border: 1px solid transparent; border-radius: {r_md}px;
}}
QFrame[class="session-row"]:hover {{
    background: {t['surface-hover']}; border-color: {t['border']};
}}
QLabel[class="session-title"] {{ font-size: 12px; font-weight: 600; }}
QLabel[class="session-summary"] {{ font-size: 10px; color: {t['muted']}; }}

QStatusBar {{ background: {t['surface']}; border-top: 1px solid {t['border']};
    border-bottom-left-radius: {r_xl}px; border-bottom-right-radius: {r_xl}px; }}
QStatusBar::item {{ border: none; }}
QDialog {{ background: {t['bg']}; }}
QTabWidget::pane {{ border: 1px solid {t['border']}; border-radius: {r_md}px; }}
QTabBar::tab {{
    padding: 6px 14px; border: 1px solid {t['border']}; border-bottom: none;
    border-top-left-radius: {r_sm}px; border-top-right-radius: {r_sm}px;
    background: {t['surface']}; color: {t['muted']};
}}
QTabBar::tab:selected {{ background: {t['bg']}; color: {t['accent-text']}; }}
QMenu {{
    background: {t['bg']}; border: 1px solid {t['border']}; border-radius: {r_sm}px; padding: 4px;
}}
QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: {t['accent-soft']}; color: {t['text']}; }}
/* 提示条走「反色」：底色取正文色、文字取应用背景色。
   不能用 on-solid —— 那是「实色底上的文字」，初音主题里它是深色，
   会让提示条变成深底深字。 */
QToolTip {{
    background: {t['text']}; color: {t['bg']}; border: none; padding: 4px 8px;
    border-radius: 4px;
}}

/* ── 色轮自定义面板（ui_qt/theme_editor.py） ── */
QFrame#token-row {{
    background: transparent; border: 1px solid transparent; border-radius: {r_sm}px;
}}
QFrame#token-row:hover {{ background: {t['surface-hover']}; border-color: {t['border']}; }}
QFrame#token-row[class="selected"] {{
    background: {t['accent-soft']}; border-color: {t['accent']};
}}
QWidget#theme-footer {{ background: {t['surface']}; border-top: 1px solid {t['border']}; }}
QWidget#preview-frame {{
    background: {t['sidebar-bg']};
    border: 1px solid {t['border']};
    border-radius: {r_md}px;
}}
"""
