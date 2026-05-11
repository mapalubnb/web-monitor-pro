"""风控模块（抓取限流 + 推送冷却）。"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime
from urllib.parse import urlparse

from .config import RiskControlConfig
from .db import PushLog, get_state, session_scope, set_state
from .differ import DiffResult
from .logger import logger


class _FetchSlot:
    """抓取配额上下文，退出时释放信号量。"""
    __slots__ = ("_sem", "_released")

    def __init__(self, sem: threading.BoundedSemaphore):
        self._sem = sem
        self._released = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if not self._released:
            self._sem.release()
            self._released = True


_CLEANUP_INTERVAL = 3600  # 每小时清理一次过期记录
_DOMAIN_EXPIRE = 86400    # 域名记录 24h 过期
_COOLDOWN_EXPIRE = 3600   # 冷却记录 1h 过期


class RiskController:
    """抓取 + 推送风控的统一入口。"""

    def __init__(self, cfg: RiskControlConfig, max_concurrent: int = 5):
        self.cfg = cfg
        self._domain_last_hit: dict[str, float] = {}
        self._cooldown_mem: dict[int, float] = {}
        self._lock = threading.Lock()
        self._sem = threading.BoundedSemaphore(max_concurrent)
        self._last_cleanup = time.time()

    def acquire_fetch(self, url: str) -> _FetchSlot:
        """获取抓取机会（阻塞直到满足风控）。"""
        self._sem.acquire()
        try:
            self._wait_for_domain(url)
            self._maybe_cleanup()
        except Exception:
            self._sem.release()
            raise
        return _FetchSlot(self._sem)

    def _wait_for_domain(self, url: str) -> None:
        domain = urlparse(url).netloc
        if not domain:
            return
        interval = self.cfg.domain_min_interval
        jitter = random.uniform(-self.cfg.jitter_ratio, self.cfg.jitter_ratio) * interval
        with self._lock:
            last = self._domain_last_hit.get(domain, 0.0)
            now = time.time()
            wait = last + interval - now + jitter
            self._domain_last_hit[domain] = now + max(wait, 0)
        if wait > 0:
            time.sleep(wait)

    def _maybe_cleanup(self) -> None:
        """定期清理过期的域名限流和冷却记录，防止内存无限增长。"""
        now = time.time()
        if (now - self._last_cleanup) < _CLEANUP_INTERVAL:
            return
        with self._lock:
            if (now - self._last_cleanup) < _CLEANUP_INTERVAL:
                return
            self._last_cleanup = now
            expired_domains = [
                d for d, ts in self._domain_last_hit.items()
                if (now - ts) > _DOMAIN_EXPIRE
            ]
            for d in expired_domains:
                del self._domain_last_hit[d]
            expired_tasks = [
                tid for tid, ts in self._cooldown_mem.items()
                if (now - ts) > _COOLDOWN_EXPIRE
            ]
            for tid in expired_tasks:
                del self._cooldown_mem[tid]

    def next_interval_after_failure(self, consecutive_failures: int, base: int) -> int:
        """失败后的退避间隔（秒）。"""
        if consecutive_failures <= 0:
            return base
        ladder = self.cfg.backoff_ladder or [60, 300, 900, 3600]
        idx = min(consecutive_failures - 1, len(ladder) - 1)
        return max(base, ladder[idx])

    def is_muted(self) -> bool:
        """是否处于免打扰期。"""
        ts = get_state("mute_until", 0)
        try:
            return float(ts) > time.time()
        except (TypeError, ValueError):
            return False

    def mute_for(self, duration_text: str) -> datetime:
        """开启免打扰（30m / 2h / 1d / 纯秒数）。"""
        seconds = _parse_duration(duration_text)
        until = time.time() + seconds
        set_state("mute_until", until)
        return datetime.fromtimestamp(until)

    def unmute(self) -> None:
        """取消免打扰。"""
        set_state("mute_until", 0)

    def mute_status(self) -> datetime | None:
        """返回免打扰截止时间，已过期则返回 None。"""
        ts = get_state("mute_until", 0)
        try:
            t = float(ts)
        except (TypeError, ValueError):
            return None
        return datetime.fromtimestamp(t) if t > time.time() else None

    def _is_cooling(self, task_id: int) -> bool:
        with self._lock:
            last = self._cooldown_mem.get(task_id, 0)
            return (time.time() - last) < self.cfg.push_cooldown_seconds

    def mark_pushed(self, task_id: int, kind: str = "change",
                    message_id: str | None = None) -> None:
        """记录推送（冷却 + DB 日志）。DB 失败不抛出，只告警。"""
        with self._lock:
            self._cooldown_mem[task_id] = time.time()
        try:
            with session_scope() as s:
                s.add(PushLog(task_id=task_id, kind=kind, message_id=message_id))
        except Exception as e:
            logger.warning("写 PushLog 失败（冷却内存已记录，不影响推送）: {}", e)

    def should_push_change(
        self, task_id: int, diff: DiffResult, keywords: list[str]
    ) -> tuple[bool, str]:
        """判断变化是否值得推送。返回 (是否推, 不推的理由)。"""
        if self.is_muted():
            return False, "服务处于免打扰期"
        if self._is_cooling(task_id):
            return False, f"任务 #{task_id} 处于推送冷却期"
        if not diff.changed:
            kws = [kw for kw in (keywords or []) if kw and kw.strip()]
            if kws:
                return False, f"本次变化未触及关键字 {kws}"
            return False, "无实质变化"

        has_keywords = any(kw and kw.strip() for kw in (keywords or []))
        if not has_keywords and diff.change_ratio < self.cfg.min_change_ratio:
            return (
                False,
                f"变化占比 {diff.change_ratio:.3%} 低于阈值 "
                f"{self.cfg.min_change_ratio:.3%}",
            )
        return True, ""

    def matched_keywords(self, diff: DiffResult, keywords: list[str]) -> list[str]:
        """返回 diff 中命中的关键词列表。"""
        if not keywords:
            return []
        kws = [kw for kw in keywords if kw and kw.strip()]
        if not kws:
            return []
        text = "\n".join(diff.added_lines + diff.removed_lines).lower()
        return [kw for kw in kws if kw.lower() in text]

    def should_alert_failure(self, consecutive_failures: int) -> bool:
        """是否应发送失败告警。"""
        return consecutive_failures >= self.cfg.alert_after_consecutive_failures

    def can_send_failure_alert(self, task_id: int) -> bool:
        """失败告警也要走冷却，避免刷屏。"""
        return not self.is_muted() and not self._is_cooling(task_id)


def _parse_duration(text: str) -> int:
    """'30m' / '2h' / '1d' / '60' → 秒。"""
    text = text.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if text and text[-1] in units:
        try:
            return int(text[:-1]) * units[text[-1]]
        except ValueError as e:
            raise ValueError(f"无效时长：{text}") from e
    try:
        return int(text)
    except ValueError as e:
        raise ValueError(f"无效时长：{text}，示例：30m / 2h / 1d") from e


__all__ = ["RiskController"]
