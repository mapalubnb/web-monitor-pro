from src.config import AppConfig
from src.db import Task
from src.fetcher import extractor
from src.fetcher.engine import FetchEngine, FetchResult, _is_content_usable


def _task() -> Task:
    return Task(id=1, name="gitbook", url="https://example.com", type="html", strategy="auto")


def test_access_denied_html_is_not_usable():
    html = """
    <html><body><main>
    You're not authorized to access this page.
    </main></body></html>
    """
    result = FetchResult(ok=True, url="https://example.com", status_code=200, content=html)

    assert _is_content_usable(result, _task()) is False


def test_deep_extract_rejects_access_denied_text(monkeypatch):
    engine = FetchEngine(AppConfig(enable_free_proxy_pool=False))
    denied = "You're not authorized to access this page. " * 10
    monkeypatch.setattr(extractor, "try_deep_extract", lambda _: denied)
    html_result = FetchResult(
        ok=True,
        url="https://example.com",
        status_code=200,
        content="<html><body>shell</body></html>",
        strategy_used="httpx",
    )

    try:
        assert engine._try_deep_extract(_task(), html_result) is None
    finally:
        engine.close()


def test_upgrade_if_empty_reports_access_denied(monkeypatch):
    engine = FetchEngine(AppConfig(enable_free_proxy_pool=False))
    task = _task()
    denied_html = "<html><body>You're not authorized to access this page.</body></html>"
    result = FetchResult(
        ok=True,
        url=task.url,
        status_code=200,
        content=denied_html,
        strategy_used="httpx",
    )
    monkeypatch.setattr(
        engine,
        "_fetch_playwright",
        lambda *_: FetchResult(
            ok=True,
            url=task.url,
            status_code=200,
            content=denied_html,
            inner_text="You're not authorized to access this page.",
            strategy_used="playwright",
        ),
    )

    try:
        upgraded = engine._upgrade_if_empty(task, {}, result)
    finally:
        engine.close()

    assert upgraded.ok is False
    assert "access denied" in (upgraded.error or "")
