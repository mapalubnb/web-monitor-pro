"""
数据库模型和会话（SQLAlchemy + SQLite）

表：
- tasks            监控任务
- change_history   变更历史
- push_log         推送日志（风控冷却）
- service_state    服务级 KV 状态

快照/diff 文件直接存文件系统，DB 只记元数据路径。
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from sqlalchemy import (
    JSON, Boolean, DateTime, ForeignKey, Integer, String, Text,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, Session, mapped_column, sessionmaker,
)

from .config import DB_PATH


class Base(DeclarativeBase):
    pass


class Task(Base):
    """监控任务。"""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(Text)

    type: Mapped[str] = mapped_column(String(16), default="html")
    strategy: Mapped[str] = mapped_column(String(32), default="auto")
    impersonate: Mapped[str] = mapped_column(String(32), default="chrome131")

    selector: Mapped[str | None] = mapped_column(Text, nullable=True)
    json_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    extract_next_data: Mapped[bool] = mapped_column(Boolean, default=False)

    interval: Mapped[int] = mapped_column(Integer, default=60)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    headers: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)

    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_snapshot_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    total_checks: Mapped[int] = mapped_column(Integer, default=0)
    total_changes: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class ChangeHistory(Base):
    """变更历史记录。"""

    __tablename__ = "change_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    added_lines: Mapped[int] = mapped_column(Integer, default=0)
    removed_lines: Mapped[int] = mapped_column(Integer, default=0)
    change_ratio: Mapped[float] = mapped_column(default=0.0)
    matched_keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    diff_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    pushed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class PushLog(Base):
    """飞书推送记录（风控冷却用）。"""

    __tablename__ = "push_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32))  # change / first / error / ...
    message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class ServiceState(Base):
    """服务级 KV（mute_until 等）。"""

    __tablename__ = "service_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


# ============================================================
# 引擎 & 会话
# ============================================================
_ENGINE = create_engine(
    f"sqlite:///{DB_PATH}",
    echo=False,
    future=True,
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
)
_SessionFactory = sessionmaker(bind=_ENGINE, expire_on_commit=False, autoflush=False)


def init_db() -> None:
    """建表。"""
    Base.metadata.create_all(_ENGINE)


@contextmanager
def session_scope() -> Iterator[Session]:
    """标准事务上下文。"""
    s = _SessionFactory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_state(key: str, default: Any = None) -> Any:
    """读取服务状态（JSON 解码）。"""
    with session_scope() as s:
        row = s.get(ServiceState, key)
        if row is None:
            return default
        try:
            return json.loads(row.value)
        except json.JSONDecodeError:
            return row.value


def set_state(key: str, value: Any) -> None:
    """写入服务状态（JSON 编码）。"""
    with session_scope() as s:
        row = s.get(ServiceState, key)
        payload = json.dumps(value, ensure_ascii=False)
        if row is None:
            s.add(ServiceState(key=key, value=payload))
        else:
            row.value = payload
