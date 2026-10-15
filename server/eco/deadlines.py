"""Append-only deadline_events log. Never update or delete rows here."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from eco.models import DeadlineEvent, Item


class ReasonRequired(ValueError):
    pass


def log_event(s: Session, item: Item, event: str, *, old_due: date | None = None,
              new_due: date | None = None, origin: str | None = None,
              reason: str | None = None) -> DeadlineEvent:
    ev = DeadlineEvent(item_uuid=item.uuid, event=event, old_due=old_due, new_due=new_due,
                       origin=origin or item.deadline_origin, reason=reason)
    s.add(ev)
    return ev


def change_due(s: Session, item: Item, new_due: date | None, *, origin: str,
               reason: str | None = None) -> None:
    """Set/extend a deadline and log it. Self-made extensions require a reason."""
    old = item.due_date
    if old == new_due:
        return
    if old is None:
        event = "set"
    elif new_due is not None and new_due > old:
        event = "extended"
        if origin == "self" and not (reason and reason.strip()):
            raise ReasonRequired("extending a deadline requires a reason")
    else:
        event = "set"
    item.due_date = new_due
    item.deadline_origin = origin
    log_event(s, item, event, old_due=old, new_due=new_due, origin=origin, reason=reason)


def mark_met_if_due(s: Session, item: Item, today: date) -> None:
    if item.due_date is not None and item.due_date >= today:
        log_event(s, item, "met", old_due=item.due_date, new_due=item.due_date)


def mark_missed(s: Session, today: date) -> int:
    """Log `missed` once per (item, due date) for open items past due. Returns count."""
    from eco.models import CLOSED_STATUSES
    n = 0
    items = s.scalars(select(Item).where(Item.deleted_at.is_(None), Item.due_date < today,
                                         Item.status.not_in(CLOSED_STATUSES))).all()
    for item in items:
        already = s.scalar(select(DeadlineEvent.id).where(
            DeadlineEvent.item_uuid == item.uuid, DeadlineEvent.event == "missed",
            DeadlineEvent.old_due == item.due_date))
        if not already:
            log_event(s, item, "missed", old_due=item.due_date, new_due=item.due_date)
            n += 1
    return n
