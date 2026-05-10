"""
主程序入口

流程：
1. 加载 .env 和 config.yaml
2. 初始化中文日志
3. 初始化数据库
4. 从 config.yaml 同步任务到 DB（首次启动时的种子数据）
5. 构造 FetchEngine / RiskController / FeishuClient / MonitorRunner / MonitorScheduler / CommandDispatcher
6. 注册飞书 WebSocket 长连接事件（消息接收 + 卡片按钮回调）
7. 启动调度器
8. 发送启动卡片
9. 阻塞运行直到收到 SIGINT/SIGTERM
"""

from __future__ import annotations

import json
import signal
import sys
import threading
import time
from datetime import datetime
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


# ============================================================
# 全局上下文
# ============================================================
class App:
    """整个服务的共享上下文。"""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.service_start_ts = time.time()

        self.risk = RiskController(cfg.risk_control, max_concurrent=cfg.max_concurrent_fetch)
        self.engine = FetchEngine(cfg)
        self.feishu = FeishuClient(cfg.feishu)
        self.runner = MonitorRunner(cfg, self.engine, self.risk, self.feishu)
        self.scheduler = MonitorScheduler(cfg, self.risk, run_fn=self.runner.run_once)
        self.dispatcher = CommandDispatcher(cfg, self.risk, self.service_start_ts,
                                            engine=self.engine)

        self._ws_client: Any = None

    # --------------------------------------------------------
    # 启动
    # --------------------------------------------------------
    def start(self) -> None:
        logger.info("═" * 60)
        logger.info("🚀 Web Monitor Pro v{} 正在启动", __version__)
        logger.info("═" * 60)

        # 从 config.yaml 同步种子任务到 DB
        self._seed_tasks_from_config()

        # 启动调度
        self.scheduler.start()

        # 推送启动卡片
        self._send_startup_card()

        # 建立飞书 WebSocket 长连接（阻塞）
        self._start_ws()

    # --------------------------------------------------------
    # 配置 → DB 任务同步
    # --------------------------------------------------------
    def _seed_tasks_from_config(self) -> None:
        if not self.cfg.tasks:
            return
        added = 0
        with session_scope() as s:
            for t in self.cfg.tasks:
                exists = s.query(Task).filter(Task.url == t.url).first()
                if exists:
                    continue
                s.add(Task(
                    name=t.name,
                    url=t.url,
                    type=t.type,
                    strategy=t.strategy,
                    impersonate=t.impersonate,
                    selector=t.selector,
                    json_path=t.json_path,
                    extract_next_data=t.extract_next_data,
                    interval=t.interval,
                    keywords=t.keywords or [],
                    headers=t.headers or {},
                    enabled=t.enabled,
                ))
                added += 1
        if added:
            logger.info("🌱 从 config.yaml 同步了 {} 个新任务到数据库", added)

    # --------------------------------------------------------
    # 启动卡片
    # --------------------------------------------------------
    def _send_startup_card(self) -> None:
        with session_scope() as s:
            task_count = s.query(Task).filter(Task.enabled.is_(True)).count()

        chat_id = self.cfg.feishu.target_chat_id
        if not chat_id:
            logger.warning("未配置 FEISHU_TARGET_CHAT_ID，跳过启动卡片推送")
            return

        card = cards.startup_card(
            task_count=task_count,
            default_interval=self.cfg.default_check_interval,
            version=__version__,
        )
        self.feishu.send_card(chat_id, card)
        logger.info("✉️  已推送启动卡片到 {}", chat_id[:16] + "...")

    # --------------------------------------------------------
    # 飞书长连接
    # --------------------------------------------------------
    def _start_ws(self) -> None:
        try:
            import lark_oapi as lark
            from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
        except ImportError as e:
            logger.error("lark-oapi 未安装，无法启动 WebSocket：{}", e)
            return

        # --- 消息接收 handler ---
        def on_message_receive(data: P2ImMessageReceiveV1) -> None:
            self._handle_message_event(data)

        # --- 卡片按钮回调 handler ---
        def on_card_action(data) -> Any:
            return self._handle_card_action(data)

        # 构造事件分发器（WebSocket 专用）
        try:
            dispatcher = (
                lark.EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(on_message_receive)
                .register_p2_card_action_trigger(on_card_action)
                .build()
            )
        except AttributeError:
            # 某些版本的 SDK 方法名不同，退化到手动字典注册
            dispatcher = (
                lark.EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(on_message_receive)
                .build()
            )
            logger.warning("lark SDK 未提供 register_p2_card_action_trigger，卡片按钮回调可能不生效")

        self._ws_client = lark.ws.Client(
            self.cfg.feishu.app_id,
            self.cfg.feishu.app_secret,
            event_handler=dispatcher,
            log_level=lark.LogLevel.WARNING,
        )
        logger.info("🔌 正在建立飞书 WebSocket 长连接...")
        # .start() 是阻塞式的，内部自动重连
        self._ws_client.start()

    # --------------------------------------------------------
    # 消息事件
    # --------------------------------------------------------
    def _handle_message_event(self, data: Any) -> None:
        """
        接收到群/私聊消息。
        立即返回，把实际处理异步化，避免飞书长连接超时（3 秒限制）。
        """
        # 先从事件里提取必要信息（快），再异步做业务处理
        try:
            event = data.event
            message = event.message
            sender = event.sender

            user_id = self._extract_sender_id(sender)
            chat_id = message.chat_id
            text = self._extract_text_from_message(message)
            if not text:
                return

            logger.debug("📨 收到消息 user={} chat={} text={!r}",
                         (user_id or "")[:10] + "...", chat_id[:10] + "...", text[:60])
        except Exception as e:
            logger.exception("解析飞书消息事件异常：{}", e)
            return

        # 异步处理：避免阻塞长连接
        def _worker():
            try:
                response = self.dispatcher.dispatch_text(text, user_id or "", chat_id)
                if response is not None:
                    self._send_response(response, chat_id)
            except Exception as exc:
                logger.exception("处理飞书消息（异步）异常：{}", exc)

        threading.Thread(
            target=_worker, name=f"msg-{int(time.time()*1000)}", daemon=True
        ).start()

    def _handle_card_action(self, data: Any) -> Any:
        """
        卡片按钮回调。
        **关键**：飞书要求 3 秒内响应，否则报 200340（"No card.action callback"）。
        所以我们必须立即返回，把实际业务处理放到后台线程。
        """
        try:
            event = getattr(data, "event", None)
            if event is None:
                return None
            operator = getattr(event, "operator", None)
            user_id = getattr(operator, "open_id", "") if operator else ""
            action_obj = getattr(event, "action", None)
            value: dict[str, Any] = {}
            if action_obj is not None:
                raw_value = getattr(action_obj, "value", None)
                if isinstance(raw_value, dict):
                    value = raw_value
                elif isinstance(raw_value, str):
                    try:
                        value = json.loads(raw_value)
                    except json.JSONDecodeError:
                        value = {"raw": raw_value}

            chat_id = ""
            ctx = getattr(event, "context", None)
            if ctx is not None:
                chat_id = getattr(ctx, "open_chat_id", "") or ""
            if not chat_id:
                chat_id = self.cfg.feishu.target_chat_id

            logger.debug("🖱️  卡片按钮回调 user={} value={}",
                         user_id[:10] + "...", value)
        except Exception as e:
            logger.exception("解析飞书卡片回调异常：{}", e)
            return None

        # 异步处理：立即返回让飞书满意，业务在后台做
        def _worker():
            try:
                response = self.dispatcher.dispatch_action(value, user_id)
                if response is not None:
                    self._send_response(response, chat_id)
            except Exception as exc:
                logger.exception("处理卡片回调（异步）异常：{}", exc)

        threading.Thread(
            target=_worker, name=f"card-{int(time.time()*1000)}", daemon=True
        ).start()
        return None

    # --------------------------------------------------------
    # 发送响应
    # --------------------------------------------------------
    def _send_response(self, response: CommandResponse, chat_id: str) -> None:
        """把 CommandResponse 发到指定 chat。"""
        if not chat_id:
            logger.warning("无有效 chat_id，响应无法发送")
            return

        # 先发主卡片
        if response.card is not None:
            self.feishu.send_card(chat_id, response.card)
        elif response.text:
            self.feishu.send_text(chat_id, response.text)

        # 再发附件
        if response.file_path is not None and response.file_path.exists():
            safe = ensure_upload_size(response.file_path)
            key = self.feishu.upload_file(safe)
            if key:
                self.feishu.send_file(
                    chat_id, key,
                    response.file_display_name or response.file_path.name,
                )

        # 额外卡片
        for extra in response.extra_cards:
            self.feishu.send_card(chat_id, extra)

        # 同步调度器（新增/暂停/恢复/删除 任务后务必调用）
        for tid in response.sync_scheduler_task_ids:
            try:
                self.scheduler.sync_task(tid)
            except Exception as e:
                logger.warning("同步任务 #{} 到调度器失败：{}", tid, e)

        # 立即触发检查
        if response.trigger_check_task_id is not None:
            # 先同步调度（例如刚新增的任务）
            self.scheduler.sync_task(response.trigger_check_task_id)
            self.scheduler.trigger_now(response.trigger_check_task_id)

    # --------------------------------------------------------
    # 辅助解析飞书事件
    # --------------------------------------------------------
    @staticmethod
    def _extract_sender_id(sender: Any) -> str | None:
        try:
            if sender is None:
                return None
            sid = getattr(sender, "sender_id", None)
            if sid is None:
                return None
            return getattr(sid, "open_id", None) or getattr(sid, "user_id", None)
        except Exception:
            return None

    @staticmethod
    def _extract_text_from_message(message: Any) -> str:
        """从飞书消息对象中拿到用户文本。兼容 text / post 两种类型。"""
        try:
            msg_type = getattr(message, "message_type", "") or ""
            content = getattr(message, "content", "") or ""
            if not content:
                return ""

            # content 是 JSON 字符串
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                return content

            if msg_type == "text":
                return (data.get("text") or "").strip()

            if msg_type == "post":
                # post 消息是富文本，多行
                lines = []
                for row in (data.get("content") or []):
                    for node in row:
                        if node.get("tag") in ("text", "a"):
                            lines.append(node.get("text", ""))
                return "\n".join(lines).strip()

            return ""
        except Exception:
            return ""

    # --------------------------------------------------------
    # 关闭
    # --------------------------------------------------------
    def shutdown(self) -> None:
        logger.info("🛑 正在关闭服务...")
        try:
            self.scheduler.stop()
        except Exception as e:
            logger.warning("关闭调度器异常：{}", e)
        try:
            if self._ws_client is not None:
                # lark SDK 的 ws.Client 没有官方 stop，但下面 sys.exit 会自然回收
                pass
        except Exception:
            pass
        logger.info("👋 再见")


# ============================================================
# main()
# ============================================================
def main() -> int:
    # 1. 加载配置
    cfg = load_config()

    # 2. 初始化日志
    setup_logger(cfg.log_level)

    errors = validate_config(cfg)
    if errors:
        logger.error("⛔ 配置不完整，服务无法启动：")
        for err in errors:
            logger.error("  - {}", err)
        logger.error("请先完成 .env 配置（参考 .env.example）")
        return 1

    # 3. 初始化数据库
    init_db()
    logger.info("💾 数据库初始化完成")

    # 4. 启动应用
    app = App(cfg)

    def _signal_handler(signum, frame):
        logger.info("收到信号 {}", signum)
        app.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        app.start()  # 阻塞
    except KeyboardInterrupt:
        app.shutdown()
    except Exception as e:
        logger.exception("服务运行异常：{}", e)
        app.shutdown()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
