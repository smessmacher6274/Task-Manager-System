"""Schema v0 (CentralEcosystemPlan.md §3). Same schema in the work and personal DBs.

Additions beyond the md: users.password_hash, sessions (web auth), sync_state
(per-user poll/push bookkeeping).

All datetimes are naive UTC. Soft deletes via deleted_at.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class PadState(Base):
    """Durable revision ledger for the existing tablet-001 wire protocol."""
    __tablename__ = "pad_state"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    ledger_json: Mapped[str] = mapped_column(Text, default="{}")


class PadReceipt(Base):
    __tablename__ = "pad_receipts"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    command_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_json: Mapped[str] = mapped_column(Text)
    result_json: Mapped[str] = mapped_column(Text)


# Local status vocabulary mirrors Redmine status names (lowercase, underscores)
# so the three-way merge is lossless. Tablet/server items start as "new".
CLOSED_STATUSES = {"closed", "rejected"}


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(200), unique=True)
    tz: Mapped[str] = mapped_column(String(64), default="America/New_York")
    password_hash: Mapped[str | None] = mapped_column(String(200))


class Session(Base):
    __tablename__ = "sessions"
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(16))  # work | personal | custom
    syncs_redmine: Mapped[bool] = mapped_column(Boolean, default=False)


class Item(Base):
    __tablename__ = "items"
    uuid: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="new")
    priority: Mapped[str] = mapped_column(String(32), default="normal")
    due_date: Mapped[date | None] = mapped_column(Date)
    deadline_origin: Mapped[str] = mapped_column(String(16), default="self")  # redmine | self
    source: Mapped[str] = mapped_column(String(16))  # tablet | server | redmine | phone
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)
    redmine_issue_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    redmine_project_id: Mapped[int | None] = mapped_column(Integer)
    tracker_id: Mapped[int | None] = mapped_column(Integer)
    needs_triage: Mapped[bool] = mapped_column(Boolean, default=False)
    dirty: Mapped[bool] = mapped_column(Boolean, default=False)


class RedmineSnapshot(Base):
    """Merge base: the last Redmine state both sides agreed on."""
    __tablename__ = "redmine_snapshot"
    item_uuid: Mapped[str] = mapped_column(ForeignKey("items.uuid"), primary_key=True)
    raw_json: Mapped[str] = mapped_column(Text)
    redmine_updated_on: Mapped[str] = mapped_column(String(32))
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SyncConflict(Base):
    __tablename__ = "sync_conflicts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_uuid: Mapped[str] = mapped_column(ForeignKey("items.uuid"), index=True)
    field: Mapped[str] = mapped_column(String(32))
    base_value: Mapped[str | None] = mapped_column(Text)
    server_value: Mapped[str | None] = mapped_column(Text)
    redmine_value: Mapped[str | None] = mapped_column(Text)
    redmine_journal_id: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|keep_server|take_redmine
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)


class Outbox(Base):
    """One pending row per item with unpushed field changes (or a create)."""
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_uuid: Mapped[str] = mapped_column(ForeignKey("items.uuid"), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(Text)


class ItemNote(Base):
    __tablename__ = "item_notes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_uuid: Mapped[str] = mapped_column(ForeignKey("items.uuid"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    author: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    private: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    redmine_journal_id: Mapped[int | None] = mapped_column(Integer)
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(Text)


class TimeEntry(Base):
    __tablename__ = "time_entries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_uuid: Mapped[str] = mapped_column(ForeignKey("items.uuid"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    hours: Mapped[float] = mapped_column(Float)
    spent_on: Mapped[date] = mapped_column(Date)
    activity_id: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str] = mapped_column(String(1000), default="")
    redmine_time_entry_id: Mapped[int | None] = mapped_column(Integer)
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(Text)


class DeadlineEvent(Base):
    """Append-only. Only ever inserted via eco.deadlines.log_event."""
    __tablename__ = "deadline_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_uuid: Mapped[str] = mapped_column(ForeignKey("items.uuid"), index=True)
    event: Mapped[str] = mapped_column(String(16))  # set | extended | met | missed
    old_due: Mapped[date | None] = mapped_column(Date)
    new_due: Mapped[date | None] = mapped_column(Date)
    origin: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class IngestLog(Base):
    __tablename__ = "ingest_log"
    msg_uuid: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class StatusMap(Base):
    __tablename__ = "status_map"
    redmine_status_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    local_status: Mapped[str] = mapped_column(String(32), unique=True)
    is_closed: Mapped[bool] = mapped_column(Boolean, default=False)


class ActivityMap(Base):
    __tablename__ = "activity_map"
    redmine_activity_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))


class CalendarDay(Base):
    __tablename__ = "calendar_days"
    __table_args__ = (UniqueConstraint("user_id", "date"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    date: Mapped[date] = mapped_column(Date)
    type: Mapped[str] = mapped_column(String(16))  # pto | holiday


class SyncState(Base):
    __tablename__ = "sync_state"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_push_at: Mapped[datetime | None] = mapped_column(DateTime)
