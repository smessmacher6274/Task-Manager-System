"""The md §5 step 8 scenarios against an in-memory Redmine (no Docker needed).
The same scenarios run against real Redmine in test_redmine_integration.py."""
from datetime import date

from sqlalchemy import select

from eco import items as svc
from eco.models import (
    Category, DeadlineEvent, Item, ItemNote, Outbox, SyncConflict, TimeEntry, User,
)
from eco.sync.outbox import push_user
from eco.sync.poller import poll_user


def _poll(dbs, fake):
    with dbs.session("work") as s:
        return poll_user(s, s.get(User, 1), fake)


def _push(dbs, fake, dry_run=False):
    with dbs.session("work") as s:
        return push_user(s, s.get(User, 1), fake, dry_run=dry_run)


def _item(dbs, iid) -> Item:
    with dbs.session("work") as s:
        return s.scalar(select(Item).where(Item.redmine_issue_id == iid))


def _edit(dbs, iid, fn):
    with dbs.session("work") as s:
        it = s.scalar(select(Item).where(Item.redmine_issue_id == iid))
        fn(s, it)


def test_new_ticket_appears(dbs, fake):
    iid = fake.seed_issue("Fix login", due="2026-10-20")
    rep = _poll(dbs, fake)
    it = _item(dbs, iid)
    assert rep.created == [it.uuid]
    assert (it.title, it.source, it.deadline_origin, it.due_date) == ("Fix login", "redmine", "redmine", date(2026, 10, 20))
    with dbs.session("work") as s:
        evs = s.scalars(select(DeadlineEvent).where(DeadlineEvent.item_uuid == it.uuid)).all()
        assert [(e.event, e.origin) for e in evs] == [("set", "redmine")]


def test_closed_ticket_not_imported_on_first_poll(dbs, fake):
    fake.seed_issue("Old", status_id=5)
    assert _poll(dbs, fake).created == []


def test_redmine_only_edit_auto_accepted(dbs, fake):
    iid = fake.seed_issue("T")
    _poll(dbs, fake)
    fake.coworker_edit(iid, subject="T renamed", due_date="2026-11-01")
    rep = _poll(dbs, fake)
    it = _item(dbs, iid)
    assert it.title == "T renamed" and it.due_date == date(2026, 11, 1)
    assert rep.accepted[it.uuid] == ["due_date", "title"]
    assert not it.dirty


def test_server_only_edit_pushed_in_batch(dbs, fake):
    iid = fake.seed_issue("T")
    _poll(dbs, fake)
    _edit(dbs, iid, lambda s, it: svc.update_item(s, it, {"title": "Server title", "priority": "high"}))
    assert _item(dbs, iid).dirty
    # poll in between must not undo it
    _poll(dbs, fake)
    assert fake.issues[iid]["subject"] == "T"
    rep = _push(dbs, fake)
    assert rep.errors == []
    assert fake.issues[iid]["subject"] == "Server title"
    assert fake.issues[iid]["priority"]["name"] == "High"
    assert not _item(dbs, iid).dirty


def test_dry_run_sends_nothing(dbs, fake):
    iid = fake.seed_issue("T")
    _poll(dbs, fake)
    _edit(dbs, iid, lambda s, it: svc.update_item(s, it, {"title": "X"}))
    rep = _push(dbs, fake, dry_run=True)
    assert rep.payloads and rep.payloads[0]["issue"] == {"subject": "X"}
    assert fake.issues[iid]["subject"] == "T"
    assert _item(dbs, iid).dirty
    with dbs.session("work") as s:
        assert s.scalar(select(Outbox)).pushed_at is None


def test_conflict_take_redmine(dbs, fake):
    iid = fake.seed_issue("T")
    _poll(dbs, fake)
    _edit(dbs, iid, lambda s, it: svc.update_item(s, it, {"title": "Mine"}))
    fake.coworker_edit(iid, subject="Theirs")
    rep = _poll(dbs, fake)
    assert list(rep.conflicts.values()) == [["title"]]
    # pending conflict field is not pushed
    _push(dbs, fake)
    assert fake.issues[iid]["subject"] == "Theirs"
    with dbs.session("work") as s:
        c = s.scalar(select(SyncConflict))
        assert (c.base_value, c.server_value, c.redmine_value) == ("T", "Mine", "Theirs")
        svc.resolve_conflict(s, c, s.get(User, 1), "take_redmine")
    assert _item(dbs, iid).title == "Theirs"
    _push(dbs, fake)
    assert fake.issues[iid]["subject"] == "Theirs"
    assert not _item(dbs, iid).dirty


def test_conflict_keep_server_adds_journal_note(dbs, fake):
    iid = fake.seed_issue("T")
    _poll(dbs, fake)
    _edit(dbs, iid, lambda s, it: svc.update_item(s, it, {"title": "Mine"}))
    fake.coworker_edit(iid, subject="Theirs")
    _poll(dbs, fake)
    with dbs.session("work") as s:
        c = s.scalar(select(SyncConflict))
        svc.resolve_conflict(s, c, s.get(User, 1), "keep_server", reason="Scope agreed with PM")
    rep = _push(dbs, fake)
    assert rep.errors == []
    assert fake.issues[iid]["subject"] == "Mine"
    note = fake.issues[iid]["journals"][-1]["notes"]
    assert "Co Worker" in note and "Scope agreed with PM" in note


def test_delete_rejects_and_done_closes(dbs, fake):
    a, b = fake.seed_issue("A", due="2099-01-01"), fake.seed_issue("B")
    _poll(dbs, fake)
    _edit(dbs, a, lambda s, it: svc.complete_item(s, it))
    _edit(dbs, b, lambda s, it: svc.delete_item(s, it))
    assert _push(dbs, fake).errors == []
    assert fake.issues[a]["status"]["name"] == "Closed"
    assert fake.issues[b]["status"]["name"] == "Rejected"
    with dbs.session("work") as s:
        assert s.scalar(select(DeadlineEvent.event).where(DeadlineEvent.event == "met")) == "met"


def test_note_and_hours_pushed(dbs, fake):
    iid = fake.seed_issue("T")
    _poll(dbs, fake)
    with dbs.session("work") as s:
        it = s.scalar(select(Item).where(Item.redmine_issue_id == iid))
        u = s.get(User, 1)
        svc.add_note(s, it, u, "Investigated, root cause found")
        svc.add_time_entry(s, it, u, hours=1.5, spent_on=date(2026, 10, 7), activity_id=9, comment="debug")
    assert _push(dbs, fake).errors == []
    assert fake.issues[iid]["journals"][-1]["notes"] == "Investigated, root cause found"
    assert fake.time_entries == [{"issue_id": iid, "hours": 1.5, "spent_on": "2026-10-07",
                                  "activity_id": 9, "comments": "debug"}]
    with dbs.session("work") as s:
        assert s.scalar(select(ItemNote)).redmine_journal_id is not None
        assert s.scalar(select(TimeEntry)).redmine_time_entry_id == 1
    # nothing re-sent on next push
    assert _push(dbs, fake).payloads == []


def test_workflow_blocked_transition_records_error(dbs, fake):
    iid = fake.seed_issue("T", status_id=4)  # Feedback
    fake.blocked.add(("Feedback", "Rejected"))
    _poll(dbs, fake)
    _edit(dbs, iid, lambda s, it: svc.delete_item(s, it))
    rep = _push(dbs, fake)
    assert len(rep.errors) == 1 and "workflow" in rep.errors[0]["error"]
    with dbs.session("work") as s:
        row = s.scalar(select(Outbox))
        assert row.pushed_at is None and "status" in row.error
    assert _item(dbs, iid).dirty  # retried next push


def test_redmine_api_error_recorded(dbs, fake):
    iid = fake.seed_issue("T")
    _poll(dbs, fake)
    _edit(dbs, iid, lambda s, it: svc.update_item(s, it, {"title": "X"}))
    fake.fail_next = "Status is invalid"
    rep = _push(dbs, fake)
    assert rep.errors and "Status is invalid" in rep.errors[0]["error"]


def test_triage_item_created_in_redmine_only_after_triage(dbs, fake):
    with dbs.session("work") as s:
        cat = s.scalar(select(Category).where(Category.kind == "work"))
        it = svc.create_item(s, 1, title="From tablet", category=cat, source="tablet")
        uuid = it.uuid
        assert it.needs_triage
    assert _push(dbs, fake).payloads == []  # never push untriaged
    with dbs.session("work") as s:
        svc.set_triage(s, s.get(Item, uuid), redmine_project_id=1, tracker_id=1)
    rep = _push(dbs, fake)
    assert rep.errors == [] and rep.payloads[0]["op"] == "create"
    with dbs.session("work") as s:
        iid = s.get(Item, uuid).redmine_issue_id
    assert fake.issues[iid]["subject"] == "From tablet"


def test_deadline_extension_requires_reason(dbs, fake):
    iid = fake.seed_issue("T", due="2026-10-10")
    _poll(dbs, fake)
    import pytest
    with pytest.raises(svc.Invalid):
        _edit(dbs, iid, lambda s, it: svc.update_item(s, it, {"due_date": "2026-10-20"}))
    _edit(dbs, iid, lambda s, it: svc.update_item(s, it, {"due_date": "2026-10-20"}, reason="blocked on vendor"))
    with dbs.session("work") as s:
        ev = s.scalars(select(DeadlineEvent).order_by(DeadlineEvent.id)).all()[-1]
        assert (ev.event, ev.reason, ev.origin) == ("extended", "blocked on vendor", "self")
