"""Server -> Redmine: the daily batch push of the outbox, notes, and time entries.

With dry_run=True every payload is logged and returned, nothing is sent,
and nothing is marked pushed.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from eco.models import (
    Category, Item, ItemNote, Outbox, RedmineSnapshot, StatusMap, SyncConflict, SyncState,
    TimeEntry, User, utcnow,
)
from eco.redmine_client import RedmineError, RedmineGateway, local_status_name
from eco.sync.merge import diff_against_base, item_fields, normalize_issue

log = logging.getLogger(__name__)


class PushError(Exception):
    pass


@dataclass
class PushReport:
    dry_run: bool
    payloads: list[dict] = field(default_factory=list)
    sent: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


class _Lookups:
    def __init__(self, s: Session, gw: RedmineGateway):
        self.status_id = {m.local_status: m.redmine_status_id for m in s.scalars(select(StatusMap))}
        self._gw = gw
        self._priorities: dict[str, int] | None = None

    def priority_id(self, name: str) -> int:
        if self._priorities is None:
            self._priorities = {local_status_name(p["name"]): p["id"] for p in self._gw.priorities()}
        if name not in self._priorities:
            raise PushError(f"unknown Redmine priority {name!r}")
        return self._priorities[name]

    def to_redmine(self, fields: dict) -> dict:
        out: dict = {}
        for f, v in fields.items():
            if f == "title":
                out["subject"] = v
            elif f == "description":
                out["description"] = v or ""
            elif f == "status":
                if v not in self.status_id:
                    raise PushError(f"no Redmine status for {v!r}")
                out["status_id"] = self.status_id[v]
            elif f == "priority":
                out["priority_id"] = self.priority_id(v)
            elif f == "due_date":
                out["due_date"] = v
        return out


def _outbox_row(s: Session, item: Item) -> Outbox:
    row = s.scalar(select(Outbox).where(Outbox.item_uuid == item.uuid, Outbox.pushed_at.is_(None)))
    if row is None:
        row = Outbox(item_uuid=item.uuid, payload_json="{}")
        s.add(row)
    return row


def _snapshot(s: Session, item: Item, issue: dict) -> None:
    snap = s.get(RedmineSnapshot, item.uuid) or RedmineSnapshot(item_uuid=item.uuid)
    snap.raw_json = json.dumps(issue)
    snap.redmine_updated_on = issue["updated_on"]
    snap.fetched_at = utcnow()
    s.add(snap)


def _verify(wanted: dict, issue: dict) -> None:
    """Redmine silently ignores disallowed workflow transitions; detect that."""
    got = normalize_issue(issue)
    bad = [f"{f} (wanted {v!r}, Redmine has {got.get(f)!r})" for f, v in wanted.items() if got.get(f) != v]
    if bad:
        raise PushError("Redmine did not apply: " + "; ".join(bad) +
                        " — likely a workflow-blocked transition or missing permission")


def push_user(s: Session, user: User, gw: RedmineGateway, *, dry_run: bool) -> PushReport:
    rep = PushReport(dry_run=dry_run)
    lk = _Lookups(s, gw)
    me_id = gw.me()["id"]
    synced_cats = select(Category.id).where(Category.user_id == user.id, Category.syncs_redmine.is_(True))
    items = s.scalars(select(Item).where(Item.user_id == user.id, Item.category_id.in_(synced_cats))).all()

    for item in items:
        if item.needs_triage:
            continue  # never push untriaged items
        # 1) create issues for triaged server/tablet items
        if item.redmine_issue_id is None:
            if item.deleted_at is not None:
                item.dirty = False
                continue
            payload = {"op": "create", "item": item.uuid, "issue": {
                "project_id": item.redmine_project_id, "tracker_id": item.tracker_id,
                "assigned_to_id": me_id, **lk.to_redmine({
                    "title": item.title, "description": item.description,
                    "priority": item.priority, "due_date": item_fields(item)["due_date"]})}}
            _send(s, item, rep, payload, dry_run, lambda: _do_create(s, gw, item, payload))
            if item.redmine_issue_id is None:
                continue
        # 2) field updates
        if item.dirty:
            snap = s.get(RedmineSnapshot, item.uuid)
            base = normalize_issue(json.loads(snap.raw_json))
            pending = set(s.scalars(select(SyncConflict.field).where(
                SyncConflict.item_uuid == item.uuid, SyncConflict.status == "pending")))
            changes = diff_against_base(base, item_fields(item), skip=pending)
            if changes:
                payload = {"op": "update", "item": item.uuid, "issue_id": item.redmine_issue_id,
                           "fields": changes, "issue": lk.to_redmine(changes)}
                _send(s, item, rep, payload, dry_run, lambda: _do_update(s, gw, item, payload))
            elif not pending:
                item.dirty = False
                row = s.scalar(select(Outbox).where(Outbox.item_uuid == item.uuid,
                                                    Outbox.pushed_at.is_(None)))
                if row is not None and not dry_run:
                    s.delete(row)

        # 3) notes -> journals
        for note in s.scalars(select(ItemNote).where(ItemNote.item_uuid == item.uuid,
                                                     ItemNote.pushed_at.is_(None))):
            payload = {"op": "note", "item": item.uuid, "issue_id": item.redmine_issue_id,
                       "note_id": note.id, "notes": note.body, "private": note.private}
            _send_child(rep, note, payload, dry_run,
                        lambda n=note: setattr(n, "redmine_journal_id",
                                               gw.add_note(item.redmine_issue_id, n.body, n.private)))
        # 4) hours -> time entries
        for te in s.scalars(select(TimeEntry).where(TimeEntry.item_uuid == item.uuid,
                                                    TimeEntry.pushed_at.is_(None))):
            payload = {"op": "time_entry", "item": item.uuid, "issue_id": item.redmine_issue_id,
                       "time_entry_id": te.id, "hours": te.hours, "spent_on": te.spent_on.isoformat(),
                       "activity_id": te.activity_id, "comments": te.comment}
            _send_child(rep, te, payload, dry_run,
                        lambda t=te: setattr(t, "redmine_time_entry_id", gw.create_time_entry(
                            item.redmine_issue_id, t.hours, t.spent_on.isoformat(),
                            t.activity_id, t.comment)))

    if not dry_run:
        state = s.get(SyncState, user.id) or SyncState(user_id=user.id)
        state.last_push_at = utcnow()
        s.add(state)
    s.flush()
    log.info("push user=%s dry_run=%s payloads=%d sent=%d errors=%d", user.id, dry_run,
             len(rep.payloads), len(rep.sent), len(rep.errors))
    return rep


def _do_create(s: Session, gw: RedmineGateway, item: Item, payload: dict) -> None:
    created = gw.create_issue(payload["issue"])
    item.redmine_issue_id = created["id"]
    issue = gw.get_issue(created["id"])
    _snapshot(s, item, issue)
    # New issues start in the default status; anything else goes out as an update.
    item.dirty = item_fields(item)["status"] != normalize_issue(issue)["status"]


def _do_update(s: Session, gw: RedmineGateway, item: Item, payload: dict) -> None:
    gw.update_issue(item.redmine_issue_id, payload["issue"])
    issue = gw.get_issue(item.redmine_issue_id)
    _snapshot(s, item, issue)  # base = what Redmine actually has now
    _verify(payload["fields"], issue)
    item.dirty = False


def _send(s: Session, item: Item, rep: PushReport, payload: dict, dry_run: bool, action) -> bool:
    row = _outbox_row(s, item)
    row.payload_json = json.dumps(payload)
    rep.payloads.append(payload)
    if dry_run:
        log.info("DRY-RUN outbox %s", row.payload_json)
        return False
    try:
        action()
    except (RedmineError, PushError) as e:
        row.error = f"{type(e).__name__}: {e}"
        rep.errors.append({**payload, "error": row.error})
        log.warning("push failed item=%s: %s", item.uuid, row.error)
        return False
    row.pushed_at, row.error = utcnow(), None
    rep.sent.append(payload)
    return True


def _send_child(rep: PushReport, row, payload: dict, dry_run: bool, action) -> None:
    rep.payloads.append(payload)
    if dry_run:
        log.info("DRY-RUN %s", json.dumps(payload))
        return
    try:
        action()
    except RedmineError as e:
        row.error = f"{type(e).__name__}: {e}"
        rep.errors.append({**payload, "error": row.error})
        return
    row.pushed_at, row.error = utcnow(), None
    rep.sent.append(payload)
