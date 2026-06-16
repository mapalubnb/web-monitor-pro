import json

from src.feishu import cards


def test_task_list_card_shows_proxy_info():
    card = cards.task_list_card([{
        "id": 1,
        "name": "示例任务",
        "url": "https://example.com",
        "interval": 60,
        "enabled": True,
        "total_changes": 0,
        "last_checked_at": None,
        "keywords": [],
        "has_snapshot": False,
        "proxy_info": "Proxifly 免费代理池（最多 200 个）",
    }])

    assert "代理：Proxifly 免费代理池" in json.dumps(card, ensure_ascii=False)


def test_task_detail_card_shows_proxy_info():
    card = cards.task_detail_card({
        "id": 1,
        "name": "示例任务",
        "url": "https://example.com",
        "type": "html",
        "strategy": "auto",
        "enabled": True,
        "interval": 60,
        "total_changes": 0,
        "consecutive_failures": 0,
        "keywords": [],
        "proxy_info": "Proxifly 免费代理池（最多 200 个）",
    })

    assert "代理" in json.dumps(card, ensure_ascii=False)
    assert "Proxifly 免费代理池" in json.dumps(card, ensure_ascii=False)


def test_fetch_failure_card_shows_bot_challenge_hint():
    card = cards.fetch_failure_card(
        1,
        "binance",
        "https://www.binance.com/en/proof-of-collateral",
        1,
        "bot challenge / JavaScript verification page detected",
        first_attempt=True,
    )
    text = json.dumps(card, ensure_ascii=False)

    assert "疑似机器人验证" in text
    assert "scrapling_stealth" in text


def test_fetch_failure_card_shows_cookie_notice_hint():
    card = cards.fetch_failure_card(
        2,
        "cookie",
        "https://example.com",
        1,
        "cookie consent page detected",
        first_attempt=True,
    )
    text = json.dumps(card, ensure_ascii=False)

    assert "Cookie 同意页" in text
    assert "/strategy 2 stealth" in text
    assert "scrapling_stealth" in text


def test_fetch_failure_card_shows_free_proxy_hint():
    card = cards.fetch_failure_card(
        9,
        "gitbook",
        "https://four-meme.gitbook.io/four.meme/protocol-integration",
        5,
        "ConnectionError: Failed to perform, curl: (7) Failed to connect "
        "to 206.123.156.229 port 11559: Could not connect to server.",
        first_attempt=False,
    )
    text = json.dumps(card, ensure_ascii=False)

    assert "免费代理不可用" in text
    assert "HTTPS_PROXY" in text
