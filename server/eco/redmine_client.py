"""Thin Redmine REST wrapper (python-redmine engine, raw JSON dicts in and out)."""
from __future__ import annotations

from datetime import datetime, timedelta

from redminelib import Redmine
from redminelib.exceptions import BaseRedmineError
from sqlalchemy import delete
from sqlalchemy.orm import Session

from eco.models import ActivityMap, StatusMap

__all__ = ["RedmineGateway", "RedmineError", "local_status_name", "seed_maps"]

RedmineError = BaseRedmineError


def local_status_name(redmine_name: str) -> str:
    return redmine_name.strip().lower().replace(" ", "_")


class RedmineGateway:
    def __init__(self, url: str, key: str):
        self.url = url.rstrip("/")
        self._rm = Redmine(self.url, key=key, requests={"timeout": 30})
        self._me: dict | None = None

    def _req(self, method: str, path: str, params: dict | None = None, data: dict | None = None):
        return self._rm.engine.request(method, f"{self.url}{path}", params=params, data=data)

    # --- reads -------------------------------------------------------------
    def me(self) -> dict:
        if self._me is None:
            self._me = self._req("get", "/users/current.json")["user"]
        return self._me

    def _paged(self, path: str, key: str, params: dict) -> list[dict]:
        out, offset = [], 0
        while True:
            page = self._req("get", path, params={**params, "limit": 100, "offset": offset})
            out.extend(page[key])
            offset += len(page[key])
            if not page[key] or offset >= page.get("total_count", 0):
                return out

    def changed_issue_ids(self, since: datetime | None) -> dict[int, str]:
        """Issues assigned to or authored by me, changed since `since` (UTC).

        First poll (since=None) fetches open issues only; later polls use
        status_id=* so closures/rejections made in Redmine are seen too.
        Returns {issue_id: updated_on}, deduped across both queries.
        """
        base: dict = {"sort": "updated_on:desc"}
        if since is None:
            base["status_id"] = "open"
        else:
            base["status_id"] = "*"
            # 2-minute overlap; unchanged issues are skipped by updated_on compare.
            base["updated_on"] = ">=" + (since - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        found: dict[int, str] = {}
        for who in ("assigned_to_id", "author_id"):
            for issue in self._paged("/issues.json", "issues", {**base, who: "me"}):
                found[issue["id"]] = issue["updated_on"]
        return found

    def get_issue(self, issue_id: int) -> dict:
        return self._req("get", f"/issues/{issue_id}.json", params={"include": "journals"})["issue"]

    def statuses(self) -> list[dict]:
        return self._req("get", "/issue_statuses.json")["issue_statuses"]

    def activities(self) -> list[dict]:
        return self._req("get", "/enumerations/time_entry_activities.json")["time_entry_activities"]

    def priorities(self) -> list[dict]:
        return self._req("get", "/enumerations/issue_priorities.json")["issue_priorities"]

    def projects(self) -> list[dict]:
        return self._paged("/projects.json", "projects", {})

    def trackers(self) -> list[dict]:
        return self._req("get", "/trackers.json")["trackers"]

    # --- writes ------------------------------------------------------------
    def update_issue(self, issue_id: int, fields: dict) -> None:
        self._req("put", f"/issues/{issue_id}.json", data={"issue": fields})

    def create_issue(self, fields: dict) -> dict:
        return self._req("post", "/issues.json", data={"issue": fields})["issue"]

    def add_note(self, issue_id: int, body: str, private: bool = False) -> int | None:
        """Adds a journal entry; returns its id (looked up after the PUT)."""
        self.update_issue(issue_id, {"notes": body, "private_notes": private})
        me_id = self.me()["id"]
        for j in reversed(self.get_issue(issue_id).get("journals", [])):
            if j.get("user", {}).get("id") == me_id and j.get("notes") == body:
                return j["id"]
        return None

    def create_time_entry(self, issue_id: int, hours: float, spent_on: str,
                          activity_id: int, comments: str) -> int:
        te = self._req("post", "/time_entries.json", data={"time_entry": {
            "issue_id": issue_id, "hours": hours, "spent_on": spent_on,
            "activity_id": activity_id, "comments": comments,
        }})["time_entry"]
        return te["id"]


def seed_maps(session: Session, gw: RedmineGateway) -> None:
    """Refresh status_map and activity_map from Redmine."""
    session.execute(delete(StatusMap))
    for s in gw.statuses():
        session.add(StatusMap(redmine_status_id=s["id"], local_status=local_status_name(s["name"]),
                              is_closed=bool(s.get("is_closed"))))
    session.execute(delete(ActivityMap))
    for a in gw.activities():
        if a.get("active", True):
            session.add(ActivityMap(redmine_activity_id=a["id"], name=a["name"]))
    session.flush()
