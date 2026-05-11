"""日志模块（loguru，控制台 + 按天轮转文件）。"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from loguru import logger

from .config import LOG_DIR


def setup_logger(level: str = "INFO") -> None:
    """初始化日志系统。"""
    logger.remove()

    # 控制台
    logger.add(
        sys.stderr,
        format=(
            "<green>{time:MM-DD HH:mm:ss}</green> | "
            "<level>{level: <7}</level> | "
            "<cyan>{name}:{line}</cyan> - <level>{message}</level>"
        ),
        level=level,
        colorize=True,
        enqueue=True,
    )

    # 单一日志文件，按天轮转，保留 14 天（节省磁盘）
    logger.add(
        LOG_DIR / "monitor_{time:YYYY-MM-DD}.log",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <7} | {name}:{function}:{line} - {message}",
        level=level,
        rotation="00:00",
        retention="14 days",
        compression="gz",
        encoding="utf-8",
        enqueue=True,
    )

    logger.info("📝 日志已就绪（级别={}，目录={}）", level, LOG_DIR)


def get_today_log_path() -> Path:
    """当天日志文件路径，供 /log 命令上传。"""
    return LOG_DIR / f"monitor_{datetime.now():%Y-%m-%d}.log"


def tail_log(n_lines: int = 100) -> str:
    """读取当日日志末尾 N 行。"""
    path = get_today_log_path()
    if not path.exists():
        return "(今天还没有日志)"
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n_lines:])
    except Exception as e:
        return f"(读取日志失败: {e})"


__all__ = ["logger", "setup_logger", "get_today_log_path", "tail_log"]
