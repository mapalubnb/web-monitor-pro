"""
数据库模型和会话管理

使用 SQLAlchemy 同步 API + SQLite（简单、零外部依赖）。
表：
- tasks              监控任务
- snapshots          每次抓取结果的快照（内容保存在文件系统，DB 只存路径/hash）
- change_history     变更历史记录
- push_log           飞书推送日志（风控冷却判定）
- service_state      服务级 KV 状态（如 mute_until 等）
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from .config import DB_PATH


# ============================================================
# 基础模型
# ============================================================
class Base(DeclarativeBase):
    pass


class Task(Base):
    """监控任务。"""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(Text)

    # 类型：html / json
    type: Mapped[str] = mapped_column(String(16), default="html")

    # 抓取策略：auto / httpx / curl_cffi / jina / firecrawl
    strategy: Mapped[str] = mapped_column(String(32), default="auto")
    # curl_cffi 模拟的浏览器
    impersonate: Mapped[str] = mapped_column(String(32), default="chrome131")

    # 提取配置
    selector: Mapped[str | None] = mapped_column(Text, nullable=True)
    json_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    extract_next_data: Mapped[bool] = mapped_column(Boolean, default=False)

    # 调度
    interval: Mapped[int] = mapped_column(Integer, default=60)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # 过滤 / 通知
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    headers: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)

    # 运行时状态
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

    # 关系
    snapshots: Mapped[list["Snapshot"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    changes: Mapped[list["ChangeHistory"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class Snapshot(Base):
    """单次抓取结果快照（元数据；正文在文件系统）。"""

    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)

    content_hash: Mapped[str] = mapped_column(String(64))
    content_length: Mapped[int] = mapped_column(Integer, default=0)
    content_path: Mapped[str] = mapped_column(Text)
    strategy_used: Mapped[str] = mapped_column(String(32))
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    task: Mapped["Task"] = relationship(back_populates="snapshots")


class ChangeHistory(Base):
    """变更历史（记录每次识别出的变化）。"""

    __tablename__ = "change_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)

    added_lines: Mapped[int] = mapped_column(Integer, default=0)
    removed_lines: Mapped[int] = mapped_column(Integer, default=0)
    change_ratio: Mapped[float] = mapped_column(default=0.0)
    matched_keywords: Mapped[list[str]] = mapped_column(JSON, default=list)

    before_snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    after_snapshot_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    diff_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    pushed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    task: Mapped["Task"] = relationship(back_populates="changes")


class PushLog(Base):
    """飞书推送日志（用于风控冷却判定）。"""

    __tablename__ = "push_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32))  # change / startup / first_snapshot / error ...
    message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class ServiceState(Base):
    """服务级 KV 存储（mute_until 等）。"""

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
    connect_args={"check_same_thread": False},  # 允许 APScheduler 跨线程访问
)
_SessionFactory = sessionmaker(bind=_ENGINE, expire_on_commit=False, autoflush=False)


def init_db() -> None:
    """建表（首次运行或升级时调用）。"""
    Base.metadata.create_all(_ENGINE)


@contextmanager
def session_scope() -> Iterator[Session]:
    """标准的事务会话上下文。用法：
    with session_scope() as s: ...
    """
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ============================================================
# 便捷查询
# ============================================================
def list_enabled_tasks() -> list[Task]:
    """返回所有启用的任务（分离实例，调用方可自由使用）。"""
    with session_scope() as s:
        tasks = s.execute(select(Task).where(Task.enabled.is_(True))).scalars().all()
        # 取出来后 detach，避免会话关闭后访问属性报错
        for t in tasks:
            s.expunge(t)
        return list(tasks)


def get_task(task_id: int) -> Task | None:
    with session_scope() as s:
        t = s.get(Task, task_id)
        if t:
            s.expunge(t)
        return t


def get_state(key: str, default: Any = None) -> Any:
    """读取服务级状态（JSON 解码）。"""
    with session_scope() as s:
        row = s.get(ServiceState, key)
        if row is None:
            return default
        try:
            return json.loads(row.value)
        except json.JSONDecodeError:
            return row.value


def set_state(key: str, value: Any) -> None:
    """写入服务级状态（JSON 编码）。"""
    with session_scope() as s:
        row = s.get(ServiceState, key)
        payload = json.dumps(value, ensure_ascii=False)
        if row is None:
            s.add(ServiceState(key=key, value=payload))
        else:
            row.value = payload
