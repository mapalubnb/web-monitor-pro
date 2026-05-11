"""Multi-strategy fetch engine with auto-escalation."""

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
    ok: bool
    url: str
    status_code: int | None = None
    content: str = ""
    content_type: str = ""
    strategy_used: str = ""
    error: str | None = None
    inner_text: str = ""

    def __bool__(self) -> bool:
        return self.ok


SUPPORTED_IMPERSONATE = (
    "chrome131", "chrome124", "chrome120",
    "firefox133", "firefox135",
    "safari18_0", "safari17_0",
)

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
    try:
        __import__(name)
        return True
    except ImportError:
        return False


_ACCEPT_ENCODING = ", ".join(filter(None, [
    "gzip", "deflate",
    "br" if _can_decode("brotli") or _can_decode("brotlicffi") else None,
    "zstd" if _can_decode("zstandard") else None,
])) or "identity"


def _build_headers(default_headers: dict, user_headers: dict) -> dict:
    h = {"User-Agent": random.choice(_UA_POOL), **_BASE_HEADERS,
         "Accept-Encoding": _ACCEPT_ENCODING}
    h.update(default_headers or {})
    h.update(user_headers or {})
    return h


_EMBEDDED_DATA_MARKERS = (
    "__NEXT_DATA__", "__NUXT_DATA__", "__NUXT__",
    "__APOLLO_STATE__", "__INITIAL_STATE__", "__PRELOADED_STATE__",
    "__REDUX_STATE__", "__INITIAL_DATA__",
    "self.__next_f",
    "data-sveltekit-fetched", "__remixContext",
)

_JSONLD_MARKER = "application/ld+json"

_CHALLENGE_MARKERS = (
    "checking your browser", "cf-challenge", "cf_chl_opt",
    "just a moment", "attention required",
    "/cdn-cgi/challenge-platform",
)

_ERROR_PAGE_MARKERS = (
    "application error: a client-side exception has occurred",
    "this page isn't working",
    "500 internal server error", "502 bad gateway",
    "503 service unavailable", "504 gateway timeout",
    "an unexpected error has occurred", "something went wrong",
    "error: chunk load failed", "unhandled runtime error",
    "hydration failed because", "there was an error while hydrating",
)

_SPA_SHELL_MARKERS = (
    'id="root"', 'id="app"', 'id="__next"', 'id="__nuxt"',
    "data-reactroot", "data-server-rendered", "self.__next_f",
)

_HEAD_RE = re.compile(r"<head[^>]*>.*?</head>", re.DOTALL | re.IGNORECASE)
_SCRIPT_RE = re.compile(r"<script[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
_STYLE_RE = re.compile(r"<style[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE)
_NAV_RE = re.compile(
    r"<(?:nav|footer|header)[^>]*>.*?</(?:nav|footer|header)>",
    re.DOTALL | re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")

_JS_BODY_LEN = "() => (document.body && document.body.innerText || '').trim().length"
_JS_BODY_READY = "() => (document.body && document.body.innerText || '').trim().length >= 200"
_JS_INNER_TEXT = "() => (document.body && document.body.innerText || '').trim()"


def _quick_visible_text(html: str) -> str:
    """Rough visible text from HTML (strips head/script/style/nav)."""
    text = _HEAD_RE.sub("", html)
    for pat in (_SCRIPT_RE, _STYLE_RE, _NAV_RE):
        text = pat.sub("", text)
    return " ".join(_TAG_RE.sub(" ", text).split())


def _looks_like_binary_garbage(text: str) -> bool:
    """Detect undecompressed binary responses (brotli/zstd failures)."""
    if not text:
        return False
    sample = text[:2048]
    bad = sum(1 for ch in sample
              if (ord(ch) < 0x20 and ch not in "\r\n\t")
              or (0x7F <= ord(ch) < 0xA0))
    return bad / len(sample) > 0.30


def _is_error_page(text: str) -> bool:
    """Detect error pages (Error Boundary / 5xx / hydration failures)."""
    if not text:
        return False
    lower = text.lower().strip()
    if len(lower) > 2000:
        return False
    return any(m in lower for m in _ERROR_PAGE_MARKERS)


def _is_content_usable(result: FetchResult, task: Task) -> bool:
    """Check if fetched content is substantial enough (not an SPA shell or garbage)."""
    if not result.ok or not result.content or not result.content.strip():
        return False

    text = result.content
    if _looks_like_binary_garbage(text):
        return False
    if task.type == "json":
        return True
    if len(text) < 200:
        return False

    lower = text.lower()
    if any(m in lower for m in _CHALLENGE_MARKERS):
        return False
    if result.inner_text and _is_error_page(result.inner_text):
        return False

    visible = _quick_visible_text(text)
    if _is_error_page(visible):
        return False

    has_embedded = any(m in text for m in _EMBEDDED_DATA_MARKERS)
    has_jsonld_only = (not has_embedded) and (_JSONLD_MARKER in lower)
    has_spa_shell = any(m in lower for m in _SPA_SHELL_MARKERS)

    if has_embedded:
        if has_spa_shell and len(visible) < 50:
            return False
        return True
    if has_jsonld_only:
        return False if has_spa_shell else len(visible) >= 200
    if has_spa_shell:
        return False
    return True


class _BrowserPool:
    """Manages Playwright Chromium: lazy init, periodic recycling, stealth injection."""

    MAX_AGE = 1800
    _BLOCK_TYPES = {"image", "font", "media"}

    def __init__(self, cfg: AppConfig):
        self._cfg = cfg
        self._pw: Any = None
        self._browser: Any = None
        self._page_count = 0
        self._created_at = 0.0

    def _should_recycle(self) -> bool:
        if self._browser is None:
            return False
        return ((time.time() - self._created_at) > self.MAX_AGE
                or self._page_count >= self._cfg.playwright_max_pages)

    def _ensure_browser(self) -> Any:
        """Ensure browser instance is alive; recycle if stale."""
        if self._should_recycle():
            self.close()
        if self._browser is not None:
            return self._browser

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("playwright not installed")
            return None
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(
                headless=True,
                args=["--disable-gpu", "--disable-dev-shm-usage",
                      "--no-sandbox", "--disable-setuid-sandbox"],
            )
            self._page_count = 0
            self._created_at = time.time()
            logger.info("Playwright Chromium started")
            return self._browser
        except Exception as e:
            logger.warning("Playwright launch failed: {}", e)
            self._pw = self._browser = None
            return None

    def render(self, url: str, headers: dict, timeout: int) -> FetchResult:
        """Render page; auto-retry without resource blocking on error pages."""
        if not self._cfg.enable_playwright:
            return FetchResult(ok=False, url=url, strategy_used="playwright",
                               error="Playwright disabled (ENABLE_PLAYWRIGHT=false)")
        browser = self._ensure_browser()
        if browser is None:
            return FetchResult(ok=False, url=url, strategy_used="playwright",
                               error="Playwright not installed or launch failed")

        result = self._render_once(url, headers, timeout, browser, block_resources=True)
        if result.ok and _is_error_page(result.inner_text):
            logger.info("[{}] error page detected, retrying without resource blocking", url)
            result = self._render_once(url, headers, timeout, browser, block_resources=False)
        return result

    def _render_once(self, url: str, headers: dict, timeout: int,
                     browser: Any, *, block_resources: bool) -> FetchResult:
        context = page = None
        try:
            context = browser.new_context(
                user_agent=headers.get("User-Agent", _UA_POOL[0]),
                viewport={"width": 1920, "height": 1080},
                locale="en-US", timezone_id="America/New_York",
            )
            try:
                from playwright_stealth import stealth_sync
                stealth_sync(context)
            except ImportError:
                pass

            page = context.new_page()
            if block_resources:
                page.route("**/*", lambda route: (
                    route.abort() if route.request.resource_type in self._BLOCK_TYPES
                    else route.continue_()))

            page.goto(url, wait_until="networkidle", timeout=timeout * 1000)

            for _ in range(3):
                if page.evaluate(_JS_BODY_LEN) >= 200:
                    break
                try:
                    page.wait_for_function(_JS_BODY_READY, timeout=5000)
                    break
                except Exception:
                    pass
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass

            inner_text = page.evaluate(_JS_INNER_TEXT)
            content = page.content()
            self._page_count += 1
            return FetchResult(ok=True, url=url, status_code=200, content=content,
                               content_type="text/html", strategy_used="playwright",
                               inner_text=inner_text)
        except Exception as e:
            return FetchResult(ok=False, url=url, strategy_used="playwright",
                               error=f"{type(e).__name__}: {e}")
        finally:
            for obj in (page, context):
                if obj:
                    try:
                        obj.close()
                    except Exception:
                        pass

    def close(self) -> None:
        """Shutdown browser and playwright process."""
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


class FetchEngine:
    """Multi-strategy fetch engine (thread-safe, connection-pooled)."""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.timeout = cfg.request_timeout
        proxy = cfg.https_proxy or cfg.http_proxy or None
        self._proxies = ({"http": cfg.http_proxy or cfg.https_proxy,
                          "https": cfg.https_proxy or cfg.http_proxy} if proxy else None)
        self._httpx_client: Any = None
        self._browser_pool = _BrowserPool(cfg)

    def fetch(self, task: Task) -> FetchResult:
        """Main entry: route to strategy and return result."""
        strategy = (task.strategy or "auto").lower()
        headers = _build_headers(self.cfg.default_headers, task.headers or {})

        if strategy == "playwright":
            return self._fetch_playwright(task, headers)
        if strategy in ("curl_cffi", "httpx"):
            fetcher = self._fetch_curl_cffi if strategy == "curl_cffi" else self._fetch_httpx
            return self._upgrade_if_empty(task, headers, fetcher(task, headers))

        last = getattr(task, "last_strategy_used", None) or ""
        if last:
            preferred = self._fetch_with_last_strategy(task, headers, last)
            if preferred is not None:
                return preferred
            logger.info("[{}] last strategy {} failed, falling back to auto chain", task.name, last)

        return self._auto_chain(task, headers)

    def _fetch_with_last_strategy(self, task: Task, headers: dict,
                                  last: str) -> FetchResult | None:
        """Try the previously successful strategy; return None if it fails."""
        if last == "playwright":
            r = self._fetch_playwright(task, headers)
            return r if r.ok else None

        if last.endswith("\u2192deep"):
            base = last.split("\u2192")[0]
            html = self._fetch_by_name(task, headers, base)
            if html.ok and html.content:
                deep = self._try_deep_extract(task, html)
                if deep is not None:
                    return deep
            return None

        if last.startswith("curl_cffi") or last == "httpx":
            r = self._fetch_by_name(task, headers, last)
            if r.ok and _is_content_usable(r, task):
                return r
            if r.ok and r.content:
                deep = self._try_deep_extract(task, r)
                if deep is not None:
                    return deep
            return None

        return None

    def _fetch_by_name(self, task: Task, headers: dict, name: str) -> FetchResult:
        if name == "httpx":
            return self._fetch_httpx(task, headers)
        if name == "playwright":
            return self._fetch_playwright(task, headers)
        return self._fetch_curl_cffi(task, headers)

    def _auto_chain(self, task: Task, headers: dict) -> FetchResult:
        """Full auto chain: curl_cffi -> httpx -> deep extract -> Playwright."""
        result = self._fetch_curl_cffi(task, headers)
        if result.ok and _is_content_usable(result, task):
            return result

        result2 = self._fetch_httpx(task, headers)
        if result2.ok and _is_content_usable(result2, task):
            return result2

        best_html = result if result.ok else result2
        if best_html.ok and best_html.content:
            deep = self._try_deep_extract(task, best_html)
            if deep is not None:
                return deep

        pw = self._fetch_playwright(task, headers)
        if pw.ok:
            logger.info("[{}] auto escalated to Playwright", task.name)
            return pw

        return result if result.ok else FetchResult(
            ok=False, url=task.url, error=result.error or "all strategies failed",
            strategy_used=result.strategy_used)

    def _try_deep_extract(self, task: Task, html_result: FetchResult) -> FetchResult | None:
        """Extract embedded SSR/RSC data from raw HTML."""
        try:
            from .extractor import try_deep_extract
            text = try_deep_extract(html_result.content)
            if text and len(text) >= 120:
                strategy = f"{html_result.strategy_used}\u2192deep"
                logger.info("[{}] deep extract ok ({} chars, strategy={})",
                            task.name, len(text), strategy)
                return FetchResult(ok=True, url=task.url, status_code=html_result.status_code,
                                   content=text, content_type="text/plain",
                                   strategy_used=strategy)
        except Exception:
            pass
        return None

    def _upgrade_if_empty(self, task: Task, headers: dict, result: FetchResult) -> FetchResult:
        """For explicitly specified curl_cffi/httpx: escalate if content is unusable."""
        if not result.ok or _is_content_usable(result, task):
            return result

        if result.content:
            deep = self._try_deep_extract(task, result)
            if deep is not None:
                return deep

        reason = ("binary garbage" if _looks_like_binary_garbage(result.content or "")
                  else f"shell/insufficient (len={len(result.content or '')})")
        logger.info("[{}] strategy={} {}, upgrading to Playwright",
                    task.name, result.strategy_used, reason)

        pw = self._fetch_playwright(task, headers)
        if pw.ok:
            pw.strategy_used = f"{result.strategy_used}\u2192playwright"
            return pw
        return result

    def _fetch_curl_cffi(self, task: Task, headers: dict) -> FetchResult:
        try:
            from curl_cffi import requests as cc
        except ImportError:
            return FetchResult(ok=False, url=task.url, strategy_used="curl_cffi",
                               error="curl_cffi not installed")

        impersonate = task.impersonate or "chrome131"
        if impersonate not in SUPPORTED_IMPERSONATE:
            impersonate = "chrome131"
        tag = f"curl_cffi/{impersonate}"

        try:
            resp = cc.get(task.url, headers=headers, impersonate=impersonate,
                          timeout=self.timeout, proxies=self._proxies, allow_redirects=True)
            return FetchResult(ok=200 <= resp.status_code < 300, url=task.url,
                               status_code=resp.status_code, content=resp.text,
                               content_type=resp.headers.get("content-type", ""),
                               strategy_used=tag)
        except Exception as e:
            return FetchResult(ok=False, url=task.url, strategy_used=tag,
                               error=f"{type(e).__name__}: {e}")

    def _fetch_httpx(self, task: Task, headers: dict) -> FetchResult:
        client = self._get_httpx_client()
        if client is None:
            return FetchResult(ok=False, url=task.url, strategy_used="httpx",
                               error="httpx not installed")
        try:
            resp = client.get(task.url, headers=headers)
            return FetchResult(ok=200 <= resp.status_code < 300, url=task.url,
                               status_code=resp.status_code, content=resp.text,
                               content_type=resp.headers.get("content-type", ""),
                               strategy_used="httpx")
        except Exception as e:
            return FetchResult(ok=False, url=task.url, strategy_used="httpx",
                               error=f"{type(e).__name__}: {e}")

    def _get_httpx_client(self):
        if self._httpx_client is None:
            try:
                import httpx
            except ImportError:
                return None
            self._httpx_client = httpx.Client(
                http2=True, timeout=self.timeout, follow_redirects=True,
                proxy=self.cfg.https_proxy or self.cfg.http_proxy or None,
                limits=httpx.Limits(max_keepalive_connections=10, max_connections=20))
        return self._httpx_client

    def _fetch_playwright(self, task: Task, headers: dict) -> FetchResult:
        return self._browser_pool.render(task.url, headers, self.cfg.playwright_timeout)

    def close(self) -> None:
        """Shutdown connections and browser."""
        if self._httpx_client:
            try:
                self._httpx_client.close()
            except Exception:
                pass
            self._httpx_client = None
        self._browser_pool.close()
