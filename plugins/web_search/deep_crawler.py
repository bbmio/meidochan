"""
深度抓取引擎（代理智能缓存、续读支持、URL 规范化）
独立模块，自行读取配置
"""
import time
import socket
import hashlib
import json
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse
from typing import Optional, Callable

import requests
from bs4 import BeautifulSoup

PLUGIN_DIR = Path(__file__).parent

PROXY_TEST_URL = "http://httpbin.org/ip"
PROXY_CHECK_INTERVAL = 60.0
PROXY_RETRY_INTERVAL = 10.0


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


_cached_proxy: Optional[dict] = None
_cached_proxy_available: bool = False
_last_proxy_check: float = 0.0


def _detect_system_proxy() -> Optional[dict]:
    try:
        raw = urllib.request.getproxies()
        http_val = raw.get("http") or raw.get("HTTP") or ""
        https_val = raw.get("https") or raw.get("HTTPS") or ""
        if http_val and not http_val.startswith("socks"):
            proxy = {"http": http_val}
            proxy["https"] = https_val if (https_val and not https_val.startswith("socks")) else http_val
            return proxy
    except Exception:
        pass
    return None


def _probe_clash_ports() -> Optional[dict]:
    for port in ("7897", "7890"):
        try:
            proxy = {"http": f"http://127.0.0.1:{port}", "https": f"http://127.0.0.1:{port}"}
            r = requests.get(PROXY_TEST_URL, proxies=proxy, timeout=2)
            if r.status_code == 200:
                return proxy
        except Exception:
            continue
    return None


def _get_configured_proxy() -> Optional[dict]:
    try:
        config = _load_config()
        proxy_cfg = config.get("proxy", {})
        http_proxy = proxy_cfg.get("http", "")
        https_proxy = proxy_cfg.get("https", "")
        if http_proxy:
            return {"http": http_proxy, "https": https_proxy or http_proxy}
    except Exception:
        pass
    return None


def _is_proxy_alive(proxy: dict) -> bool:
    try:
        r = requests.get(PROXY_TEST_URL, proxies=proxy, timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def proxy_precheck(proxy: Optional[dict] = None, timeout: float = 1.0) -> Optional[str]:
    """TCP 层快速判断"代理端口是否连得上"，避免长时间干等。

    - 返回 None：可用 / 未配置代理 / 地址无法解析（交给上层按直连处理）
    - 返回 "host:port"：该代理连不上

    背景：config.yaml 里写死的代理不会经过 _get_proxy() 的探活（见 _get_configured_proxy），
    于是"代理没启动"时要等到请求超时才报错（实测一次搜索要等 30+ 秒）。
    """
    p = proxy if proxy is not None else _get_proxy()
    if not p:
        return None
    url = p.get("http") or p.get("https") or ""
    if not url:
        return None
    try:
        parsed = urlparse(url if "://" in url else "http://" + url)
        host = parsed.hostname
        port = int(parsed.port or 80)
    except Exception:
        return None
    if not host:
        return None
    try:
        with socket.socket() as s:
            s.settimeout(timeout)
            if s.connect_ex((host, port)) == 0:
                return None
    except Exception:
        return None
    return f"{host}:{port}"


def _get_proxy() -> Optional[dict]:
    """按优先级挑一个**确认可用**的代理；都不通则返回 None（调用方按直连处理）。

    优先级：① config.yaml 显式配置 → ② 系统代理 / 环境变量 → ③ Clash 常见端口

    ⚠️ 配置里的代理也必须探活。历史实现只要 config.yaml 写了 proxy 就无条件
    return，于是代理一挂（Clash 重启 / 换端口 / 退出）所有请求直接撞
    WinError 10061，而环境变量里真正可用的代理永远轮不到 —— 自动探测链被短路了。
    """
    global _cached_proxy, _cached_proxy_available, _last_proxy_check

    now = time.time()

    # 缓存：可用代理缓存久一点；不可用状态只缓存短时间（代理可能刚被启动）
    if _cached_proxy is not None or _last_proxy_check > 0:
        interval = PROXY_CHECK_INTERVAL if _cached_proxy_available else PROXY_RETRY_INTERVAL
        if (now - _last_proxy_check) < interval:
            return _cached_proxy if _cached_proxy_available else None

    def _accept(proxy: Optional[dict]) -> bool:
        """TCP 预检通过即采用。

        这里刻意不调 _is_proxy_alive()：它要请求 httpbin.org，既是外网站点
        （本身经常不可达），又会给每次探测加最多 3 秒。而我们要拦的 10061 是
        TCP 层拒绝，TCP 预检足够；代理"端口通但出不去"的少数情况交给调用方的
        通道降级兜底（见 search_engine.SearchEngine.search）。
        """
        return bool(proxy) and proxy_precheck(proxy) is None

    # ① 显式配置的代理（探活后再用，不再无条件短路）
    configured = _get_configured_proxy()
    if _accept(configured):
        _cached_proxy, _cached_proxy_available, _last_proxy_check = configured, True, now
        return configured

    # ② 系统代理 / 环境变量（urllib.request.getproxies 会一并读取 HTTP_PROXY 等）
    sys_proxy = _detect_system_proxy()
    if _accept(sys_proxy):
        _cached_proxy, _cached_proxy_available, _last_proxy_check = sys_proxy, True, now
        return sys_proxy

    # ③ Clash 等本地常见端口（最后兜底，会发真实请求验证出口是否通）
    clash_proxy = _probe_clash_ports()
    if clash_proxy:
        _cached_proxy, _cached_proxy_available, _last_proxy_check = clash_proxy, True, now
        return clash_proxy

    _cached_proxy, _cached_proxy_available, _last_proxy_check = None, False, now
    return None


def _normalize_url(url: str) -> str:
    parsed = urlparse(url)
    query = "&".join(sorted(parsed.query.split("&"))) if parsed.query else ""
    path = parsed.path.rstrip("/") if parsed.path != "/" else "/"
    normalized = urlunparse((
        parsed.scheme,
        parsed.netloc,
        path,
        parsed.params,
        query,
        ""
    ))
    return normalized


class DeepCrawler:
    def __init__(self, config: dict):
        crawler_cfg = config.get("crawler", {})
        self.max_depth = crawler_cfg.get("max_depth", 3)
        self.max_pages = crawler_cfg.get("max_pages", 50)
        self.rate_limit = crawler_cfg.get("rate_limit", 1.0)
        self.timeout = crawler_cfg.get("timeout", 15)
        self.user_agent = crawler_cfg.get("user_agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
        self.presets = config.get("presets", {})
        self._visited: set[str] = set()
        self._last_request_time = 0.0

    def _rate_limit_wait(self):
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < 1.0 / self.rate_limit:
            time.sleep(1.0 / self.rate_limit - elapsed)
        self._last_request_time = time.time()

    def _get_page_html(self, url: str) -> Optional[str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        self._rate_limit_wait()
        try:
            resp = requests.get(
                url, headers=headers, timeout=self.timeout,
                proxies=_get_proxy()
            )
            resp.encoding = resp.apparent_encoding or "utf-8"
            if resp.status_code != 200:
                print(f" HTTP {resp.status_code}: {url}")
                return None
            return resp.text
        except requests.exceptions.Timeout:
            print(f"⏰ 请求超时: {url}")
            return None
        except requests.exceptions.SSLError as e:
            print(f" SSL 错误 {url}: {e}")
            return None
        except requests.exceptions.ConnectionError as e:
            print(f" 连接错误 {url}: {e}")
            return None
        except Exception as e:
            print(f" 请求失败 {url}: {e}")
            return None

    def fetch_page(self, url: str) -> Optional[dict]:
        html = self._get_page_html(url)
        if not html:
            return None

        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.string.strip() if soup.title and soup.title.string else ""

        from content_extractor import extract_content
        markdown, plain_text = extract_content(html, url, self.presets)

        MIN_CONTENT_LENGTH = 100
        if not plain_text or len(plain_text) < MIN_CONTENT_LENGTH:
            print(f" 页面内容过短 ({len(plain_text)} 字符): {url}")
            return None

        return {
            "title": title,
            "url": url,
            "markdown": markdown,
            "text": plain_text,
            "content_length": len(plain_text)
        }

    def crawl_recursive(
        self, start_url: str, max_depth: int = None,
        progress_callback: Callable = None
    ) -> list[dict]:
        if max_depth is None:
            max_depth = self.max_depth

        self._visited.clear()
        results: list[dict] = []
        base_domain = urlparse(start_url).netloc

        def _crawl(url: str, depth: int):
            if depth > max_depth or len(results) >= self.max_pages:
                return

            url_normalized = _normalize_url(url)
            url_hash = hashlib.md5(url_normalized.encode()).hexdigest()
            if url_hash in self._visited:
                return
            self._visited.add(url_hash)

            print(f" [深度 {depth}] 抓取: {url}")
            page = self.fetch_page(url)
            if not page:
                return

            results.append(page)
            if progress_callback:
                progress_callback(len(results), page["title"])

            html = self._get_page_html(url)
            if not html:
                return
            soup = BeautifulSoup(html, "html.parser")
            links = set()
            for a in soup.find_all("a", href=True):
                href = a["href"].strip()
                if href.startswith(("#", "javascript:", "mailto:", "tel:")):
                    continue
                full_url = urljoin(url, href)
                parsed = urlparse(full_url)
                if parsed.netloc != base_domain:
                    continue
                path = parsed.path.lower()
                ext = path.rsplit(".", 1)[-1] if "." in path else ""
                if ext in ("", "html", "htm", "md", "php", "asp", "jsp"):
                    clean = _normalize_url(full_url)
                    links.add(clean)

            for link in links:
                if len(results) >= self.max_pages:
                    break
                _crawl(link, depth + 1)

        _crawl(start_url, 1)
        return results