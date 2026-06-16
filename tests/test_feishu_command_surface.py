import json

from src.config import AppConfig
from src.feishu import cards
from src.feishu.commands import CommandDispatcher


class _Risk:
    def unmute(self):
        return None


def test_help_card_only_shows_public_commands():
    text = json.dumps(cards.help_card(), ensure_ascii=False)

    assert "/add <url>" in text
    assert "/strategy <id> auto" in text
    assert "/strategy <id> stealth" in text
    assert "scrapling_stealth" in text
    assert "/strategy <id> browser" in text
    assert "playwright" in text
    assert "/strategy <id> fast" in text
    assert "curl_cffi" in text
    assert "/strategy <id> http" in text
    assert "httpx" in text
    assert "/strategy <id> scrapling" in text
    assert "scrapling_static" in text
    assert "/status" in text
    assert "/config" not in text
    assert "/sniff" not in text
    assert "/reset" not in text
    assert "/history" not in text
    assert "/snapshot" not in text


def test_removed_text_commands_return_migration_hint():
    dispatcher = CommandDispatcher(AppConfig(), _Risk(), service_start_ts=0)

    resp = dispatcher.dispatch_text("/config", user_id="u", chat_id="c")
    text = json.dumps(resp.card, ensure_ascii=False)

    assert "已合并" in text
    assert "/status" in text

    resp = dispatcher.dispatch_text("/reset 1 --strategy playwright", user_id="u", chat_id="c")
    text = json.dumps(resp.card, ensure_ascii=False)

    assert "已合并" in text
    assert "/strategy <ID> <策略>" in text


def test_status_card_includes_config_summary():
    card = cards.status_card({
        "uptime": "1 分 2 秒",
        "total_tasks": 2,
        "active_tasks": 1,
        "pushes_today": 3,
        "checks_today": 4,
        "errors_today": 0,
        "mute_status": "否",
        "hostname": "host",
        "python_version": "3.12",
        "version": "0.4.0",
        "memory": "100 MB",
        "default_check_interval": 60,
        "max_concurrent_fetch": 5,
        "domain_min_interval": 10,
        "request_timeout": 30,
        "min_change_ratio": 0.005,
        "push_cooldown_seconds": 30,
        "proxy_info": "未启用",
        "external_apis": "Playwright、Scrapling",
    })
    text = json.dumps(card, ensure_ascii=False)

    assert "运行概览" in text
    assert "抓取配置" in text
    assert "代理" in text
    assert "增强模块" in text
