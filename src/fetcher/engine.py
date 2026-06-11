"""Multi-strategy fetch engine with auto-escalation."""

from __future__ import annotations

import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import urljoin

from ..config import AppConfig
from ..db import Task
from ..logger import logger
from ..proxy_pool import FreeProxyPool


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

SCRAPLING_STRATEGIES = (
    "scrapling_static",
    "scrapling_dynamic",
    "scrapling_stealth",
    "scrapling_auto",
)

SUPPORTED_STRATEGIES = (
    "auto",
    "httpx",
    "curl_cffi",
    "playwright",
    *SCRAPLING_STRATEGIES,
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
    "javascript is disabled",
    "verify that you're not a robot",
    "verify that you are not a robot",
    "this requires javascript",
    "enable javascript and then reload the page",
    "please enable js and disable any ad blocker",
    "robot check",
    "anti-bot",
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

_ACCESS_DENIED_MARKERS = (
    "you're not authorized to access this page",
    "you are not authorized to access this page",
    "not authorized to access this page",
    "you don't have access to this page",
    "you do not have access to this page",
    "you do not have permission to access this page",
    "you don't have permission to access this page",
    "you need permission to access this page",
    "access to this page is restricted",
    "this page is private",
    "this page is not public",
    "private page",
    "access denied",
    "403 forbidden",
    "sign in to access this page",
    "log in to access this page",
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
_MARKDOWN_ALTERNATE_RE = re.compile(
    r"<link\b"
    r"(?=[^>]*\brel=[\"'][^\"']*\balternate\b[^\"']*[\"'])"
    r"(?=[^>]*\btype=[\"']text/markdown[\"'])"
    r"(?=[^>]*\bhref=[\"']([^\"']+)[\"'])"
    r"[^>]*>",
    re.IGNORECASE,
)

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


def _is_access_denied_text(text: str) -> bool:
    """Detect auth/permission placeholder pages served with HTTP 200."""
    if not text:
        return False
    lower = " ".join(text.lower().split())
    if not any(m in lower for m in _ACCESS_DENIED_MARKERS):
        return False
    # Avoid false positives in long articles that merely discuss "access denied".
    return len(lower) < 5000 or "you're not authorized to access this page" in lower


def _is_challenge_text(text: str) -> bool:
    """Detect bot/JS verification pages that may still return HTTP 200."""
    if not text:
        return False
    lower = " ".join(text.lower().split())
    return any(m in lower for m in _CHALLENGE_MARKERS)


def _find_markdown_alternate(html: str, base_url: str) -> str | None:
    """Find a page-declared Markdown alternate URL, used by GitBook-style docs."""
    if not html:
        return None
    match = _MARKDOWN_ALTERNATE_RE.search(html)
    if not match:
        return None
    return urljoin(base_url, unescape(match.group(1)))


def _is_markdown_not_found(text: str) -> bool:
    lower = " ".join((text or "").lower().split())
    return lower.startswith("# page not found") or (
        "## suggested pages" in lower and "does not exist" in lower
    )


def _is_content_usable(result: FetchResult, task: Task) -> bool:
    """Check if fetched content is substantial enough (not an SPA shell or garbage)."""
    if not result.ok or not result.content or not result.content.strip():
        return False

    text = result.content
    if _is_access_denied_text(text) or _is_access_denied_text(result.inner_text):
        return False
    if _looks_like_binary_garbage(text):
        return False
    if task.type == "json":
        return True
    if len(text) < 200:
        return False

    lower = text.lower()
    if _is_challenge_text(lower):
        return False
    if result.inner_text and _is_error_page(result.inner_text):
        return False

    visible = _quick_visible_text(text)
    if _is_access_denied_text(visible):
        return False
    if _is_error_page(visible):
        return False

    has_embedded = any(m in text for m in _EMBEDDED_DATA_MARKERS)
    # RSC Flight with BAILOUT_TO_CLIENT_SIDE_RENDERING means the embedded RSC markers
    # (self.__next_f) are present but contain no extractable content — treat as no data.
    if has_embedded and "BAILOUT_TO_CLIENT_SIDE_RENDERING" in text:
        has_rsc_only = all(
            m not in text for m in _EMBEDDED_DATA_MARKERS if m != "self.__next_f"
        )
        if has_rsc_only:
            has_embedded = False
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
    """Manages Playwright Chromium rendering on a dedicated single thread.

    Playwright sync API is thread-bound — a browser created in thread A cannot
    be used from thread B. APScheduler dispatches tasks to a threadpool, so
    different tasks (or the same task on successive runs) may land on different
    threads, which used to force the browser to be closed and relaunched on
    every thread switch.

    To avoid that churn, we pin Playwright to a single dedicated thread via a
    ``ThreadPoolExecutor(max_workers=1)``. Every render call is submitted to
    this executor and blocks for its result; the browser is therefore created
    and reused on exactly one thread across the entire process.

    The executor naturally serialises render calls (only one in flight at any
    time), so we no longer need a separate lock for the browser lifecycle.
    """

    MAX_AGE = 1800
    _BLOCK_TYPES = {"image", "font", "media"}
    # External domains whose resources often hang in restricted networks
    # (e.g. mainland China) and are non-essential for content extraction.
    _BLOCK_DOMAINS = (
        "fonts.googleapis.com", "fonts.gstatic.com",
        "www.googletagmanager.com", "www.google-analytics.com",
        "analytics.google.com", "connect.facebook.net",
        "platform.twitter.com",
    )

    def __init__(self, cfg: AppConfig):
        self._cfg = cfg
        self._pw: Any = None
        self._browser: Any = None
        self._page_count = 0
        self._created_at = 0.0
        self._executor: ThreadPoolExecutor | None = None
        self._exec_lock = threading.Lock()

    def _get_executor(self) -> ThreadPoolExecutor:
        """Return the dedicated single-thread executor (lazy init)."""
        if self._executor is None:
            with self._exec_lock:
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(
                        max_workers=1,
                        thread_name_prefix="playwright",
                    )
        return self._executor

    def _should_recycle(self) -> bool:
        """Must be called from the executor thread."""
        if self._browser is None:
            return False
        return ((time.time() - self._created_at) > self.MAX_AGE
                or self._page_count >= self._cfg.playwright_max_pages)

    def _proxy_config(self) -> dict | None:
        """Build Playwright proxy config from AppConfig (HTTP/HTTPS proxy)."""
        proxy_url = self._cfg.https_proxy or self._cfg.http_proxy
        if not proxy_url:
            return None
        return {"server": proxy_url}

    def _ensure_browser(self) -> Any:
        """Ensure browser instance is alive on the executor thread."""
        if self._should_recycle():
            self._close_internal()
        if self._browser is not None:
            return self._browser

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("playwright not installed")
            return None
        try:
            self._pw = sync_playwright().start()
            launch_kwargs: dict[str, Any] = dict(
                headless=True,
                args=["--disable-gpu", "--disable-dev-shm-usage",
                      "--no-sandbox", "--disable-setuid-sandbox"],
            )
            proxy_cfg = self._proxy_config()
            if proxy_cfg:
                launch_kwargs["proxy"] = proxy_cfg
            self._browser = self._pw.chromium.launch(**launch_kwargs)
            self._page_count = 0
            self._created_at = time.time()
            proxy_note = f" via proxy {proxy_cfg['server']}" if proxy_cfg else ""
            logger.info("Playwright Chromium started (thread={}){}",
                        threading.get_ident(), proxy_note)
            return self._browser
        except Exception as e:
            logger.warning("Playwright launch failed: {}", e)
            self._pw = self._browser = None
            return None

    def render(self, url: str, headers: dict, timeout: int) -> FetchResult:
        """Submit the render job to the dedicated Playwright thread."""
        if not self._cfg.enable_playwright:
            return FetchResult(ok=False, url=url, strategy_used="playwright",
                               error="Playwright disabled (ENABLE_PLAYWRIGHT=false)")

        executor = self._get_executor()
        future = executor.submit(self._render_on_thread, url, headers, timeout)
        try:
            # Allow generous headroom above the configured timeout to cover
            # browser launch + retry without blocking forever.
            return future.result(timeout=timeout * 2 + 30)
        except Exception as e:
            return FetchResult(ok=False, url=url, strategy_used="playwright",
                               error=f"executor error: {type(e).__name__}: {e}")

    def _render_on_thread(self, url: str, headers: dict, timeout: int) -> FetchResult:
        """Runs on the dedicated Playwright thread."""
        browser = self._ensure_browser()
        if browser is None:
            return FetchResult(ok=False, url=url, strategy_used="playwright",
                               error="Playwright not installed or launch failed")

        result = self._render_once(url, headers, timeout, browser, block_resources=True)
        if result.ok and _is_error_page(result.inner_text):
            logger.info("[{}] error page detected, retrying without resource blocking", url)
            result = self._render_once(url, headers, timeout, browser, block_resources=False)

        # If stealth + route-handler mode timed out, retry in bare mode
        # (no stealth, no route interception).  page.route("**/*") can
        # interfere with navigation events on some sites, causing goto to
        # hang until timeout even though the site is reachable.
        if not result.ok and "Timeout" in (result.error or ""):
            logger.info("[{}] stealth mode timed out, retrying in bare mode", url)
            result = self._render_bare(url, headers, timeout, browser)
        return result

    def _render_once(self, url: str, headers: dict, timeout: int,
                     browser: Any, *, block_resources: bool) -> FetchResult:
        """Full render with stealth + route interception."""
        return self._render_core(
            url, headers, timeout, browser,
            use_stealth=True, block_resources=block_resources,
            error_prefix="",
        )

    def _render_bare(self, url: str, headers: dict, timeout: int,
                     browser: Any) -> FetchResult:
        """Bare-bones render: no stealth, no route interception.

        Fallback when stealth + routing causes navigation timeouts.
        """
        result = self._render_core(
            url, headers, timeout, browser,
            use_stealth=False, block_resources=False,
            error_prefix="bare: ",
        )
        if result.ok:
            logger.info("[{}] bare mode render ok ({} chars)", url, len(result.inner_text))
        return result

    def _render_core(self, url: str, headers: dict, timeout: int,
                     browser: Any, *, use_stealth: bool,
                     block_resources: bool, error_prefix: str) -> FetchResult:
        """Shared render logic for both stealth and bare modes."""
        context = page = None
        try:
            user_agent = headers.get("__override_user_agent__")
            ctx_kwargs: dict[str, Any] = dict(
                viewport={"width": 1920, "height": 1080},
                locale="en-US", timezone_id="America/New_York",
            )
            if user_agent:
                ctx_kwargs["user_agent"] = user_agent
            context = browser.new_context(**ctx_kwargs)

            if use_stealth:
                try:
                    from playwright_stealth import stealth_sync
                    stealth_sync(context)
                except ImportError:
                    pass

            page = context.new_page()

            if use_stealth or block_resources:
                _blocked_types = self._BLOCK_TYPES if block_resources else set()
                _blocked_domains = self._BLOCK_DOMAINS

                def _route_handler(route):
                    req = route.request
                    if _blocked_types and req.resource_type in _blocked_types:
                        return route.abort()
                    try:
                        from urllib.parse import urlparse
                        host = urlparse(req.url).hostname or ""
                        if host in _blocked_domains:
                            return route.abort()
                    except Exception:
                        pass
                    return route.continue_()

                page.route("**/*", _route_handler)

            # Graduated wait strategy
            ms = timeout * 1000
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=ms)
            except Exception as goto_err:
                if "Timeout" not in type(goto_err).__name__:
                    raise
                logger.info("[{}] domcontentloaded timeout, retrying with commit", url)
                page.goto(url, wait_until="commit", timeout=ms)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=ms)
                except Exception:
                    pass

            # Best-effort networkidle
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass

            # Poll for body content
            for _ in range(5):
                if page.evaluate(_JS_BODY_LEN) >= 200:
                    break
                try:
                    page.wait_for_function(_JS_BODY_READY, timeout=5000)
                    break
                except Exception:
                    pass
                try:
                    page.wait_for_load_state("networkidle", timeout=3000)
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
                               error=f"{error_prefix}{type(e).__name__}: {e}")
        finally:
            for obj in (page, context):
                if obj:
                    try:
                        obj.close()
                    except Exception:
                        pass

    def _close_internal(self) -> None:
        """Shutdown browser — must run on the executor thread."""
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
        """Public shutdown (e.g. on application exit)."""
        # If executor was never created but browser was somehow started
        # (shouldn't happen, but defensive), clean up directly.
        if self._executor is None:
            self._close_internal()
            return
        try:
            self._executor.submit(self._close_internal).result(timeout=10)
        except Exception as e:
            logger.warning("Playwright shutdown error: {}", e)
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None


class FetchEngine:
    """Multi-strategy fetch engine (thread-safe, connection-pooled)."""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.timeout = cfg.request_timeout
        self._httpx_client: Any = None
        self._httpx_clients: dict[str, Any] = {}
        self._free_proxy_pool = FreeProxyPool(cfg)
        self._browser_pool = _BrowserPool(cfg)

    def _select_proxy(self, allowed_schemes: set[str] | None = None) -> str | None:
        """Return explicit proxy first, otherwise an optional free-pool proxy."""
        if self.cfg.https_proxy or self.cfg.http_proxy:
            return self.cfg.https_proxy or self.cfg.http_proxy
        return self._free_proxy_pool.get_proxy(allowed_schemes=allowed_schemes)

    @staticmethod
    def _proxy_dict(proxy_url: str | None) -> dict | None:
        if not proxy_url:
            return None
        return {"http": proxy_url, "https": proxy_url}

    def fetch(self, task: Task) -> FetchResult:
        """Main entry: route to strategy and return result."""
        strategy = (task.strategy or "auto").lower()
        headers = _build_headers(self.cfg.default_headers, task.headers or {})

        if strategy == "playwright":
            return self._fetch_playwright(task, headers)
        if strategy in SCRAPLING_STRATEGIES:
            return self._upgrade_if_empty(task, headers, self._fetch_scrapling(task, headers, strategy))
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
            return r if r.ok and _is_content_usable(r, task) else None
        if last in SCRAPLING_STRATEGIES or last.startswith("scrapling_"):
            r = self._fetch_scrapling(task, headers, last.split("→")[0])
            return r if r.ok and _is_content_usable(r, task) else None

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
        if name in SCRAPLING_STRATEGIES or name.startswith("scrapling_"):
            return self._fetch_scrapling(task, headers, name)
        return self._fetch_curl_cffi(task, headers)

    def _auto_chain(self, task: Task, headers: dict) -> FetchResult:
        """Full auto chain: curl_cffi -> httpx -> deep extract -> Playwright."""
        result = self._fetch_curl_cffi(task, headers)
        if result.ok and _is_content_usable(result, task):
            return result
        if result.ok and not _is_content_usable(result, task):
            logger.debug("[{}] curl_cffi got response (status={}, len={}) but content unusable",
                         task.name, result.status_code, len(result.content or ""))

        result2 = self._fetch_httpx(task, headers)
        if result2.ok and _is_content_usable(result2, task):
            return result2

        best_html = result if result.ok else result2
        if best_html.ok and best_html.content:
            markdown = self._try_markdown_alternate(task, best_html)
            if markdown is not None:
                return markdown
            deep = self._try_deep_extract(task, best_html)
            if deep is not None:
                return deep

        scrapling_result = self._fetch_scrapling(task, headers, "scrapling_auto")
        if scrapling_result.ok and _is_content_usable(scrapling_result, task):
            logger.info("[{}] auto escalated to {}", task.name, scrapling_result.strategy_used)
            return scrapling_result
        if scrapling_result.ok and scrapling_result.content:
            markdown = self._try_markdown_alternate(task, scrapling_result)
            if markdown is not None:
                return markdown
            deep = self._try_deep_extract(task, scrapling_result)
            if deep is not None:
                return deep

        pw = self._fetch_playwright(task, headers)
        if pw.ok and _is_content_usable(pw, task):
            logger.info("[{}] auto escalated to Playwright", task.name)
            return pw

        # All strategies exhausted.  Last resort: extract meta tags (title,
        # description, og:*) from the SPA shell so the task at least has a
        # baseline to diff against.  This is better than total failure for
        # pure-CSR sites when Playwright cannot render (e.g. blocked by
        # network restrictions).
        best = result if result.ok else result2
        if best.ok and not _is_content_usable(best, task):
            meta = self._try_meta_extract(task, best)
            if meta is not None:
                pw_err = pw.error or ""
                logger.info("[{}] all strategies failed, using meta-tag fallback "
                            "(playwright: {})", task.name, pw_err)
                return meta

            pw_err = pw.error or ""
            reason = self._unusable_reason(best)
            if pw.ok and not _is_content_usable(pw, task):
                reason = self._unusable_reason(pw)
            logger.warning("[{}] all strategies failed ({}, playwright: {})",
                           task.name, reason, pw_err)
            return FetchResult(
                ok=False, url=task.url, strategy_used=best.strategy_used,
                error=f"{reason}; playwright failed or returned unusable content ({pw_err})")

        return result if result.ok else FetchResult(
            ok=False, url=task.url, error=result.error or "all strategies failed",
            strategy_used=result.strategy_used)

    def _try_deep_extract(self, task: Task, html_result: FetchResult) -> FetchResult | None:
        """Extract embedded SSR/RSC data from raw HTML."""
        try:
            from .extractor import try_deep_extract
            if _is_access_denied_text(html_result.content):
                logger.info("[{}] deep extract skipped access-denied page", task.name)
                return None
            text = try_deep_extract(html_result.content)
            if text and len(text) >= 120:
                if _is_access_denied_text(text):
                    logger.info("[{}] deep extract rejected access-denied page", task.name)
                    return None
                strategy = f"{html_result.strategy_used}\u2192deep"
                logger.info("[{}] deep extract ok ({} chars, strategy={})",
                            task.name, len(text), strategy)
                return FetchResult(ok=True, url=task.url, status_code=html_result.status_code,
                                   content=text, content_type="text/plain",
                                   strategy_used=strategy)
        except Exception:
            pass
        return None

    def _try_markdown_alternate(self, task: Task, html_result: FetchResult) -> FetchResult | None:
        """Fetch a declared Markdown alternate, common on GitBook documentation pages."""
        markdown_url = _find_markdown_alternate(html_result.content, html_result.url or task.url)
        if not markdown_url:
            return None

        try:
            proxy_url = self._select_proxy(allowed_schemes={"http", "https"})
            client = self._get_httpx_client(proxy_url)
            if client is None:
                return None
            resp = client.get(markdown_url, headers={
                "Accept": "text/markdown,text/plain;q=0.9,text/html;q=0.5,*/*;q=0.1",
                "User-Agent": random.choice(_UA_POOL),
            })
            text = resp.text or ""
            if not (200 <= resp.status_code < 300):
                return None
            if len(text.strip()) < 30:
                return None
            if _is_access_denied_text(text) or _is_markdown_not_found(text):
                return None
            strategy = f"{html_result.strategy_used}→markdown"
            logger.info("[{}] markdown alternate ok ({} chars, url={})",
                        task.name, len(text), markdown_url)
            return FetchResult(
                ok=True,
                url=task.url,
                status_code=resp.status_code,
                content=text,
                content_type=resp.headers.get("content-type", "text/markdown"),
                strategy_used=strategy,
            )
        except Exception as e:
            logger.debug("[{}] markdown alternate failed: {}", task.name, e)
            return None

    def _try_meta_extract(self, task: Task, html_result: FetchResult) -> FetchResult | None:
        """Last-resort fallback: extract meta tags from an SPA shell.

        When we have HTML but both _is_content_usable and Playwright fail,
        meta tags (title, og:title, og:description, etc.) still give us a
        diffable baseline.  Returns None if nothing useful can be extracted.
        """
        try:
            from .extractor import extract_meta_fallback
            if _is_access_denied_text(html_result.content):
                return None
            text = extract_meta_fallback(html_result.content)
            if text and len(text) >= 30:
                if _is_access_denied_text(text):
                    return None
                strategy = f"{html_result.strategy_used}→meta"
                logger.info("[{}] meta-tag fallback ok ({} chars)", task.name, len(text))
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
            markdown = self._try_markdown_alternate(task, result)
            if markdown is not None:
                return markdown
            deep = self._try_deep_extract(task, result)
            if deep is not None:
                return deep
            stealth = self._try_scrapling_stealth_upgrade(task, headers, result)
            if stealth is not None:
                return stealth

        reason = ("binary garbage" if _looks_like_binary_garbage(result.content or "")
                  else f"shell/insufficient (len={len(result.content or '')})")
        logger.info("[{}] strategy={} {}, upgrading to Playwright",
                    task.name, result.strategy_used, reason)

        pw = self._fetch_playwright(task, headers)
        if pw.ok and _is_content_usable(pw, task):
            pw.strategy_used = f"{result.strategy_used}\u2192playwright"
            return pw
        return FetchResult(
            ok=False,
            url=task.url,
            status_code=result.status_code,
            strategy_used=result.strategy_used,
            error=self._unusable_reason(pw if pw.ok else result),
        )

    def _try_scrapling_stealth_upgrade(
        self, task: Task, headers: dict, result: FetchResult
    ) -> FetchResult | None:
        """Use Scrapling stealth for bot/JS challenge pages before Playwright fallback."""
        if not _is_challenge_text(result.content or result.inner_text):
            return None
        stealth = self._fetch_scrapling(task, headers, "scrapling_stealth")
        if stealth.ok and _is_content_usable(stealth, task):
            stealth.strategy_used = f"{result.strategy_used}→scrapling_stealth"
            return stealth
        return None

    @staticmethod
    def _unusable_reason(result: FetchResult) -> str:
        content = result.inner_text or result.content or ""
        if _is_access_denied_text(content):
            return "access denied / unauthorized page detected"
        if _is_challenge_text(content):
            return "bot challenge / JavaScript verification page detected"
        if _looks_like_binary_garbage(result.content or ""):
            return "binary garbage response"
        return f"content unusable (len={len(result.content or '')})"

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
            proxy_url = self._select_proxy()
            resp = cc.get(task.url, headers=headers, impersonate=impersonate,
                          timeout=self.timeout, proxies=self._proxy_dict(proxy_url),
                          allow_redirects=True)
            return FetchResult(ok=200 <= resp.status_code < 300, url=task.url,
                               status_code=resp.status_code, content=resp.text,
                               content_type=resp.headers.get("content-type", ""),
                               strategy_used=tag)
        except Exception as e:
            return FetchResult(ok=False, url=task.url, strategy_used=tag,
                               error=f"{type(e).__name__}: {e}")

    def _fetch_httpx(self, task: Task, headers: dict) -> FetchResult:
        proxy_url = self._select_proxy(allowed_schemes={"http", "https"})
        client = self._get_httpx_client(proxy_url)
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

    def _get_httpx_client(self, proxy_url: str | None = None):
        if proxy_url and not proxy_url.startswith(("http://", "https://")):
            proxy_url = None
        if proxy_url:
            if proxy_url in self._httpx_clients:
                return self._httpx_clients[proxy_url]
        elif self._httpx_client is not None:
            return self._httpx_client

        try:
            import httpx
        except ImportError:
            return None

        client = httpx.Client(
            http2=True, timeout=self.timeout, follow_redirects=True,
            proxy=proxy_url or self.cfg.https_proxy or self.cfg.http_proxy or None,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20))

        if proxy_url:
            # Keep the cache bounded; free proxies churn quickly.
            if len(self._httpx_clients) >= 20:
                _, old = self._httpx_clients.popitem()
                try:
                    old.close()
                except Exception:
                    pass
            self._httpx_clients[proxy_url] = client
        else:
            self._httpx_client = client
        return client

    def _fetch_playwright(self, task: Task, headers: dict) -> FetchResult:
        # Pass through the user-supplied UA only if it was explicitly set on
        # the task, to keep Chromium's native UA consistent with Sec-CH-UA
        # client hints. We re-key it to avoid collision with the random UA
        # that `_build_headers` injects for the HTTP-based strategies.
        pw_headers = dict(headers)
        user_headers = task.headers or {}
        explicit_ua = next(
            (v for k, v in user_headers.items() if k.lower() == "user-agent"),
            None,
        )
        if explicit_ua:
            pw_headers["__override_user_agent__"] = explicit_ua
        return self._browser_pool.render(task.url, pw_headers, self.cfg.playwright_timeout)

    def _fetch_scrapling(self, task: Task, headers: dict, strategy: str) -> FetchResult:
        """Fetch with Scrapling's static/dynamic/stealth fetchers.

        Scrapling is an optional enhancement layer.  Keep failures contained so
        existing deployments can run even before installing the extra package.
        """
        if not self.cfg.enable_scrapling:
            return FetchResult(ok=False, url=task.url, strategy_used=strategy,
                               error="Scrapling disabled (ENABLE_SCRAPLING=false)")

        strategy = strategy if strategy in SCRAPLING_STRATEGIES else "scrapling_static"
        if strategy == "scrapling_auto":
            first = self._fetch_scrapling(task, headers, "scrapling_static")
            if first.ok and _is_content_usable(first, task):
                return first
            second = self._fetch_scrapling(task, headers, "scrapling_stealth")
            return second if second.ok else first

        try:
            if strategy == "scrapling_static":
                from scrapling.fetchers import Fetcher
                proxy_url = self._select_proxy()
                page = Fetcher.get(
                    task.url,
                    headers=headers,
                    impersonate=task.impersonate or "chrome131",
                    timeout=self.timeout,
                    proxy=proxy_url,
                    selector_config=self._scrapling_selector_config(task),
                )
            elif strategy == "scrapling_dynamic":
                from scrapling.fetchers import DynamicFetcher
                proxy_url = self._select_proxy()
                page = DynamicFetcher.fetch(
                    task.url,
                    headless=True,
                    network_idle=True,
                    disable_resources=True,
                    timeout=self.cfg.playwright_timeout * 1000,
                    wait_selector=task.wait_selector or None,
                    extra_headers=headers,
                    proxy=proxy_url,
                    selector_config=self._scrapling_selector_config(task),
                )
            else:
                from scrapling.fetchers import StealthyFetcher
                proxy_url = self._select_proxy()
                page = StealthyFetcher.fetch(
                    task.url,
                    headless=True,
                    network_idle=True,
                    disable_resources=True,
                    solve_cloudflare=True,
                    block_webrtc=True,
                    timeout=self.cfg.playwright_timeout * 1000,
                    wait_selector=task.wait_selector or None,
                    extra_headers=headers,
                    proxy=proxy_url,
                    selector_config=self._scrapling_selector_config(task),
                )
        except ImportError:
            return FetchResult(ok=False, url=task.url, strategy_used=strategy,
                               error="scrapling not installed")
        except Exception as e:
            return FetchResult(ok=False, url=task.url, strategy_used=strategy,
                               error=f"{type(e).__name__}: {e}")

        status = getattr(page, "status", None) or getattr(page, "status_code", None)
        headers_out = getattr(page, "headers", {}) or {}
        content_type = ""
        if isinstance(headers_out, dict):
            content_type = headers_out.get("content-type", "") or headers_out.get("Content-Type", "")
        content = self._scrapling_body_to_text(page)
        inner_text = self._scrapling_inner_text(page)
        ok = status is None or 200 <= int(status) < 300
        return FetchResult(
            ok=ok,
            url=task.url,
            status_code=int(status) if status is not None else None,
            content=content,
            content_type=content_type,
            strategy_used=strategy,
            inner_text=inner_text,
        )

    @staticmethod
    def _scrapling_body_to_text(page: Any) -> str:
        body = getattr(page, "body", b"")
        encoding = getattr(page, "encoding", None) or "utf-8"
        if isinstance(body, bytes):
            text = body.decode(encoding, errors="replace")
        else:
            text = str(body or "")
        if text.strip():
            return text
        try:
            return str(page)
        except Exception:
            return ""

    @staticmethod
    def _scrapling_inner_text(page: Any) -> str:
        try:
            return str(page.get_all_text(separator="\n", strip=True))
        except Exception:
            return ""

    @staticmethod
    def _scrapling_selector_config(task: Task) -> dict:
        from ..config import DATA_DIR
        return {
            "adaptive": bool(getattr(task, "adaptive_selector", False)),
            "storage_args": {
                "storage_file": str(DATA_DIR / "scrapling_adaptive.db"),
                "url": task.url,
            },
        }

    def close(self) -> None:
        """Shutdown connections and browser."""
        if self._httpx_client:
            try:
                self._httpx_client.close()
            except Exception:
                pass
            self._httpx_client = None
        for client in self._httpx_clients.values():
            try:
                client.close()
            except Exception:
                pass
        self._httpx_clients.clear()
        self._browser_pool.close()
