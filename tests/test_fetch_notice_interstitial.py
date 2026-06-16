import json
from contextlib import nullcontext
from types import SimpleNamespace

from src.db import Task
from src.fetcher import extractor
from src.fetcher.engine import (
    FetchResult,
    _is_content_usable,
    extracted_content_failure_reason,
)
from src.tasks import monitor_task
from src.tasks.monitor_task import MonitorRunner


GALAXY_NOTICE = (
    "You are leaving the Galaxy website and being directed to an external "
    "third-party website that we think might be of interest to you. "
    "Third-party websites are not under the control of Galaxy, and Galaxy is "
    "not responsible for the accuracy or completeness of the contents or the "
    "proper operation of any linked site. Please note the security and privacy "
    "policies on third-party websites differ from Galaxy policies, please read "
    "third-party privacy and security policies closely. If you do not wish to "
    "continue to the third-party site, click “Cancel”. The inclusion of any "
    "linked website does not imply Galaxy’s endorsement or adoption of the "
    "statements therein and is only provided for your convenience."
)


class _Session:
    def __init__(self, task):
        self.task = task

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def get(self, *_):
        return self.task

    def expunge(self, *_):
        return None


class _Risk:
    def __init__(self):
        self.pushed = 0

    def acquire_fetch(self, _url):
        return nullcontext()

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


class _Engine:
    def fetch(self, task):
        return FetchResult(
            ok=True,
            url=task.url,
            status_code=200,
            content="<html><body>large page html</body></html>",
            strategy_used="curl_cffi/chrome131",
        )


def _task():
    return SimpleNamespace(
        id=1,
        name="galaxy",
        url="https://www.galaxy.com/insights/research",
        consecutive_failures=0,
        last_content_hash=None,
        last_snapshot_path=None,
        enabled=True,
        last_checked_at=None,
        total_checks=0,
    )


def test_external_link_notice_page_is_not_usable():
    task = Task(
        id=1,
        name="galaxy",
        url="https://www.galaxy.com/insights/research",
        type="html",
        strategy="auto",
    )
    result = FetchResult(
        ok=True,
        url=task.url,
        status_code=200,
        content=f"<html><body>{GALAXY_NOTICE}</body></html>",
        inner_text=GALAXY_NOTICE,
        strategy_used="curl_cffi/chrome131",
    )

    assert _is_content_usable(result, task) is False
    assert extracted_content_failure_reason(GALAXY_NOTICE) == (
        "external link notice page detected"
    )


def test_extractor_skips_notice_candidate_when_page_text_is_available(monkeypatch):
    real_text = "Bitcoin Market Research weekly report " * 220
    html = f"""
    <html><body>
      <aside>{GALAXY_NOTICE}</aside>
      <main>{real_text}</main>
    </body></html>
    """
    task = Task(
        id=1,
        name="galaxy",
        url="https://www.galaxy.com/insights/research",
        type="html",
        strategy="auto",
    )
    result = FetchResult(
        ok=True,
        url=task.url,
        status_code=200,
        content=html,
        strategy_used="curl_cffi/chrome131",
    )
    monkeypatch.setattr(extractor, "_main_content", lambda _html: GALAXY_NOTICE)

    text = extractor.extract(task, result)

    assert "Bitcoin Market Research" in text
    assert text != GALAXY_NOTICE


def test_monitor_rejects_extracted_external_link_notice(monkeypatch):
    task = _task()
    cfg = SimpleNamespace(
        circuit_breaker_threshold=20,
        feishu=SimpleNamespace(target_chat_id="chat"),
    )
    risk = _Risk()
    feishu = _Feishu()
    runner = MonitorRunner(cfg, _Engine(), risk, feishu)

    monkeypatch.setattr(monitor_task, "session_scope", lambda: _Session(task))
    monkeypatch.setattr(monitor_task, "extract", lambda *_: GALAXY_NOTICE)

    runner._run_locked(task.id)

    assert task.consecutive_failures == 1
    assert task.last_content_hash is None
    assert risk.pushed == 1
    text = json.dumps(feishu.cards[0], ensure_ascii=False)
    assert "外链跳转" in text
    assert "/debug <id>" in text
