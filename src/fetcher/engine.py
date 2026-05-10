"""
多策略抓取引擎

优先级（自动模式）：
  1. curl_cffi 伪装 Chrome TLS/JA3 指纹（主力，能过多数 Cloudflare）
  2. httpx + 浏览器请求头（轻量备选，适合 L1-L2 静态页）
  3. 外部渲染 API（Jina Reader / Firecrawl，兜底）

所有策略都自动带：
  - 浏览器级请求头（User-Agent、Accept、Sec-Ch-Ua 等）
  - 可选代理
  - 超时控制
  - 失败自动 fallback 到下一策略
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Any

from ..config import AppConfig
from ..db import Task
from ..logger import logger


# ============================================================
# 结果数据结构
# ============================================================
@dataclass
class FetchResult:
    """抓取结果。"""

    ok: bool
    url: str
    status_code: int | None = None
    content: str = ""
    content_type: str = ""
    strategy_used: str = ""
    error: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.ok


# ============================================================
# 浏览器请求头生成
# ============================================================
# curl_cffi 支持的 impersonate 目标
SUPPORTED_IMPERSONATE = [
    "chrome131", "chrome124", "chrome120",
    "firefox133", "firefox135",
    "safari18_0", "safari17_0",
]

# 备选 User-Agent（供 httpx 使用，与 curl_cffi 的 impersonate 无关）
FALLBACK_USER_AGENTS = [
    # Chrome 131 on Windows 11
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Chrome 131 on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Firefox 133 on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    # Safari 18 on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Safari/605.1.15",
]


def _browser_headers(default_headers: dict[str, str], user_headers: dict[str, str]) -> dict[str, str]:
    """合成一套像真实浏览器的请求头。顺序：硬编码基线 -> 全局默认 -> 任务自定义。"""
    base = {
        "User-Agent": random.choice(FALLBACK_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
    }
    base.update(default_headers or {})
    base.update(user_headers or {})
    return base


# ============================================================
# 核心抓取引擎
# ============================================================
class FetchEngine:
    """多策略抓取引擎。线程安全，可被多个任务复用。"""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.timeout = cfg.request_timeout
        self.proxies = self._build_proxies()

    def _build_proxies(self) -> dict[str, str] | None:
        """构造 proxies 字典。"""
        if self.cfg.http_proxy or self.cfg.https_proxy:
            return {
                "http": self.cfg.http_proxy or self.cfg.https_proxy,
                "https": self.cfg.https_proxy or self.cfg.http_proxy,
            }
        return None

    # --------------------------------------------------------
    # 对外入口
    # --------------------------------------------------------
    def fetch(self, task: Task) -> FetchResult:
        """
        根据任务配置的 strategy 字段抓取。
        strategy = 'auto'           → 依次尝试 curl_cffi → httpx → jina
        strategy = 'curl_cffi'      → curl_cffi；若提取后内容过少自动升级 jina
        strategy = 'httpx'          → httpx；若提取后内容过少自动升级 jina
        strategy = 'jina'           → 仅用 Jina Reader
        strategy = 'firecrawl'      → 仅用 Firecrawl
        """
        strategy = (task.strategy or "auto").lower()
        headers = _browser_headers(self.cfg.default_headers, task.headers or {})

        # 强制指定 jina / firecrawl
        if strategy == "jina":
            return self._fetch_jina(task, headers)
        if strategy == "firecrawl":
            return self._fetch_firecrawl(task, headers)

        # 对于 curl_cffi / httpx / auto：先尝试静态抓取
        if strategy == "curl_cffi":
            result = self._fetch_curl_cffi(task, headers)
            return self._maybe_fallback_to_jina(task, headers, result)

        if strategy == "httpx":
            result = self._fetch_httpx(task, headers)
            return self._maybe_fallback_to_jina(task, headers, result)

        # auto：依次尝试
        logger.debug("🔀 任务 [{}] 使用自动策略，开始依次尝试", task.name)

        # 1) curl_cffi（最强）
        result = self._fetch_curl_cffi(task, headers)
        if result.ok and self._has_enough_content(result, task):
            return result
        logger.debug(
            "  ↪ curl_cffi 不可用（HTTP={} content_len={}），继续",
            result.status_code, len(result.content or ""),
        )

        # 2) httpx（轻量）
        result2 = self._fetch_httpx(task, headers)
        if result2.ok and self._has_enough_content(result2, task):
            return result2
        logger.debug(
            "  ↪ httpx 不可用（HTTP={} content_len={}），继续",
            result2.status_code, len(result2.content or ""),
        )

        # 3) Jina Reader（外部渲染，默认启用，无 key 走公共限流）
        result3 = self._fetch_jina(task, headers)
        if result3.ok:
            logger.info(
                "🔀 任务 [{}] 自动升级到 Jina Reader 获取内容（curl_cffi/httpx 拿到的是空壳）",
                task.name,
            )
            return result3
        logger.debug("  ↪ Jina Reader 也失败：{}", result3.error)

        # 全部失败：返回最可能有信息的结果
        # 如果 curl_cffi 至少 HTTP 成功了（只是内容不够），依然返回它（比纯错误强）
        if result.ok:
            return result
        result.error = result.error or "所有策略均失败"
        return result

    def _maybe_fallback_to_jina(
        self, task: Task, headers: dict[str, str], result: FetchResult
    ) -> FetchResult:
        """
        若用户明确指定了 curl_cffi / httpx，但抓到的是空壳 SPA 页，
        静默升级到 Jina Reader（渲染后的结果），同时在日志提示。
        """
        if not result.ok:
            return result
        if self._has_enough_content(result, task):
            return result

        logger.info(
            "🔀 任务 [{}] 策略={} 抓到空壳（content_len={}），自动升级到 Jina Reader",
            task.name, result.strategy_used, len(result.content or ""),
        )
        jina = self._fetch_jina(task, headers)
        if jina.ok:
            jina.strategy_used = f"{result.strategy_used}→jina"
            return jina
        return result  # jina 也失败就返回原结果（至少有 HTML 可以看）

    # --------------------------------------------------------
    # 具体策略实现
    # --------------------------------------------------------
    def _fetch_curl_cffi(self, task: Task, headers: dict[str, str]) -> FetchResult:
        """使用 curl_cffi 伪装浏览器 TLS 指纹抓取。"""
        try:
            from curl_cffi import requests as cc_requests
        except ImportError:
            return FetchResult(
                ok=False, url=task.url, strategy_used="curl_cffi",
                error="curl_cffi 未安装（请 pip install curl_cffi）",
            )

        impersonate = task.impersonate or "chrome131"
        if impersonate not in SUPPORTED_IMPERSONATE:
            logger.warning("⚠️  不支持的 impersonate={}，回退到 chrome131", impersonate)
            impersonate = "chrome131"

        try:
            logger.debug("🌐 [curl_cffi/{}] 正在请求：{}", impersonate, task.url)
            resp = cc_requests.get(
                task.url,
                headers=headers,
                impersonate=impersonate,
                timeout=self.timeout,
                proxies=self.proxies,
                allow_redirects=True,
            )
            content_type = resp.headers.get("content-type", "")
            return FetchResult(
                ok=200 <= resp.status_code < 300,
                url=task.url,
                status_code=resp.status_code,
                content=resp.text,
                content_type=content_type,
                strategy_used=f"curl_cffi/{impersonate}",
                headers=dict(resp.headers),
            )
        except Exception as e:
            return FetchResult(
                ok=False, url=task.url, strategy_used=f"curl_cffi/{impersonate}",
                error=f"{type(e).__name__}: {e}",
            )

    def _fetch_httpx(self, task: Task, headers: dict[str, str]) -> FetchResult:
        """使用 httpx 抓取（适合静态/轻反爬页面）。"""
        try:
            import httpx
        except ImportError:
            return FetchResult(
                ok=False, url=task.url, strategy_used="httpx",
                error="httpx 未安装",
            )

        try:
            logger.debug("🌐 [httpx] 正在请求：{}", task.url)
            with httpx.Client(
                http2=True,
                timeout=self.timeout,
                follow_redirects=True,
                proxy=self.cfg.https_proxy or self.cfg.http_proxy or None,
            ) as client:
                resp = client.get(task.url, headers=headers)
            content_type = resp.headers.get("content-type", "")
            return FetchResult(
                ok=200 <= resp.status_code < 300,
                url=task.url,
                status_code=resp.status_code,
                content=resp.text,
                content_type=content_type,
                strategy_used="httpx",
                headers=dict(resp.headers),
            )
        except Exception as e:
            return FetchResult(
                ok=False, url=task.url, strategy_used="httpx",
                error=f"{type(e).__name__}: {e}",
            )

    def _fetch_jina(self, task: Task, headers: dict[str, str]) -> FetchResult:
        """
        使用 Jina Reader（r.jina.ai）作为兜底：
        - 它会返回该页面的纯文本/markdown 版本，自动渲染 JS
        - 免费额度 1M 次/月（配 API Key），无 key 也能用，但速率受限
        """
        try:
            import httpx
        except ImportError:
            return FetchResult(
                ok=False, url=task.url, strategy_used="jina",
                error="httpx 未安装（Jina 需要）",
            )

        jina_url = f"https://r.jina.ai/{task.url}"
        jina_headers: dict[str, str] = {
            "Accept": "text/plain",
            "User-Agent": headers.get("User-Agent", "web-monitor-pro/0.1"),
        }
        if self.cfg.jina_reader_api_key:
            jina_headers["Authorization"] = f"Bearer {self.cfg.jina_reader_api_key}"

        try:
            logger.debug("🌐 [jina] 正在请求：{}", jina_url)
            with httpx.Client(timeout=self.timeout * 2, follow_redirects=True) as client:
                resp = client.get(jina_url, headers=jina_headers)
            return FetchResult(
                ok=200 <= resp.status_code < 300,
                url=task.url,
                status_code=resp.status_code,
                content=resp.text,
                content_type=resp.headers.get("content-type", "text/plain"),
                strategy_used="jina",
                headers=dict(resp.headers),
            )
        except Exception as e:
            return FetchResult(
                ok=False, url=task.url, strategy_used="jina",
                error=f"{type(e).__name__}: {e}",
            )

    def _fetch_firecrawl(self, task: Task, headers: dict[str, str]) -> FetchResult:
        """使用 Firecrawl API 抓取（需要 API Key）。"""
        if not self.cfg.firecrawl_api_key:
            return FetchResult(
                ok=False, url=task.url, strategy_used="firecrawl",
                error="未配置 FIRECRAWL_API_KEY",
            )

        try:
            import httpx
        except ImportError:
            return FetchResult(
                ok=False, url=task.url, strategy_used="firecrawl",
                error="httpx 未安装（Firecrawl 需要）",
            )

        api_url = "https://api.firecrawl.dev/v1/scrape"
        payload: dict[str, Any] = {"url": task.url, "formats": ["markdown"]}
        fc_headers = {
            "Authorization": f"Bearer {self.cfg.firecrawl_api_key}",
            "Content-Type": "application/json",
        }
        try:
            logger.debug("🌐 [firecrawl] 正在请求：{}", task.url)
            with httpx.Client(timeout=self.timeout * 2) as client:
                resp = client.post(api_url, headers=fc_headers, json=payload)
            if resp.status_code != 200:
                return FetchResult(
                    ok=False, url=task.url, status_code=resp.status_code,
                    strategy_used="firecrawl",
                    error=f"Firecrawl HTTP {resp.status_code}: {resp.text[:200]}",
                )
            data = resp.json()
            markdown = (data.get("data") or {}).get("markdown", "")
            return FetchResult(
                ok=True, url=task.url, status_code=200,
                content=markdown, content_type="text/markdown",
                strategy_used="firecrawl",
            )
        except Exception as e:
            return FetchResult(
                ok=False, url=task.url, strategy_used="firecrawl",
                error=f"{type(e).__name__}: {e}",
            )

    # --------------------------------------------------------
    # 辅助：判断抓到的内容"是否含实质内容"（不是 SPA 空壳）
    # --------------------------------------------------------
    @staticmethod
    def _has_enough_content(result: FetchResult, task: Task) -> bool:
        """
        更严格的可用性判断：
        - HTTP 2xx
        - JSON 任务：只要非空就行
        - HTML 任务：不仅要够长，还要能提取出足够的可见文本（不是 SPA 空壳）
        """
        if not result.ok:
            return False

        text = result.content or ""
        if not text.strip():
            return False

        # JSON 任务：非空即可
        if task.type == "json":
            return True

        # 小于 500 字节基本是错误页
        if len(text) < 500:
            return False

        # Cloudflare 挑战页的特征词
        lower = text.lower()
        challenge_markers = (
            "checking your browser",
            "cf-challenge",
            "cf_chl_opt",
            "just a moment",
            "attention required",
            "/cdn-cgi/challenge-platform",
        )
        if any(m in lower for m in challenge_markers):
            return False

        # 如果 HTML 特别短（< 3KB），算壳
        # 真正的 SPA 空壳判定我们放在 extractor 的后置检查里
        # 这里做个"内嵌数据快速检测"：有任一 SPA 数据嵌入点就算可用
        if _has_any_embedded_data(text):
            return True

        # 否则：看可见文本量（粗略估算，直接正则剥标签）
        visible = _quick_visible_text(text)
        return len(visible) >= 400


# ============================================================
# 辅助：判断 HTML 是否有内嵌数据（Next/Nuxt/Apollo/window.__X__ 等）
# ============================================================
_EMBEDDED_DATA_MARKERS = (
    '__NEXT_DATA__', '__NUXT_DATA__', '__NUXT__',
    '__APOLLO_STATE__', '__INITIAL_STATE__', '__PRELOADED_STATE__',
    '__REDUX_STATE__', '__INITIAL_DATA__',
    'application/ld+json',
    'data-sveltekit-fetched',
    '__remixContext',
)


def _has_any_embedded_data(html: str) -> bool:
    """检测 HTML 是否存在任一常见的数据嵌入点。"""
    return any(marker in html for marker in _EMBEDDED_DATA_MARKERS)


_QUICK_STRIP_RE = re.compile(r"<script[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
_QUICK_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE)
_QUICK_TAG_RE = re.compile(r"<[^>]+>")


def _quick_visible_text(html: str) -> str:
    """粗略估算可见文本量（不依赖 selectolax，避免循环导入）。"""
    text = _QUICK_STRIP_RE.sub("", html)
    text = _QUICK_STYLE_RE.sub("", text)
    text = _QUICK_TAG_RE.sub(" ", text)
    return " ".join(text.split())
