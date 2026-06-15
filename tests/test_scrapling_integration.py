from src.config import AppConfig, load_config
from src.db import Task
from src.feishu.commands import _auto_adaptive_selector, _auto_wait_selector
from src.fetcher.engine import SCRAPLING_STRATEGIES, SUPPORTED_STRATEGIES, FetchEngine, FetchResult
from src.fetcher.extractor import _by_selector


def test_scrapling_strategies_are_registered():
    assert SCRAPLING_STRATEGIES == (
        "scrapling_static",
        "scrapling_dynamic",
        "scrapling_stealth",
        "scrapling_auto",
    )
    for strategy in SCRAPLING_STRATEGIES:
        assert strategy in SUPPORTED_STRATEGIES


def test_task_model_contains_adaptive_selector_fields():
    columns = Task.__table__.columns
    for name in (
        "adaptive_selector",
        "selector_identifier",
        "adaptive_threshold",
        "wait_selector",
    ):
        assert name in columns


def test_enable_scrapling_env_switch(monkeypatch, tmp_path):
    monkeypatch.setenv("ENABLE_SCRAPLING", "false")
    cfg = load_config(
        env_path=tmp_path / ".env",
        yaml_path=tmp_path / "config.yaml",
    )
    assert cfg.enable_scrapling is False


def test_adaptive_selector_falls_back_to_selectolax_when_unavailable():
    task = Task(
        id=1,
        name="demo",
        url="https://example.com",
        selector="main",
        adaptive_selector=True,
        selector_identifier="main",
        adaptive_threshold=40,
    )
    html = "<html><body><main><h1>Hello</h1><p>World</p></main></body></html>"

    assert _by_selector(html, "main", task) == "Hello\nWorld"


def test_selector_defaults_to_adaptive_unless_disabled():
    assert _auto_adaptive_selector(".content") is True
    assert _auto_adaptive_selector(None, requested=True) is True
    assert _auto_adaptive_selector(".content", disabled=True) is False


def test_browser_strategies_reuse_selector_as_wait_target():
    assert _auto_wait_selector("main", "scrapling_stealth") == "main"
    assert _auto_wait_selector("main", "playwright") == "main"
    assert _auto_wait_selector("main", "httpx") is None
    assert _auto_wait_selector("main", "httpx", "#ready") == "#ready"


def test_auto_chain_uses_light_scrapling_before_playwright(monkeypatch):
    engine = FetchEngine(AppConfig(enable_free_proxy_pool=False))
    task = Task(
        id=1,
        name="spa",
        url="https://example.com/app",
        type="html",
        strategy="auto",
    )
    shell = "<html><body><div id=\"__next\"></div></body></html>"
    real_html = "<html><body><main>" + ("rendered content " * 30) + "</main></body></html>"
    calls = []

    monkeypatch.setattr(
        engine,
        "_fetch_curl_cffi",
        lambda *_: FetchResult(ok=True, url=task.url, content=shell, strategy_used="curl_cffi"),
    )
    monkeypatch.setattr(
        engine,
        "_fetch_httpx",
        lambda *_: FetchResult(ok=True, url=task.url, content=shell, strategy_used="httpx"),
    )
    monkeypatch.setattr(
        engine,
        "_fetch_scrapling",
        lambda _task, _headers, strategy: (
            calls.append(strategy)
            or FetchResult(ok=True, url=task.url, content=shell, strategy_used=strategy)
        ),
    )
    monkeypatch.setattr(
        engine,
        "_fetch_playwright",
        lambda *_: FetchResult(
            ok=True,
            url=task.url,
            content=real_html,
            inner_text="rendered content " * 30,
            strategy_used="playwright",
        ),
    )

    try:
        result = engine.fetch(task)
    finally:
        engine.close()

    assert result.ok is True
    assert result.strategy_used == "playwright"
    assert calls == ["scrapling_static"]
