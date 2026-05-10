"""
中文日志模块

基于 loguru：
- 控制台：彩色、中文友好
- 文件：按天轮转，保留 30 天，自动压缩
- 提供 `get_today_log_path()` 供飞书 `/log` 命令下载
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from loguru import logger

from .config import LOG_DIR

_LOG_FORMAT_CONSOLE = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> "
    "| <level>{level: <7}</level> "
    "| <cyan>{name}</cyan>:<cyan>{line}</cyan> "
    "- <level>{message}</level>"
)

_LOG_FORMAT_FILE = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <7} | {name}:{function}:{line} - {message}"
)


def setup_logger(level: str = "INFO") -> None:
    """
    初始化日志系统。

    Args:
        level: 日志级别（DEBUG / INFO / WARNING / ERROR）
    """
    logger.remove()  # 清掉 loguru 默认的 handler

    # 控制台输出
    logger.add(
        sys.stderr,
        format=_LOG_FORMAT_CONSOLE,
        level=level,
        colorize=True,
        enqueue=True,  # 多进程/多线程安全
    )

    # 文件输出：按天轮转，保留 30 天，自动压缩
    logger.add(
        LOG_DIR / "monitor_{time:YYYY-MM-DD}.log",
        format=_LOG_FORMAT_FILE,
        level=level,
        rotation="00:00",       # 每天 0 点切分
        retention="30 days",    # 保留 30 天
        compression="gz",       # 旧日志 gzip 压缩
        encoding="utf-8",
        enqueue=True,
    )

    # 错误单独一份文件（方便排障）
    logger.add(
        LOG_DIR / "error_{time:YYYY-MM-DD}.log",
        format=_LOG_FORMAT_FILE,
        level="ERROR",
        rotation="00:00",
        retention="30 days",
        compression="gz",
        encoding="utf-8",
        enqueue=True,
    )

    logger.info("📝 日志系统已就绪（级别={}，目录={}）", level, LOG_DIR)


def get_today_log_path() -> Path:
    """返回今天的日志文件路径，供飞书 /log 命令上传下载。"""
    today = datetime.now().strftime("%Y-%m-%d")
    return LOG_DIR / f"monitor_{today}.log"


def get_log_dir() -> Path:
    """返回日志目录。"""
    return LOG_DIR


def tail_log(n_lines: int = 100) -> str:
    """
    读取当天日志的最后 n 行，供飞书 /log 命令直接展示摘要。
    """
    log_path = get_today_log_path()
    if not log_path.exists():
        return "(今天还没有日志)"

    try:
        # 简单实现：大文件可考虑倒序读，这里先直接读全部
        with log_path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        tail = lines[-n_lines:] if len(lines) > n_lines else lines
        return "".join(tail)
    except Exception as e:
        return f"(读取日志失败: {e})"


# 对外导出一个可直接用的 logger
__all__ = ["logger", "setup_logger", "get_today_log_path", "get_log_dir", "tail_log"]
