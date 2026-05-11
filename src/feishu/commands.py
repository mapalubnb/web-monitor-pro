"""
飞书命令解析与分发

用法：
- dispatch_text(text, user_id, chat_id)   处理文本消息
- dispatch_action(value, user_id)          处理卡片按钮点击

每个命令返回 CommandResponse（可带卡片 + 文件附件）。
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import shlex
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import func, select

from ..config import AppConfig
from ..db import ChangeHistory, PushLog, Task, session_scope
from ..logger import get_today_log_path, logger, tail_log
from ..risk_control import RiskController
from . import cards

__version__ = "0.2.0"


# ============================================================
# 响应结构
# ============================================================
@dataclass
class CommandResponse:
    card: dict[str, Any] | None = None
    text: str | None = None
    file_path: Path | None = None
    file_display_name: str = ""
    extra_cards: list[dict[str, Any]] = field(default_factory=list)
    trigger_check_task_id: int | None = None
    sync_scheduler_task_ids: list[int] = field(default_factory=list)

    @classmethod
    def err(cls, reason: str, suggestion: str = "") -> "CommandResponse":
        return cls(card=cards.error_card("操作失败", reason, suggestion))

    @classmethod
    def ok(cls, title: str, detail: str = "") -> "CommandResponse":
        return cls(card=cards.success_card(title, detail))


# ============================================================
# 命令分发器
# ============================================================
class CommandDispatcher:

    def __init__(self, cfg: AppConfig, risk: RiskController,
                 service_start_ts: float, engine: Any = None):
        self.cfg = cfg
        self.risk = risk
        self.service_start_ts = service_start_ts
        self.engine = engine

        self._text_handlers: dict[str, Callable[[list[str]], CommandResponse]] = {
            "help": self._cmd_help,
            "add": self._cmd_add,
            "list": self._cmd_list,
            "pause": lambda a: self._set_enabled(_first_int(a), False),
            "resume": lambda a: self._set_enabled(_first_int(a), True),
            "remove": self._cmd_remove,
            "delete": self._cmd_remove,
            "check": self._cmd_check,
            "history": self._cmd_history,
            "snapshot": self._cmd_snapshot,
            "keyword": self._cmd_keyword,
            "config": self._cmd_config,
            "log": self._cmd_log,
            "logs": self._cmd_log,
            "status": self._cmd_status,
            "mute": self._cmd_mute,
            "unmute": lambda _: (self.risk.unmute(),
                                 CommandResponse.ok("🔔 已关闭免打扰"))[1],
            "sniff": self._cmd_sniff,
            "debug": self._cmd_debug,
            "interval": self._cmd_interval,
            "reset": self._cmd_reset,
        }

        self._action_handlers: dict[str, Callable[[dict], CommandResponse]] = {
            "pause": lambda v: self._set_enabled(int(v["task_id"]), False),
            "resume": lambda v: self._set_enabled(int(v["task_id"]), True),
            "remove": lambda v: self._cmd_remove([str(v["task_id"])]),
            "check": lambda v: self._cmd_check([str(v["task_id"])]),
            "history": lambda v: self._cmd_history([str(v["task_id"])]),
            "snapshot": lambda v: self._cmd_snapshot([str(v["task_id"])]),
            "task_detail": self._action_task_detail,
            "open_url": lambda _: None,  # 浏览器自己处理
        }

    # ============================================================
    # 入口
    # ============================================================
    def dispatch_text(self, text: str, user_id: str, chat_id: str
                      ) -> CommandResponse | None:
        text = _strip_mention(text).strip()
        if not text or not text.startswith("/"):
            return None

        try:
            tokens = shlex.split(text)
        except ValueError as e:
            return CommandResponse.err(f"命令解析失败：{e}")

        command = tokens[0].lstrip("/").lower()
        args = tokens[1:]

        handler = self._text_handlers.get(command)
        if handler is None:
            return CommandResponse.err(
                f"未知命令：`/{command}`", "发送 `/help` 查看所有命令"
            )

        if not self._is_authorized(user_id):
            return CommandResponse.err(
                "你没有权限执行此命令",
                "管理员可通过配置 FEISHU_ADMIN_OPEN_IDS 添加你",
            )

        logger.info("👤 /{} 用户={} 参数={}", command, user_id[:10] + "...", args)
        try:
            return handler(args)
        except SystemExit:
            return CommandResponse.err(
                f"命令 `/{command}` 参数错误", "发送 `/help` 查看用法"
            )
        except Exception as e:
            logger.exception("命令异常：{}", e)
            return CommandResponse.err(f"命令执行异常：{e}")

    def dispatch_action(self, value: dict[str, Any], user_id: str
                        ) -> CommandResponse | None:
        action = value.get("action")
        if not action:
            return None
        handler = self._action_handlers.get(action)
        if handler is None:
            return CommandResponse.err(f"未知按钮动作：{action}")
        if not self._is_authorized(user_id):
            return CommandResponse.err("你没有权限")
        logger.info("🖱️  按钮 {} value={}", action, value)
        try:
            return handler(value)
        except Exception as e:
            logger.exception("按钮异常：{}", e)
            return CommandResponse.err(f"操作失败：{e}")

    def _is_authorized(self, user_id: str) -> bool:
        admins = self.cfg.feishu.admin_open_ids
        return not admins or user_id in admins

    # ============================================================
    # Task 查找通用 helper（消除 12 处重复）
    # ============================================================
    @staticmethod
    def _get_task(task_id: int | None, usage: str = ""
                  ) -> tuple[Task | None, CommandResponse | None]:
        """
        获取任务，返回 (task 或 None, 错误响应 或 None)。
        任务在 session 外返回前已 expunge。
        """
        if task_id is None:
            return None, CommandResponse.err(usage or "需要任务 ID")
        with session_scope() as s:
            t = s.get(Task, task_id)
            if t is None:
                return None, CommandResponse.err(f"未找到任务 #{task_id}")
            s.expunge(t)
            return t, None

    # ============================================================
    # /help
    # ============================================================
    def _cmd_help(self, args: list[str]) -> CommandResponse:
        return CommandResponse(card=cards.help_card())

    # ============================================================
    # /add
    # ============================================================
    def _cmd_add(self, args: list[str]) -> CommandResponse:
        parser = _make_parser("add")
        parser.add_argument("url")
        parser.add_argument("--name", default="")
        parser.add_argument("--interval", type=int,
                            default=self.cfg.default_check_interval)
        parser.add_argument("--type", default="html", choices=["html", "json"])
        parser.add_argument("--strategy", default="auto",
                            choices=["auto", "httpx", "curl_cffi", "playwright"])
        parser.add_argument("--impersonate", default="chrome131")
        parser.add_argument("--selector", default=None)
        parser.add_argument("--json-path", dest="json_path", default=None)
        parser.add_argument("--extract-next-data", dest="extract_next_data",
                            action="store_true")
        # --keyword 可重复；每个 --keyword 的值再支持逗号/中文逗号分隔。
        parser.add_argument("--keyword", action="append", default=[])
        ns = parser.parse_args(args)

        url = ns.url.strip()
        if not url.startswith(("http://", "https://")):
            return CommandResponse.err("URL 必须以 http:// 或 https:// 开头")

        name = ns.name or _url_to_name(url)

        # 解析关键字：支持 --keyword a --keyword b,c --keyword "d e"
        # 英文/中文逗号、中文顿号都算分隔符
        keywords = _parse_keywords(ns.keyword)

        with session_scope() as s:
            exists = s.execute(
                select(Task).where(Task.url == url)
            ).scalar_one_or_none()
            if exists:
                return CommandResponse.err(
                    f"URL 已存在：任务 #{exists.id} [{exists.name}]",
                    f"用 `/reset {exists.id}` 重置，或 `/remove {exists.id}` 后重建",
                )
            t = Task(
                name=name, url=url, type=ns.type, strategy=ns.strategy,
                impersonate=ns.impersonate, selector=ns.selector,
                json_path=ns.json_path, extract_next_data=ns.extract_next_data,
                interval=ns.interval, keywords=keywords, enabled=True,
            )
            s.add(t)
            s.flush()
            task_id = t.id

        logger.info(
            "➕ 新增任务 #{} [{}] url={} 关键字={}",
            task_id, name, url, keywords,
        )
        kw_line = (
            f"\n🎯 关键字：{', '.join(f'`{k}`' for k in keywords)}"
            if keywords else ""
        )
        return CommandResponse(
            card=cards.success_card(
                "任务已添加",
                f"**#{task_id} · {name}**\n"
                f"🔗 {url}\n"
                f"⏱️ 间隔 {ns.interval}s · 🎯 策略 {ns.strategy}"
                f"{kw_line}\n\n"
                f"首次抓取后会建立基准快照并推送卡片。",
            ),
            trigger_check_task_id=task_id,
        )

    # ============================================================
    # /list
    # ============================================================
    def _cmd_list(self, args: list[str]) -> CommandResponse:
        with session_scope() as s:
            rows = s.execute(select(Task).order_by(Task.id)).scalars().all()
            tasks = [_task_to_dict(t) for t in rows]
        return CommandResponse(card=cards.task_list_card(tasks))

    # ============================================================
    # enabled 切换（pause / resume）
    # ============================================================
    def _set_enabled(self, task_id: int | None, enabled: bool) -> CommandResponse:
        if task_id is None:
            return CommandResponse.err("用法：`/pause <ID>` 或 `/resume <ID>`")
        with session_scope() as s:
            t = s.get(Task, task_id)
            if t is None:
                return CommandResponse.err(f"未找到任务 #{task_id}")
            t.enabled = enabled
            name = t.name
        verb = "▶️ 已恢复" if enabled else "⏸️ 已暂停"
        logger.info("{} 任务 #{} [{}]", verb, task_id, name)
        resp = CommandResponse.ok(f"{verb} #{task_id} · {name}")
        resp.sync_scheduler_task_ids = [task_id]
        return resp

    # ============================================================
    # /remove
    # ============================================================
    def _cmd_remove(self, args: list[str]) -> CommandResponse:
        task_id = _first_int(args)
        if task_id is None:
            return CommandResponse.err("用法：`/remove <ID>`")
        with session_scope() as s:
            t = s.get(Task, task_id)
            if t is None:
                return CommandResponse.err(f"未找到任务 #{task_id}")
            name = t.name
            s.delete(t)
        logger.info("🗑️  已删除任务 #{} [{}]", task_id, name)
        resp = CommandResponse.ok(f"🗑️ 已删除任务 #{task_id} · {name}")
        resp.sync_scheduler_task_ids = [task_id]
        return resp

    # ============================================================
    # /check
    # ============================================================
    def _cmd_check(self, args: list[str]) -> CommandResponse:
        task_id = _first_int(args)
        t, err = self._get_task(task_id, "用法：`/check <ID>`")
        if err is not None:
            return err
        return CommandResponse(
            card=cards.success_card(
                "⚡ 已触发立即检查",
                f"任务 #{task_id} · {t.name}\n抓取结果稍后推送（如有变化）。",
            ),
            trigger_check_task_id=task_id,
        )

    # ============================================================
    # /history
    # ============================================================
    def _cmd_history(self, args: list[str]) -> CommandResponse:
        task_id = _first_int(args)
        if task_id is None:
            return CommandResponse.err("用法：`/history <ID>`")
        with session_scope() as s:
            t = s.get(Task, task_id)
            if t is None:
                return CommandResponse.err(f"未找到任务 #{task_id}")
            rows = s.execute(
                select(ChangeHistory)
                .where(ChangeHistory.task_id == task_id)
                .order_by(ChangeHistory.created_at.desc())
                .limit(10)
            ).scalars().all()
            items = [{
                "created_at": c.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "added_lines": c.added_lines,
                "removed_lines": c.removed_lines,
                "change_ratio": c.change_ratio,
                "matched_keywords": c.matched_keywords or [],
            } for c in rows]
            name = t.name
        return CommandResponse(card=cards.history_card(name, task_id, items))

    # ============================================================
    # /snapshot —— 下载最新快照
    # ============================================================
    def _cmd_snapshot(self, args: list[str]) -> CommandResponse:
        task_id = _first_int(args)
        t, err = self._get_task(task_id, "用法：`/snapshot <ID>`")
        if err is not None:
            return err

        snap_path_str = t.last_snapshot_path
        if not snap_path_str or not Path(snap_path_str).exists():
            return CommandResponse.err(
                f"任务 #{task_id} 暂无快照",
                f"用 `/check {task_id}` 立即抓取一次以建立基准快照",
            )

        snap_path = Path(snap_path_str)
        content_len = snap_path.stat().st_size
        last_checked = (
            t.last_checked_at.strftime("%Y-%m-%d %H:%M:%S")
            if t.last_checked_at else "从未"
        )
        return CommandResponse(
            card=cards.success_card(
                "📥 快照已发送",
                f"**#{task_id} · {t.name}**\n"
                f"🔗 {t.url}\n"
                f"🕐 抓取时间：`{last_checked}`\n"
                f"📝 文件大小：`{_humanize_size(content_len)}`",
            ),
            file_path=snap_path,
            file_display_name=f"[{t.name}] 最新快照.txt",
        )

    # ============================================================
    # /keyword
    # ============================================================
    def _cmd_keyword(self, args: list[str]) -> CommandResponse:
        """
        /keyword <ID> add <关键字>[, <关键字2>, ...]
        /keyword <ID> remove <关键字>[, <关键字2>, ...]
        /keyword <ID> list              查看当前关键字
        /keyword <ID> clear             清空所有关键字
        """
        if len(args) < 2:
            return CommandResponse.err(
                "用法：\n"
                "• `/keyword <ID> add <关键字1> [关键字2] ...`\n"
                "• `/keyword <ID> remove <关键字1> [关键字2] ...`\n"
                "• `/keyword <ID> list` / `clear`"
            )
        try:
            task_id = int(args[0])
        except ValueError:
            return CommandResponse.err("任务 ID 必须为整数")
        op = args[1].lower()

        with session_scope() as s:
            t = s.get(Task, task_id)
            if t is None:
                return CommandResponse.err(f"未找到任务 #{task_id}")
            name = t.name
            current = list(t.keywords or [])

            if op == "list":
                text = (
                    "\n".join(f"• `{k}`" for k in current)
                    if current else "（未配置关键字）"
                )
                return CommandResponse(card=cards.success_card(
                    f"🎯 任务 #{task_id} · {name} 的关键字",
                    text + f"\n\n共 {len(current)} 个。",
                ))

            if op == "clear":
                if not current:
                    return CommandResponse.err("当前就没有关键字")
                t.keywords = []
                logger.info("🎯 任务 #{} [{}] 清空了 {} 个关键字",
                            task_id, name, len(current))
                return CommandResponse.ok(
                    "🧹 已清空关键字",
                    f"任务 #{task_id} · {name}\n清除了 {len(current)} 个关键字",
                )

            if op not in ("add", "remove"):
                return CommandResponse.err(
                    "操作必须是 add / remove / list / clear"
                )

            if len(args) < 3:
                return CommandResponse.err(f"用法：`/keyword {task_id} {op} <关键字>`")

            # 解析关键字（支持空格分隔 + 逗号/顿号分隔混合）
            raw_inputs = args[2:]
            new_kws = _parse_keywords(raw_inputs)
            if not new_kws:
                return CommandResponse.err("未解析到有效关键字（不能为空或纯空白）")

            if op == "add":
                added, skipped = [], []
                for kw in new_kws:
                    if kw in current:
                        skipped.append(kw)
                    else:
                        current.append(kw)
                        added.append(kw)
                if not added:
                    return CommandResponse.err(
                        f"关键字都已存在：{', '.join(f'`{k}`' for k in skipped)}"
                    )
                t.keywords = current
                detail = (
                    f"任务 #{task_id} · {name}\n"
                    f"➕ 新增：{', '.join(f'`{k}`' for k in added)}"
                )
                if skipped:
                    detail += f"\n⏭️ 已存在跳过：{', '.join(f'`{k}`' for k in skipped)}"
                detail += f"\n\n当前共 {len(current)} 个关键字。"
                logger.info("🎯 任务 #{} [{}] ➕ 关键字 {}",
                            task_id, name, added)
                return CommandResponse.ok("🎯 已更新关键字", detail)

            # op == "remove"
            removed, missing = [], []
            for kw in new_kws:
                if kw in current:
                    current.remove(kw)
                    removed.append(kw)
                else:
                    missing.append(kw)
            if not removed:
                return CommandResponse.err(
                    f"以下关键字都不存在：{', '.join(f'`{k}`' for k in missing)}"
                )
            t.keywords = current
            detail = (
                f"任务 #{task_id} · {name}\n"
                f"➖ 移除：{', '.join(f'`{k}`' for k in removed)}"
            )
            if missing:
                detail += f"\n⏭️ 未找到跳过：{', '.join(f'`{k}`' for k in missing)}"
            detail += f"\n\n当前共 {len(current)} 个关键字。"
            logger.info("🎯 任务 #{} [{}] ➖ 关键字 {}",
                        task_id, name, removed)
            return CommandResponse.ok("🎯 已更新关键字", detail)

    # ============================================================
    # /config
    # ============================================================
    def _cmd_config(self, args: list[str]) -> CommandResponse:
        apis = []
        if self.cfg.enable_playwright:
            apis.append("Playwright 渲染")
        summary = {
            "default_check_interval": self.cfg.default_check_interval,
            "max_concurrent_fetch": self.cfg.max_concurrent_fetch,
            "domain_min_interval": self.cfg.risk_control.domain_min_interval,
            "request_timeout": self.cfg.request_timeout,
            "jitter_ratio": self.cfg.risk_control.jitter_ratio,
            "min_change_ratio": self.cfg.risk_control.min_change_ratio,
            "push_cooldown_seconds": self.cfg.risk_control.push_cooldown_seconds,
            "alert_after_consecutive_failures":
                self.cfg.risk_control.alert_after_consecutive_failures,
            "proxy_info": (self.cfg.https_proxy or self.cfg.http_proxy or "未启用"),
            "external_apis": "、".join(apis) if apis else "未启用",
        }
        return CommandResponse(card=cards.config_card(summary))

    # ============================================================
    # /log
    # ============================================================
    def _cmd_log(self, args: list[str]) -> CommandResponse:
        parser = _make_parser("log")
        parser.add_argument("--tail", type=int, default=100)
        ns = parser.parse_args(args)
        tail_n = max(10, min(ns.tail, 2000))

        text = tail_log(tail_n)
        log_path = get_today_log_path()
        size = _humanize_size(log_path.stat().st_size) if log_path.exists() else "未生成"
        date = datetime.now().strftime("%Y-%m-%d")

        return CommandResponse(
            card=cards.log_card(text, size, date),
            file_path=log_path if log_path.exists() else None,
            file_display_name=log_path.name if log_path.exists() else "",
        )

    # ============================================================
    # /status
    # ============================================================
    def _cmd_status(self, args: list[str]) -> CommandResponse:
        uptime = _humanize_duration(int(time.time() - self.service_start_ts))

        with session_scope() as s:
            total = s.execute(select(func.count()).select_from(Task)).scalar_one()
            active = s.execute(
                select(func.count()).select_from(Task).where(Task.enabled.is_(True))
            ).scalar_one()
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            pushes = s.execute(
                select(func.count()).select_from(PushLog)
                .where(PushLog.created_at >= today)
            ).scalar_one()
            checks = s.execute(
                select(func.sum(Task.total_checks))
            ).scalar_one() or 0
            errors = s.execute(
                select(func.sum(Task.consecutive_failures))
            ).scalar_one() or 0

        mute_until = self.risk.mute_status()
        mute_text = f"⏰ 至 {mute_until:%H:%M:%S}" if mute_until else "否"

        return CommandResponse(card=cards.status_card({
            "uptime": uptime,
            "total_tasks": total,
            "active_tasks": active,
            "pushes_today": pushes,
            "checks_today": int(checks),
            "errors_today": int(errors),
            "mute_status": mute_text,
            "hostname": socket.gethostname(),
            "python_version": platform.python_version(),
            "version": __version__,
            "memory": f"{_memory_mb():.1f} MB" if _memory_mb() else "-",
        }))

    # ============================================================
    # /mute
    # ============================================================
    def _cmd_mute(self, args: list[str]) -> CommandResponse:
        if not args:
            return CommandResponse.err("用法：`/mute 30m`（支持 s/m/h/d）")
        try:
            until = self.risk.mute_for(args[0])
        except ValueError as e:
            return CommandResponse.err(str(e))
        return CommandResponse.ok(
            "🔇 已开启免打扰",
            f"到期：**{until:%Y-%m-%d %H:%M:%S}**\n`/unmute` 可提前恢复",
        )

    # ============================================================
    # /sniff
    # ============================================================
    def _cmd_sniff(self, args: list[str]) -> CommandResponse:
        if not args:
            return CommandResponse.err("用法：`/sniff <URL>`")
        return CommandResponse(card=cards.sniff_helper_card(args[0].strip()))

    # ============================================================
    # /debug
    # ============================================================
    def _cmd_debug(self, args: list[str]) -> CommandResponse:
        """抓一次 + 诊断 + 给建议 + 附 HTML。"""
        task_id = _first_int(args)
        t, err = self._get_task(task_id, "用法：`/debug <ID>`")
        if err is not None:
            return err
        if self.engine is None:
            return CommandResponse.err("诊断功能未就绪（engine 未注入）")

        from ..fetcher.extractor import diagnose_html

        logger.info("🔍 诊断任务 #{} [{}]", task_id, t.name)
        try:
            result = self.engine.fetch(t)
        except Exception as e:
            return CommandResponse.err(f"抓取失败：{e}")

        if not result.ok:
            return CommandResponse.err(
                f"抓取失败 HTTP={result.status_code}",
                result.error or "",
            )

        f = diagnose_html(result.content or "")
        detail = (
            f"**📊 诊断结果**\n\n"
            f"🔖 任务：#{task_id} · {t.name}\n"
            f"🌐 URL：{t.url}\n"
            f"🎯 策略：`{result.strategy_used}`\n"
            f"📏 HTML：{_humanize_size(f['html_size'])}\n"
            f"👁️ 可见文本：{f['visible_text_length']} 字\n"
            f"🏗️ 框架：{'、'.join(f['frameworks']) or '未识别'}\n\n"
            f"**📦 数据嵌入点**\n"
            f"{chr(10).join(f['data_points']) if f['data_points'] else '❌ 未找到'}\n\n"
            f"**💡 建议**\n{chr(10).join(f'• {s}' for s in f['suggestions'])}"
        )

        # 附上 HTML 文件
        html_path: Path | None = None
        try:
            import tempfile
            fp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".html", delete=False, encoding="utf-8"
            )
            fp.write(result.content or "")
            fp.close()
            html_path = Path(fp.name)
        except Exception as e:
            logger.debug("生成 HTML 附件失败：{}", e)

        return CommandResponse(
            card=cards.success_card("🔍 抓取诊断报告", detail),
            file_path=html_path,
            file_display_name=f"task_{task_id}_debug.html" if html_path else "",
        )

    # ============================================================
    # /interval
    # ============================================================
    def _cmd_interval(self, args: list[str]) -> CommandResponse:
        if len(args) < 2:
            return CommandResponse.err("用法：`/interval <ID> <秒>`")
        try:
            task_id = int(args[0])
            seconds = int(args[1])
        except ValueError:
            return CommandResponse.err("参数必须是数字")
        if seconds < 10:
            return CommandResponse.err("间隔不能小于 10 秒")

        with session_scope() as s:
            t = s.get(Task, task_id)
            if t is None:
                return CommandResponse.err(f"未找到任务 #{task_id}")
            t.interval = seconds
            name = t.name

        logger.info("⏱️ 任务 #{} [{}] 间隔 → {}s", task_id, name, seconds)
        resp = CommandResponse.ok(
            "⏱️ 已更新间隔",
            f"任务 #{task_id} · {name}\n新间隔：**{seconds} 秒**",
        )
        resp.sync_scheduler_task_ids = [task_id]
        return resp

    # ============================================================
    # /reset
    # ============================================================
    def _cmd_reset(self, args: list[str]) -> CommandResponse:
        parser = _make_parser("reset")
        parser.add_argument("task_id", type=int)
        parser.add_argument("--strategy", default=None,
                            choices=["auto", "httpx", "curl_cffi", "playwright"])
        parser.add_argument("--impersonate", default=None)
        parser.add_argument("--selector", default=None)
        parser.add_argument("--extract-next-data", dest="extract_next_data",
                            action="store_true", default=None)
        try:
            ns = parser.parse_args(args)
        except SystemExit:
            return CommandResponse.err("用法：`/reset <ID> [--strategy playwright]`")

        changes: list[str] = []
        with session_scope() as s:
            t = s.get(Task, ns.task_id)
            if t is None:
                return CommandResponse.err(f"未找到任务 #{ns.task_id}")
            t.last_content_hash = None
            t.last_snapshot_path = None
            t.consecutive_failures = 0
            if ns.strategy:
                t.strategy = ns.strategy
                changes.append(f"策略→`{ns.strategy}`")
            if ns.impersonate:
                t.impersonate = ns.impersonate
                changes.append(f"指纹→`{ns.impersonate}`")
            if ns.selector is not None:
                t.selector = ns.selector
                changes.append(f"选择器→`{ns.selector}`")
            if ns.extract_next_data:
                t.extract_next_data = True
                changes.append("启用 SPA 提取")
            name = t.name

        logger.info("🔄 任务 #{} [{}] 已重置，{}",
                    ns.task_id, name, "; ".join(changes) or "策略不变")
        detail = f"任务 #{ns.task_id} · {name}\n基准已清空，下次抓取作为新首次建立。"
        if changes:
            detail += "\n📝 同时调整：" + "、".join(changes)
        return CommandResponse(
            card=cards.success_card("🔄 已重置", detail),
            trigger_check_task_id=ns.task_id,
        )

    # ============================================================
    # 按钮：task_detail
    # ============================================================
    def _action_task_detail(self, v: dict[str, Any]) -> CommandResponse:
        task_id = int(v["task_id"])
        with session_scope() as s:
            t = s.get(Task, task_id)
            if t is None:
                return CommandResponse.err(f"未找到任务 #{task_id}")
            return CommandResponse(card=cards.task_detail_card(_task_to_dict(t)))


# ============================================================
# 模块级辅助函数
# ============================================================
_MENTION_RE = re.compile(r"^@_user_\d+\s*|<at[^>]*>.*?</at>\s*")


def _strip_mention(text: str) -> str:
    return _MENTION_RE.sub("", text)


def _first_int(args: list[str]) -> int | None:
    if not args:
        return None
    try:
        return int(args[0])
    except ValueError:
        return None


# 关键字分隔符：英文逗号、中文逗号、中文顿号、分号（中英）
_KW_SEP_RE = re.compile(r"[,，、;；]+")


def _parse_keywords(raw_inputs: list[str]) -> list[str]:
    """
    解析多种形式的关键字输入，返回清理后的关键字列表（去空、去重、保持顺序）。

    支持：
    - ["招聘", "金融"]                   → ["招聘", "金融"]
    - ["招聘,金融,Python"]               → ["招聘", "金融", "Python"]
    - ["招聘、金融"]                     → ["招聘", "金融"]
    - ["招聘 金融"]  （已被 shlex 拆开）→ ["招聘", "金融"]（外层传进来多个元素）
    - ["  ", ""]                        → [] （全空字符串被过滤）
    """
    result: list[str] = []
    seen: set[str] = set()
    for raw in raw_inputs:
        if not raw:
            continue
        for part in _KW_SEP_RE.split(raw):
            kw = part.strip()
            if not kw:
                continue
            if kw not in seen:
                seen.add(kw)
                result.append(kw)
    return result


def _make_parser(prog: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"/{prog}", add_help=False)
    p.error = lambda msg: (_ for _ in ()).throw(SystemExit(msg))  # type: ignore
    return p


def _url_to_name(url: str) -> str:
    from urllib.parse import urlparse
    p = urlparse(url)
    return (p.netloc + (p.path if len(p.path) > 1 else ""))[:60]


def _humanize_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    if seconds < 86400:
        return f"{seconds // 3600} 小时 {(seconds % 3600) // 60} 分"
    return f"{seconds // 86400} 天 {(seconds % 86400) // 3600} 小时"


def _humanize_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _memory_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return 0.0


def _task_to_dict(t: Task) -> dict[str, Any]:
    return {
        "id": t.id,
        "name": t.name,
        "url": t.url,
        "type": t.type,
        "strategy": t.strategy,
        "impersonate": t.impersonate,
        "interval": t.interval,
        "enabled": t.enabled,
        "keywords": t.keywords or [],
        "total_checks": t.total_checks,
        "total_changes": t.total_changes,
        "consecutive_failures": t.consecutive_failures,
        "last_checked_at": (
            t.last_checked_at.strftime("%Y-%m-%d %H:%M:%S")
            if t.last_checked_at else None
        ),
        "last_changed_at": (
            t.last_changed_at.strftime("%Y-%m-%d %H:%M:%S")
            if t.last_changed_at else None
        ),
        "has_snapshot": bool(
            t.last_snapshot_path and Path(t.last_snapshot_path).exists()
        ),
    }


__all__ = ["CommandDispatcher", "CommandResponse"]
