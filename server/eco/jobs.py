"""Scheduled jobs (CentralEcosystemPlan.md §2). Cron strings come from settings
so tests can make them fire within minutes."""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from eco import backup, deadlines, notify
from eco.config import Settings, redmine_key_for
from eco.db import Databases
from eco.models import CalendarDay, User
from eco.redmine_client import RedmineGateway
from eco.sync.outbox import PushReport, push_user
from eco.sync.poller import PollReport, poll_user

log = logging.getLogger(__name__)

# Poll and push both read and write the snapshot/outbox; never let them overlap
# (scheduler threads and API requests share this lock).
SYNC_LOCK = threading.RLock()


def _today(settings: Settings):
    return datetime.now(ZoneInfo(settings.tz)).date()


def _work_users(dbs: Databases) -> list[User]:
    with dbs.session("work") as s:
        return [u for u in s.scalars(select(User)) if redmine_key_for(u.id)]


def poll_one(dbs: Databases, settings: Settings, user_id: int) -> PollReport:
    key = redmine_key_for(user_id)
    if not key:
        return PollReport()
    with SYNC_LOCK, dbs.session("work") as s:
        return poll_user(s, s.get(User, user_id), RedmineGateway(settings.redmine_url, key),
                         today=_today(settings))


def poll_all(dbs: Databases, settings: Settings) -> None:
    for u in _work_users(dbs):
        try:
            poll_one(dbs, settings, u.id)
        except Exception:
            log.exception("poll failed for user %s", u.id)


def push_one(dbs: Databases, settings: Settings, user_id: int, dry_run: bool | None = None) -> PushReport:
    dry = settings.outbox_dry_run if dry_run is None else dry_run
    with SYNC_LOCK, dbs.session("work") as s:
        user = s.get(User, user_id)
        email = user.email
        try:
            rep = push_user(s, user, RedmineGateway(settings.redmine_url, redmine_key_for(user_id)),
                            dry_run=dry)
        except Exception as e:
            log.exception("push crashed for user %s", user_id)
            rep = PushReport(dry_run=dry, errors=[{"op": "push", "error": f"{type(e).__name__}: {e}"}])
    if rep.errors:
        try:
            notify.send_push_failure(settings, email, rep.errors)
        except OSError:
            log.exception("could not send push-failure alert")
    return rep


def push_all(dbs: Databases, settings: Settings) -> None:
    for u in _work_users(dbs):
        push_one(dbs, settings, u.id)


def reminders(dbs: Databases, settings: Settings) -> None:
    today = _today(settings)
    with dbs.session("work") as s:
        users = list(s.scalars(select(User)))
        off = {d.user_id for d in s.scalars(select(CalendarDay).where(CalendarDay.date == today))}
    for u in users:
        if u.id in off:
            log.info("reminder skipped for user %s (calendar day off)", u.id)
            continue
        try:
            notify.send_reminder(settings, u.email)
        except OSError:
            log.exception("reminder failed for user %s", u.id)


def mark_missed(dbs: Databases, settings: Settings) -> None:
    today = _today(settings)
    for kind in ("work", "personal"):
        with dbs.session(kind) as s:
            n = deadlines.mark_missed(s, today)
            log.info("marked %d missed deadlines in %s", n, kind)


def nightly_backup(dbs: Databases, settings: Settings) -> None:
    paths = backup.backup_all(dbs.paths, settings.backup_dir, settings.backup_keep_days,
                              today=_today(settings))
    log.info("backups written: %s", [str(p) for p in paths])


def build_scheduler(dbs: Databases, settings: Settings) -> BackgroundScheduler:
    tz = ZoneInfo(settings.tz)
    sched = BackgroundScheduler(timezone=tz)
    jobs = [
        ("poll", settings.job_poll_cron, poll_all),
        ("reminder", settings.job_reminder_cron, reminders),
        ("push", settings.job_push_cron, push_all),
        ("missed", settings.job_missed_cron, mark_missed),
        ("backup", settings.job_backup_cron, nightly_backup),
    ]
    for name, cron, fn in jobs:
        sched.add_job(fn, CronTrigger.from_crontab(cron, timezone=tz), args=[dbs, settings],
                      id=name, name=name, coalesce=True, max_instances=1, misfire_grace_time=3600)
    return sched
