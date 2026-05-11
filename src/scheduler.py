"""任务调度器（APScheduler 封装）。"""

from __future__ import annotations

import random
import uuid
from datetime import timedelta
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from .config import AppConfig
from .db import Task, now_utc, session_scope
from .logger import logger
from .risk_control import RiskController


class MonitorScheduler:
    """APScheduler 的薄封装，面向 Task。"""

    def __init__(self, cfg: AppConfig, risk: RiskController,
                 run_fn: Callable[[int], None]):
        self.cfg = cfg
        self.risk = risk
        self._run_fn = run_fn
        self._sched = BackgroundScheduler(
            executors={"default": {"type": "threadpool",
                                   "max_workers": max(cfg.max_concurrent_fetch * 2, 4)}},
            job_defaults={"coalesce": True, "max_instances": 1,
                          "misfire_grace_time": 60},
            timezone="UTC",
        )

    def start(self) -> None:
        self._sched.start()
        with session_scope() as s:
            tasks = s.execute(
                select(Task).where(Task.enabled.is_(True))
            ).scalars().all()
            for t in tasks:
                self._add_or_update(t)
        logger.info("📅 调度器启动（{} 个任务）", len(tasks))

    def stop(self) -> None:
        try:
            self._sched.shutdown(wait=False)
        except Exception:
            pass

    def sync_task(self, task_id: int) -> None:
        """根据 DB 状态同步调度。"""
        job_id = f"task_{task_id}"
        with session_scope() as s:
            t = s.get(Task, task_id)
            if t is None or not t.enabled:
                if self._sched.get_job(job_id):
                    self._sched.remove_job(job_id)
                    logger.info("📅 移除调度 #{}", task_id)
                return
            s.expunge(t)
        self._add_or_update(t)

    def trigger_now(self, task_id: int) -> None:
        """立即触发一次检查（异步执行）。

        - interval job 的下次执行推迟到完整周期之后，避免紧接着又跑一次；
        - 用 uuid 做后缀保证 job_id 唯一，不会和快速连点 /check 的 job 冲突；
        - MonitorRunner 内有任务锁，所以即便 job 入队也不会并发执行。
        """
        logger.info("⚡ 立即触发任务 #{}", task_id)

        interval_job_id = f"task_{task_id}"
        interval_job = self._sched.get_job(interval_job_id)
        if interval_job is not None:
            try:
                interval_sec = interval_job.trigger.interval.total_seconds()
                new_next = now_utc() + timedelta(seconds=interval_sec)
                self._sched.modify_job(interval_job_id, next_run_time=new_next)
            except Exception as e:
                logger.warning("推迟 interval 作业失败: {}", e)

        self._sched.add_job(
            func=self._run_fn,
            args=[task_id],
            trigger="date",
            run_date=now_utc(),
            id=f"task_{task_id}_now_{uuid.uuid4().hex}",
            misfire_grace_time=30,
        )

    def _add_or_update(self, task: Task) -> None:
        """按 interval 注册/更新作业，带初次调度错峰抖动。"""
        interval = self.risk.next_interval_after_failure(
            task.consecutive_failures,
            task.interval or self.cfg.default_check_interval,
        )
        jitter = random.randint(0, min(interval, 30))
        self._sched.add_job(
            func=self._run_fn,
            args=[task.id],
            trigger=IntervalTrigger(seconds=interval),
            id=f"task_{task.id}",
            replace_existing=True,
            next_run_time=now_utc() + timedelta(seconds=jitter),
            name=f"[{task.id}] {task.name}",
        )


__all__ = ["MonitorScheduler"]
