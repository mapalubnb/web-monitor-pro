from contextlib import contextmanager
from types import SimpleNamespace

from src.config import AppConfig
from src.feishu import commands
from src.feishu.commands import CommandDispatcher, _normalize_strategy, _strategy_label


class _Risk:
    pass


@contextmanager
def _session(task):
    yield SimpleNamespace(get=lambda *_: task)


def test_strategy_aliases_are_easy_to_use():
    assert _normalize_strategy("stealth") == "scrapling_stealth"
    assert _normalize_strategy("browser") == "playwright"
    assert _normalize_strategy("fast") == "curl_cffi"
    assert _normalize_strategy("http") == "httpx"
    assert _normalize_strategy("自动") == "auto"
    assert _strategy_label("scrapling_stealth").startswith("隐身浏览器")


def test_strategy_command_switches_strategy_and_resets_baseline(monkeypatch):
    task = SimpleNamespace(
        id=3,
        name="demo",
        strategy="auto",
        selector="main",
        wait_selector=None,
        last_content_hash="old",
        last_snapshot_path="/tmp/old.txt",
        last_strategy_used="httpx",
        consecutive_failures=5,
    )
    monkeypatch.setattr(commands, "session_scope", lambda: _session(task))
    dispatcher = CommandDispatcher(AppConfig(), _Risk(), service_start_ts=0)

    resp = dispatcher._cmd_strategy(["3", "stealth"])

    assert task.strategy == "scrapling_stealth"
    assert task.wait_selector == "main"
    assert task.last_content_hash is None
    assert task.last_snapshot_path is None
    assert task.last_strategy_used is None
    assert task.consecutive_failures == 0
    assert resp.trigger_check_task_id == 3
    assert "scrapling_stealth" in str(resp.card)
