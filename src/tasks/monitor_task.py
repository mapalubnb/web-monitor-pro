"""
单个监控任务的执行闭环

MonitorRunner.run_once(task_id)：
  1. 从数据库加载 Task
  2. 风控放行（限流 + 并发）
  3. 调用 FetchEngine.fetch()
  4. 提取内容 & hash
  5. 与上次快照对比（diff）
  6. 持久化快照 & 变更
  7. 根据情况推送飞书卡片 + 文件附件
  8. 更新 Task 运行时状态 & 下次调度间隔
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import SNAPSHOT_DIR, AppConfig
from ..db import ChangeHistory, Snapshot, Task, session_scope
from ..differ import DiffResult, compute_diff
from ..feishu import FeishuClient, cards
from ..feishu.client import ensure_upload_size
from ..fetcher import FetchEngine, FetchResult, content_hash, extract
from ..logger import logger
from ..risk_control import RiskController


class MonitorRunner:
    """单任务执行器（无状态，可被调度器并发调用）。"""

    def __init__(
        self,
        cfg: AppConfig,
        engine: FetchEngine,
        risk: RiskController,
        feishu: FeishuClient,
    ):
        self.cfg = cfg
        self.engine = engine
        self.risk = risk
        self.feishu = feishu

    # ========================================================
    # 对外入口
    # ========================================================
    def run_once(self, task_id: int) -> None:
        """执行一次监控。任何内部异常都会被捕获并记录，不向上抛。"""
        with session_scope() as s:
            task = s.get(Task, task_id)
            if task is None:
                logger.warning("任务 #{} 不存在，跳过本次执行", task_id)
                return
            if not task.enabled:
                logger.debug("任务 #{} [{}] 已暂停，跳过", task_id, task.name)
                return
            # 取出需要的字段，session 外继续使用
            s.expunge(task)

        logger.info("⚡ 开始检查任务 #{} [{}] url={}", task.id, task.name, task.url)

        try:
            with self.risk.fetch.acquire(task.url):
                result = self.engine.fetch(task)
        except Exception as e:
            logger.exception("抓取阶段异常: {}", e)
            self._handle_fetch_failure(task, error=str(e))
            return

        if not result.ok:
            logger.warning(
                "❌ 任务 #{} 抓取失败：strategy={} status={} err={}",
                task.id, result.strategy_used, result.status_code, result.error,
            )
            self._handle_fetch_failure(task, error=result.error or f"HTTP {result.status_code}")
            return

        # 提取 + 归一化
        try:
            extracted = extract(task, result)
        except Exception as e:
            logger.exception("提取阶段异常: {}", e)
            self._handle_fetch_failure(task, error=f"提取失败: {e}")
            return

        if not extracted.strip():
            logger.warning(
                "任务 #{} [{}] 提取结果为空（HTTP={} strategy={} content_len={}）。"
                "💡 在飞书发送 `/debug {}` 可查看页面诊断报告",
                task.id, task.name, result.status_code,
                result.strategy_used, len(result.content or ""), task.id,
            )
            self._handle_fetch_failure(task, error="提取结果为空（可能是纯前端 SPA，请试 /debug 或 /sniff）")
            return

        new_hash = content_hash(extracted)

        # 处理：首次 or 对比
        if task.last_content_hash is None:
            self._handle_first_snapshot(task, result, extracted, new_hash)
        elif task.last_content_hash == new_hash:
            self._handle_no_change(task)
        else:
            self._handle_change(task, result, extracted, new_hash)

    # ========================================================
    # 首次快照
    # ========================================================
    def _handle_first_snapshot(
        self,
        task: Task,
        result: FetchResult,
        extracted: str,
        new_hash: str,
    ) -> None:
        logger.info("📸 任务 #{} [{}] 首次建立基准快照（长度 {}）",
                    task.id, task.name, len(extracted))
        snap_path = self._save_snapshot(task.id, extracted)

        with session_scope() as s:
            db_task = s.get(Task, task.id)
            if db_task is None:
                return
            snap = Snapshot(
                task_id=task.id,
                content_hash=new_hash,
                content_length=len(extracted),
                content_path=str(snap_path),
                strategy_used=result.strategy_used,
                http_status=result.status_code,
            )
            s.add(snap)
            s.flush()
            db_task.last_content_hash = new_hash
            db_task.last_snapshot_path = str(snap_path)
            db_task.last_checked_at = datetime.utcnow()
            db_task.total_checks += 1
            db_task.consecutive_failures = 0

        # 推送首次快照卡片 + 附上 txt 文件
        card = cards.first_snapshot_card(
            task_id=task.id,
            task_name=task.name,
            url=task.url,
            content_length=len(extracted),
            strategy=result.strategy_used,
        )
        chat_id = self.cfg.feishu.target_chat_id
        if not chat_id:
            logger.warning("未配置 FEISHU_TARGET_CHAT_ID，首次快照卡片未推送")
            return

        display = f"[{task.name}] 首次快照.txt"
        safe_path = ensure_upload_size(snap_path)
        self.feishu.send_card_and_file(chat_id, card, safe_path, display)
        self.risk.push.mark_pushed(task.id, kind="first_snapshot")

    # ========================================================
    # 无变化
    # ========================================================
    def _handle_no_change(self, task: Task) -> None:
        logger.info("✅ 任务 #{} [{}] 无变化", task.id, task.name)
        with session_scope() as s:
            db_task = s.get(Task, task.id)
            if db_task is None:
                return
            db_task.last_checked_at = datetime.utcnow()
            db_task.total_checks += 1
            db_task.consecutive_failures = 0

    # ========================================================
    # 发现变化
    # ========================================================
    def _handle_change(
        self,
        task: Task,
        result: FetchResult,
        extracted: str,
        new_hash: str,
    ) -> None:
        # 读上一次快照
        before_text = ""
        if task.last_snapshot_path and Path(task.last_snapshot_path).exists():
            try:
                before_text = Path(task.last_snapshot_path).read_text(
                    encoding="utf-8", errors="replace"
                )
            except Exception as e:
                logger.warning("读取上次快照失败: {}", e)

        diff = compute_diff(before_text, extracted, is_json=(task.type == "json"))

        # 保存新快照
        snap_path = self._save_snapshot(task.id, extracted)
        diff_path = self._save_diff(task.id, diff.unified_diff)

        # 持久化
        with session_scope() as s:
            db_task = s.get(Task, task.id)
            if db_task is None:
                return
            snap = Snapshot(
                task_id=task.id,
                content_hash=new_hash,
                content_length=len(extracted),
                content_path=str(snap_path),
                strategy_used=result.strategy_used,
                http_status=result.status_code,
            )
            s.add(snap)
            s.flush()

            matched = self.risk.push.matched_keywords(diff, list(db_task.keywords or []))
            change = ChangeHistory(
                task_id=task.id,
                added_lines=len(diff.added_lines),
                removed_lines=len(diff.removed_lines),
                change_ratio=diff.change_ratio,
                matched_keywords=matched,
                after_snapshot_id=snap.id,
                diff_path=str(diff_path),
            )
            s.add(change)
            s.flush()
            change_id = change.id

            # 更新任务状态
            db_task.last_content_hash = new_hash
            db_task.last_snapshot_path = str(snap_path)
            db_task.last_checked_at = datetime.utcnow()
            db_task.last_changed_at = datetime.utcnow()
            db_task.total_checks += 1
            db_task.total_changes += 1
            db_task.consecutive_failures = 0

            # 快照副本给后续使用
            task_name = db_task.name
            task_url = db_task.url
            keywords = list(db_task.keywords or [])

        # 风控决策
        should_push, reason = self.risk.push.should_push_change(task.id, diff, keywords)
        if not should_push:
            logger.info(
                "🔇 任务 #{} [{}] 有变化但未推送：{}",
                task.id, task_name, reason,
            )
            return

        logger.info(
            "🔔 任务 #{} [{}] 变化将推送：➕{} ➖{} 变化占比={:.2%} 命中关键字={}",
            task.id, task_name,
            len(diff.added_lines), len(diff.removed_lines),
            diff.change_ratio, matched,
        )

        # 推送
        chat_id = self.cfg.feishu.target_chat_id
        if not chat_id:
            logger.warning("未配置 FEISHU_TARGET_CHAT_ID，变更卡片未推送")
            return

        card = cards.change_card(
            task_id=task.id,
            task_name=task_name,
            url=task_url,
            added_count=len(diff.added_lines),
            removed_count=len(diff.removed_lines),
            change_ratio=diff.change_ratio,
            diff_summary=diff.summary,
            strategy=result.strategy_used,
            matched_keywords=matched,
            has_diff_file=bool(diff.unified_diff),
        )
        file_path = diff_path if diff_path and diff_path.exists() else None
        safe_file = ensure_upload_size(file_path) if file_path else None
        card_msg_id, _ = self.feishu.send_card_and_file(
            chat_id, card, safe_file,
            f"[{task_name}] diff.txt" if safe_file else "",
        )
        self.risk.push.mark_pushed(task.id, kind="change", message_id=card_msg_id)

        # 标记推送完成
        with session_scope() as s:
            ch = s.get(ChangeHistory, change_id)
            if ch:
                ch.pushed = True

    # ========================================================
    # 失败处理
    # ========================================================
    def _handle_fetch_failure(self, task: Task, error: str) -> None:
        with session_scope() as s:
            db_task = s.get(Task, task.id)
            if db_task is None:
                return
            db_task.consecutive_failures += 1
            db_task.last_checked_at = datetime.utcnow()
            db_task.total_checks += 1
            fails = db_task.consecutive_failures
            name = db_task.name
            url = db_task.url

        # 连续失败 N 次才告警，避免单次抖动刷屏
        if self.risk.push.should_alert_failure(fails) and not self.risk.push.is_muted():
            chat_id = self.cfg.feishu.target_chat_id
            if chat_id and not self.risk.push.is_cooling(task.id):
                card = cards.fetch_failure_card(
                    task_id=task.id, task_name=name, url=url,
                    consecutive_failures=fails, error=error,
                )
                self.feishu.send_card(chat_id, card)
                self.risk.push.mark_pushed(task.id, kind="error")

    # ========================================================
    # 持久化快照 / diff
    # ========================================================
    @staticmethod
    def _save_snapshot(task_id: int, content: str) -> Path:
        """保存当前归一化后的正文到 data/snapshots/。"""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = SNAPSHOT_DIR / f"task_{task_id}_{ts}.txt"
        path.write_text(content, encoding="utf-8")
        # 同时维护一份 latest 快捷方式，用于下次比对和 /check 等命令读取
        latest = SNAPSHOT_DIR / f"task_{task_id}_latest.txt"
        latest.write_text(content, encoding="utf-8")
        return latest

    @staticmethod
    def _save_diff(task_id: int, unified: str) -> Path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = SNAPSHOT_DIR / f"task_{task_id}_diff_{ts}.diff"
        path.write_text(unified or "", encoding="utf-8")
        return path


# 让 DiffResult 出现在模块符号中，给 IDE 一点帮助
_ = DiffResult
_ = Any

__all__ = ["MonitorRunner"]
