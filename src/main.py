"""
主程序入口

1. 加载 .env + config.yaml
2. 初始化日志 + 数据库
3. 构造 Engine / Risk / Feishu / Runner / Scheduler / Dispatcher
4. 注册飞书 WebSocket 事件
5. 启动调度器 + 发送启动卡片 + 阻塞运行
"""

from __future__ import annotations

import json
import signal
import sys
import threading
import time
from typing import Any

from . import __version__
from .config import AppConfig, load_config, validate_config
from .db import Task, init_db, session_scope
from .feishu import CommandDispatcher, CommandResponse, FeishuClient, cards
from .feishu.client import ensure_upload_size
from .fetcher import FetchEngine
from .logger import logger, setup_logger
from .risk_control import RiskController
from .scheduler import MonitorScheduler
from .tasks import MonitorRunner


class App:
    """服务共享上下文。"""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.service_start_ts = time.time()

        self.risk = RiskController(
            cfg.risk_control, max_concurrent=cfg.max_concurrent_fetch
        )
        self.engine = FetchEngine(cfg)
        self.feishu = FeishuClient(cfg.feishu)
        self.runner = MonitorRunner(cfg, self.engine, self.risk, self.feishu)
        self.scheduler = MonitorScheduler(cfg, self.risk, run_fn=self.runner.run_once)
        self.dispatcher = CommandDispatcher(
            cfg, self.risk, self.service_start_ts, engine=self.engine
        )

        self._ws_client: Any = None

    # ============================================================
    # 启动
    # ============================================================
    def start(self) -> None:
        logger.info("═" * 60)
        logger.info("🚀 Web Monitor Pro v{} 正在启动", __version__)
        logger.info("═" * 60)

        self._seed_tasks_from_config()
        self.scheduler.start()
        self._send_startup_card()
        self._start_ws()  # 阻塞

    def shutdown(self) -> None:
        logger.info("🛑 正在关闭服务...")
        try:
            self.scheduler.stop()
        except Exception as e:
            logger.warning("关闭调度器异常：{}", e)
        try:
            self.engine.close()
        except Exception:
            pass
        logger.info("👋 再见")

    # ============================================================
    # 种子任务同步
    # ============================================================
    def _seed_tasks_from_config(self) -> None:
        if not self.cfg.tasks:
            return
        added = 0
        with session_scope() as s:
            for t in self.cfg.tasks:
                if s.query(Task).filter(Task.url == t.url).first():
                    continue
                s.add(Task(
                    name=t.name, url=t.url, type=t.type,
                    strategy=t.strategy, impersonate=t.impersonate,
                    selector=t.selector, json_path=t.json_path,
                    extract_next_data=t.extract_next_data,
                    interval=t.interval,
                    keywords=t.keywords or [],
                    headers=t.headers or {},
                    enabled=t.enabled,
                ))
                added += 1
        if added:
            logger.info("🌱 同步 {} 个 YAML 任务到 DB", added)

    # ============================================================
    # 启动卡片
    # ============================================================
    def _send_startup_card(self) -> None:
        with session_scope() as s:
            count = s.query(Task).filter(Task.enabled.is_(True)).count()

        chat_id = self.cfg.feishu.target_chat_id
        if not chat_id:
            logger.warning("未配置 FEISHU_TARGET_CHAT_ID，跳过启动卡片")
            return

        self.feishu.send_card(chat_id, cards.startup_card(
            task_count=count,
            default_interval=self.cfg.default_check_interval,
            version=__version__,
        ))

    # ============================================================
    # 飞书长连接
    # ============================================================
    def _start_ws(self) -> None:
        try:
            import lark_oapi as lark
        except ImportError as e:
            logger.error("lark-oapi 未安装：{}", e)
            return

        # 打印 SDK 版本，排障有用
        try:
            from lark_oapi.core.const import VERSION as _lark_version
            logger.info("📦 lark-oapi 版本: {}", _lark_version)
        except ImportError:
            logger.warning("无法确定 lark-oapi 版本（可能过老，建议升级到 >=1.4）")

        builder = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
        )

        # 卡片回调注册（lark-oapi >= 1.4 标准方法）
        try:
            builder = builder.register_p2_card_action_trigger(self._on_card_action)
            logger.info("🖱️  卡片按钮回调已注册")
        except AttributeError:
            logger.error(
                "⚠️ lark-oapi 缺少 register_p2_card_action_trigger。"
                "请升级：pip install -U lark-oapi>=1.4"
            )

        self._ws_client = lark.ws.Client(
            self.cfg.feishu.app_id,
            self.cfg.feishu.app_secret,
            event_handler=builder.build(),
            log_level=lark.LogLevel.WARNING,
        )
        logger.info("🔌 正在建立飞书 WebSocket 长连接...")
        self._ws_client.start()  # 阻塞，内部自动重连

    # ============================================================
    # 事件处理（全部异步，保证 3 秒内返回避免 200340）
    # ============================================================
    def _on_message(self, data: Any) -> None:
        """IM 消息。"""
        try:
            event = data.event
            user_id = self._extract_sender_id(event.sender)
            chat_id = event.message.chat_id
            text = self._extract_text(event.message)
            if not text:
                return
        except Exception as e:
            logger.exception("解析消息事件异常: {}", e)
            return

        def worker():
            try:
                resp = self.dispatcher.dispatch_text(text, user_id or "", chat_id)
                if resp is not None:
                    self._send_response(resp, chat_id)
            except Exception as e:
                logger.exception("消息处理异步异常: {}", e)

        threading.Thread(target=worker, daemon=True).start()

    def _on_card_action(self, data: Any) -> Any:
        """卡片按钮回调（必须 3s 内返回）。"""
        try:
            event = getattr(data, "event", None)
            if event is None:
                return self._card_response()

            operator = getattr(event, "operator", None)
            user_id = getattr(operator, "open_id", "") if operator else ""
            action_obj = getattr(event, "action", None)
            value: dict[str, Any] = {}
            if action_obj is not None:
                raw = getattr(action_obj, "value", None)
                if isinstance(raw, dict):
                    value = raw
                elif isinstance(raw, str):
                    try:
                        value = json.loads(raw)
                    except json.JSONDecodeError:
                        value = {"raw": raw}

            ctx = getattr(event, "context", None)
            chat_id = (getattr(ctx, "open_chat_id", "") if ctx else "") \
                or self.cfg.feishu.target_chat_id
        except Exception as e:
            logger.exception("解析卡片回调异常: {}", e)
            return self._card_response()

        def worker():
            try:
                resp = self.dispatcher.dispatch_action(value, user_id)
                if resp is not None:
                    self._send_response(resp, chat_id)
            except Exception as e:
                logger.exception("卡片回调异步异常: {}", e)

        threading.Thread(target=worker, daemon=True).start()
        # 立刻给飞书一个 toast 作为"已收到"确认，主响应由后台异步推送
        return self._card_response(toast="info", content="处理中…")

    # ============================================================
    # 发送响应
    # ============================================================
    def _send_response(self, resp: CommandResponse, chat_id: str) -> None:
        if not chat_id:
            return

        if resp.card is not None:
            self.feishu.send_card(chat_id, resp.card)
        elif resp.text:
            self.feishu.send_text(chat_id, resp.text)

        if resp.file_path is not None and resp.file_path.exists():
            safe = ensure_upload_size(resp.file_path)
            key = self.feishu.upload_file(safe)
            if key:
                self.feishu.send_file(
                    chat_id, key,
                    resp.file_display_name or resp.file_path.name,
                )

        for extra in resp.extra_cards:
            self.feishu.send_card(chat_id, extra)

        for tid in resp.sync_scheduler_task_ids:
            try:
                self.scheduler.sync_task(tid)
            except Exception as e:
                logger.warning("同步调度 #{} 失败: {}", tid, e)

        if resp.trigger_check_task_id is not None:
            self.scheduler.sync_task(resp.trigger_check_task_id)
            self.scheduler.trigger_now(resp.trigger_check_task_id)

    # ============================================================
    # SDK 辅助
    # ============================================================
    @staticmethod
    def _card_response(toast: str = "", content: str = "") -> Any:
        """
        返回 SDK 期望的 P2CardActionTriggerResponse。
        可选 toast 参数会让用户在点按钮后立即看到一个气泡提示，体验更好。

        lark-oapi v1.4+ 的正确路径（从 SDK 源码验证）：
        lark_oapi.event.callback.model.p2_card_action_trigger
        """
        payload: dict[str, Any] = {}
        if toast:
            payload["toast"] = {"type": toast, "content": content or "已收到"}
        try:
            from lark_oapi.event.callback.model.p2_card_action_trigger import (
                P2CardActionTriggerResponse,
            )
            return P2CardActionTriggerResponse(payload)
        except ImportError:
            pass
        # 极老版本兼容
        try:
            from lark_oapi.card.model.p2_card_action_trigger import (
                P2CardActionTriggerResponse,
            )
            return P2CardActionTriggerResponse(payload)
        except ImportError:
            pass
        return None

    @staticmethod
    def _extract_sender_id(sender: Any) -> str | None:
        try:
            sid = getattr(sender, "sender_id", None) if sender else None
            if sid is None:
                return None
            return getattr(sid, "open_id", None) or getattr(sid, "user_id", None)
        except Exception:
            return None

    @staticmethod
    def _extract_text(message: Any) -> str:
        """兼容 text / post 两种消息类型。"""
        try:
            msg_type = getattr(message, "message_type", "") or ""
            content = getattr(message, "content", "") or ""
            if not content:
                return ""
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                return content

            if msg_type == "text":
                return (data.get("text") or "").strip()
            if msg_type == "post":
                lines: list[str] = []
                for row in (data.get("content") or []):
                    for node in row:
                        if node.get("tag") in ("text", "a"):
                            lines.append(node.get("text", ""))
                return "\n".join(lines).strip()
            return ""
        except Exception:
            return ""


# ============================================================
# main()
# ============================================================
def main() -> int:
    cfg = load_config()
    setup_logger(cfg.log_level)

    errors = validate_config(cfg)
    if errors:
        logger.error("⛔ 配置不完整：")
        for err in errors:
            logger.error("  - {}", err)
        logger.error("请完成 .env 配置（参考 .env.example）")
        return 1

    init_db()
    logger.info("💾 数据库就绪")

    app = App(cfg)

    def handler(signum, _frame):
        logger.info("收到信号 {}", signum)
        app.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    try:
        app.start()
    except KeyboardInterrupt:
        app.shutdown()
    except Exception as e:
        logger.exception("服务运行异常: {}", e)
        app.shutdown()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
