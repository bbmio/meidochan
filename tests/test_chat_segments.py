"""聊天区「文本段 / 代码块段」切分测试（代码块折叠功能的基础）。

split_markdown 是纯逻辑：把 Markdown 切成文本段和代码块段，
未闭合的 ``` 也要当成代码块（流式生成中就能折叠）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ui_qt.chat_view import split_markdown  # noqa: E402


def test_plain_text_only():
    assert split_markdown("hello world") == [("text", "hello world")]


def test_text_then_code():
    parts = split_markdown("说明：\n```python\nprint(1)\n```\n结束")
    assert [kind for kind, _ in parts] == ["text", "code", "text"]
    assert parts[1][1] == ("python", "print(1)")
    assert parts[2][1].strip() == "结束"


def test_multiple_code_blocks():
    parts = split_markdown("```\na\n```\n中间\n```js\nb\n```")
    assert [kind for kind, _ in parts] == ["code", "text", "code"]
    assert parts[0][1] == ("", "a")
    assert parts[1][1].strip() == "中间"
    assert parts[2][1] == ("js", "b")


def test_unterminated_fence_is_code():
    """流式中间态：``` 还没闭合，剩余内容整体算代码块。"""
    parts = split_markdown("看这里\n```python\nx = 1\ny = 2\n")
    assert [kind for kind, _ in parts] == ["text", "code"]
    assert parts[1][1][0] == "python"
    assert "x = 1" in parts[1][1][1] and "y = 2" in parts[1][1][1]


def test_empty_segments_dropped():
    parts = split_markdown("```python\nx\n```")
    assert parts == [("code", ("python", "x"))]


def test_empty_input():
    assert split_markdown("") == []
    assert split_markdown(None) == []


def test_indented_code_preserved():
    """代码块里的缩进必须原样保留（否则改文件时会错位）。"""
    code = "def f():\n    return 1\n"
    parts = split_markdown(f"```python\n{code}```")
    assert parts[0][1][1] == "def f():\n    return 1"
