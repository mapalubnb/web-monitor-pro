from types import SimpleNamespace

from src.tasks import monitor_task
from src.tasks.monitor_task import MonitorRunner


class _Session:
    def __init__(self, task):
        self.task = task

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def get(self, *_):
        return self.task


class _Risk:
    def __init__(self):
        self.pushed = 0

    def should_alert_failure(self, _fails):
        return False

    def can_send_failure_alert(self, _task_id):
        return True

    def mark_pushed(self, *_args, **_kwargs):
        self.pushed += 1


class _Feishu:
    def __init__(self):
        self.cards = []

    def send_card(self, _chat_id, card):
        self.cards.append(card)


def _runner(task):
    cfg = SimpleNamespace(
        circuit_breaker_threshold=20,
        feishu=SimpleNamespace(target_chat_id="chat"),
    )
    risk = _Risk()
    feishu = _Feishu()
    return MonitorRunner(cfg, None, risk, feishu), risk, feishu


def _task(consecutive_failures=0):
    return SimpleNamespace(
        id=1,
        name="gitbook",
        url="https://example.com",
        consecutive_failures=consecutive_failures,
        last_content_hash=None,
        last_snapshot_path=None,
        enabled=True,
        last_checked_at=None,
        total_checks=0,
    )


def test_first_baseline_failure_sends_immediate_card(monkeypatch):
    task = _task(consecutive_failures=0)
    runner, risk, feishu = _runner(task)
    monkeypatch.setattr(monitor_task, "session_scope", lambda: _Session(task))

    runner._handle_failure(task, "access denied / unauthorized page detected")

    assert task.consecutive_failures == 1
    assert risk.pushed == 1
    assert len(feishu.cards) == 1
    assert feishu.cards[0]["header"]["title"]["content"].startswith("🚨 首次抓取失败")


def test_repeated_pre_baseline_failure_waits_for_threshold(monkeypatch):
    task = _task(consecutive_failures=1)
    runner, risk, feishu = _runner(task)
    monkeypatch.setattr(monitor_task, "session_scope", lambda: _Session(task))

    runner._handle_failure(task, "access denied / unauthorized page detected")

    assert task.consecutive_failures == 2
    assert risk.pushed == 0
    assert feishu.cards == []
