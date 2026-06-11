from types import SimpleNamespace

from src.config import AppConfig
from src.db import Task
from src.fetcher.engine import FetchEngine, FetchResult, _find_markdown_alternate
from src.fetcher.extractor import extract


def _task() -> Task:
    return Task(
        id=1,
        name="gitbook",
        url="https://four-meme.gitbook.io/four.meme/protocol-integration",
        type="html",
        strategy="auto",
    )


def test_find_markdown_alternate_from_gitbook_html():
    html = """
    <html><head>
      <link rel="canonical" href="https://example.com/docs/page"/>
      <link rel="alternate" type="text/markdown" href="/docs/page.md"/>
    </head></html>
    """

    assert _find_markdown_alternate(html, "https://example.com/docs/page") == (
        "https://example.com/docs/page.md"
    )


def test_markdown_alternate_is_used_for_unusable_gitbook_shell(monkeypatch):
    html = """
    <html id="__next_error__"><head>
      <link rel="alternate" type="text/markdown"
            href="https://four-meme.gitbook.io/four.meme/brand/protocol-integration.md"/>
    </head><body>You're not authorized to access this page.</body></html>
    """
    markdown = "# Protocol Integration\n\n#### Documents\n\n{% file src=\"/files/demo\" %}\n"

    class Client:
        def get(self, url, headers=None):
            assert url == "https://four-meme.gitbook.io/four.meme/brand/protocol-integration.md"
            return SimpleNamespace(
                status_code=200,
                text=markdown,
                headers={"content-type": "text/markdown; charset=utf-8"},
            )

    engine = FetchEngine(AppConfig(enable_free_proxy_pool=False))
    monkeypatch.setattr(engine, "_get_httpx_client", lambda _proxy=None: Client())
    monkeypatch.setattr(engine, "_select_proxy", lambda allowed_schemes=None: None)

    try:
        result = engine._upgrade_if_empty(
            _task(),
            {},
            FetchResult(
                ok=True,
                url="https://four-meme.gitbook.io/four.meme/protocol-integration",
                status_code=200,
                content=html,
                strategy_used="httpx",
            ),
        )
    finally:
        engine.close()

    assert result.ok is True
    assert result.strategy_used == "httpx→markdown"
    assert "Protocol Integration" in result.content


def test_markdown_result_extracts_as_plain_text():
    task = _task()
    result = FetchResult(
        ok=True,
        url=task.url,
        status_code=200,
        content="# Protocol Integration\n\n#### Documents\n\ncontent",
        content_type="text/markdown; charset=utf-8",
        strategy_used="httpx→markdown",
    )

    assert extract(task, result).startswith("# Protocol Integration")
