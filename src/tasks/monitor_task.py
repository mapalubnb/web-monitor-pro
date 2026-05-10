"""
单个监控任务的执行闭环

MonitorRunner.run_once(task_id):
  1. 加载 Task（若禁用/不存在则跳过）
  2. 风控放行 + 抓取
  3. 提取 + hash
  4. 与上次快照对比 → 首次 / 无变化 / 变化
  5. 推送飞书卡片 + 附件
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..config import SNAPSHOT_DIR, AppConfig
from ..db import ChangeHistory, Task, session_scope
from ..differ import compute_diff, filter_by_keywords
from ..feishu import FeishuClient, cards
from ..feishu.client import ensure_upload_size
from ..fetcher import FetchEngine, FetchResult, content_hash, extract
from ..logger import logger
from ..risk_control import RiskController


class MonitorRunner:
    """单任务执行器（无状态，调度器并发调用）。"""

    def __init__(self, cfg: AppConfig, engine: FetchEngine,
                 risk: RiskController, feishu: FeishuClient):
        self.cfg = cfg
        self.engine = engine
        self.risk = risk
        self.feishu = feishu

    # ============================================================
    # 入口
    # ============================================================
    def run_once(self, task_id: int) -> None:
        """执行一次监控；内部异常全部捕获不向上抛。"""
        task = self._load_task(task_id)
        if task is None:
            return

        logger.info("⚡ 检查 #{} [{}]", task.id, task.name)

        # 抓取
        try:
            with self.risk.acquire_fetch(task.url):
                result = self.engine.fetch(task)
        except Exception as e:
            logger.exception("抓取异常: {}", e)
            self._handle_failure(task, str(e))
            return

        if not result.ok:
            logger.warning(
                "❌ #{} 失败 HTTP={} err={}",
                task.id, result.status_code, result.error,
            )
            self._handle_failure(
                task, result.error or f"HTTP {result.status_code}"
            )
            return

        # 提取 + 归一化
        try:
            extracted = extract(task, result)
        except Exception as e:
            logger.exception("提取异常: {}", e)
            self._handle_failure(task, f"提取失败: {e}")
            return

        if not extracted.strip():
            logger.warning(
                "#{} [{}] 提取结果为空 (strategy={} len={}) "
                "→ 建议 `/debug {}`",
                task.id, task.name, result.strategy_used,
                len(result.content or ""), task.id,
            )
            self._handle_failure(
                task, "提取结果为空（疑似纯 SPA，试 /debug 或 /reset --strategy jina）"
            )
            return

        # 对比
        new_hash = content_hash(extracted)
        if task.last_content_hash is None:
            self._handle_first(task, result, extracted, new_hash)
        elif task.last_content_hash == new_hash:
            self._handle_no_change(task)
        else:
            self._handle_change(task, result, extracted, new_hash)

    # ============================================================
    # 内部：加载任务
    # ============================================================
    def _load_task(self, task_id: int) -> Task | None:
        with session_scope() as s:
            t = s.get(Task, task_id)
            if t is None:
                logger.warning("任务 #{} 不存在，跳过", task_id)
                return None
            if not t.enabled:
                return None
            s.expunge(t)
            return t

    # ============================================================
    # 情况 1：首次
    # ============================================================
    def _handle_first(self, task: Task, result: FetchResult,
                      extracted: str, new_hash: str) -> None:
        logger.info(
            "📸 #{} [{}] 建立首次快照（{} 字）",
            task.id, task.name, len(extracted),
        )
        snap_path = self._save_snapshot(task.id, extracted)

        with session_scope() as s:
            t = s.get(Task, task.id)
            if t is None:
                return
            t.last_content_hash = new_hash
            t.last_snapshot_path = str(snap_path)
            t.last_checked_at = datetime.utcnow()
            t.total_checks += 1
            t.consecutive_failures = 0

        chat_id = self.cfg.feishu.target_chat_id
        if not chat_id:
            return

        card = cards.first_snapshot_card(
            task_id=task.id, task_name=task.name, url=task.url,
            content_length=len(extracted), strategy=result.strategy_used,
        )
        self.feishu.send_card_and_file(
            chat_id, card,
            ensure_upload_size(snap_path),
            f"[{task.name}] 首次快照.txt",
        )
        self.risk.mark_pushed(task.id, kind="first_snapshot")

    # ============================================================
    # 情况 2：无变化
    # ============================================================
    def _handle_no_change(self, task: Task) -> None:
        logger.debug("✅ #{} 无变化", task.id)
        # 如果之前有 pending（上次检测到变化但还没确认），现在内容恢复了
        # → 说明是闪烁，清除 pending
        pending_path = self._pending_path(task.id)
        if pending_path.exists():
            self._clear_pending(pending_path)
            logger.info(
                "🔄 #{} [{}] 内容恢复到基准（闪烁），已清除 pending",
                task.id, task.name,
            )
        with session_scope() as s:
            t = s.get(Task, task.id)
            if t is None:
                return
            t.last_checked_at = datetime.utcnow()
            t.total_checks += 1
            t.consecutive_failures = 0

    # ============================================================
    # 情况 3：变化（含二次确认，防止页面闪烁/抖动）
    # ============================================================
    def _handle_change(self, task: Task, result: FetchResult,
                       extracted: str, new_hash: str) -> None:
        """
        变化处理流程（二次确认机制）：

        为了防止页面内容闪烁（如 SPA 渲染不稳定、CDN 缓存切换）导致
        "一行被删了又马上加回来"的假阳性推送，采用二次确认：

        1. 首次发现变化 → 写 pending 标记（不推送、不更新基准）
        2. 下次检查时：
           - 若内容 hash 仍与 pending 一致 → 确认为真变化 → 推送
           - 若内容恢复为基准 hash → 闪烁 → 清除 pending、不推送
           - 若内容变成第三种 hash → 更新 pending、等待再次确认
        """
        keywords = list(task.keywords or [])
        has_keywords = any(kw and kw.strip() for kw in keywords)

        pending_path = self._pending_path(task.id)
        pending_hash = self._read_pending(pending_path)

        if pending_hash is None:
            # === 首次发现变化：标记 pending，不推送 ===
            self._write_pending(pending_path, new_hash)
            logger.info(
                "⏳ #{} [{}] 发现变化，等待下次确认（防闪烁）",
                task.id, task.name,
            )
            # 只更新检查时间，不动基准
            with session_scope() as s:
                t = s.get(Task, task.id)
                if t is None:
                    return
                t.last_checked_at = datetime.utcnow()
                t.total_checks += 1
                t.consecutive_failures = 0
            return

        # === 有 pending 标记 ===
        if new_hash == task.last_content_hash:
            # 恢复到基准 → 页面闪烁，清除 pending
            self._clear_pending(pending_path)
            logger.info(
                "🔄 #{} [{}] 内容恢复原样（页面闪烁），已清除 pending",
                task.id, task.name,
            )
            with session_scope() as s:
                t = s.get(Task, task.id)
                if t is None:
                    return
                t.last_checked_at = datetime.utcnow()
                t.total_checks += 1
                t.consecutive_failures = 0
            return

        if new_hash != pending_hash:
            # 变成了第三种内容 → 更新 pending，继续等
            self._write_pending(pending_path, new_hash)
            logger.info(
                "⏳ #{} [{}] 内容再次变化（不同于上次 pending），重新等待确认",
                task.id, task.name,
            )
            with session_scope() as s:
                t = s.get(Task, task.id)
                if t is None:
                    return
                t.last_checked_at = datetime.utcnow()
                t.total_checks += 1
                t.consecutive_failures = 0
            return

        # === new_hash == pending_hash：二次确认通过 → 确认变化 ===
        self._clear_pending(pending_path)
        logger.info(
            "✅ #{} [{}] 二次确认通过，确认为真实变化",
            task.id, task.name,
        )

        # 读取基准快照做 diff
        before = ""
        if task.last_snapshot_path and Path(task.last_snapshot_path).exists():
            try:
                before = Path(task.last_snapshot_path).read_text(
                    encoding="utf-8", errors="replace"
                )
            except Exception as e:
                logger.warning("读取上次快照失败: {}", e)

        full_diff = compute_diff(before, extracted, is_json=(task.type == "json"))
        diff = filter_by_keywords(full_diff, keywords) if has_keywords else full_diff

        # 关键词模式下，过滤后没行 → 静默推进基准
        if has_keywords and not diff.changed:
            logger.info(
                "🔇 #{} [{}] 页面有变化但未触及关键字 {} → 静默更新基准",
                task.id, task.name, [kw for kw in keywords if kw and kw.strip()],
            )
            snap_path = self._save_snapshot(task.id, extracted)
            with session_scope() as s:
                t = s.get(Task, task.id)
                if t is None:
                    return
                t.last_content_hash = new_hash
                t.last_snapshot_path = str(snap_path)
                t.last_checked_at = datetime.utcnow()
                t.total_checks += 1
                t.consecutive_failures = 0
            return

        # 持久化
        snap_path = self._save_snapshot(task.id, extracted)
        diff_path = self._save_diff(task.id, diff.unified_diff)

        with session_scope() as s:
            t = s.get(Task, task.id)
            if t is None:
                return
            matched = self.risk.matched_keywords(diff, keywords)

            s.add(ChangeHistory(
                task_id=task.id,
                added_lines=len(diff.added_lines),
                removed_lines=len(diff.removed_lines),
                change_ratio=diff.change_ratio,
                matched_keywords=matched,
                diff_path=str(diff_path),
            ))

            t.last_content_hash = new_hash
            t.last_snapshot_path = str(snap_path)
            t.last_checked_at = datetime.utcnow()
            t.last_changed_at = datetime.utcnow()
            t.total_checks += 1
            t.total_changes += 1
            t.consecutive_failures = 0

            task_name = t.name
            task_url = t.url

        # 风控过滤
        should_push, reason = self.risk.should_push_change(
            task.id, diff, keywords
        )
        if not should_push:
            logger.info("🔇 #{} 有变化但未推送：{}", task.id, reason)
            return

        logger.info(
            "🔔 #{} [{}] 推送变化：➕{} ➖{} 占比={:.2%} 关键字={}",
            task.id, task_name,
            len(diff.added_lines), len(diff.removed_lines),
            diff.change_ratio, matched,
        )

        chat_id = self.cfg.feishu.target_chat_id
        if not chat_id:
            return

        card = cards.change_card(
            task_id=task.id, task_name=task_name, url=task_url,
            added_count=len(diff.added_lines),
            removed_count=len(diff.removed_lines),
            change_ratio=diff.change_ratio,
            diff_summary=diff.summary,
            strategy=result.strategy_used,
            matched_keywords=matched,
            has_diff_file=bool(diff.unified_diff),
            keyword_filtered=has_keywords,
        )
        file_path = (
            ensure_upload_size(diff_path)
            if diff_path and diff_path.exists() else None
        )
        card_msg_id, _ = self.feishu.send_card_and_file(
            chat_id, card, file_path,
            f"[{task_name}] diff.txt" if file_path else "",
        )
        self.risk.mark_pushed(task.id, kind="change", message_id=card_msg_id)

    # ============================================================
    # 失败处理（连续失败 N 次才告警）
    # ============================================================
    def _handle_failure(self, task: Task, error: str) -> None:
        with session_scope() as s:
            t = s.get(Task, task.id)
            if t is None:
                return
            t.consecutive_failures += 1
            t.last_checked_at = datetime.utcnow()
            t.total_checks += 1
            fails = t.consecutive_failures
            name = t.name
            url = t.url

        if (
            self.risk.should_alert_failure(fails)
            and self.risk.can_send_failure_alert(task.id)
        ):
            chat_id = self.cfg.feishu.target_chat_id
            if chat_id:
                self.feishu.send_card(
                    chat_id,
                    cards.fetch_failure_card(
                        task.id, name, url, fails, error,
                    ),
                )
                self.risk.mark_pushed(task.id, kind="error")

    # ============================================================
    # 持久化快照/diff（单写，无冗余时间戳备份）
    # ============================================================
    @staticmethod
    def _save_snapshot(task_id: int, content: str) -> Path:
        """保存当前快照到 latest 文件，不再写时间戳副本节省磁盘。"""
        path = SNAPSHOT_DIR / f"task_{task_id}_latest.txt"
        path.write_text(content, encoding="utf-8")
        return path

    @staticmethod
    def _save_diff(task_id: int, unified: str) -> Path:
        """保存 diff；只保留最新（旧的在 DB 里有记录）。"""
        path = SNAPSHOT_DIR / f"task_{task_id}_latest.diff"
        path.write_text(unified or "", encoding="utf-8")
        return path

    # ============================================================
    # Pending（二次确认）文件操作
    # ============================================================
    @staticmethod
    def _pending_path(task_id: int) -> Path:
        """pending 标记文件路径。"""
        return SNAPSHOT_DIR / f"task_{task_id}_pending.hash"

    @staticmethod
    def _read_pending(path: Path) -> str | None:
        """读取 pending hash；不存在返回 None。"""
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8").strip()
                return content if content else None
            except Exception:
                return None
        return None

    @staticmethod
    def _write_pending(path: Path, hash_val: str) -> None:
        """写入 pending hash。"""
        path.write_text(hash_val, encoding="utf-8")

    @staticmethod
    def _clear_pending(path: Path) -> None:
        """清除 pending 标记。"""
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


__all__ = ["MonitorRunner"]
