"""配置加载（.env 凭证 + config.yaml 业务参数）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

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

    enable_playwright: bool = True
    playwright_timeout: int = 30
    playwright_max_pages: int = 20
    http_proxy: str = ""
    https_proxy: str = ""

    # 连续失败多少次触发熔断（自动禁用任务并告警）
    circuit_breaker_threshold: int = 20

    # 快照/诊断文件保留期（天）。启动时会一次性清理超期文件。
    snapshot_retention_days: int = 30
    debug_html_retention_days: int = 7

    risk_control: RiskControlConfig = field(default_factory=RiskControlConfig)
    tasks: list[TaskConfig] = field(default_factory=list)
    default_headers: dict[str, str] = field(default_factory=dict)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _dc_from_dict(cls, data: dict[str, Any]) -> Any:
    """Build a dataclass from a dict, silently dropping unknown keys.

    YAML 里手抖写了不认识的字段（比如把 ``timeout`` 误拼成 ``time_out``）时，
    直接传给 dataclass 会抛 ``TypeError: unexpected keyword``，让整个服务起不来。
    这里改成静默丢弃未知字段并打一个 warning，保证可用性优先。
    """
    if not data:
        return cls()
    known = {f.name for f in fields(cls)}
    unknown = set(data.keys()) - known
    if unknown:
        # 延迟导入 logger 避免循环依赖
        from .logger import logger
        logger.warning("{} 忽略未知字段: {}", cls.__name__, sorted(unknown))
    return cls(**{k: v for k, v in data.items() if k in known})


def _split_csv(value: str) -> list[str]:
    return [x.strip() for x in value.split(",") if x.strip()]


def _safe_int(value: str, default: int) -> int:
    """安全的 int 转换。"""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


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
    risk_control = _dc_from_dict(RiskControlConfig, yaml_data.get("risk_control") or {})
    tasks = [_dc_from_dict(TaskConfig, t) for t in (yaml_data.get("tasks") or [])]
    default_headers = yaml_data.get("default_headers") or {}

    return AppConfig(
        feishu=feishu,
        default_check_interval=_safe_int(os.getenv("DEFAULT_CHECK_INTERVAL", "60"), 60),
        max_concurrent_fetch=_safe_int(os.getenv("MAX_CONCURRENT_FETCH", "5"), 5),
        request_timeout=_safe_int(os.getenv("REQUEST_TIMEOUT", "30"), 30),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        enable_playwright=os.getenv("ENABLE_PLAYWRIGHT", "true").strip().lower() in ("true", "1", "yes"),
        playwright_timeout=_safe_int(os.getenv("PLAYWRIGHT_TIMEOUT", "30"), 30),
        playwright_max_pages=_safe_int(os.getenv("PLAYWRIGHT_MAX_PAGES", "20"), 20),
        http_proxy=os.getenv("HTTP_PROXY", ""),
        https_proxy=os.getenv("HTTPS_PROXY", ""),
        circuit_breaker_threshold=_safe_int(
            os.getenv("CIRCUIT_BREAKER_THRESHOLD", "20"), 20,
        ),
        snapshot_retention_days=_safe_int(
            os.getenv("SNAPSHOT_RETENTION_DAYS", "30"), 30,
        ),
        debug_html_retention_days=_safe_int(
            os.getenv("DEBUG_HTML_RETENTION_DAYS", "7"), 7,
        ),
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
