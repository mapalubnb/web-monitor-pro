"""Monitor task execution: fetch -> extract -> diff -> notify."""

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

CIRCUIT_BREAKER_THRESHOLD = 20


class MonitorRunner:
    """Executes a single monitoring task (stateless, called by scheduler)."""

    def __init__(self, cfg: AppConfig, engine: FetchEngine,
                 risk: RiskController, feishu: FeishuClient):
        self.cfg = cfg
        self.engine = engine
        self.risk = risk
        self.feishu = feishu

    def run_once(self, task_id: int) -> None:
        """Run one check cycle; all exceptions caught internally."""
        task = self._load_task(task_id)
        if task is None:
            return

        logger.info("check #{} [{}]", task.id, task.name)

        # Fetch
        try:
            with self.risk.acquire_fetch(task.url):
                result = self.engine.fetch(task)
        except Exception as e:
            logger.exception("fetch error: {}", e)
            self._handle_failure(task, str(e))
            return

        if not result.ok:
            logger.warning("#{} failed HTTP={} err={}", task.id, result.status_code, result.error)
            self._handle_failure(task, result.error or f"HTTP {result.status_code}")
            return

        # Extract + normalize
        try:
            extracted = extract(task, result)
        except Exception as e:
            logger.exception("extraction error: {}", e)
            self._handle_failure(task, f"extraction failed: {e}")
            return

        if not extracted.strip():
            logger.warning("#{} [{}] empty extraction (strategy={} len={})",
                           task.id, task.name, result.strategy_used, len(result.content or ""))
            self._handle_failure(task, "empty extraction (try /debug or /reset --strategy playwright)")
            return

        # Compare
        new_hash = content_hash(extracted)
        if task.last_content_hash is None:
            self._handle_first(task, result, extracted, new_hash)
        elif task.last_content_hash == new_hash:
            self._handle_no_change(task)
        else:
            self._handle_change(task, result, extracted, new_hash)

    # --- Task DB helpers ---

    def _load_task(self, task_id: int) -> Task | None:
        """Load and expunge task if it exists and is enabled."""
        with session_scope() as s:
            t = s.get(Task, task_id)
            if t is None or not t.enabled:
                return None
            s.expunge(t)
            return t

    def _touch_task(self, task_id: int) -> None:
        """Update check timestamp and reset failure counter."""
        with session_scope() as s:
            t = s.get(Task, task_id)
            if t is None:
                return
            t.last_checked_at = datetime.utcnow()
            t.total_checks += 1
            t.consecutive_failures = 0

    def _update_baseline(self, task_id: int, new_hash: str,
                         snap_path: Path, strategy: str) -> None:
        """Update baseline snapshot, hash, strategy and reset counters."""
        with session_scope() as s:
            t = s.get(Task, task_id)
            if t is None:
                return
            t.last_content_hash = new_hash
            t.last_snapshot_path = str(snap_path)
            t.last_strategy_used = strategy
            t.last_checked_at = datetime.utcnow()
            t.total_checks += 1
            t.consecutive_failures = 0

    # --- Pending file helpers ---

    @staticmethod
    def _pending_path(task_id: int) -> Path:
        return SNAPSHOT_DIR / f"task_{task_id}_pending.hash"

    @staticmethod
    def _read_pending(path: Path) -> tuple[str | None, str]:
        """Read pending file. Returns (hash, strategy) or (None, '')."""
        if not path.exists():
            return None, ""
        try:
            data = path.read_text(encoding="utf-8").strip()
            if not data:
                return None, ""
            if "|" in data:
                h, s = data.split("|", 1)
                return h, s
            return data, ""
        except Exception:
            return None, ""

    @staticmethod
    def _write_pending(path: Path, hash_val: str, strategy: str) -> None:
        path.write_text(f"{hash_val}|{strategy}", encoding="utf-8")

    @staticmethod
    def _clear_pending(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass

    # --- Snapshot helpers ---

    @staticmethod
    def _save_snapshot(task_id: int, content: str) -> Path:
        path = SNAPSHOT_DIR / f"task_{task_id}_latest.txt"
        path.write_text(content, encoding="utf-8")
        return path

    @staticmethod
    def _save_diff(task_id: int, unified: str) -> Path:
        path = SNAPSHOT_DIR / f"task_{task_id}_latest.diff"
        path.write_text(unified or "", encoding="utf-8")
        return path

    # --- Case 1: First snapshot ---

    def _handle_first(self, task: Task, result: FetchResult,
                      extracted: str, new_hash: str) -> None:
        logger.info("#{} [{}] first snapshot ({} chars, strategy={})",
                    task.id, task.name, len(extracted), result.strategy_used)
        snap_path = self._save_snapshot(task.id, extracted)
        self._update_baseline(task.id, new_hash, snap_path, result.strategy_used)

        chat_id = self.cfg.feishu.target_chat_id
        if not chat_id:
            return
        card = cards.first_snapshot_card(
            task_id=task.id, task_name=task.name, url=task.url,
            content_length=len(extracted), strategy=result.strategy_used)
        self.feishu.send_card_and_file(
            chat_id, card, ensure_upload_size(snap_path),
            f"[{task.name}] first_snapshot.txt")
        self.risk.mark_pushed(task.id, kind="first_snapshot")

    # --- Case 2: No change ---

    def _handle_no_change(self, task: Task) -> None:
        pending_path = self._pending_path(task.id)
        if pending_path.exists():
            self._clear_pending(pending_path)
            logger.debug("#{} [{}] content restored to baseline (flicker), pending cleared",
                         task.id, task.name)
        self._touch_task(task.id)

    # --- Case 3: Change detected (with two-phase confirmation) ---

    def _handle_change(self, task: Task, result: FetchResult,
                       extracted: str, new_hash: str) -> None:
        """Two-phase confirmation with strategy consistency checks."""
        keywords = list(task.keywords or [])
        has_keywords = any(kw and kw.strip() for kw in keywords)
        strategy = result.strategy_used or ""

        pending_path = self._pending_path(task.id)
        pending_hash, pending_strategy = self._read_pending(pending_path)

        # Phase 1: First detection -> write pending, don't push
        if pending_hash is None:
            self._write_pending(pending_path, new_hash, strategy)
            logger.info("#{} [{}] change detected (strategy={}), awaiting confirmation",
                        task.id, task.name, strategy)
            self._touch_task(task.id)
            return

        # Strategy consistency: if strategy changed, reset pending
        if pending_strategy and strategy != pending_strategy:
            self._write_pending(pending_path, new_hash, strategy)
            logger.info("#{} [{}] strategy drift ({}->{}), pending reset",
                        task.id, task.name, pending_strategy, strategy)
            self._touch_task(task.id)
            return

        # Restored to baseline -> flicker
        if new_hash == task.last_content_hash:
            self._clear_pending(pending_path)
            logger.debug("#{} [{}] content restored (flicker), pending cleared",
                         task.id, task.name)
            self._touch_task(task.id)
            return

        # Third hash -> update pending, keep waiting
        if new_hash != pending_hash:
            self._write_pending(pending_path, new_hash, strategy)
            logger.info("#{} [{}] content changed again, pending updated", task.id, task.name)
            self._touch_task(task.id)
            return

        # Phase 2: Confirmed (hash == pending_hash, same strategy)
        self._clear_pending(pending_path)
        logger.info("#{} [{}] change confirmed (strategy={})", task.id, task.name, strategy)

        # Strategy drift protection: baseline from different strategy -> silent rebase
        baseline_strategy = getattr(task, "last_strategy_used", None) or ""
        if baseline_strategy and strategy != baseline_strategy:
            logger.info("#{} [{}] strategy switched ({}->{}), silent rebase",
                        task.id, task.name, baseline_strategy, strategy)
            snap_path = self._save_snapshot(task.id, extracted)
            self._update_baseline(task.id, new_hash, snap_path, strategy)
            return

        # Compute diff against baseline
        before = ""
        if task.last_snapshot_path and Path(task.last_snapshot_path).exists():
            try:
                before = Path(task.last_snapshot_path).read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                logger.warning("failed to read baseline snapshot: {}", e)

        full_diff = compute_diff(before, extracted, is_json=(task.type == "json"))
        diff = filter_by_keywords(full_diff, keywords) if has_keywords else full_diff

        # Keyword mode: filtered diff empty -> silent baseline advance
        if has_keywords and not diff.changed:
            logger.info("#{} [{}] change doesn't match keywords, silent advance",
                        task.id, task.name)
            snap_path = self._save_snapshot(task.id, extracted)
            self._update_baseline(task.id, new_hash, snap_path, strategy)
            return

        # Persist change
        snap_path = self._save_snapshot(task.id, extracted)
        diff_path = self._save_diff(task.id, diff.unified_diff)

        with session_scope() as s:
            t = s.get(Task, task.id)
            if t is None:
                return
            matched = self.risk.matched_keywords(diff, keywords)
            s.add(ChangeHistory(
                task_id=task.id, added_lines=len(diff.added_lines),
                removed_lines=len(diff.removed_lines),
                change_ratio=diff.change_ratio,
                matched_keywords=matched, diff_path=str(diff_path)))

            t.last_content_hash = new_hash
            t.last_snapshot_path = str(snap_path)
            t.last_strategy_used = strategy
            t.last_checked_at = datetime.utcnow()
            t.last_changed_at = datetime.utcnow()
            t.total_checks += 1
            t.total_changes += 1
            t.consecutive_failures = 0
            task_name, task_url = t.name, t.url

        # Push decision
        should_push, reason = self.risk.should_push_change(task.id, diff, keywords)
        if not should_push:
            logger.info("#{} change suppressed: {}", task.id, reason)
            return

        logger.info("#{} [{}] pushing change: +{} -{} ratio={:.2%}",
                     task.id, task_name, len(diff.added_lines),
                     len(diff.removed_lines), diff.change_ratio)

        chat_id = self.cfg.feishu.target_chat_id
        if not chat_id:
            return

        card = cards.change_card(
            task_id=task.id, task_name=task_name, url=task_url,
            added_count=len(diff.added_lines), removed_count=len(diff.removed_lines),
            change_ratio=diff.change_ratio, diff_summary=diff.summary,
            strategy=result.strategy_used, matched_keywords=matched,
            has_diff_file=bool(diff.unified_diff), keyword_filtered=has_keywords)

        file_path = ensure_upload_size(diff_path) if diff_path and diff_path.exists() else None
        card_msg_id, _ = self.feishu.send_card_and_file(
            chat_id, card, file_path,
            f"[{task_name}] diff.txt" if file_path else "")
        self.risk.mark_pushed(task.id, kind="change", message_id=card_msg_id)

    # --- Failure handling ---

    def _handle_failure(self, task: Task, error: str) -> None:
        with session_scope() as s:
            t = s.get(Task, task.id)
            if t is None:
                return
            t.consecutive_failures += 1
            t.last_checked_at = datetime.utcnow()
            t.total_checks += 1
            fails, name, url = t.consecutive_failures, t.name, t.url
            tripped = fails >= CIRCUIT_BREAKER_THRESHOLD
            if tripped:
                t.enabled = False

        chat_id = self.cfg.feishu.target_chat_id
        if not chat_id:
            return

        if tripped:
            logger.warning("#{} [{}] 熔断触发（连续 {} 次失败）", task.id, name, fails)
            self.feishu.send_card(chat_id, cards.error_card(
                f"🔌 任务 #{task.id} 已熔断",
                f"任务 **{name}** 连续失败 **{fails}** 次，已自动禁用。\n"
                f"最后错误：{error[:200]}",
                f"`/resume {task.id}` 恢复任务 · `/debug {task.id}` 诊断问题"))
            self.risk.mark_pushed(task.id, kind="error")
        elif (self.risk.should_alert_failure(fails)
              and self.risk.can_send_failure_alert(task.id)):
            self.feishu.send_card(chat_id, cards.fetch_failure_card(task.id, name, url, fails, error))
            self.risk.mark_pushed(task.id, kind="error")


__all__ = ["MonitorRunner"]
