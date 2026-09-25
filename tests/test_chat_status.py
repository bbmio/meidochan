"""引擎状态行识别测试（detect_status）。

回归点：状态行内容里会带工具名，而项目 20 个工具里有 14 个名字含下划线
（write_file / kb_search / mcp_status …）。旧正则用 `[^_]` 排除下划线，
导致这些状态行识别失败，原文 `_ 正在调用 xxx..._` 会漏进聊天气泡。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ui_qt.chat_view import detect_status  # noqa: E402


def test_tool_name_with_underscore_is_recognised():
    """含下划线的工具名必须能识别（本次修复的核心回归点）。"""
    assert detect_status("_ 正在调用 web_search..._") == "正在调用 web_search..."
    assert detect_status("_ 正在调用 mcp_status..._") == "正在调用 mcp_status..."
    assert detect_status("_ 正在调用 kb_rebuild..._") == "正在调用 kb_rebuild..."


def test_multiple_underscores_is_recognised():
    assert detect_status("_ 正在调用 a_b_c_d..._") == "正在调用 a_b_c_d..."


def test_tool_name_without_underscore_is_recognised():
    assert detect_status("_ 正在调用 ls..._") == "正在调用 ls..."


def test_final_reply_status_is_recognised():
    assert detect_status("_ 正在生成最终回复..._") == "正在生成最终回复..."


def test_surrounding_whitespace_is_tolerated():
    assert detect_status("  _ 正在调用 ls..._  \n") == "正在调用 ls..."


def test_markdown_italic_is_not_a_status():
    """Markdown 斜体（下划线紧跟内容、没有空格）不能被误判成状态行。"""
    assert detect_status("_强调_") is None
    assert detect_status("这是 _斜体_ 文字") is None


def test_plain_text_is_not_a_status():
    assert detect_status("普通正文") is None
    assert detect_status("") is None
    assert detect_status(None) is None


def test_requires_space_after_leading_underscore():
    """引擎的状态行固定是「下划线 + 空格 + 内容 + 下划线」。

    下划线后紧跟内容（没有空格）是 Markdown 斜体的写法，不能当状态行，
    否则模型输出的 `_某段强调_` 会被当成状态行从气泡里抽走。
    """
    assert detect_status("_正在调用 ls..._") is None
    assert detect_status("_某个强调_") is None
