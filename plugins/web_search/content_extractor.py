"""
正文提取模块
从 HTML 中提取正文，转换为 Markdown 格式
纯函数，零依赖 main.py
"""
import re
import warnings
from bs4 import BeautifulSoup


def _normalize_selectors(raw) -> list:
    if isinstance(raw, str):
        return [s.strip() for s in raw.split(",") if s.strip()]
    if isinstance(raw, list):
        return [s.strip() for s in raw if isinstance(s, str) and s.strip()]
    return []


def _try_readability(html: str) -> str:
    try:
        from readability import Document
        doc = Document(html)
        return doc.summary()
    except ImportError:
        return ""
    except Exception as e:
        print(f" readability 提取失败: {e}")
        return ""


def _html_to_markdown(html_content: str) -> str:
    try:
        from markdownify import markdownify as md
        return md(html_content, heading_style="ATX", strip=["script", "style", "nav", "footer"])
    except ImportError:
        warnings.warn("markdownify 未安装，提取内容将降级为纯文本。建议: pip install markdownify")
        soup = BeautifulSoup(html_content, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        return soup.get_text(separator="\n")
    except Exception as e:
        print(f" markdownify 转换失败: {e}")
        soup = BeautifulSoup(html_content, "html.parser")
        return soup.get_text(separator="\n")


def _apply_presets(soup: BeautifulSoup, url: str, presets: dict) -> BeautifulSoup:
    from urllib.parse import urlparse
    hostname = urlparse(url).hostname or ""

    matched_preset = None
    for domain_pattern, preset in presets.items():
        if domain_pattern.startswith("*."):
            suffix = domain_pattern[2:]
            if hostname.endswith(suffix) or hostname == suffix:
                matched_preset = preset
                break
        elif hostname == domain_pattern:
            matched_preset = preset
            break

    if not matched_preset:
        return soup

    content_sels = _normalize_selectors(matched_preset.get("content_selector", ""))
    exclude_sels = _normalize_selectors(matched_preset.get("exclude_selectors", []))

    for sel in exclude_sels:
        try:
            for tag in soup.select(sel):
                tag.decompose()
        except Exception as e:
            print(f" [content_extractor] 排除选择器 '{sel}' 执行异常: {e}")

    if content_sels:
        selector_str = ", ".join(content_sels)
        try:
            selected = soup.select(selector_str)
            if selected:
                new_soup = BeautifulSoup("", "html.parser")
                for s in selected[:1]:
                    new_soup.append(s)
                return new_soup
        except Exception as e:
            print(f" [content_extractor] 内容选择器 '{selector_str}' 执行异常: {e}")

    return soup


def extract_content(html: str, url: str = "", presets: dict = None) -> tuple:
    if presets is None:
        presets = {}

    soup = BeautifulSoup(html, "html.parser")

    soup_after_presets = _apply_presets(soup, url, presets)

    preset_modified = False
    if presets:
        from urllib.parse import urlparse
        hostname = urlparse(url).hostname or ""
        matched = None
        for domain_pattern, preset in presets.items():
            if domain_pattern.startswith("*."):
                suffix = domain_pattern[2:]
                if hostname.endswith(suffix) or hostname == suffix:
                    matched = preset
                    break
            elif hostname == domain_pattern:
                matched = preset
                break
        if matched and matched.get("content_selector"):
            content_sels = _normalize_selectors(matched.get("content_selector", ""))
            if content_sels:
                preset_modified = True

    if preset_modified:
        readable_html = ""
    else:
        readable_html = _try_readability(html)

    if readable_html:
        md = _html_to_markdown(readable_html)
        plain = BeautifulSoup(readable_html, "html.parser").get_text(separator="\n")
    else:
        work_soup = soup_after_presets if preset_modified else soup
        for tag in work_soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
            tag.decompose()
        body_html = str(work_soup)
        md = _html_to_markdown(body_html)
        plain = work_soup.get_text(separator="\n")

    md = re.sub(r'\n{3,}', '\n\n', md).strip()
    plain = re.sub(r'\n{3,}', '\n\n', plain).strip()

    return md, plain