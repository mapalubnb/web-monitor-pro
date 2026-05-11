"""
飞书客户端封装

封装 lark-oapi SDK，提供：
- send_text(chat_id, text)           发送纯文本
- send_card(chat_id, card)           发送交互式卡片（dict 形式）
- reply_card(message_id, card)       在原消息下回复
- upload_file(path, file_type)       上传文件（返回 file_key）
- send_file(chat_id, file_key, name) 发送文件消息
- send_and_attach(chat_id, card, file_path, filename)  便捷组合

所有方法都是同步的，调用方自行在线程池/调度器中使用。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from ..config import FeishuConfig
from ..logger import logger


class FeishuClient:
    """lark-oapi 的高层封装。"""

    def __init__(self, cfg: FeishuConfig):
        self.cfg = cfg
        self._client = self._build_client()

    # --------------------------------------------------------
    # 内部：构建 lark SDK 客户端
    # --------------------------------------------------------
    def _build_client(self):
        try:
            import lark_oapi as lark
        except ImportError as e:
            raise RuntimeError("lark-oapi 未安装，请 pip install lark-oapi>=1.4.0") from e

        client = (
            lark.Client.builder()
            .app_id(self.cfg.app_id)
            .app_secret(self.cfg.app_secret)
            .log_level(lark.LogLevel.WARNING)  # 降低 SDK 自身日志噪音
            .build()
        )
        logger.info("🤖 飞书 SDK 客户端已初始化（App ID: {}...）", self.cfg.app_id[:8])
        return client

    # --------------------------------------------------------
    # 发送纯文本
    # --------------------------------------------------------
    def send_text(self, chat_id: str, text: str) -> str | None:
        """发送纯文本消息。返回 message_id 或 None（失败）。"""
        return self._send_message(chat_id, "text", {"text": text})

    # --------------------------------------------------------
    # 发送交互卡片
    # --------------------------------------------------------
    def send_card(self, chat_id: str, card: dict[str, Any]) -> str | None:
        """
        发送交互式卡片。

        Args:
            chat_id: 目标 chat_id（oc_ 开头）或 user open_id（ou_ 开头）
            card: 卡片 JSON（飞书卡片 2.0 格式 dict）
        """
        content = json.dumps(card, ensure_ascii=False)
        return self._send_message(chat_id, "interactive", content_is_string=True, content=content)

    def reply_card(self, message_id: str, card: dict[str, Any]) -> str | None:
        """在原消息下回复一个卡片。"""
        try:
            from lark_oapi.api.im.v1 import (
                ReplyMessageRequest,
                ReplyMessageRequestBody,
            )
        except ImportError:
            logger.error("lark-oapi 缺少 reply_message 模块")
            return None
        try:
            body = ReplyMessageRequestBody.builder() \
                .msg_type("interactive") \
                .content(json.dumps(card, ensure_ascii=False)) \
                .build()
            req = ReplyMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(body) \
                .build()
            resp = self._client.im.v1.message.reply(req)
            if not resp.success():
                logger.warning("飞书 reply_card 失败 code={} msg={}", resp.code, resp.msg)
                return None
            return getattr(resp.data, "message_id", None) if resp.data else None
        except Exception as e:
            logger.error("飞书 reply_card 异常: {}", e)
            return None

    # --------------------------------------------------------
    # 发送消息底层
    # --------------------------------------------------------
    def _send_message(
        self,
        chat_id: str,
        msg_type: str,
        content_dict: dict[str, Any] | None = None,
        *,
        content_is_string: bool = False,
        content: str | None = None,
    ) -> str | None:
        try:
            from lark_oapi.api.im.v1 import (
                CreateMessageRequest,
                CreateMessageRequestBody,
            )
        except ImportError:
            logger.error("lark-oapi 缺少 im.v1 模块")
            return None

        if content_is_string:
            body_content = content or ""
        else:
            body_content = json.dumps(content_dict or {}, ensure_ascii=False)

        # 根据 chat_id / open_id / user_id 判断 receive_id_type
        receive_id_type = self._infer_receive_id_type(chat_id)

        try:
            body = CreateMessageRequestBody.builder() \
                .receive_id(chat_id) \
                .msg_type(msg_type) \
                .content(body_content) \
                .build()
            req = CreateMessageRequest.builder() \
                .receive_id_type(receive_id_type) \
                .request_body(body) \
                .build()
            resp = self._client.im.v1.message.create(req)
            if not resp.success():
                logger.warning(
                    "飞书发送消息失败 type={} code={} msg={}",
                    msg_type, resp.code, resp.msg,
                )
                return None
            msg_id = getattr(resp.data, "message_id", None) if resp.data else None
            logger.debug("✉️  飞书消息已发送 id={}", msg_id)
            return msg_id
        except Exception as e:
            logger.error("飞书发送消息异常: {}", e)
            return None

    @staticmethod
    def _infer_receive_id_type(receive_id: str) -> str:
        if receive_id.startswith("oc_"):
            return "chat_id"
        if receive_id.startswith("ou_"):
            return "open_id"
        if receive_id.startswith("on_"):
            return "union_id"
        if "@" in receive_id:
            return "email"
        return "chat_id"  # 兜底

    # --------------------------------------------------------
    # 文件上传 / 发送
    # --------------------------------------------------------
    def upload_file(self, path: str | Path, file_type: str = "stream",
                    file_name: str = "") -> str | None:
        """
        上传文件到飞书。返回 file_key 供后续发送使用。

        Args:
            path: 本地文件路径
            file_type: opus/mp4/pdf/doc/xls/ppt/stream（普通文本用 stream）
            file_name: 飞书端显示的文件名（默认用本地文件名）
        """
        try:
            from lark_oapi.api.im.v1 import (
                CreateFileRequest,
                CreateFileRequestBody,
            )
        except ImportError:
            logger.error("lark-oapi 缺少文件上传模块")
            return None

        p = Path(path)
        if not p.exists():
            logger.warning("文件不存在，无法上传: {}", p)
            return None

        try:
            display_name = file_name or p.name
            file_size = p.stat().st_size
            with p.open("rb") as f:
                body = CreateFileRequestBody.builder() \
                    .file_type(file_type) \
                    .file_name(display_name) \
                    .file(f) \
                    .build()
                # 某些 SDK 版本需要 file_size
                try:
                    body.file_size = file_size  # type: ignore[attr-defined]
                except Exception:
                    pass
                req = CreateFileRequest.builder().request_body(body).build()
                resp = self._client.im.v1.file.create(req)
            if not resp.success():
                logger.warning("飞书文件上传失败 code={} msg={}", resp.code, resp.msg)
                return None
            return getattr(resp.data, "file_key", None) if resp.data else None
        except Exception as e:
            logger.error("飞书文件上传异常: {}", e)
            return None

    def send_file(self, chat_id: str, file_key: str) -> str | None:
        """发送已上传的文件（显示名由 upload_file 的 file_name 决定）。"""
        return self._send_message(chat_id, "file", {"file_key": file_key})

    def send_card_and_file(
        self,
        chat_id: str,
        card: dict[str, Any],
        file_path: str | Path | None = None,
        file_display_name: str = "",
    ) -> tuple[str | None, str | None]:
        """
        组合：先发卡片，再发文件（若提供）。
        返回 (card_message_id, file_message_id)
        """
        card_msg_id = self.send_card(chat_id, card)
        file_msg_id: str | None = None
        if file_path is not None and Path(file_path).exists():
            safe = ensure_upload_size(Path(file_path))
            file_key = self.upload_file(
                safe, file_name=file_display_name or Path(file_path).name)
            if file_key:
                file_msg_id = self.send_file(chat_id, file_key)
            # 清理截断产生的临时文件
            if safe != Path(file_path):
                try:
                    safe.unlink(missing_ok=True)
                except Exception:
                    pass
        return card_msg_id, file_msg_id

    # --------------------------------------------------------
    # 内置 SDK 客户端直连（高级用法）
    # --------------------------------------------------------
    @property
    def raw(self):
        """返回原生 lark.Client，供需要调用其他 API 的高级场景使用。"""
        return self._client


__all__ = ["FeishuClient"]


# ============================================================
# 便捷：确保上传的临时文件不超过飞书文件大小限制（30MB）
# ============================================================
def ensure_upload_size(path: Path, max_mb: int = 28) -> Path:
    """
    若文件超过限制，截取前 max_mb MB 写到新临时文件返回；否则原路返回。
    """
    max_bytes = max_mb * 1024 * 1024
    if path.stat().st_size <= max_bytes:
        return path

    truncated = path.with_suffix(path.suffix + ".truncated")
    with path.open("rb") as src, truncated.open("wb") as dst:
        dst.write(src.read(max_bytes))
        dst.write(b"\n\n[... file truncated due to size limit ...]\n")
    logger.warning(
        "📎 文件 {} 超过 {}MB，已截断为 {}",
        path.name, max_mb, truncated.name,
    )
    return truncated


__all__.append("ensure_upload_size")
