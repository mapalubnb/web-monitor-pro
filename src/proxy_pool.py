"""Optional rotating proxy pool backed by Proxifly's free proxy list."""

from __future__ import annotations

import random
import threading
import time
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .config import DATA_DIR, AppConfig
from .logger import logger

_SUPPORTED_SCHEMES = {"http", "https", "socks4", "socks5"}


def parse_proxy_lines(text: str, *, max_count: int = 200) -> list[str]:
    """Parse Proxifly txt data into normalized proxy URLs."""
    seen: set[str] = set()
    proxies: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parsed = urlparse(line)
        if parsed.scheme not in _SUPPORTED_SCHEMES or not parsed.hostname or not parsed.port:
            continue
        proxy = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        if proxy in seen:
            continue
        seen.add(proxy)
        proxies.append(proxy)
        if len(proxies) >= max_count:
            break
    return proxies


class FreeProxyPool:
    """Lazy, thread-safe proxy list refresher and round-robin selector."""

    def __init__(self, cfg: AppConfig):
        self.enabled = cfg.enable_free_proxy_pool
        self.source_url = cfg.free_proxy_source_url
        self.refresh_seconds = max(cfg.free_proxy_refresh_seconds, 60)
        self.max_count = max(cfg.free_proxy_max_count, 1)
        self.cache_path = DATA_DIR / "free_proxy_pool.txt"
        self._lock = threading.Lock()
        self._proxies: list[str] = []
        self._idx = 0
        self._last_refresh = 0.0

    def get_proxy(self, allowed_schemes: set[str] | None = None) -> str | None:
        """Return the next proxy URL, or None when disabled/unavailable."""
        if not self.enabled:
            return None
        with self._lock:
            self._refresh_if_needed()
            if not self._proxies:
                return None
            for _ in range(len(self._proxies)):
                proxy = self._proxies[self._idx % len(self._proxies)]
                self._idx = (self._idx + 1) % len(self._proxies)
                scheme = urlparse(proxy).scheme
                if not allowed_schemes or scheme in allowed_schemes:
                    return proxy
            return None

    def _refresh_if_needed(self) -> None:
        now = time.time()
        if self._proxies and (now - self._last_refresh) < self.refresh_seconds:
            return

        proxies = self._download()
        if proxies:
            random.shuffle(proxies)
            self._proxies = proxies
            self._last_refresh = now
            self._write_cache(proxies)
            logger.info("free proxy pool refreshed: {} proxies", len(proxies))
            return

        cached = self._read_cache()
        if cached:
            self._proxies = cached
            self._last_refresh = now
            logger.warning("free proxy pool using cached list: {} proxies", len(cached))

    def _download(self) -> list[str]:
        req = Request(
            self.source_url,
            headers={"User-Agent": "web-monitor-pro/0.4"},
        )
        try:
            with urlopen(req, timeout=15) as resp:
                body = resp.read(2_000_000).decode("utf-8", errors="replace")
        except (OSError, URLError) as e:
            logger.warning("free proxy pool refresh failed: {}", e)
            return []
        return parse_proxy_lines(body, max_count=self.max_count)

    def _write_cache(self, proxies: list[str]) -> None:
        try:
            self.cache_path.write_text("\n".join(proxies) + "\n", encoding="utf-8")
        except Exception as e:
            logger.debug("failed to write free proxy cache: {}", e)

    def _read_cache(self) -> list[str]:
        if not self.cache_path.exists():
            return []
        try:
            return parse_proxy_lines(
                self.cache_path.read_text(encoding="utf-8", errors="replace"),
                max_count=self.max_count,
            )
        except Exception:
            return []


__all__ = ["FreeProxyPool", "parse_proxy_lines"]
