"""数据库模型和会话（SQLAlchemy + SQLite）。"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import (
    JSON, Boolean, DateTime, ForeignKey, Integer, String, Text,
    create_engine, event,
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, Session, mapped_column, sessionmaker,
)

from .config import DB_PATH


def now_utc() -> datetime:
    """Timezone-aware UTC now.

    Python 3.12+ 弃用了 ``datetime.utcnow()``。全项目都应通过本函数获取
    当前时间，既消除 DeprecationWarning，也避免 naive/aware 混用带来的
    微妙 bug。返回值会被 SQLAlchemy DateTime 列直接存为 UTC。
    """
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Task(Base):
    """监控任务。"""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    # /add 去重会按 url 精确匹配，种子任务同步也走 url 查询，加索引加速 O(n) → O(log n)。
    url: Mapped[str] = mapped_column(Text, index=True)

    type: Mapped[str] = mapped_column(String(16), default="html")
    strategy: Mapped[str] = mapped_column(String(32), default="auto")
    impersonate: Mapped[str] = mapped_column(String(32), default="chrome131")

    selector: Mapped[str | None] = mapped_column(Text, nullable=True)
    adaptive_selector: Mapped[bool] = mapped_column(Boolean, default=False)
    selector_identifier: Mapped[str | None] = mapped_column(Text, nullable=True)
    adaptive_threshold: Mapped[int] = mapped_column(Integer, default=40)
    wait_selector: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    last_strategy_used: Mapped[str | None] = mapped_column(String(64), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    total_checks: Mapped[int] = mapped_column(Integer, default=0)
    total_changes: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_utc, onupdate=now_utc
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)


class PushLog(Base):
    """飞书推送记录（风控冷却用）。"""

    __tablename__ = "push_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32))  # change / first / error / ...
    message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc, index=True)


class ServiceState(Base):
    """服务级 KV（mute_until 等）。"""

    __tablename__ = "service_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_utc, onupdate=now_utc
    )


_ENGINE = create_engine(
    f"sqlite:///{DB_PATH}",
    echo=False,
    future=True,
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
)


# 启用 WAL + NORMAL synchronous：提升并发读写性能，更适合 APScheduler
# 多线程轮询 + 飞书事件线程同时读写 SQLite 的场景。
@event.listens_for(_ENGINE, "connect")
def _sqlite_pragmas(dbapi_conn, _conn_record):
    try:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()
    except Exception as exc:
        from .logger import logger
        logger.error("SQLite pragma 设置失败（数据库可能在非 WAL 模式下运行）: {}", exc)


_SessionFactory = sessionmaker(bind=_ENGINE, expire_on_commit=False, autoflush=False)


def init_db() -> None:
    """建表 + 自动迁移（给已有表补齐新列）。"""
    Base.metadata.create_all(_ENGINE)
    _auto_migrate()


def _column_default_sql(col: Any) -> str:
    """Derive the SQL DEFAULT clause for ``ALTER TABLE ADD COLUMN``.

    Only handles scalar constants; callables (e.g. ``default=datetime.utcnow``
    or ``default=list``) don't translate cleanly to SQL ``DEFAULT``, so we
    emit a safe literal for JSON-typed columns (``'[]'`` / ``'{}'``) and
    leave other callables without a default.
    """
    default = col.default
    # JSON default list / dict from SQLAlchemy ORM: emit matching SQL literal
    # so pre-existing rows don't read back as NULL and crash caller code.
    if isinstance(col.type, JSON):
        val = getattr(default, "arg", None) if default is not None else None
        if val is list or (callable(val) and val.__name__ == "list"):
            return " DEFAULT '[]'"
        if val is dict or (callable(val) and val.__name__ == "dict"):
            return " DEFAULT '{}'"
        # Fallback for JSON columns without a callable default
        return " DEFAULT '[]'"

    if default is None:
        return ""

    val = default.arg
    if callable(val):
        return ""
    if isinstance(val, bool):
        return f" DEFAULT {1 if val else 0}"
    if isinstance(val, (int, float)):
        return f" DEFAULT {val}"
    if isinstance(val, str):
        # Escape single quotes in SQL string literals
        escaped = val.replace("'", "''")
        return f" DEFAULT '{escaped}'"
    return ""


def _auto_migrate() -> None:
    """检查并补齐 ORM 模型中定义但 SQLite 表中缺失的列。

    注意：SQLite 的 ``ALTER TABLE ADD COLUMN`` 有限制——不支持加 ``UNIQUE`` 约束、
    不支持删除列、不支持改类型。此处仅做"加列 + 设默认值"的向前兼容；
    如需重命名/删除列，请手动迁移或使用 Alembic。
    """
    from sqlalchemy import inspect as sa_inspect, text
    from .logger import logger

    inspector = sa_inspect(_ENGINE)
    for table_name, model_table in Base.metadata.tables.items():
        if not inspector.has_table(table_name):
            continue
        existing = {col["name"] for col in inspector.get_columns(table_name)}
        for col in model_table.columns:
            if col.name in existing:
                continue
            col_type = col.type.compile(dialect=_ENGINE.dialect)
            default_sql = _column_default_sql(col)
            with _ENGINE.begin() as conn:
                conn.execute(text(
                    f"ALTER TABLE {table_name} ADD COLUMN "
                    f"{col.name} {col_type}{default_sql}"
                ))
            logger.info("migrated: ALTER TABLE {} ADD COLUMN {} {}{}",
                        table_name, col.name, col_type, default_sql)

    # 对在 ORM 里声明了 index=True 但旧表还没有索引的列，补建索引。
    for table_name, model_table in Base.metadata.tables.items():
        if not inspector.has_table(table_name):
            continue
        try:
            existing_indexes = {
                ix["name"] for ix in inspector.get_indexes(table_name)
            }
        except Exception:
            existing_indexes = set()
        for idx in model_table.indexes:
            if idx.name and idx.name not in existing_indexes:
                try:
                    with _ENGINE.begin() as conn:
                        idx.create(bind=conn)
                    logger.info("migrated: CREATE INDEX {} ON {}",
                                idx.name, table_name)
                except Exception as e:
                    logger.warning("create index {} failed: {}", idx.name, e)


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
