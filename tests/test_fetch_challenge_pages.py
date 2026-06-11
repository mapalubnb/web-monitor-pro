from src.config import AppConfig
from src.db import Task
from src.fetcher.engine import FetchEngine, FetchResult, _is_content_usable


def _task() -> Task:
    return Task(
        id=1,
        name="binance",
        url="https://www.binance.com/en/proof-of-collateral",
        type="html",
        strategy="auto",
    )


def _challenge_html() -> str:
    return """
    <html><body>
    <h1>JavaScript is disabled</h1>
    <p>In order to continue, we need to verify that you're not a robot.
    This requires JavaScript. Enable JavaScript and then reload the page.</p>
    </body></html>
    """


def test_javascript_robot_challenge_is_not_usable():
    result = FetchResult(
        ok=True,
        url="https://www.binance.com/en/proof-of-collateral",
        status_code=200,
        content=_challenge_html(),
        strategy_used="httpx",
    )

    assert _is_content_usable(result, _task()) is False


def test_challenge_page_upgrades_to_scrapling_stealth(monkeypatch):
    engine = FetchEngine(AppConfig(enable_free_proxy_pool=False))
    real_html = "<html><body><main>" + ("Proof of collateral reserves. " * 20) + "</main></body></html>"
    monkeypatch.setattr(
        engine,
        "_fetch_scrapling",
        lambda *_: FetchResult(
            ok=True,
            url="https://www.binance.com/en/proof-of-collateral",
            status_code=200,
            content=real_html,
            strategy_used="scrapling_stealth",
        ),
    )

    try:
        result = engine._upgrade_if_empty(
            _task(),
            {},
            FetchResult(
                ok=True,
                url="https://www.binance.com/en/proof-of-collateral",
                status_code=200,
                content=_challenge_html(),
                strategy_used="httpx",
            ),
        )
    finally:
        engine.close()

    assert result.ok is True
    assert result.strategy_used == "httpx→scrapling_stealth"


def test_deep_extract_rejects_javascript_challenge(monkeypatch):
    engine = FetchEngine(AppConfig(enable_free_proxy_pool=False))
    challenge_text = (
        "JavaScript is disabled\n"
        "In order to continue, we need to verify that you're not a robot. "
        "This requires JavaScript. Enable JavaScript and then reload the page."
    )
    monkeypatch.setattr(
        "src.fetcher.extractor.try_deep_extract",
        lambda _html: challenge_text,
    )

    try:
        result = engine._try_deep_extract(
            _task(),
            FetchResult(
                ok=True,
                url="https://www.binance.com/en/proof-of-collateral",
                status_code=200,
                content="<html><body>shell</body></html>",
                strategy_used="httpx",
            ),
        )
    finally:
        engine.close()

    assert result is None


def test_challenge_page_failure_reason_is_clear(monkeypatch):
    engine = FetchEngine(AppConfig(enable_free_proxy_pool=False))
    monkeypatch.setattr(
        engine,
        "_fetch_scrapling",
        lambda *_: FetchResult(
            ok=False,
            url="https://www.binance.com/en/proof-of-collateral",
            strategy_used="scrapling_stealth",
            error="blocked",
        ),
    )
    monkeypatch.setattr(
        engine,
        "_fetch_playwright",
        lambda *_: FetchResult(
            ok=True,
            url="https://www.binance.com/en/proof-of-collateral",
            status_code=200,
            content=_challenge_html(),
            inner_text="JavaScript is disabled. This requires JavaScript.",
            strategy_used="playwright",
        ),
    )

    try:
        result = engine._upgrade_if_empty(
            _task(),
            {},
            FetchResult(
                ok=True,
                url="https://www.binance.com/en/proof-of-collateral",
                status_code=200,
                content=_challenge_html(),
                strategy_used="httpx",
            ),
        )
    finally:
        engine.close()

    assert result.ok is False
    assert "JavaScript verification" in (result.error or "")


def test_high_risk_domain_tries_scrapling_stealth_first(monkeypatch):
    engine = FetchEngine(AppConfig(enable_free_proxy_pool=False))
    calls = []
    real_html = "<html><body><main>" + ("Proof of collateral reserves. " * 20) + "</main></body></html>"
    monkeypatch.setattr(
        engine,
        "_fetch_scrapling",
        lambda _task, _headers, strategy: (
            calls.append(strategy)
            or FetchResult(
                ok=True,
                url="https://www.binance.com/en/proof-of-collateral",
                status_code=200,
                content=real_html,
                strategy_used=strategy,
            )
        ),
    )

    try:
        result = engine.fetch(_task())
    finally:
        engine.close()

    assert result.ok is True
    assert calls == ["scrapling_stealth"]


def test_high_risk_challenge_fails_without_plain_http_fallback(monkeypatch):
    engine = FetchEngine(AppConfig(enable_free_proxy_pool=False))
    calls = []
    monkeypatch.setattr(
        engine,
        "_fetch_scrapling",
        lambda _task, _headers, strategy: (
            calls.append(strategy)
            or FetchResult(
                ok=True,
                url="https://www.binance.com/en/proof-of-collateral",
                status_code=200,
                content=_challenge_html(),
                inner_text="JavaScript is disabled. This requires JavaScript.",
                strategy_used=strategy,
            )
        ),
    )
    monkeypatch.setattr(
        engine,
        "_fetch_curl_cffi",
        lambda *_: calls.append("curl_cffi") or FetchResult(ok=False, url=_task().url),
    )

    try:
        result = engine.fetch(_task())
    finally:
        engine.close()

    assert result.ok is False
    assert "JavaScript verification" in (result.error or "")
    assert calls == ["scrapling_stealth"]
