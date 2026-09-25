"""
多搜索引擎调度层（代理统一、回退提示、异常分类）
复用 deep_crawler 的代理智能缓存，避免重复探测
"""
import json
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests

PLUGIN_DIR = Path(__file__).parent

from deep_crawler import proxy_precheck, _get_proxy

# 必应中国：国内可直连（实测 200 / 0.3s），是"没有代理也能搜到东西"的兜底通道
BING_CN_URL = "https://cn.bing.com/search"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def _proxy_usable() -> bool:
    """是否"配置了代理、且代理端口真的连得上"。

    config.yaml 里写死的代理不会自动探活（见 deep_crawler._get_configured_proxy），
    这里补一层 TCP 预检，用来决定是否走"需要代理才能用"的通道。
    """
    proxy = _get_proxy()
    if not proxy:
        return False
    return proxy_precheck(proxy) is None


def _load_config() -> dict:
    yaml_path = PLUGIN_DIR / "config.yaml"
    if yaml_path.exists():
        import yaml
        with open(yaml_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    manifest_path = PLUGIN_DIR / "manifest.json"
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    return manifest.get("config", {})


# ========== DuckDuckGo ==========
def _search_duckduckgo(query: str, max_results: int = 5) -> list[dict]:
    try:
        import inspect

        try:
            # 新包名（duckduckgo_search 已更名为 ddgs，旧包会打弃用告警）
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
    except ImportError:
        raise RuntimeError("依赖缺失：请安装 duckduckgo-search 或 ddgs")

    try:
        proxy_dict = _get_proxy()
        proxy_str = proxy_dict.get("http") if proxy_dict else None

        # 参数按已安装版本的签名适配：
        #   ddgs 8.x 中 max_results 属于 text()，不属于构造函数
        #   （旧实现把 max_results 传给了 DDGS()，导致每次搜索都直接抛 TypeError）
        ctor_params = inspect.signature(DDGS.__init__).parameters
        kwargs: dict = {}
        if proxy_str and "proxy" in ctor_params:
            kwargs["proxy"] = proxy_str
        if "timeout" in ctor_params:
            kwargs["timeout"] = 8

        with DDGS(**kwargs) as ddgs:
            try:
                results = list(ddgs.text(query, max_results=max_results))
            except TypeError:
                results = list(ddgs.text(query))[:max_results]
        return [
            {"title": r.get("title", "无标题"), "snippet": r.get("body", ""), "url": r.get("href", "")}
            for r in results
        ]
    except Exception as e:
        raise RuntimeError(f"DuckDuckGo 搜索失败: {e}")


# ========== SearX(NG) ==========
_SEARXNG_FALLBACK_INSTANCES = [
    "https://baresearch.org",
    "https://searx.be",
    "https://priv.au",
    "https://search.sapti.me",
    "https://opnxng.com",
    "https://search.inetol.net",
]

_searxng_instances_cache = None
_searxng_cache_time = 0
_SEARXNG_CACHE_DURATION = 3600


def _get_searxng_instances() -> list[str]:
    global _searxng_instances_cache, _searxng_cache_time

    now = time.time()
    if _searxng_instances_cache and (now - _searxng_cache_time) < _SEARXNG_CACHE_DURATION:
        return _searxng_instances_cache

    instances = []
    try:
        resp = requests.get("https://searx.space/data/instances.json", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            for url, info in data.get("instances", {}).items():
                if info.get("http", False) and info.get("network_type") != "tor":
                    instances.append(url.rstrip("/"))
    except Exception:
        pass

    if not instances:
        instances = _SEARXNG_FALLBACK_INSTANCES

    _searxng_instances_cache = instances
    _searxng_cache_time = now
    return instances


def _search_searx(query: str, max_results: int = 5, local_url: str = None) -> list[dict]:
    proxy = _get_proxy()

    instances = []
    if local_url:
        instances.append(local_url.rstrip("/"))
    # 公共实例成功率低、耗时长（实测 6 个实例 41 秒仍全失败）→ 只取前 3 个作最后兜底
    instances.extend(_get_searxng_instances()[:3])

    last_error = None
    net_fail = 0
    for instance in instances:
        try:
            is_local = urlparse(instance).hostname in ("127.0.0.1", "localhost")
            req_proxy = None if is_local else proxy
            resp = requests.get(
                f"{instance}/search",
                params={"q": query, "format": "json", "language": "zh-CN"},
                timeout=6,
                proxies=req_proxy
            )
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results", [])
                if results:
                    return [
                        {"title": r.get("title", "无标题"),
                         "snippet": r.get("content", ""),
                         "url": r.get("url", "")}
                        for r in results[:max_results]
                    ]
        except Exception as e:
            last_error = e
            # 连续网络类失败：多半是代理/网络整体不通，不再逐个实例硬等
            if isinstance(e, (requests.exceptions.ProxyError, requests.exceptions.ConnectionError)):
                net_fail += 1
                if net_fail >= 3:
                    break
            continue

    raise RuntimeError(
        f"SearX 共尝试 {len(instances)} 个实例均不可用"
        f"（本机：{local_url or '未配置'}；最后一次错误 {type(last_error).__name__}: "
        f"{str(last_error)[:100]}）。建议：启动本机 SearXNG（docker）或启动代理后重试。"
    )


# ========== Bing 中国（无需代理） ==========
def _search_bing_cn(query: str, max_results: int = 5, base_url: str = BING_CN_URL) -> list[dict]:
    """必应中国直连搜索。

    为什么需要它：DuckDuckGo / Google / 公共 SearXNG 实例在国内**直连均不可达**，
    实测只有"必应中国"能在无代理时正常返回结果（200 / 0.3s）。
    有可用代理时也允许走代理（保持与其它引擎一致的出口）。

    仅解析搜索结果页 HTML，不做反检测对抗；请求频率由调用方控制。
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise RuntimeError("依赖缺失：请安装 beautifulsoup4（pip install beautifulsoup4）")

    params = {"q": query, "count": max_results, "mkt": "zh-CN", "setlang": "zh-hans"}
    headers = {"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"}

    # 直连优先：实测直连 0.3s，走代理反而要 6s+（国内站点绕过代理更快、更稳）
    # 直连失败（例如在境外/被劫持）再退回代理
    resp = None
    last_error: Optional[Exception] = None
    for use_proxy in (False, True):
        if use_proxy and not _proxy_usable():
            break
        try:
            resp = requests.get(
                base_url,
                params=params,
                headers=headers,
                timeout=8,
                proxies=_get_proxy() if use_proxy else {"http": None, "https": None},
            )
            resp.raise_for_status()
            break
        except requests.exceptions.RequestException as e:
            last_error = e
            resp = None

    if resp is None:
        raise RuntimeError(
            f"必应中国（{base_url}）连接失败："
            f"{type(last_error).__name__ if last_error else '未配置可用代理'}"
        )

    soup = BeautifulSoup(resp.text, "lxml")
    results: list[dict] = []
    for li in soup.select("li.b_algo"):
        link = li.find("h2").find("a") if li.find("h2") else None
        if link is None:
            continue
        title = link.get_text(" ", strip=True)
        url = link.get("href", "")
        snippet_el = li.select_one(".b_caption p") or li.select_one("p")
        snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
        if title and url:
            results.append({"title": title, "snippet": snippet, "url": url})
        if len(results) >= max_results:
            break

    if not results:
        raise RuntimeError("必应中国未解析到结果（页面结构可能变化，或被要求人机验证）")
    return results


# ========== 搜索引擎调度器 ==========
class SearchEngine:
    def __init__(self, config: dict):
        self.config = config
        search_cfg = config.get("search", {})
        self.engine = search_cfg.get("default_engine", "duckduckgo")
        self.max_results = search_cfg.get("max_results", 5)
        self.searxng_url = search_cfg.get("searxng_url", "")
        self.bing_cn_url = search_cfg.get("bing_cn_url", BING_CN_URL)

    def _offline_reason(self) -> Optional[str]:
        """只有三条通道（代理 / 本机 SearXNG / 必应中国直连）全都不通时，才认定"离线"。

        返回 None 表示可以继续尝试；返回字符串表示直接失败（内容是给用户看的人话提示）。
        """
        if _proxy_usable():
            return None
        # 本机 SearXNG 不走代理：它在跑就仍然可搜
        if self.searxng_url and proxy_precheck({"http": self.searxng_url}) is None:
            return None
        # 必应中国可直连：它是"没有代理"时的主力通道
        if proxy_precheck({"http": self.bing_cn_url}) is None:
            return None
        return (
            "联网失败：本机代理、SearXNG、必应中国（cn.bing.com）都连不上，请检查网络连接。"
            "（代理地址见 plugins/web_search/config.yaml 的 proxy 段）"
        )

    def search(self, query: str) -> list[dict]:
        hint = self._offline_reason()
        if hint:
            raise RuntimeError(hint)

        if self.engine == "bing_cn":
            return _search_bing_cn(query, self.max_results, self.bing_cn_url)

        if self.engine == "duckduckgo":
            # 选路策略（按"快且稳"排序，避免任何通道长时间挂住）：
            #   ① 本机 SearXNG 在跑 → 最快最稳
            #   ② 必应中国直连（实测 0.3s、无需代理）→ 主力通道
            #   ③ 有可用代理 → DuckDuckGo（结果更全，但被限流时会较慢）
            #   ④ 公共 SearX 实例（成功率低、耗时长）→ 仅作最后兜底
            # 注意：**返回 0 条**和抛异常一样算该通道失败，继续降级
            #（DuckDuckGo 被限流时正是"不抛异常、返回空列表"）
            channels: list[tuple[str, callable]] = []

            if self.searxng_url and proxy_precheck({"http": self.searxng_url}) is None:
                channels.append((
                    "本机 SearXNG",
                    lambda: _search_searx(query, self.max_results, self.searxng_url),
                ))
            channels.append((
                "必应中国",
                lambda: _search_bing_cn(query, self.max_results, self.bing_cn_url),
            ))
            if _proxy_usable():
                channels.append(("DuckDuckGo", lambda: _search_duckduckgo(query, self.max_results)))
            channels.append((
                "SearX 公共实例",
                lambda: _search_searx(query, self.max_results, self.searxng_url),
            ))

            errors: list[str] = []
            for name, run in channels:
                try:
                    results = run()
                except Exception as e:
                    errors.append(f"{name}：{e}")
                    continue
                if results:
                    return results
                errors.append(f"{name}：返回 0 条")

            if all("返回 0 条" in e for e in errors):
                return []  # 通道都通、但确实没有结果 → 如实返回空
            raise RuntimeError("全部搜索通道都失败 → " + "；".join(errors))

        elif self.engine == "searxng":
            try:
                return _search_searx(query, self.max_results, self.searxng_url)
            except Exception as e_searx:
                try:
                    return _search_bing_cn(query, self.max_results, self.bing_cn_url)
                except Exception as e_bing:
                    raise RuntimeError(f"SearX（{e_searx}）与必应中国（{e_bing}）都失败")

        elif self.engine == "google":
            return self._search_google(query)

        elif self.engine == "bing":
            return self._search_bing(query)

        else:
            raise ValueError(f"未知搜索引擎: {self.engine}")

    def _search_google(self, query: str) -> list[dict]:
        api_key = self.config.get("search", {}).get("google_api_key", "")
        cse_id = self.config.get("search", {}).get("google_cse_id", "")
        if not api_key or not cse_id:
            raise RuntimeError(
                "Google 搜索走的是 Custom Search JSON API，需要 Key 与搜索引擎 ID，"
                "而该 API 已对新客户关闭注册（既有客户须在 2027-01-01 前迁移），"
                "新账号已无法开通。请改用免 Key 的 duckduckgo（默认）或 bing_cn。"
            )
        try:
            resp = requests.get(
                "https://www.googleapis.com/customsearch/v1",
                params={"key": api_key, "cx": cse_id, "q": query, "num": self.max_results},
                timeout=10, proxies=_get_proxy()
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
            return [{"title": i.get("title", ""), "snippet": i.get("snippet", ""), "url": i.get("link", "")} for i in
                    items]
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response else "?"
            if status == 403:
                raise RuntimeError("Google 配额超限或 API Key 无效，请检查 API Key 和配额。")
            elif status == 429:
                raise RuntimeError("Google API 请求过于频繁，请稍后重试或降低搜索频率。")
            raise RuntimeError(f"Google HTTP 错误 {status}: {e}")
        except requests.exceptions.Timeout:
            raise RuntimeError("Google 搜索超时，请检查代理或网络设置。")
        except Exception as e:
            raise RuntimeError(f"Google 搜索失败: {e}")

    def _search_bing(self, query: str) -> list[dict]:
        api_key = self.config.get("search", {}).get("bing_api_key", "")
        if not api_key:
            raise RuntimeError(
                "Bing Search API 已于 2025-08-11 被微软完全退役，产品不再提供、"
                "也无法注册，此通道已不可用。请改用免 Key 的 duckduckgo（默认）"
                "或 bing_cn（必应中国直连）。"
            )
        try:
            resp = requests.get(
                "https://api.bing.microsoft.com/v7.0/search",
                headers={"Ocp-Apim-Subscription-Key": api_key},
                params={"q": query, "count": self.max_results, "mkt": "zh-CN"},
                timeout=10, proxies=_get_proxy()
            )
            resp.raise_for_status()
            results = resp.json().get("webPages", {}).get("value", [])
            return [{"title": r.get("name", ""), "snippet": r.get("snippet", ""), "url": r.get("url", "")} for r in
                    results]
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response else "?"
            if status == 401:
                raise RuntimeError("Bing API Key 无效，请检查 config.yaml 中的 bing_api_key。")
            elif status == 429:
                raise RuntimeError("Bing API 请求过于频繁，请稍后重试。")
            raise RuntimeError(f"Bing HTTP 错误 {status}: {e}")
        except requests.exceptions.Timeout:
            raise RuntimeError("Bing 搜索超时，网络连接超时。")
        except Exception as e:
            raise RuntimeError(f"Bing 搜索失败: {e}")