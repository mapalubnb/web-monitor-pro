from src.config import load_config
from src.db import Task
from src.fetcher.engine import SCRAPLING_STRATEGIES, SUPPORTED_STRATEGIES
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
