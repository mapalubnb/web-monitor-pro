"""
配置加载模块

- .env：敏感凭证（飞书 AppID/Secret 等）
- config.yaml：业务配置（任务、风控参数）
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# 项目路径
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = DATA_DIR / "logs"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
DB_PATH = DATA_DIR / "monitor.db"

for _dir in (DATA_DIR, LOG_DIR, SNAPSHOT_DIR):
    _dir.mkdir(parents=True, exist_ok=True)


@dataclass
class FeishuConfig:
    """飞书应用配置。"""
    app_id: str = ""
    app_secret: str = ""
    encrypt_key: str = ""
    verification_token: str = ""
    target_chat_id: str = ""
    admin_open_ids: list[str] = field(default_factory=list)


@dataclass
class RiskControlConfig:
    """风控配置。"""
    domain_min_interval: int = 10
    jitter_ratio: float = 0.3
    alert_after_consecutive_failures: int = 3
    min_change_ratio: float = 0.005
    push_cooldown_seconds: int = 30
    backoff_ladder: list[int] = field(default_factory=lambda: [60, 300, 900, 3600])


@dataclass
class TaskConfig:
    """单个监控任务的 YAML 种子配置。"""
    name: str
    url: str
    type: str = "html"
    strategy: str = "auto"
    impersonate: str = "chrome131"
    selector: str | None = None
    json_path: str | None = None
    extract_next_data: bool = False
    interval: int = 60
    keywords: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


@dataclass
class AppConfig:
    """全局应用配置。"""
    feishu: FeishuConfig = field(default_factory=FeishuConfig)
    default_check_interval: int = 60
    max_concurrent_fetch: int = 5
    request_timeout: int = 30
    log_level: str = "INFO"

    jina_reader_api_key: str = ""
    firecrawl_api_key: str = ""
    http_proxy: str = ""
    https_proxy: str = ""

    risk_control: RiskControlConfig = field(default_factory=RiskControlConfig)
    tasks: list[TaskConfig] = field(default_factory=list)
    default_headers: dict[str, str] = field(default_factory=dict)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _split_csv(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def load_config(
    env_path: Path | None = None,
    yaml_path: Path | None = None,
) -> AppConfig:
    """加载完整配置。"""
    env_path = env_path or (PROJECT_ROOT / ".env")
    yaml_path = yaml_path or (PROJECT_ROOT / "config.yaml")

    if env_path.exists():
        load_dotenv(env_path)

    feishu = FeishuConfig(
        app_id=os.getenv("FEISHU_APP_ID", ""),
        app_secret=os.getenv("FEISHU_APP_SECRET", ""),
        encrypt_key=os.getenv("FEISHU_ENCRYPT_KEY", ""),
        verification_token=os.getenv("FEISHU_VERIFICATION_TOKEN", ""),
        target_chat_id=os.getenv("FEISHU_TARGET_CHAT_ID", ""),
        admin_open_ids=_split_csv(os.getenv("FEISHU_ADMIN_OPEN_IDS", "")),
    )

    yaml_data = _load_yaml(yaml_path)
    risk_control = RiskControlConfig(**(yaml_data.get("risk_control") or {}))
    tasks = [TaskConfig(**t) for t in (yaml_data.get("tasks") or [])]
    default_headers = yaml_data.get("default_headers") or {}

    return AppConfig(
        feishu=feishu,
        default_check_interval=int(os.getenv("DEFAULT_CHECK_INTERVAL", "60")),
        max_concurrent_fetch=int(os.getenv("MAX_CONCURRENT_FETCH", "5")),
        request_timeout=int(os.getenv("REQUEST_TIMEOUT", "30")),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        jina_reader_api_key=os.getenv("JINA_READER_API_KEY", ""),
        firecrawl_api_key=os.getenv("FIRECRAWL_API_KEY", ""),
        http_proxy=os.getenv("HTTP_PROXY", ""),
        https_proxy=os.getenv("HTTPS_PROXY", ""),
        risk_control=risk_control,
        tasks=tasks,
        default_headers=default_headers,
    )


def validate_config(cfg: AppConfig) -> list[str]:
    """校验配置完整性，返回错误列表。"""
    errors: list[str] = []
    if not cfg.feishu.app_id:
        errors.append("缺少 FEISHU_APP_ID")
    if not cfg.feishu.app_secret:
        errors.append("缺少 FEISHU_APP_SECRET")
    if not cfg.feishu.target_chat_id:
        errors.append("缺少 FEISHU_TARGET_CHAT_ID")
    return errors
