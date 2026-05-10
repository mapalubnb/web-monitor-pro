"""
飞书命令解析与分发

支持两种触发方式：
1. 用户文本消息（@机器人 + /command 或 私聊 /command）
2. 卡片按钮点击（action 回调）

分发器 CommandDispatcher：
- dispatch_text(text, user_id, chat_id, message_id)
- dispatch_action(action_value, user_id, chat_id, message_id)

每个命令返回一个 CommandResponse（卡片 + 可选附件文件）。
"""

from __future__ import annotations

import argparse
import json
import platform
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


# ============================================================
# 命令响应
# ============================================================
@dataclass
class CommandResponse:
    """命令执行后的响应。"""

    card: dict[str, Any] | None = None         # 要回复的卡片
    text: str | None = None                    # 纯文本回复（与 card 二选一）
    file_path: Path | None = None              # 额外发送的文件
    file_display_name: str = ""                # 文件展示名
    extra_cards: list[dict[str, Any]] = field(default_factory=list)  # 额外要发的卡片
    trigger_check_task_id: int | None = None   # 请求立即触发某任务的检查

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
    """命令解析与分发。"""

    def __init__(self, cfg: AppConfig, risk: RiskController, service_start_ts: float):
        self.cfg = cfg
        self.risk = risk
        self.service_start_ts = service_start_ts

        # 文本命令 → handler 方法
        self._text_handlers: dict[str, Callable[..., CommandResponse]] = {
            "help": self._cmd_help,
            "add": self._cmd_add,
            "list": self._cmd_list,
            "pause": self._cmd_pause,
            "resume": self._cmd_resume,
            "remove": self._cmd_remove,
            "delete": self._cmd_remove,
            "check": self._cmd_check,
            "history": self._cmd_history,
            "keyword": self._cmd_keyword,
            "config": self._cmd_config,
            "log": self._cmd_log,
            "logs": self._cmd_log,
            "status": self._cmd_status,
            "mute": self._cmd_mute,
            "unmute": self._cmd_unmute,
            "sniff": self._cmd_sniff,
        }

        # 卡片按钮 action → handler 方法
        self._action_handlers: dict[str, Callable[..., CommandResponse]] = {
            "pause": self._action_pause,
            "resume": self._action_resume,
            "remove": self._action_remove,
            "check": self._action_check,
            "history": self._action_history,
            "task_detail": self._action_task_detail,
            "open_url": self._action_noop,
        }

    # ========================================================
    # 入口：文本
    # ========================================================
    def dispatch_text(self, text: str, user_id: str, chat_id: str) -> CommandResponse | None:
        """
        处理文本消息。返回 None 表示不是命令（忽略）。
        """
        text = _strip_mention(text).strip()
        if not text or not text.startswith("/"):
            return None

        # shlex 拆分支持带引号参数
        try:
            tokens = shlex.split(text)
        except ValueError as e:
            return CommandResponse.err(f"命令解析失败：{e}")

        command = tokens[0].lstrip("/").lower()
        args = tokens[1:]

        if command not in self._text_handlers:
            return CommandResponse.err(
                f"未知命令：`/{command}`",
                "发送 `/help` 查看所有命令",
            )

        if not self._is_authorized(user_id):
            return CommandResponse.err(
                "你没有权限执行此命令",
                "管理员可通过配置 FEISHU_ADMIN_OPEN_IDS 添加你",
            )

        logger.info("👤 用户 {} 执行命令 /{} 参数={}", user_id[:10] + "...", command, args)
        try:
            return self._text_handlers[command](args)
        except SystemExit:
            # argparse 解析失败会调 SystemExit
            return CommandResponse.err(f"命令 `/{command}` 参数错误", "发送 `/help` 查看用法")
        except Exception as e:
            logger.exception("命令执行异常：{}", e)
            return CommandResponse.err(f"命令执行异常：{e}")

    # ========================================================
    # 入口：按钮
    # ========================================================
    def dispatch_action(self, action_value: dict[str, Any], user_id: str) -> CommandResponse | None:
        action = action_value.get("action")
        if not action:
            return None
        handler = self._action_handlers.get(action)
        if handler is None:
            return CommandResponse.err(f"未知按钮动作：{action}")
        if not self._is_authorized(user_id):
            return CommandResponse.err("你没有权限")
        logger.info("🖱️  用户 {} 点击按钮 action={} value={}",
                    user_id[:10] + "...", action, action_value)
        try:
            return handler(action_value)
        except Exception as e:
            logger.exception("按钮处理异常：{}", e)
            return CommandResponse.err(f"操作失败：{e}")

    # ========================================================
    # 权限
    # ========================================================
    def _is_authorized(self, user_id: str) -> bool:
        admins = self.cfg.feishu.admin_open_ids
        if not admins:
            return True  # 未配置白名单则默认允许（也有 /help 等无害命令）
        return user_id in admins

    # ========================================================
    # 文本命令实现
    # ========================================================
    def _cmd_help(self, args: list[str]) -> CommandResponse:
        return CommandResponse(card=cards.help_card())

    # ---- /add ----
    def _cmd_add(self, args: list[str]) -> CommandResponse:
        parser = _make_parser("add")
        parser.add_argument("url", help="要监控的 URL")
        parser.add_argument("--name", default="", help="任务名称（默认取域名+路径）")
        parser.add_argument("--interval", type=int, default=self.cfg.default_check_interval,
                          help="检查间隔（秒）")
        parser.add_argument("--type", default="html", choices=["html", "json"], help="内容类型")
        parser.add_argument("--strategy", default="auto",
                          choices=["auto", "httpx", "curl_cffi", "jina", "firecrawl"],
                          help="抓取策略")
        parser.add_argument("--impersonate", default="chrome131", help="curl_cffi 模拟浏览器")
        parser.add_argument("--selector", default=None, help="CSS 选择器（仅 html 类型）")
        parser.add_argument("--json-path", dest="json_path", default=None,
                          help="JSON 路径，如 data[*].name（仅 json 类型）")
        parser.add_argument("--extract-next-data", dest="extract_next_data",
                          action="store_true", help="提取 Next.js __NEXT_DATA__")
        parser.add_argument("--keyword", action="append", default=[],
                          help="关键字（命中才推送，可多个）")
        ns = parser.parse_args(args)

        url = ns.url.strip()
        if not url.startswith(("http://", "https://")):
            return CommandResponse.err("URL 必须以 http:// 或 https:// 开头")

        name = ns.name or _url_to_name(url)

        with session_scope() as s:
            # 去重：同 URL 已存在则拒绝
            exists = s.execute(select(Task).where(Task.url == url)).scalar_one_or_none()
            if exists:
                return CommandResponse.err(
                    f"URL 已存在：任务 #{exists.id} [{exists.name}]",
                    f"用 `/check {exists.id}` 立即检查，或 `/remove {exists.id}` 后重建",
                )
            task = Task(
                name=name,
                url=url,
                type=ns.type,
                strategy=ns.strategy,
                impersonate=ns.impersonate,
                selector=ns.selector,
                json_path=ns.json_path,
                extract_next_data=ns.extract_next_data,
                interval=ns.interval,
                keywords=ns.keyword or [],
                enabled=True,
            )
            s.add(task)
            s.flush()
            task_id = task.id

        logger.info("➕ 新增监控任务 #{} [{}] url={}", task_id, name, url)
        return CommandResponse(
            card=cards.success_card(
                "任务已添加",
                f"**#{task_id} · {name}**\n"
                f"🔗 {url}\n"
                f"⏱️ 间隔 {ns.interval}s · 🎯 策略 {ns.strategy}\n\n"
                f"首次抓取完成后会自动建立基准快照并推送确认卡片。",
            ),
            trigger_check_task_id=task_id,
        )

    # ---- /list ----
    def _cmd_list(self, args: list[str]) -> CommandResponse:
        with session_scope() as s:
            rows = s.execute(select(Task).order_by(Task.id)).scalars().all()
            tasks = [self._task_to_dict(t) for t in rows]
        return CommandResponse(card=cards.task_list_card(tasks))

    # ---- /pause ----
    def _cmd_pause(self, args: list[str]) -> CommandResponse:
        task_id = _parse_task_id(args)
        if task_id is None:
            return CommandResponse.err("用法：`/pause <任务ID>`")
        return self._set_enabled(task_id, False)

    # ---- /resume ----
    def _cmd_resume(self, args: list[str]) -> CommandResponse:
        task_id = _parse_task_id(args)
        if task_id is None:
            return CommandResponse.err("用法：`/resume <任务ID>`")
        return self._set_enabled(task_id, True)

    def _set_enabled(self, task_id: int, enabled: bool) -> CommandResponse:
        with session_scope() as s:
            t = s.get(Task, task_id)
            if t is None:
                return CommandResponse.err(f"未找到任务 #{task_id}")
            t.enabled = enabled
            name = t.name
        verb = "▶️ 已恢复" if enabled else "⏸️ 已暂停"
        logger.info("{} 任务 #{} [{}]", verb, task_id, name)
        return CommandResponse.ok(f"{verb} #{task_id} · {name}")

    # ---- /remove ----
    def _cmd_remove(self, args: list[str]) -> CommandResponse:
        task_id = _parse_task_id(args)
        if task_id is None:
            return CommandResponse.err("用法：`/remove <任务ID>`")
        with session_scope() as s:
            t = s.get(Task, task_id)
            if t is None:
                return CommandResponse.err(f"未找到任务 #{task_id}")
            name = t.name
            s.delete(t)
        logger.info("🗑️  已删除任务 #{} [{}]", task_id, name)
        return CommandResponse.ok(f"🗑️ 已删除任务 #{task_id} · {name}")

    # ---- /check ----
    def _cmd_check(self, args: list[str]) -> CommandResponse:
        task_id = _parse_task_id(args)
        if task_id is None:
            return CommandResponse.err("用法：`/check <任务ID>`")
        with session_scope() as s:
            t = s.get(Task, task_id)
            if t is None:
                return CommandResponse.err(f"未找到任务 #{task_id}")
            name = t.name
        return CommandResponse(
            card=cards.success_card(
                f"⚡ 已触发立即检查",
                f"任务 #{task_id} · {name}\n抓取结果将在片刻后推送（如有变化）。",
            ),
            trigger_check_task_id=task_id,
        )

    # ---- /history ----
    def _cmd_history(self, args: list[str]) -> CommandResponse:
        task_id = _parse_task_id(args)
        if task_id is None:
            return CommandResponse.err("用法：`/history <任务ID>`")
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

    # ---- /keyword ----
    def _cmd_keyword(self, args: list[str]) -> CommandResponse:
        if len(args) < 3:
            return CommandResponse.err(
                "用法：`/keyword <任务ID> add <关键字>` 或 `/keyword <任务ID> remove <关键字>`"
            )
        try:
            task_id = int(args[0])
        except ValueError:
            return CommandResponse.err("任务 ID 必须为整数")
        op = args[1].lower()
        keyword = " ".join(args[2:]).strip()
        if op not in ("add", "remove"):
            return CommandResponse.err("操作必须为 add 或 remove")

        with session_scope() as s:
            t = s.get(Task, task_id)
            if t is None:
                return CommandResponse.err(f"未找到任务 #{task_id}")
            keywords = list(t.keywords or [])
            if op == "add":
                if keyword in keywords:
                    return CommandResponse.err(f"关键字已存在：`{keyword}`")
                keywords.append(keyword)
                verb = "➕ 已添加"
            else:
                if keyword not in keywords:
                    return CommandResponse.err(f"关键字不存在：`{keyword}`")
                keywords.remove(keyword)
                verb = "➖ 已移除"
            t.keywords = keywords
            name = t.name

        logger.info("🎯 任务 #{} [{}] {} 关键字 {}", task_id, name, verb, keyword)
        return CommandResponse.ok(
            f"{verb}关键字",
            f"任务 #{task_id} · {name}\n关键字：`{keyword}`",
        )

    # ---- /config ----
    def _cmd_config(self, args: list[str]) -> CommandResponse:
        proxy_info = self.cfg.https_proxy or self.cfg.http_proxy or "未启用"
        apis = []
        if self.cfg.jina_reader_api_key:
            apis.append("Jina Reader")
        if self.cfg.firecrawl_api_key:
            apis.append("Firecrawl")
        cfg_summary = {
            "default_check_interval": self.cfg.default_check_interval,
            "max_concurrent_fetch": self.cfg.max_concurrent_fetch,
            "domain_min_interval": self.cfg.risk_control.domain_min_interval,
            "request_timeout": self.cfg.request_timeout,
            "jitter_ratio": self.cfg.risk_control.jitter_ratio,
            "min_change_ratio": self.cfg.risk_control.min_change_ratio,
            "push_cooldown_seconds": self.cfg.risk_control.push_cooldown_seconds,
            "alert_after_consecutive_failures": self.cfg.risk_control.alert_after_consecutive_failures,
            "proxy_info": proxy_info,
            "external_apis": "、".join(apis) if apis else "未启用",
        }
        return CommandResponse(card=cards.config_card(cfg_summary))

    # ---- /log ----
    def _cmd_log(self, args: list[str]) -> CommandResponse:
        parser = _make_parser("log")
        parser.add_argument("--tail", type=int, default=100, help="显示最近 N 行")
        ns = parser.parse_args(args)
        tail_n = max(10, min(ns.tail, 2000))

        text = tail_log(tail_n)
        log_path = get_today_log_path()
        size_str = "未生成"
        date_str = datetime.now().strftime("%Y-%m-%d")
        if log_path.exists():
            size = log_path.stat().st_size
            size_str = _humanize_size(size)

        return CommandResponse(
            card=cards.log_card(text, size_str, date_str),
            file_path=log_path if log_path.exists() else None,
            file_display_name=log_path.name if log_path.exists() else "",
        )

    # ---- /status ----
    def _cmd_status(self, args: list[str]) -> CommandResponse:
        uptime_sec = int(time.time() - self.service_start_ts)
        uptime = _humanize_duration(uptime_sec)

        with session_scope() as s:
            total_tasks = s.execute(select(func.count()).select_from(Task)).scalar_one()
            active_tasks = s.execute(
                select(func.count()).select_from(Task).where(Task.enabled.is_(True))
            ).scalar_one()
            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            pushes_today = s.execute(
                select(func.count()).select_from(PushLog).where(PushLog.created_at >= today_start)
            ).scalar_one()
            checks_today = s.execute(
                select(func.sum(Task.total_checks)).select_from(Task)
            ).scalar_one() or 0
            errors_today = s.execute(
                select(func.sum(Task.consecutive_failures)).select_from(Task)
            ).scalar_one() or 0

        mute_until = self.risk.mute_status()
        mute_status = (
            f"⏰ 至 {mute_until.strftime('%H:%M:%S')}"
            if mute_until else "否"
        )
        mem_mb = _get_memory_mb()

        data = {
            "uptime": uptime,
            "total_tasks": total_tasks,
            "active_tasks": active_tasks,
            "pushes_today": pushes_today,
            "checks_today": int(checks_today),
            "errors_today": int(errors_today),
            "mute_status": mute_status,
            "hostname": socket.gethostname(),
            "python_version": platform.python_version(),
            "version": "0.1.0",
            "memory": f"{mem_mb:.1f} MB" if mem_mb else "-",
        }
        return CommandResponse(card=cards.status_card(data))

    # ---- /mute, /unmute ----
    def _cmd_mute(self, args: list[str]) -> CommandResponse:
        if not args:
            return CommandResponse.err("用法：`/mute 30m`（支持 s/m/h/d）")
        try:
            until = self.risk.mute_for(args[0])
        except ValueError as e:
            return CommandResponse.err(str(e))
        return CommandResponse.ok(
            "🔇 已开启免打扰",
            f"到期时间：**{until.strftime('%Y-%m-%d %H:%M:%S')}**\n"
            f"期间检测到的变更不会推送，使用 `/unmute` 可提前恢复。",
        )

    def _cmd_unmute(self, args: list[str]) -> CommandResponse:
        self.risk.push.unmute()
        return CommandResponse.ok("🔔 已关闭免打扰", "变更推送已恢复")

    # ---- /sniff ----
    def _cmd_sniff(self, args: list[str]) -> CommandResponse:
        if not args:
            return CommandResponse.err("用法：`/sniff <URL>`")
        url = args[0].strip()
        return CommandResponse(card=cards.sniff_helper_card(url))

    # ========================================================
    # 按钮动作
    # ========================================================
    def _action_pause(self, v: dict[str, Any]) -> CommandResponse:
        return self._set_enabled(int(v["task_id"]), False)

    def _action_resume(self, v: dict[str, Any]) -> CommandResponse:
        return self._set_enabled(int(v["task_id"]), True)

    def _action_remove(self, v: dict[str, Any]) -> CommandResponse:
        return self._cmd_remove([str(v["task_id"])])

    def _action_check(self, v: dict[str, Any]) -> CommandResponse:
        return self._cmd_check([str(v["task_id"])])

    def _action_history(self, v: dict[str, Any]) -> CommandResponse:
        return self._cmd_history([str(v["task_id"])])

    def _action_task_detail(self, v: dict[str, Any]) -> CommandResponse:
        task_id = int(v["task_id"])
        with session_scope() as s:
            t = s.get(Task, task_id)
            if t is None:
                return CommandResponse.err(f"未找到任务 #{task_id}")
            return CommandResponse(card=cards.task_detail_card(self._task_to_dict(t)))

    def _action_noop(self, v: dict[str, Any]) -> CommandResponse | None:
        return None  # 如"打开页面"由浏览器处理，不需要后端响应

    # ========================================================
    # 工具
    # ========================================================
    @staticmethod
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
        }


# ============================================================
# 辅助工具函数
# ============================================================
def _strip_mention(text: str) -> str:
    """移除飞书 @用户 的 markdown 前缀。"""
    # 飞书 @ 格式通常是 "@_user_1 " 或 "<at user_id=xxx>名字</at>"
    import re
    text = re.sub(r"^@_user_\d+\s*", "", text)
    text = re.sub(r"<at[^>]*>.*?</at>\s*", "", text)
    return text


def _parse_task_id(args: list[str]) -> int | None:
    if not args:
        return None
    try:
        return int(args[0])
    except ValueError:
        return None


class _ParseError(Exception):
    pass


def _make_parser(prog: str) -> argparse.ArgumentParser:
    """构造一个不会自动 exit 的 ArgumentParser。"""
    parser = argparse.ArgumentParser(prog=f"/{prog}", add_help=False)

    def _err(message):
        raise SystemExit(f"argparse error: {message}")
    parser.error = _err  # type: ignore[assignment]
    return parser


def _url_to_name(url: str) -> str:
    from urllib.parse import urlparse
    p = urlparse(url)
    short = p.netloc + (p.path if len(p.path) > 1 else "")
    return short[:60]


def _humanize_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    if seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h} 小时 {m} 分"
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    return f"{d} 天 {h} 小时"


def _humanize_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _get_memory_mb() -> float | None:
    """读取当前进程内存占用（MB），失败返回 None。"""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = int(line.split()[1])
                    return kb / 1024.0
    except Exception:
        return None
    return None


__all__ = ["CommandDispatcher", "CommandResponse"]
