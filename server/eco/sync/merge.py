"""Three-way merge between the stored Redmine snapshot (base), the server item,
and freshly fetched Redmine data. Pure functions; no DB access."""
from __future__ import annotations

from dataclasses import dataclass, field

from eco.models import Item
from eco.redmine_client import local_status_name

SYNC_FIELDS = ("title", "description", "status", "priority", "due_date")


def normalize_issue(issue: dict) -> dict[str, str | None]:
    """Redmine issue JSON -> comparable field dict (all str or None)."""
    return {
        "title": issue.get("subject") or "",
        "description": (issue.get("description") or "").replace("\r\n", "\n"),
        "status": local_status_name(issue["status"]["name"]),
        "priority": local_status_name(issue["priority"]["name"]),
        "due_date": issue.get("due_date") or None,
    }


def item_fields(item: Item) -> dict[str, str | None]:
    return {
        "title": item.title or "",
        "description": (item.description or "").replace("\r\n", "\n"),
        "status": item.status,
        "priority": item.priority,
        "due_date": item.due_date.isoformat() if item.due_date else None,
    }


@dataclass
class Conflict:
    field: str
    base: str | None
    server: str | None
    redmine: str | None


@dataclass
class MergeResult:
    take_redmine: dict[str, str | None] = field(default_factory=dict)  # auto-accept
    server_changed: set[str] = field(default_factory=set)               # -> outbox
    conflicts: list[Conflict] = field(default_factory=list)             # -> PR


def three_way(base: dict, server: dict, redmine: dict) -> MergeResult:
    res = MergeResult()
    for f in SYNC_FIELDS:
        b, s, r = base.get(f), server.get(f), redmine.get(f)
        if r == b:
            if s != b:
                res.server_changed.add(f)
        elif s == b:
            res.take_redmine[f] = r
        elif s == r:
            pass  # both sides made the same change
        else:
            res.conflicts.append(Conflict(f, b, s, r))
    return res


def diff_against_base(base: dict, server: dict, skip: set[str] = frozenset()) -> dict[str, str | None]:
    """Server-side field changes still to push (excluding fields in `skip`)."""
    return {f: server.get(f) for f in SYNC_FIELDS if f not in skip and server.get(f) != base.get(f)}
