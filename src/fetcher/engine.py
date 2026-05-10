"""
多策略抓取引擎

auto 模式优先级：
  1. curl_cffi（伪装 Chrome TLS，过 Cloudflare）
  2. httpx（轻量静态页）
  3. Jina Reader（外部渲染兜底）

用户指定 curl_cffi / httpx 时，若抓到空壳会自动升级到 Jina。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Any

from ..config import AppConfig
from ..db import Task
from ..logger import logger


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

    def __bool__(self) -> bool:
        return self.ok


# curl_cffi 支持的浏览器指纹
SUPPORTED_IMPERSONATE = (
    "chrome131", "chrome124", "chrome120",
    "firefox133", "firefox135",
    "safari18_0", "safari17_0",
)

# httpx 用的 UA 池（随机选一个）
_UA_POOL = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Safari/605.1.15",
)

_BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
}


def _can_decode(name: str) -> bool:
    """检测 httpx 是否能解这种压缩编码。"""
    try:
        __import__(name)
        return True
    except ImportError:
        return False


# 只广告我们真正能解压的编码；否则服务端发 br 我们没法解（乱码）
_ACCEPT_ENCODING = ", ".join(filter(None, [
    "gzip",
    "deflate",
    "br" if _can_decode("brotli") or _can_decode("brotlicffi") else None,
    "zstd" if _can_decode("zstandard") else None,
])) or "identity"


def _headers(default_headers: dict, user_headers: dict,
             accept_encoding: str | None = None) -> dict:
    """合成浏览器级请求头。accept_encoding 允许调用方覆盖（如 httpx 分支）。"""
    h = {
        "User-Agent": random.choice(_UA_POOL),
        **_BASE_HEADERS,
        "Accept-Encoding": accept_encoding or _ACCEPT_ENCODING,
    }
    h.update(default_headers or {})
    h.update(user_headers or {})
    return h


# ============================================================
# HTML 空壳检测（判定是否需要 fallback 到 Jina）
# ============================================================
_EMBEDDED_DATA_MARKERS = (
    "__NEXT_DATA__", "__NUXT_DATA__", "__NUXT__",
    "__APOLLO_STATE__", "__INITIAL_STATE__", "__PRELOADED_STATE__",
    "__REDUX_STATE__", "__INITIAL_DATA__",
    "application/ld+json",
    "data-sveltekit-fetched", "__remixContext",
)

_CHALLENGE_MARKERS = (
    "checking your browser", "cf-challenge", "cf_chl_opt",
    "just a moment", "attention required",
    "/cdn-cgi/challenge-platform",
)

_HEAD_RE = re.compile(r"<head[^>]*>.*?</head>", re.DOTALL | re.IGNORECASE)
_SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE)
_NAV_RE = re.compile(
    r"<(?:nav|footer|header)[^>]*>.*?</(?:nav|footer|header)>",
    re.DOTALL | re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _quick_visible_text(html: str) -> str:
    """粗略估算 body 内真实可见文本（不含 head/meta/script/style）。"""
    text = _HEAD_RE.sub("", html)
    text = _SCRIPT_RE.sub("", text)
    text = _STYLE_RE.sub("", text)
    text = _NAV_RE.sub("", text)
    text = _TAG_RE.sub(" ", text)
    return " ".join(text.split())


def _looks_like_binary_garbage(text: str) -> bool:
    """
    检测响应是否是未解压的二进制字节流（brotli/zstd 等解压失败的征兆）。
    采样前 2KB，统计不可打印字符占比 > 30% 就判定为乱码。
    """
    if not text:
        return False
    sample = text[:2048]
    if not sample:
        return False
    # 统计 "可疑字符"：不是 ASCII 可打印也不是 CJK 等常见范围
    bad = sum(1 for ch in sample
              if ord(ch) < 0x20 and ch not in "\r\n\t"
              or 0x7F <= ord(ch) < 0xA0)  # C1 控制字符
    return bad / len(sample) > 0.30


def _is_content_usable(result: FetchResult, task: Task) -> bool:
    """判断抓取内容是否足够（避免把 SPA 空壳或乱码当成正常结果）。"""
    if not result.ok or not (result.content or "").strip():
        return False

    text = result.content

    # 乱码检测（未解压的压缩字节）
    if _looks_like_binary_garbage(text):
        return False

    if task.type == "json":
        return True

    if len(text) < 500:
        return False
    lower = text.lower()
    if any(m in lower for m in _CHALLENGE_MARKERS):
        return False
    # 有内嵌数据或足够可见文本 → 可用
    if any(m in text for m in _EMBEDDED_DATA_MARKERS):
        return True
    return len(_quick_visible_text(text)) >= 400


# ============================================================
# 抓取引擎
# ============================================================
class FetchEngine:
    """多策略抓取引擎（线程安全，复用连接池）。"""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.timeout = cfg.request_timeout
        proxy = cfg.https_proxy or cfg.http_proxy or None
        self._proxies = (
            {"http": cfg.http_proxy or cfg.https_proxy,
             "https": cfg.https_proxy or cfg.http_proxy}
            if proxy else None
        )
        self._httpx_client: Any = None  # 懒加载，首次使用时创建

    # ----- 对外入口 -----
    def fetch(self, task: Task) -> FetchResult:
        strategy = (task.strategy or "auto").lower()
        headers = _headers(self.cfg.default_headers, task.headers or {})

        if strategy == "jina":
            return self._fetch_jina(task, headers)
        if strategy == "firecrawl":
            return self._fetch_firecrawl(task, headers)

        if strategy == "curl_cffi":
            result = self._fetch_curl_cffi(task, headers)
            return self._upgrade_to_jina_if_empty(task, headers, result)
        if strategy == "httpx":
            result = self._fetch_httpx(task, headers)
            return self._upgrade_to_jina_if_empty(task, headers, result)

        # auto：curl_cffi → httpx → jina
        result = self._fetch_curl_cffi(task, headers)
        if result.ok and _is_content_usable(result, task):
            return result

        result2 = self._fetch_httpx(task, headers)
        if result2.ok and _is_content_usable(result2, task):
            return result2

        result3 = self._fetch_jina(task, headers)
        if result3.ok:
            logger.info("🔀 [{}] auto 升级到 Jina（前两个策略拿到空壳）", task.name)
            return result3

        # 全部失败：返回最有信息的
        if result.ok:
            return result
        result.error = result.error or "所有策略均失败"
        return result

    def _upgrade_to_jina_if_empty(
        self, task: Task, headers: dict, result: FetchResult
    ) -> FetchResult:
        """明确指定 curl_cffi/httpx 但拿到空壳时，升级到 Jina。"""
        if not result.ok or _is_content_usable(result, task):
            return result
        # 区分原因便于排查
        content = result.content or ""
        if _looks_like_binary_garbage(content):
            reason = f"响应疑似未解压（乱码字节 len={len(content)}）"
        else:
            reason = f"空壳/内容不足（len={len(content)}）"
        logger.info(
            "🔀 [{}] 策略={} {}，自动升级 Jina",
            task.name, result.strategy_used, reason,
        )
        jina = self._fetch_jina(task, headers)
        if jina.ok:
            jina.strategy_used = f"{result.strategy_used}→jina"
            return jina
        return result

    # ----- 策略：curl_cffi -----
    def _fetch_curl_cffi(self, task: Task, headers: dict) -> FetchResult:
        try:
            from curl_cffi import requests as cc
        except ImportError:
            return FetchResult(
                ok=False, url=task.url, strategy_used="curl_cffi",
                error="curl_cffi 未安装",
            )

        impersonate = task.impersonate or "chrome131"
        if impersonate not in SUPPORTED_IMPERSONATE:
            impersonate = "chrome131"

        try:
            resp = cc.get(
                task.url, headers=headers, impersonate=impersonate,
                timeout=self.timeout, proxies=self._proxies,
                allow_redirects=True,
            )
            return FetchResult(
                ok=200 <= resp.status_code < 300,
                url=task.url, status_code=resp.status_code,
                content=resp.text,
                content_type=resp.headers.get("content-type", ""),
                strategy_used=f"curl_cffi/{impersonate}",
            )
        except Exception as e:
            return FetchResult(
                ok=False, url=task.url,
                strategy_used=f"curl_cffi/{impersonate}",
                error=f"{type(e).__name__}: {e}",
            )

    # ----- 策略：httpx（复用 Client 连接池）-----
    def _get_httpx_client(self):
        """懒加载共享 httpx Client（连接池复用，省 TLS 握手）。"""
        if self._httpx_client is None:
            try:
                import httpx
            except ImportError:
                return None
            self._httpx_client = httpx.Client(
                http2=True,
                timeout=self.timeout,
                follow_redirects=True,
                proxy=self.cfg.https_proxy or self.cfg.http_proxy or None,
                limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
            )
        return self._httpx_client

    def _fetch_httpx(self, task: Task, headers: dict) -> FetchResult:
        client = self._get_httpx_client()
        if client is None:
            return FetchResult(
                ok=False, url=task.url, strategy_used="httpx",
                error="httpx 未安装",
            )
        try:
            resp = client.get(task.url, headers=headers)
            return FetchResult(
                ok=200 <= resp.status_code < 300,
                url=task.url, status_code=resp.status_code,
                content=resp.text,
                content_type=resp.headers.get("content-type", ""),
                strategy_used="httpx",
            )
        except Exception as e:
            return FetchResult(
                ok=False, url=task.url, strategy_used="httpx",
                error=f"{type(e).__name__}: {e}",
            )

    # ----- 策略：Jina Reader -----
    def _fetch_jina(self, task: Task, headers: dict) -> FetchResult:
        client = self._get_httpx_client()
        if client is None:
            return FetchResult(
                ok=False, url=task.url, strategy_used="jina",
                error="httpx 未安装",
            )
        jina_url = f"https://r.jina.ai/{task.url}"
        jh = {
            "Accept": "text/plain",
            "User-Agent": headers.get("User-Agent", "web-monitor-pro/0.1"),
            "x-no-cache": "true",  # 禁用 Jina 缓存，确保每次拿到最新页面
        }
        if self.cfg.jina_reader_api_key:
            jh["Authorization"] = f"Bearer {self.cfg.jina_reader_api_key}"
        try:
            # Jina 渲染可能慢，独立超时
            resp = client.get(jina_url, headers=jh, timeout=self.timeout * 2)
            return FetchResult(
                ok=200 <= resp.status_code < 300,
                url=task.url, status_code=resp.status_code,
                content=resp.text,
                content_type=resp.headers.get("content-type", "text/plain"),
                strategy_used="jina",
            )
        except Exception as e:
            return FetchResult(
                ok=False, url=task.url, strategy_used="jina",
                error=f"{type(e).__name__}: {e}",
            )

    # ----- 策略：Firecrawl -----
    def _fetch_firecrawl(self, task: Task, headers: dict) -> FetchResult:
        if not self.cfg.firecrawl_api_key:
            return FetchResult(
                ok=False, url=task.url, strategy_used="firecrawl",
                error="未配置 FIRECRAWL_API_KEY",
            )
        client = self._get_httpx_client()
        if client is None:
            return FetchResult(
                ok=False, url=task.url, strategy_used="firecrawl",
                error="httpx 未安装",
            )
        try:
            resp = client.post(
                "https://api.firecrawl.dev/v1/scrape",
                json={"url": task.url, "formats": ["markdown"]},
                headers={
                    "Authorization": f"Bearer {self.cfg.firecrawl_api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout * 2,
            )
            if resp.status_code != 200:
                return FetchResult(
                    ok=False, url=task.url, status_code=resp.status_code,
                    strategy_used="firecrawl",
                    error=f"Firecrawl HTTP {resp.status_code}",
                )
            data = resp.json()
            md = (data.get("data") or {}).get("markdown", "")
            return FetchResult(
                ok=True, url=task.url, status_code=200,
                content=md, content_type="text/markdown",
                strategy_used="firecrawl",
            )
        except Exception as e:
            return FetchResult(
                ok=False, url=task.url, strategy_used="firecrawl",
                error=f"{type(e).__name__}: {e}",
            )

    def close(self) -> None:
        """关闭连接池。服务停止时调用。"""
        if self._httpx_client is not None:
            try:
                self._httpx_client.close()
            except Exception:
                pass
            self._httpx_client = None
