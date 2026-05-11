"""
多策略抓取引擎

auto 模式优先级：
  1. curl_cffi（伪装 Chrome TLS，过 Cloudflare）
  2. httpx（轻量静态页）
  3. 深度提取（SPA 嵌入数据 + RSC Flight，零网络开销）
  4. Playwright（headless Chromium 渲染，纯 CSR 兜底）

用户指定 curl_cffi / httpx 时，若抓到空壳会自动逐级升级。
"""

from __future__ import annotations

import random
import re
import time
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
# HTML 空壳检测（判定是否需要 fallback）
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


_SPA_SHELL_MARKERS = (
    'id="root"', 'id="app"', 'id="__next"', 'id="__nuxt"',
    "data-reactroot", "data-server-rendered",
    "self.__next_f",
)


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

    if len(text) < 200:
        return False
    lower = text.lower()
    if any(m in lower for m in _CHALLENGE_MARKERS):
        return False

    # 有内嵌结构化数据（__NEXT_DATA__ / __NUXT__ / JSON-LD 等）→ extract() 能处理
    if any(m in text for m in _EMBEDDED_DATA_MARKERS):
        return True

    # 有 SPA 壳特征但无内嵌数据 → 不可信，让 auto 链走 deep extract → Playwright
    # 包括: id="__next"/id="root"/self.__next_f/data-reactroot 等
    if any(m in lower for m in _SPA_SHELL_MARKERS):
        return False

    # 无 SPA 壳特征 → 普通页面（可能很小如 Coming Soon），有内容就行
    return True


# ============================================================
# Playwright 浏览器池（懒加载 + 定期回收 + 资源屏蔽）
# ============================================================
_BLOCK_RESOURCE_TYPES = {"image", "stylesheet", "font", "media"}


class _BrowserPool:
    """管理 Playwright Chromium 实例，懒加载、定期回收、stealth 注入。"""

    MAX_AGE = 1800  # 30 分钟强制回收

    def __init__(self, cfg: AppConfig):
        self._cfg = cfg
        self._pw: Any = None
        self._browser: Any = None
        self._page_count = 0
        self._created_at = 0.0

    def _should_recycle(self) -> bool:
        if self._browser is None:
            return False
        age_exceeded = (time.time() - self._created_at) > self.MAX_AGE
        pages_exceeded = self._page_count >= self._cfg.playwright_max_pages
        return age_exceeded or pages_exceeded

    def _ensure_browser(self) -> Any:
        """确保浏览器实例存在且健康，需要时回收重建。"""
        if self._should_recycle():
            self._close_browser()

        if self._browser is not None:
            return self._browser

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("🎭 playwright 未安装，无法启动浏览器渲染")
            return None

        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                ],
            )
            self._page_count = 0
            self._created_at = time.time()
            logger.info("🎭 Playwright Chromium 已启动")
            return self._browser
        except Exception as e:
            logger.warning("🎭 Playwright 启动失败: {}", e)
            self._pw = None
            self._browser = None
            return None

    def render(self, url: str, headers: dict, timeout: int) -> FetchResult:
        """渲染页面并返回完整 HTML。"""
        if not self._cfg.enable_playwright:
            return FetchResult(
                ok=False, url=url, strategy_used="playwright",
                error="Playwright 未启用（ENABLE_PLAYWRIGHT=false）",
            )

        browser = self._ensure_browser()
        if browser is None:
            return FetchResult(
                ok=False, url=url, strategy_used="playwright",
                error="Playwright 未安装或启动失败",
            )

        context = None
        page = None
        try:
            context = browser.new_context(
                user_agent=headers.get("User-Agent", _UA_POOL[0]),
            )

            # 注入 stealth 脚本
            try:
                from playwright_stealth import stealth_sync
                stealth_sync(context)
            except ImportError:
                pass  # stealth 未安装也能用，只是可能被检测

            page = context.new_page()

            # 屏蔽图片/CSS/字体/媒体，节省内存和带宽
            page.route(
                "**/*",
                lambda route: (
                    route.abort()
                    if route.request.resource_type in _BLOCK_RESOURCE_TYPES
                    else route.continue_()
                ),
            )

            page.goto(url, wait_until="networkidle", timeout=timeout * 1000)

            # 等待 DOM 内有实质内容（CSR 站点异步加载数据需要额外时间）
            content = page.content()
            body_text = page.evaluate(
                "() => (document.body && document.body.innerText || '').trim()"
            )
            if len(body_text) < 200:
                # 内容太少，可能异步数据还没加载完，等待 DOM 变化
                try:
                    page.wait_for_function(
                        "() => (document.body && document.body.innerText || '').trim().length >= 200",
                        timeout=8000,
                    )
                    content = page.content()
                except Exception:
                    # 超时也没关系，用已有内容
                    pass

            self._page_count += 1

            return FetchResult(
                ok=True, url=url, status_code=200,
                content=content,
                content_type="text/html",
                strategy_used="playwright",
            )
        except Exception as e:
            return FetchResult(
                ok=False, url=url, strategy_used="playwright",
                error=f"{type(e).__name__}: {e}",
            )
        finally:
            if page:
                try:
                    page.close()
                except Exception:
                    pass
            if context:
                try:
                    context.close()
                except Exception:
                    pass

    def _close_browser(self) -> None:
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None
        self._page_count = 0

    def close(self) -> None:
        """服务停止时调用。"""
        self._close_browser()


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
        self._browser_pool = _BrowserPool(cfg)

    # ----- 对外入口 -----
    def fetch(self, task: Task) -> FetchResult:
        strategy = (task.strategy or "auto").lower()
        headers = _headers(self.cfg.default_headers, task.headers or {})

        if strategy == "playwright":
            return self._fetch_playwright(task, headers)

        if strategy == "curl_cffi":
            result = self._fetch_curl_cffi(task, headers)
            return self._upgrade_if_empty(task, headers, result)
        if strategy == "httpx":
            result = self._fetch_httpx(task, headers)
            return self._upgrade_if_empty(task, headers, result)

        # auto：curl_cffi → httpx → 深度提取 → Playwright
        result = self._fetch_curl_cffi(task, headers)
        if result.ok and _is_content_usable(result, task):
            return result

        result2 = self._fetch_httpx(task, headers)
        if result2.ok and _is_content_usable(result2, task):
            return result2

        # ★ 深度提取：从已拿到的 HTML 中挖掘 SSR 嵌入数据 / RSC Flight
        best_html = result if result.ok else result2
        if best_html.ok and best_html.content:
            deep = self._try_deep_extract(task, best_html)
            if deep is not None:
                return deep

        # ★ Playwright（最终兜底）
        pw_result = self._fetch_playwright(task, headers)
        if pw_result.ok:
            logger.info("🎭 [{}] auto 升级到 Playwright（前置策略均未获得有效内容）", task.name)
            return pw_result

        # 全部失败：返回最有信息的
        if result.ok:
            return result
        result.error = result.error or "所有策略均失败"
        return result

    def _try_deep_extract(
        self, task: Task, html_result: FetchResult,
    ) -> FetchResult | None:
        """对已拿到的 HTML 做深度 SSR 数据提取，成功返回 FetchResult，失败返回 None。"""
        try:
            from .extractor import try_deep_extract
            text = try_deep_extract(html_result.content)
            if text and len(text) >= 120:
                logger.info(
                    "🔍 [{}] 深度提取成功（{} 字，策略={}→deep）",
                    task.name, len(text), html_result.strategy_used,
                )
                return FetchResult(
                    ok=True, url=task.url,
                    status_code=html_result.status_code,
                    content=text,
                    content_type="text/plain",
                    strategy_used=f"{html_result.strategy_used}→deep",
                )
        except Exception as e:
            logger.debug("深度提取异常: {}", e)
        return None

    def _upgrade_if_empty(
        self, task: Task, headers: dict, result: FetchResult,
    ) -> FetchResult:
        """明确指定 curl_cffi/httpx 但拿到空壳时，逐级升级。"""
        if not result.ok or _is_content_usable(result, task):
            return result

        # ★ 先尝试深度提取
        if result.content:
            deep = self._try_deep_extract(task, result)
            if deep is not None:
                return deep

        # 区分原因便于排查
        content = result.content or ""
        if _looks_like_binary_garbage(content):
            reason = f"响应疑似未解压（乱码字节 len={len(content)}）"
        else:
            reason = f"空壳/内容不足（len={len(content)}）"

        # Playwright
        logger.info(
            "🎭 [{}] 策略={} {}，自动升级 Playwright",
            task.name, result.strategy_used, reason,
        )
        pw = self._fetch_playwright(task, headers)
        if pw.ok:
            pw.strategy_used = f"{result.strategy_used}→playwright"
            return pw

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

    # ----- 策略：Playwright（headless Chromium 渲染）-----
    def _fetch_playwright(self, task: Task, headers: dict) -> FetchResult:
        return self._browser_pool.render(
            task.url, headers, self.cfg.playwright_timeout,
        )

    def close(self) -> None:
        """关闭连接池和浏览器。服务停止时调用。"""
        if self._httpx_client is not None:
            try:
                self._httpx_client.close()
            except Exception:
                pass
            self._httpx_client = None
        self._browser_pool.close()
