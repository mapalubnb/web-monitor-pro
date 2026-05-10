"""
风控模块

职责：
1. 抓取侧风控
   - 单域名最小间隔（避免高频冲击目标站点）
   - 请求抖动（避免固定周期被识别）
   - 指数退避（失败后自动拉长间隔）
   - 全局并发信号量（避免任务雪崩）

2. 推送侧风控
   - 推送冷却（同任务 N 秒内只推一次）
   - 变化噪音过滤（change_ratio 低于阈值丢弃）
   - 连续失败 N 次才告警（避免单次抖动刷屏）
   - 全局 mute（临时免打扰）

所有状态用线程安全的内存结构 + 数据库混合持久化。
"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

from .config import RiskControlConfig
from .db import PushLog, get_state, session_scope, set_state
from .differ import DiffResult
from .logger import logger


# ============================================================
# 抓取侧风控
# ============================================================
class FetchRiskController:
    """
    抓取侧风控：在调用 FetchEngine 之前调用 acquire() 即可获得放行。
    """

    def __init__(self, cfg: RiskControlConfig, max_concurrent: int = 5):
        self.cfg = cfg
        self._domain_last_hit: dict[str, float] = {}
        self._lock = threading.Lock()
        self._semaphore = threading.BoundedSemaphore(max_concurrent)

    # ----- 域名限流 -----
    def _wait_for_domain(self, url: str) -> None:
        """若同一域名距上次请求不足 domain_min_interval，则 sleep 补齐。"""
        domain = urlparse(url).netloc
        if not domain:
            return
        with self._lock:
            last = self._domain_last_hit.get(domain, 0.0)
            now = time.time()
            interval = self.cfg.domain_min_interval
            wait = last + interval - now
            # 加抖动
            jitter = random.uniform(-self.cfg.jitter_ratio, self.cfg.jitter_ratio) * interval
            wait += jitter
            if wait > 0:
                logger.debug("⏱️  风控：域名 [{}] 需等待 {:.1f}s", domain, wait)
            self._domain_last_hit[domain] = now + max(wait, 0)
        if wait > 0:
            time.sleep(wait)

    def acquire(self, url: str) -> "FetchSlot":
        """
        获取一次抓取机会（阻塞直到满足所有风控条件）。
        用法：
            with risk.acquire(url):
                engine.fetch(task)
        """
        self._semaphore.acquire()
        try:
            self._wait_for_domain(url)
        except Exception:
            self._semaphore.release()
            raise
        return FetchSlot(self._semaphore)

    # ----- 退避调度 -----
    def next_interval_after_failure(self, consecutive_failures: int, base: int) -> int:
        """
        根据连续失败次数，返回下次调度的间隔（秒）。
        失败时按阶梯退避，成功时使用 base。
        """
        if consecutive_failures <= 0:
            return base
        ladder = self.cfg.backoff_ladder or [60, 300, 900, 3600]
        idx = min(consecutive_failures - 1, len(ladder) - 1)
        return max(base, ladder[idx])


class FetchSlot:
    """抓取配额的上下文管理器，退出时释放信号量。"""

    def __init__(self, semaphore: threading.BoundedSemaphore):
        self._semaphore = semaphore
        self._released = False

    def __enter__(self) -> "FetchSlot":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._released:
            self._semaphore.release()
            self._released = True


# ============================================================
# 推送侧风控
# ============================================================
class PushRiskController:
    """推送侧风控。"""

    def __init__(self, cfg: RiskControlConfig):
        self.cfg = cfg
        self._cooldown_mem: dict[int, float] = {}  # task_id -> last_push_ts
        self._lock = threading.Lock()

    # ----- 免打扰 -----
    def is_muted(self) -> bool:
        """检查全局是否被 mute。"""
        mute_until = get_state("mute_until", 0)
        try:
            return float(mute_until) > time.time()
        except (TypeError, ValueError):
            return False

    def mute(self, duration_seconds: int) -> datetime:
        """开启免打扰，返回到期时间。"""
        until = time.time() + duration_seconds
        set_state("mute_until", until)
        return datetime.fromtimestamp(until)

    def unmute(self) -> None:
        set_state("mute_until", 0)

    # ----- 冷却 -----
    def is_cooling(self, task_id: int) -> bool:
        """该任务是否仍在推送冷却期内。"""
        with self._lock:
            last = self._cooldown_mem.get(task_id, 0)
            return (time.time() - last) < self.cfg.push_cooldown_seconds

    def mark_pushed(self, task_id: int, kind: str = "change", message_id: str | None = None) -> None:
        """记录一次推送（更新冷却 + 写 PushLog 表）。"""
        with self._lock:
            self._cooldown_mem[task_id] = time.time()
        with session_scope() as s:
            s.add(PushLog(task_id=task_id, kind=kind, message_id=message_id))

    # ----- 变化过滤 -----
    def should_push_change(self, task_id: int, diff: DiffResult, keywords: list[str]) -> tuple[bool, str]:
        """
        判断变化是否值得推送。

        Returns:
            (should_push, reason) reason 仅在被过滤时有值
        """
        if self.is_muted():
            return False, "服务处于免打扰期"

        if self.is_cooling(task_id):
            return False, f"任务 #{task_id} 处于推送冷却期（{self.cfg.push_cooldown_seconds}s）"

        if not diff.changed:
            return False, "无实质变化"

        # 噪音过滤
        if diff.change_ratio < self.cfg.min_change_ratio:
            return False, (
                f"变化占比 {diff.change_ratio:.3%} 低于阈值 "
                f"{self.cfg.min_change_ratio:.3%}，判定为噪音"
            )

        # 关键字命中（如果任务配置了关键字，只有命中才推）
        if keywords:
            changed_text = "\n".join(diff.added_lines + diff.removed_lines).lower()
            matched = [kw for kw in keywords if kw.lower() in changed_text]
            if not matched:
                return False, f"未命中关键字 {keywords}"

        return True, ""

    def matched_keywords(self, diff: DiffResult, keywords: list[str]) -> list[str]:
        """返回本次 diff 命中的关键字列表。"""
        if not keywords:
            return []
        changed_text = "\n".join(diff.added_lines + diff.removed_lines).lower()
        return [kw for kw in keywords if kw.lower() in changed_text]

    # ----- 失败告警节流 -----
    def should_alert_failure(self, consecutive_failures: int) -> bool:
        """连续失败 N 次后才告警，避免单次抖动刷屏。"""
        return consecutive_failures >= self.cfg.alert_after_consecutive_failures


# ============================================================
# 对外接口
# ============================================================
class RiskController:
    """抓取 + 推送风控的组合入口。"""

    def __init__(self, cfg: RiskControlConfig, max_concurrent: int = 5):
        self.cfg = cfg
        self.fetch = FetchRiskController(cfg, max_concurrent)
        self.push = PushRiskController(cfg)

    def mute_for(self, duration_text: str) -> datetime:
        """
        便捷 API：接受 30m / 2h / 1d 格式的时长。
        """
        seconds = _parse_duration(duration_text)
        return self.push.mute(seconds)

    def mute_status(self) -> datetime | None:
        mute_until = get_state("mute_until", 0)
        try:
            ts = float(mute_until)
        except (TypeError, ValueError):
            return None
        if ts <= time.time():
            return None
        return datetime.fromtimestamp(ts)


def _parse_duration(text: str) -> int:
    """解析 '30m' / '2h' / '1d' 为秒数。"""
    text = text.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if text and text[-1] in units:
        try:
            value = int(text[:-1])
        except ValueError as e:
            raise ValueError(f"无效的时长格式：{text}") from e
        return value * units[text[-1]]
    try:
        return int(text)  # 纯数字视为秒
    except ValueError as e:
        raise ValueError(f"无效的时长格式：{text}，示例：30m / 2h / 1d") from e


__all__ = [
    "RiskController",
    "FetchRiskController",
    "PushRiskController",
    "FetchSlot",
]
