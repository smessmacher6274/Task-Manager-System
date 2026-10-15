"""Server-side item operations (used by the API, the MQTT bridge, and the poller)."""
from __future__ import annotations

import json
import uuid as uuidlib
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from eco import deadlines
from eco.models import (
    CLOSED_STATUSES, Category, Item, ItemNote, Outbox, SyncConflict, TimeEntry, User, utcnow,
)

EDITABLE = ("title", "description", "status", "priority", "due_date")


class NotFound(LookupError):
    pass


class Invalid(ValueError):
    pass


def category_for(s: Session, user_id: int, ref: str | int | None, kind_default: str) -> Category:
    """Find a user's category by id, name, or kind; falls back to the first of kind_default."""
    cats = s.scalars(select(Category).where(Category.user_id == user_id)).all()
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        for c in cats:
            if c.id == int(ref):
                return c
    if isinstance(ref, str):
        for c in cats:
            if c.name.lower() == ref.lower():
                return c
        for c in cats:
            if c.kind == ref.lower():
                return c
    for c in cats:
        if c.kind == kind_default:
            return c
    raise NotFound(f"no category {ref!r} for user {user_id}")


def syncs(s: Session, item: Item) -> bool:
    cat = s.get(Category, item.category_id)
    return bool(cat and cat.syncs_redmine)


def get_item(s: Session, user_id: int, item_uuid: str) -> Item:
    item = s.get(Item, item_uuid)
    if item is None or item.user_id != user_id:
        raise NotFound(item_uuid)
    return item


def touch_outbox(s: Session, item: Item) -> None:
    """Make sure a synced, dirty item has one pending outbox row (payload is
    recomputed at push time; the stored one is informational)."""
    if not syncs(s, item) or item.needs_triage:
        return  # untriaged items are queued by set_triage
    item.dirty = True
    row = s.scalar(select(Outbox).where(Outbox.item_uuid == item.uuid, Outbox.pushed_at.is_(None)))
    if row is None:
        row = Outbox(item_uuid=item.uuid, payload_json="{}")
        s.add(row)
    row.payload_json = json.dumps({"pending": True, "queued_at": utcnow().isoformat()})


def create_item(s: Session, user_id: int, *, title: str, category: Category, source: str,
                description: str = "", due_date: date | None = None, priority: str = "normal",
                item_uuid: str | None = None, captured_at: datetime | None = None,
                redmine_project_id: int | None = None, tracker_id: int | None = None,
                default_deadline_days: int = 7, today: date | None = None) -> Item:
    if not title.strip():
        raise Invalid("title is required")
    item = Item(
        uuid=item_uuid or str(uuidlib.uuid4()), user_id=user_id, category_id=category.id,
        title=title.strip(), description=description, status="new", priority=priority,
        deadline_origin="self", source=source, captured_at=captured_at or utcnow(),
        redmine_project_id=redmine_project_id, tracker_id=tracker_id,
        needs_triage=category.syncs_redmine and not (redmine_project_id and tracker_id),
    )
    s.add(item)
    s.flush()
    if due_date is None:
        due_date = (today or date.today()) + timedelta(days=default_deadline_days)
    deadlines.change_due(s, item, due_date, origin="self")
    if category.syncs_redmine and not item.needs_triage:
        touch_outbox(s, item)
    return item


def apply_fields(s: Session, item: Item, fields: dict, *, origin: str, reason: str | None = None,
                 today: date | None = None) -> None:
    """Set item fields from str/None values (merge or API). Logs deadline events."""
    for f, v in fields.items():
        if f == "due_date":
            new_due = date.fromisoformat(v) if isinstance(v, str) and v else (v or None)
            deadlines.change_due(s, item, new_due, origin=origin,
                                 reason=reason or ("changed in Redmine" if origin == "redmine" else None))
        elif f in ("title", "description", "status", "priority"):
            old_status = item.status
            setattr(item, f, v if v is not None else "")
            if f == "status" and v == "closed" and old_status != "closed":
                deadlines.mark_met_if_due(s, item, today or date.today())


def update_item(s: Session, item: Item, changes: dict, *, reason: str | None = None) -> Item:
    if item.deleted_at:
        raise Invalid("item is deleted")
    unknown = set(changes) - set(EDITABLE)
    if unknown:
        raise Invalid(f"not editable: {sorted(unknown)}")
    changes = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in changes.items()}
    try:
        apply_fields(s, item, changes, origin="self", reason=reason)
    except deadlines.ReasonRequired as e:
        raise Invalid(str(e)) from e
    touch_outbox(s, item)
    return item


def complete_item(s: Session, item: Item) -> Item:
    return update_item(s, item, {"status": "closed"})


def delete_item(s: Session, item: Item) -> Item:
    """Soft delete; synced items get pushed to Redmine as Rejected."""
    if item.deleted_at:
        return item
    item.status = "rejected"
    item.deleted_at = utcnow()
    touch_outbox(s, item)
    return item


def set_triage(s: Session, item: Item, *, redmine_project_id: int, tracker_id: int) -> Item:
    item.redmine_project_id = redmine_project_id
    item.tracker_id = tracker_id
    item.needs_triage = False
    touch_outbox(s, item)
    return item


def add_note(s: Session, item: Item, user: User, body: str, private: bool = False,
             author: str | None = None) -> ItemNote:
    if not body.strip():
        raise Invalid("note body is required")
    note = ItemNote(item_uuid=item.uuid, user_id=user.id, author=author or user.name,
                    body=body, private=private)
    s.add(note)
    s.flush()
    return note


def add_time_entry(s: Session, item: Item, user: User, *, hours: float, spent_on: date,
                   activity_id: int, comment: str = "") -> TimeEntry:
    if hours <= 0:
        raise Invalid("hours must be positive")
    te = TimeEntry(item_uuid=item.uuid, user_id=user.id, hours=hours, spent_on=spent_on,
                   activity_id=activity_id, comment=comment)
    s.add(te)
    s.flush()
    return te


def resolve_conflict(s: Session, conflict: SyncConflict, user: User, choice: str,
                     reason: str | None = None) -> SyncConflict:
    """take_redmine: adopt Redmine's value. keep_server: keep ours, push it, and
    add a journal note explaining the rejection of the coworker's change."""
    if conflict.status != "pending":
        raise Invalid("conflict already resolved")
    item = s.get(Item, conflict.item_uuid)
    if choice == "take_redmine":
        apply_fields(s, item, {conflict.field: conflict.redmine_value}, origin="redmine")
    elif choice == "keep_server":
        if not (reason and reason.strip()):
            raise Invalid("keeping the server value requires a reason")
        who = _journal_author(s, item, conflict.redmine_journal_id) or "a Redmine user"
        add_note(s, item, user, author="eco-server",
                 body=(f"Kept {conflict.field} = {conflict.server_value!r} instead of the change "
                       f"to {conflict.redmine_value!r} by {who}. Reason: {reason.strip()}"))
        touch_outbox(s, item)
    else:
        raise Invalid(f"unknown choice {choice!r}")
    conflict.status = choice
    conflict.resolved_at = utcnow()
    return conflict


def _journal_author(s: Session, item: Item, journal_id: int | None) -> str | None:
    from eco.models import RedmineSnapshot
    snap = s.get(RedmineSnapshot, item.uuid)
    if not snap or journal_id is None:
        return None
    for j in json.loads(snap.raw_json).get("journals", []):
        if j.get("id") == journal_id:
            return j.get("user", {}).get("name")
    return None


def is_open(item: Item) -> bool:
    return item.deleted_at is None and item.status not in CLOSED_STATUSES
