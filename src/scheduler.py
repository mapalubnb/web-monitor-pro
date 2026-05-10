"""
任务调度器

职责：
- 启动时为所有启用任务注册 APScheduler 作业（按 interval 秒触发）
- 动态 add/pause/resume/remove/reschedule 作业
- 支持立即触发（用户点 /check 或新建任务后）
- 所有作业都在线程池里运行，互不阻塞
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from .config import AppConfig
from .db import Task, session_scope
from .logger import logger
from .risk_control import RiskController


class MonitorScheduler:
    """APScheduler 的轻量封装，面向 Task 对象。"""

    def __init__(self, cfg: AppConfig, risk: RiskController, run_fn: Callable[[int], None]):
        """
        Args:
            run_fn: callable(task_id: int) -> None，即 MonitorRunner.run_once
        """
        self.cfg = cfg
        self.risk = risk
        self._run_fn = run_fn
        # max_workers 与 risk_control 的并发限制呼应
        executors = {
            "default": {"type": "threadpool", "max_workers": max(cfg.max_concurrent_fetch * 2, 4)},
        }
        job_defaults = {
            "coalesce": True,        # 错过多次触发时只补跑一次
            "max_instances": 1,      # 同任务同一时刻只跑 1 个实例
            "misfire_grace_time": 60,
        }
        self._sched = BackgroundScheduler(
            executors=executors, job_defaults=job_defaults, timezone="UTC"
        )

    # --------------------------------------------------------
    # 生命周期
    # --------------------------------------------------------
    def start(self) -> None:
        """注册所有启用任务并启动调度器。"""
        logger.info("📅 调度器正在启动...")
        self._sched.start()

        with session_scope() as s:
            tasks = s.execute(select(Task).where(Task.enabled.is_(True))).scalars().all()
            for t in tasks:
                self._add_or_update_job(t)
            logger.info("📅 已注册 {} 个调度作业", len(tasks))

    def stop(self) -> None:
        logger.info("📅 调度器停止中...")
        try:
            self._sched.shutdown(wait=False)
        except Exception:
            pass

    # --------------------------------------------------------
    # 作业管理
    # --------------------------------------------------------
    def _job_id(self, task_id: int) -> str:
        return f"task_{task_id}"

    def _add_or_update_job(self, task: Task) -> None:
        """按 task.interval 注册/更新作业。"""
        interval = self.risk.fetch.next_interval_after_failure(
            task.consecutive_failures, task.interval or self.cfg.default_check_interval,
        )
        # 轻微错峰：初次调度时间往后抖 0-interval 秒，避免所有任务同时触发
        jitter = random.randint(0, min(interval, 30))
        next_run = datetime.utcnow() + timedelta(seconds=jitter)

        job_id = self._job_id(task.id)
        self._sched.add_job(
            func=self._run_fn,
            args=[task.id],
            trigger=IntervalTrigger(seconds=interval),
            id=job_id,
            replace_existing=True,
            next_run_time=next_run,
            name=f"[{task.id}] {task.name}",
        )
        logger.debug("📅 已注册作业 {} 间隔={}s 首次于={}", job_id, interval, next_run)

    def sync_task(self, task_id: int) -> None:
        """
        根据数据库中任务的最新状态同步调度（add/update/remove）。
        - enabled=True  → 添加或更新
        - enabled=False → 移除
        - 不存在        → 移除
        """
        with session_scope() as s:
            t = s.get(Task, task_id)
        job_id = self._job_id(task_id)

        if t is None or not t.enabled:
            if self._sched.get_job(job_id):
                self._sched.remove_job(job_id)
                logger.info("📅 已从调度中移除 task #{}", task_id)
            return

        self._add_or_update_job(t)

    def reschedule_all(self) -> None:
        """重新扫描 DB 并同步全部调度。"""
        with session_scope() as s:
            tasks = s.execute(select(Task)).scalars().all()
        for t in tasks:
            self.sync_task(t.id)

    def trigger_now(self, task_id: int) -> None:
        """立即触发一次（在调度线程池中异步执行）。"""
        logger.info("⚡ 用户触发立即检查任务 #{}", task_id)
        self._sched.add_job(
            func=self._run_fn,
            args=[task_id],
            trigger="date",
            run_date=datetime.utcnow(),
            id=f"{self._job_id(task_id)}_immediate_{datetime.utcnow().timestamp()}",
            replace_existing=False,
            misfire_grace_time=30,
        )


__all__ = ["MonitorScheduler"]
