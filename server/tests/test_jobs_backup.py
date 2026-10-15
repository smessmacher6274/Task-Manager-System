from datetime import date

from sqlalchemy import select

from eco import backup, deadlines
from eco import items as svc
from eco.db import Databases
from eco.models import Category, DeadlineEvent, Item, User


def test_backup_and_restore_round_trip(dbs, settings, tmp_path):
    with dbs.session("personal") as s:
        cat = s.scalar(select(Category))
        svc.create_item(s, 1, title="Keep me", category=cat, source="server")
    paths = backup.backup_all(dbs.paths, settings.backup_dir, 14, today=date(2026, 10, 7))
    assert sorted(p.name for p in paths) == ["personal-20261007.db", "work-20261007.db"]

    with dbs.session("personal") as s:
        for it in s.scalars(select(Item)):
            it.title = "damaged"
    dbs.dispose()
    backup.restore(settings.backup_dir / "personal-20261007.db", settings.personal_db_path)
    d2 = Databases(settings.work_db_path, settings.personal_db_path)
    with d2.session("personal") as s:
        assert [i.title for i in s.scalars(select(Item))] == ["Keep me"]
    d2.dispose()


def test_prune_old_backups(settings):
    settings.backup_dir.mkdir(parents=True)
    for stamp in ("20260901", "20261006"):
        (settings.backup_dir / f"work-{stamp}.db").write_bytes(b"")
    removed = backup.prune(settings.backup_dir, 14, today=date(2026, 10, 7))
    assert [p.name for p in removed] == ["work-20260901.db"]


def test_missed_deadline_logged_once(dbs):
    with dbs.session("personal") as s:
        cat = s.scalar(select(Category))
        it = svc.create_item(s, 1, title="Late", category=cat, source="server", due_date=date(2026, 10, 1))
        svc.create_item(s, 1, title="Fine", category=cat, source="server", due_date=date(2026, 10, 30))
        assert deadlines.mark_missed(s, date(2026, 10, 7)) == 1
        assert deadlines.mark_missed(s, date(2026, 10, 8)) == 0
        evs = s.scalars(select(DeadlineEvent.event).where(DeadlineEvent.item_uuid == it.uuid)).all()
        assert evs == ["set", "missed"]


def test_reminder_skips_calendar_day_off(dbs, settings, monkeypatch):
    from eco import jobs, notify
    from eco.models import CalendarDay
    sent = []
    monkeypatch.setattr(notify, "send_reminder", lambda st, to: sent.append(to))
    jobs.reminders(dbs, settings)
    assert sent == ["me@eco.test"]
    with dbs.session("work") as s:
        s.add(CalendarDay(user_id=1, date=jobs._today(settings), type="pto"))
    sent.clear()
    jobs.reminders(dbs, settings)
    assert sent == []


def test_scheduler_builds_all_jobs(dbs, settings):
    from eco.jobs import build_scheduler
    sched = build_scheduler(dbs, settings)
    assert sorted(j.id for j in sched.get_jobs()) == ["backup", "missed", "poll", "push", "reminder"]
