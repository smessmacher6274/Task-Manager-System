"""Redmine -> server: fetch changed issues and run the three-way merge."""
from __future__ import annotations

import json
import logging
import uuid as uuidlib
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from eco import deadlines
from eco.items import apply_fields, touch_outbox
from eco.models import (
    CLOSED_STATUSES, Category, Item, RedmineSnapshot, SyncConflict, SyncState, User, utcnow,
)
from eco.redmine_client import RedmineGateway, seed_maps
from eco.sync.merge import item_fields, normalize_issue, three_way

log = logging.getLogger(__name__)


@dataclass
class PollReport:
    created: list[str] = field(default_factory=list)
    accepted: dict[str, list[str]] = field(default_factory=dict)
    conflicts: dict[str, list[str]] = field(default_factory=dict)
    skipped: int = 0


def _latest_foreign_journal(issue: dict, me_id: int) -> int | None:
    for j in reversed(issue.get("journals", [])):
        if j.get("user", {}).get("id") != me_id:
            return j["id"]
    return None


def _save_snapshot(s: Session, item: Item, issue: dict) -> None:
    snap = s.get(RedmineSnapshot, item.uuid) or RedmineSnapshot(item_uuid=item.uuid)
    snap.raw_json = json.dumps(issue)
    snap.redmine_updated_on = issue["updated_on"]
    snap.fetched_at = utcnow()
    s.add(snap)


def poll_user(s: Session, user: User, gw: RedmineGateway, today: date | None = None) -> PollReport:
    today = today or date.today()
    report = PollReport()
    started = utcnow()
    seed_maps(s, gw)
    state = s.get(SyncState, user.id) or SyncState(user_id=user.id)
    s.add(state)
    work_cat = s.scalar(select(Category).where(Category.user_id == user.id,
                                               Category.syncs_redmine.is_(True)))
    if work_cat is None:
        raise RuntimeError(f"user {user.id} has no Redmine-synced category")
    me_id = gw.me()["id"]

    for issue_id, updated_on in gw.changed_issue_ids(state.last_poll_at).items():
        item = s.scalar(select(Item).where(Item.redmine_issue_id == issue_id))
        snap = s.get(RedmineSnapshot, item.uuid) if item else None
        if snap and snap.redmine_updated_on == updated_on:
            report.skipped += 1
            continue
        issue = gw.get_issue(issue_id)
        fresh = normalize_issue(issue)

        if item is None:
            if fresh["status"] in CLOSED_STATUSES:
                continue
            item = Item(user_id=user.id, category_id=work_cat.id, source="redmine",
                        title=fresh["title"], description=fresh["description"],
                        status=fresh["status"], priority=fresh["priority"],
                        deadline_origin="redmine", redmine_issue_id=issue_id,
                        redmine_project_id=issue["project"]["id"], tracker_id=issue["tracker"]["id"],
                        captured_at=utcnow(), needs_triage=False, dirty=False,
                        uuid=str(uuidlib.uuid4()))
            s.add(item)
            s.flush()
            if fresh["due_date"]:
                deadlines.change_due(s, item, date.fromisoformat(fresh["due_date"]), origin="redmine")
            _save_snapshot(s, item, issue)
            report.created.append(item.uuid)
            continue

        base = normalize_issue(json.loads(snap.raw_json)) if snap else fresh
        result = three_way(base, item_fields(item), fresh)
        if result.take_redmine:
            apply_fields(s, item, result.take_redmine, origin="redmine", today=today)
            report.accepted[item.uuid] = sorted(result.take_redmine)
        journal_id = _latest_foreign_journal(issue, me_id)
        for c in result.conflicts:
            existing = s.scalar(select(SyncConflict).where(
                SyncConflict.item_uuid == item.uuid, SyncConflict.field == c.field,
                SyncConflict.status == "pending"))
            if existing:  # still unresolved; refresh Redmine's side
                existing.redmine_value, existing.server_value = c.redmine, c.server
                existing.redmine_journal_id = journal_id
            else:
                s.add(SyncConflict(item_uuid=item.uuid, field=c.field, base_value=c.base,
                                   server_value=c.server, redmine_value=c.redmine,
                                   redmine_journal_id=journal_id))
        if result.conflicts:
            report.conflicts[item.uuid] = [c.field for c in result.conflicts]
        if result.server_changed:
            touch_outbox(s, item)
        item.redmine_project_id = issue["project"]["id"]
        item.tracker_id = issue["tracker"]["id"]
        _save_snapshot(s, item, issue)

    state.last_poll_at = started
    s.flush()
    log.info("poll user=%s created=%d accepted=%d conflicts=%d skipped=%d", user.id,
             len(report.created), len(report.accepted), len(report.conflicts), report.skipped)
    return report
