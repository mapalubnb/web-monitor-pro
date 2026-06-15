import time

from src.config import AppConfig, load_config
from src.fetcher.engine import _BrowserPool


def test_playwright_idle_seconds_env_switch(monkeypatch, tmp_path):
    monkeypatch.setenv("PLAYWRIGHT_IDLE_SECONDS", "120")

    cfg = load_config(
        env_path=tmp_path / ".env",
        yaml_path=tmp_path / "config.yaml",
    )

    assert cfg.playwright_idle_seconds == 120


def test_browser_pool_closes_idle_browser():
    cfg = AppConfig(playwright_idle_seconds=1)
    pool = _BrowserPool(cfg)

    class Browser:
        closed = False

        def close(self):
            self.closed = True

    class Playwright:
        stopped = False

        def stop(self):
            self.stopped = True

    browser = Browser()
    playwright = Playwright()
    pool._browser = browser
    pool._pw = playwright
    pool._last_used_at = time.time() - 5

    pool._close_if_idle(pool._last_used_at)

    assert browser.closed is True
    assert playwright.stopped is True
    assert pool._browser is None
    assert pool._pw is None


def test_browser_pool_force_recycle_flag_triggers_recycle():
    pool = _BrowserPool(AppConfig())
    pool._browser = object()
    pool._force_recycle = True

    assert pool._should_recycle() is True
