"""
web_search 插件入口
智能联网搜索：多搜索引擎 + 缓存联动 + 深度抓取（续读支持） + 递归爬取
"""
import os
import json
import sys
from pathlib import Path
from typing import Optional

PLUGIN_DIR = Path(__file__).parent
sys.path.insert(0, str(PLUGIN_DIR))


def _load_yaml_config() -> dict:
    yaml_path = PLUGIN_DIR / "config.yaml"
    if yaml_path.exists():
        import yaml
        with open(yaml_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def _load_manifest_config() -> dict:
    manifest_path = PLUGIN_DIR / "manifest.json"
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    return manifest.get("config", {})


def load_config() -> dict:
    cfg = _load_yaml_config()
    if not cfg:
        cfg = _load_manifest_config()
    return cfg


def save_config(cfg: dict):
    import yaml
    yaml_path = PLUGIN_DIR / "config.yaml"
    with open(yaml_path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)


_config_cache: Optional[dict] = None


def get_config() -> dict:
    global _config_cache
    if _config_cache is None:
        _config_cache = load_config()
    return _config_cache


def reload_config():
    global _config_cache
    _config_cache = None


# ========== 缓存管理器（单例） ==========
from cache_manager import get_cache_manager


# ========== 命令实现 ==========

def search_command(query: str, force_refresh: bool = False) -> str:
    """ /search <关键词> [force_refresh] - 快速联网搜索，支持缓存 """
    if not query or not query.strip():
        return " 请输入搜索关键词，用法：/search <关键词>"

    query = query.strip()
    cache_mgr = get_cache_manager()

    # 1. 非强制刷新时先查缓存
    if not force_refresh:
        cached = cache_mgr.search_cache(query)
        if cached:
            return cached

    # 2. 执行搜索
    from search_engine import SearchEngine
    engine = SearchEngine(get_config())
    try:
        results = engine.search(query)
    except Exception as e:
        return f" 搜索失败：{e}"

    # 3. 检查是否为搜索引擎错误（空列表或仅含错误信息）
    if not results:
        return " 没有找到相关结果噗咕～"

    # 检查是否所有结果都是错误标记
    error_titles = {"搜索失败", "DuckDuckGo 搜索失败", "SearX 搜索失败",
                    "Google 搜索失败", "Bing 搜索失败", "Google 搜索超时",
                    "Bing 搜索超时", "错误", "需要配置"}
    if all(any(et in r.get("title", "") for et in error_titles) for r in results):
        # 聚合错误信息
        errors = [r.get("snippet", r.get("title", "")) for r in results]
        return " 搜索失败：" + "；".join(errors)

    # 4. 格式化输出
    MAX_TOTAL_LEN = 3000
    lines = [f" 搜索：{query}\n"]
    current_len = len(lines[0])
    truncated = False
    for i, r in enumerate(results, 1):
        snippet_text = f"{i}. **{r['title']}**\n   {r['snippet']}\n    {r['url']}\n\n"
        if current_len + len(snippet_text) > MAX_TOTAL_LEN:
            lines.append(f"\n... (共 {len(results)} 条结果，仅显示前 {i - 1} 条)")
            truncated = True
            break
        lines.append(snippet_text)
        current_len += len(snippet_text)
    if not truncated:
        lines.append(f"\n共 {len(results)} 条结果")

    # 5. 后台写入缓存
    cache_mgr.add_to_cache(results)

    return "\n".join(lines)


def deep_command(url: str, offset: int = 0) -> str:
    """ /deep <URL> [offset] - 深度抓取单页，支持续读 """
    if not url or not url.strip():
        return " 请输入URL，用法：/deep <URL> [offset]"

    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url

    from deep_crawler import DeepCrawler
    crawler = DeepCrawler(get_config())
    try:
        result = crawler.fetch_page(url)
        if not result:
            return f" 无法抓取该页面：{url}"

        title = result.get("title", "无标题")
        markdown = result.get("markdown", "")
        text_len = len(markdown)
        toc = _extract_headings(markdown)
        internal_links = _extract_internal_links(url, markdown, crawler)

        MAX_DISPLAY = 8000
        display_md = markdown[offset:]
        is_truncated = len(display_md) > MAX_DISPLAY
        if is_truncated:
            display_md = display_md[:MAX_DISPLAY]
            last_newline = display_md.rfind("\n\n")
            if last_newline > MAX_DISPLAY * 0.8:
                display_md = display_md[:last_newline]

        output = f" **{title}**\n {url}\n 总字符数：{text_len}\n"
        output += f"当前显示范围：{offset}-{offset + len(display_md)}\n\n{display_md}"

        if is_truncated:
            next_offset = offset + len(display_md)
            output += f"\n\n... (内容过长已截断。如需继续，可使用 /deep {url} {next_offset} 读取后续内容)"

        if toc:
            output += f"\n\n **页面目录**：\n{toc}"
        if internal_links:
            output += f"\n\n **相关链接（同域名）**：\n{internal_links}"

        return output
    except Exception as e:
        return f" 深度抓取失败：{e}"


def _extract_headings(markdown: str) -> str:
    """提取 Markdown 中的标题"""
    import re
    headings = re.findall(r'^(#{1,6})\s+(.+)$', markdown, re.MULTILINE)
    if not headings:
        return ""
    lines = []
    for level_str, title in headings:
        level = len(level_str)
        indent = "  " * (level - 1)
        lines.append(f"{indent}- {title}")
    return "\n".join(lines)


def _extract_internal_links(base_url: str, markdown: str, crawler) -> str:
    """提取同域名下的 Markdown 链接"""
    from urllib.parse import urlparse
    import re
    domain = urlparse(base_url).netloc
    links = re.findall(r'\[([^\]]+)\]\(([^)]+)\)', markdown)
    internal = []
    for text, url in links:
        if url.startswith("http"):
            if urlparse(url).netloc == domain:
                internal.append(f"- [{text}]({url})")
    if not internal:
        return ""
    return "\n".join(internal[:10])


def crawl_command(arg: str) -> str:
    """ /crawl <URL> [深度] - 递归爬取 """
    if not arg or not arg.strip():
        return " 请输入URL，用法：/crawl <URL> [深度]"

    parts = arg.strip().split()
    url = parts[0]
    depth = 2
    if len(parts) > 1 and parts[1].isdigit():
        depth = int(parts[1])

    if not url.startswith("http"):
        url = "https://" + url

    from deep_crawler import DeepCrawler
    crawler = DeepCrawler(get_config())

    try:
        pages = crawler.crawl_recursive(url, max_depth=depth)
        if not pages:
            return f" 爬取失败或无内容：{url}"

        total_chars = sum(len(p.get("markdown", "")) for p in pages)
        output = f" 递归爬取完成！\n 起始URL：{url}\n 共抓取 {len(pages)} 个页面\n 总字符数：{total_chars}\n"
        output += "--- 页面列表 ---\n"

        MAX_LIST_ITEMS = 20
        for i, p in enumerate(pages[:MAX_LIST_ITEMS], 1):
            output += f"{i}. [{p.get('title', '无标题')}]({p.get('url', '')}) ({len(p.get('markdown', ''))} 字符)\n"
        if len(pages) > MAX_LIST_ITEMS:
            output += f"\n... 还有 {len(pages) - MAX_LIST_ITEMS} 个页面未显示。可使用 /deep <URL> 单独查看特定页面。"

        return output
    except Exception as e:
        return f" 递归爬取失败：{e}"


# ========== 格式化辅助函数 ==========

def _format_full_config(cfg: dict) -> str:
    search = cfg.get("search", {})
    crawler = cfg.get("crawler", {})
    lines = [
        " **搜索插件配置**",
        "",
        f" 搜索引擎：{search.get('default_engine', 'duckduckgo')}（每页 {search.get('max_results', 5)} 条）",
        f" 爬虫参数：深度 {crawler.get('max_depth', 3)} 层 · 上限 {crawler.get('max_pages', 50)} 页 · 限速 {crawler.get('rate_limit', 1.0)}/秒",
        f"⏱ 超时：{crawler.get('timeout', 15)} 秒",
        "",
        " /search_config help 查看抓取续读帮助"
    ]
    return "\n".join(lines)


def _format_help() -> str:
    return (
        " **/search_config 用法：**\n\n"
        "| 命令 | 说明 |\n"
        "|------|------|\n"
        "| `/search_config` | 查看全部配置 |\n"
        "| `/search_config engine <名称>` | 切换搜索引擎 (duckduckgo/searxng/bing_cn/google/bing) |\n"
        "| 提示 | `bing_cn` = 必应中国，国内可直连、无需代理；duckduckgo 在无代理时会自动改走它 |\n"
        "| `/search_config rate <数值>` | 爬虫限速 (0.1~10) |\n"
        "| `/search_config depth <数值>` | 最大递归深度 (1~5) |\n"
        "| `/search` | 快速联网搜索，添加 `force_refresh` 跳过缓存 |\n"
        "| `/deep` | 深度抓取单页，支持续读：/deep <URL> [offset] |\n"
        "| `/crawl` | 递归爬取网站，/crawl <URL> [深度] |\n\n"
        " **续读示例**：\n"
        "```\n"
        "/deep https://example.com/longpage\n"
        "# 若截断提示：... 如需继续，可使用 /deep https://example.com/longpage 8000\n"
        "```\n"
        "系统会自动从上次结束位置继续输出。"
    )


# ========== config_command（结构化参数） ==========

def config_command(key: str = "", value: str = "") -> str:
    """
    /search_config — 查看/修改搜索插件配置
    参数 key 和 value 分别对应配置项名称和新值。
    """
    cfg = get_config()

    if not key:
        return _format_full_config(cfg)

    key = key.lower().strip()

    if key == "engine":
        if not value:
            engine = cfg.get("search", {}).get("default_engine", "duckduckgo")
            engine_names = {
                "duckduckgo": "DuckDuckGo（免费）",
                "searxng": "SearXNG（本地私有引擎）",
                "google": "Google（需 API Key）",
                "bing": "Bing（需 API Key）",
            }
            return f" 当前搜索引擎：{engine}（{engine_names.get(engine, engine)}）"
        valid = {
            "duckduckgo": "DuckDuckGo（免费）",
            "searxng": "SearXNG（本地私有引擎）",
            "google": "Google（需 API Key）",
            "bing": "Bing（需 API Key）",
        }
        if value.lower() in valid:
            cfg.setdefault("search", {})["default_engine"] = value.lower()
            save_config(cfg)
            reload_config()
            return f" 已切换为：{value.lower()}（{valid[value.lower()]}）"
        return f" 不支持的引擎，可选：{', '.join(valid.keys())}"

    elif key == "rate":
        if not value:
            rate = cfg.get("crawler", {}).get("rate_limit", 1.0)
            return f" 当前限速：{rate} 请求/秒"
        try:
            rate = float(value)
            if rate < 0.1 or rate > 10:
                return " 限速范围：0.1 ~ 10"
            cfg.setdefault("crawler", {})["rate_limit"] = rate
            save_config(cfg)
            reload_config()
            return f" 限速设为 {rate} 请求/秒"
        except ValueError:
            return " 请输入数字，如 /search_config rate 2"

    elif key == "depth":
        if not value:
            d = cfg.get("crawler", {}).get("max_depth", 3)
            return f" 当前最大爬取深度：{d} 层"
        try:
            d = int(value)
            if d < 1 or d > 5:
                return " 深度范围：1 ~ 5"
            cfg.setdefault("crawler", {})["max_depth"] = d
            save_config(cfg)
            reload_config()
            return f" 最大深度设为 {d} 层"
        except ValueError:
            return " 请输入整数，如 /search_config depth 3"

    elif key == "help":
        return _format_help()

    else:
        return (
            f" 未知配置项：{key}\n"
            f" 输入 /search_config help 查看用法"
        )


# ========== 插件注册 ==========
def register_commands():
    return {
        "/search": search_command,
        "/deep": deep_command,
        "/crawl": crawl_command,
        "/search_config": config_command,
    }