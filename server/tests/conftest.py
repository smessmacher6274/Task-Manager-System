from __future__ import annotations

import copy
import itertools
from datetime import datetime, timedelta, timezone

import pytest

from eco.config import Settings
from eco.db import Databases
from eco.redmine_client import RedmineError
from eco.users import ensure_user


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(work_db_path=tmp_path / "work.db", personal_db_path=tmp_path / "personal.db",
                    backup_dir=tmp_path / "backups", scheduler_enabled=False, mqtt_enabled=False,
                    outbox_dry_run=False)


@pytest.fixture
def dbs(settings) -> Databases:
    d = Databases(settings.work_db_path, settings.personal_db_path)
    ensure_user(d, user_id=1, name="Me Tester", email="me@eco.test", password="pw")
    yield d
    d.dispose()


STATUSES = [(1, "New", False), (2, "In Progress", False), (3, "Resolved", False),
            (4, "Feedback", False), (5, "Closed", True), (6, "Rejected", True)]
PRIORITIES = [(1, "Low"), (2, "Normal"), (3, "High")]


class FakeRedmine:
    """In-memory stand-in for RedmineGateway, for unit tests."""

    def __init__(self, me_id: int = 10):
        self.me_id = me_id
        self.issues: dict[int, dict] = {}
        self.time_entries: list[dict] = []
        self.blocked: set[tuple[str, str]] = set()  # (from_status, to_status) silently ignored
        self.fail_next: str | None = None
        self._ids = itertools.count(100)
        self._jids = itertools.count(1000)
        self._clock = datetime(2026, 10, 1, tzinfo=timezone.utc)

    # helpers for tests
    def _tick(self) -> str:
        self._clock += timedelta(minutes=1)
        return self._clock.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _status(self, sid):
        n = next(s for s in STATUSES if s[0] == sid)
        return {"id": n[0], "name": n[1], "is_closed": n[2]}

    def seed_issue(self, subject, *, author=None, assigned=None, due=None, status_id=1, description=""):
        iid = next(self._ids)
        self.issues[iid] = {
            "id": iid, "subject": subject, "description": description,
            "project": {"id": 1, "name": "Eco Test"}, "tracker": {"id": 1, "name": "Bug"},
            "status": self._status(status_id), "priority": {"id": 2, "name": "Normal"},
            "author": {"id": author or self.me_id}, "assigned_to": {"id": assigned or self.me_id},
            "due_date": due, "updated_on": self._tick(), "journals": [],
        }
        return iid

    def coworker_edit(self, iid, notes=None, **fields):
        issue = self.issues[iid]
        for k, v in fields.items():
            if k == "status_id":
                issue["status"] = self._status(v)
            elif k == "priority_id":
                issue["priority"] = {"id": v, "name": dict(PRIORITIES)[v]}
            else:
                issue[k] = v
        issue["journals"].append({"id": next(self._jids), "user": {"id": 99, "name": "Co Worker"},
                                  "notes": notes or "", "created_on": self._tick()})
        issue["updated_on"] = self._tick()

    # gateway API
    def me(self):
        return {"id": self.me_id}

    def statuses(self):
        return [{"id": i, "name": n, "is_closed": c} for i, n, c in STATUSES]

    def activities(self):
        return [{"id": 8, "name": "Design", "active": True}, {"id": 9, "name": "Development", "active": True}]

    def priorities(self):
        return [{"id": i, "name": n} for i, n in PRIORITIES]

    def changed_issue_ids(self, since):
        return {i: d["updated_on"] for i, d in self.issues.items()
                if since is not None or not d["status"]["is_closed"]}

    def get_issue(self, iid):
        return copy.deepcopy(self.issues[iid])

    def _maybe_fail(self):
        if self.fail_next:
            msg, self.fail_next = self.fail_next, None
            raise RedmineError(msg)

    def update_issue(self, iid, fields):
        self._maybe_fail()
        issue = self.issues[iid]
        for k, v in fields.items():
            if k == "status_id":
                if (issue["status"]["name"], self._status(v)["name"]) in self.blocked:
                    continue  # Redmine silently ignores disallowed transitions
                issue["status"] = self._status(v)
            elif k == "priority_id":
                issue["priority"] = {"id": v, "name": dict(PRIORITIES)[v]}
            elif k in ("notes", "private_notes"):
                continue
            else:
                issue[k] = v
        if fields.get("notes"):
            issue["journals"].append({"id": next(self._jids), "user": {"id": self.me_id, "name": "Me"},
                                      "notes": fields["notes"], "created_on": self._tick()})
        issue["updated_on"] = self._tick()

    def create_issue(self, fields):
        self._maybe_fail()
        iid = self.seed_issue(fields["subject"], description=fields.get("description", ""),
                              due=fields.get("due_date"))
        return {"id": iid}

    def add_note(self, iid, body, private=False):
        self.update_issue(iid, {"notes": body})
        return self.issues[iid]["journals"][-1]["id"]

    def create_time_entry(self, iid, hours, spent_on, activity_id, comments):
        self._maybe_fail()
        self.time_entries.append({"issue_id": iid, "hours": hours, "spent_on": spent_on,
                                  "activity_id": activity_id, "comments": comments})
        return len(self.time_entries)


@pytest.fixture
def fake() -> FakeRedmine:
    return FakeRedmine()
