from src.config import AppConfig
from src.db import Task
from src.fetcher import extractor
from src.fetcher.engine import FetchEngine, FetchResult, _is_content_usable


def _task() -> Task:
    return Task(id=1, name="cookie", url="https://example.com", type="html", strategy="auto")


def _cookie_html() -> str:
    return """
    <html><body>
    <section id="cookie-banner">
      <h1>We value your privacy</h1>
      <p>We use cookies to improve your experience and measure traffic.</p>
      <button>Accept all cookies</button>
      <button>Cookie settings</button>
    </section>
    </body></html>
    """


def test_cookie_notice_page_is_not_usable():
    result = FetchResult(
        ok=True,
        url="https://example.com",
        status_code=200,
        content=_cookie_html(),
        inner_text=(
            "We value your privacy\n"
            "We use cookies to improve your experience and measure traffic.\n"
            "Accept all cookies\nCookie settings"
        ),
        strategy_used="httpx",
    )

    assert _is_content_usable(result, _task()) is False


def test_deep_extract_rejects_cookie_notice_text(monkeypatch):
    engine = FetchEngine(AppConfig(enable_free_proxy_pool=False))
    monkeypatch.setattr(
        extractor,
        "try_deep_extract",
        lambda _html: (
            "We value your privacy. We use cookies to improve your experience. "
            "Accept all cookies. Cookie settings. "
        ) * 3,
    )
    html_result = FetchResult(
        ok=True,
        url="https://example.com",
        status_code=200,
        content="<html><body>shell</body></html>",
        inner_text="We use cookies. Accept all cookies. Cookie settings.",
        strategy_used="httpx",
    )

    try:
        assert engine._try_deep_extract(_task(), html_result) is None
    finally:
        engine.close()


def test_upgrade_if_empty_reports_cookie_notice(monkeypatch):
    engine = FetchEngine(AppConfig(enable_free_proxy_pool=False))
    task = _task()
    result = FetchResult(
        ok=True,
        url=task.url,
        status_code=200,
        content=_cookie_html(),
        inner_text="We use cookies. Accept all cookies. Cookie settings.",
        strategy_used="httpx",
    )
    monkeypatch.setattr(
        engine,
        "_fetch_playwright",
        lambda *_: FetchResult(
            ok=True,
            url=task.url,
            status_code=200,
            content=_cookie_html(),
            inner_text="We use cookies. Accept all cookies. Cookie settings.",
            strategy_used="playwright",
        ),
    )

    try:
        upgraded = engine._upgrade_if_empty(task, {}, result)
    finally:
        engine.close()

    assert upgraded.ok is False
    assert "cookie consent" in (upgraded.error or "")
