"""FastAPI app: JSON API under /api (used by the web UI and tests)."""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import date

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from eco import auth, items as svc
from eco.config import Settings, get_settings, redmine_key_for
from eco.db import Databases, DbKind
from eco.jobs import build_scheduler, poll_one, push_one
from eco.models import (
    ActivityMap, Category, DeadlineEvent, Item, ItemNote, Outbox, RedmineSnapshot, SyncConflict,
    SyncState, TimeEntry, User,
)
from eco.redmine_client import RedmineGateway

log = logging.getLogger(__name__)


# --- schemas ----------------------------------------------------------------
class LoginIn(BaseModel):
    email: str
    password: str


class ItemIn(BaseModel):
    title: str
    category: str  # category key, e.g. "work:1" (ids repeat across the two DBs)
    description: str = ""
    due_date: date | None = None
    priority: str = "normal"
    redmine_project_id: int | None = None
    tracker_id: int | None = None


class ItemPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    due_date: date | None = None
    reason: str | None = None  # required when extending a deadline


class TriageIn(BaseModel):
    redmine_project_id: int
    tracker_id: int


class NoteIn(BaseModel):
    body: str
    private: bool = False


class TimeIn(BaseModel):
    hours: float = Field(gt=0)
    spent_on: date
    activity_id: int
    comment: str = ""


class ResolveIn(BaseModel):
    choice: str  # keep_server | take_redmine
    reason: str | None = None


# --- serializers --------------------------------------------------------------
def cat_key(db: str, category_id: int) -> str:
    """Categories are numbered per database, so they are identified as "<db>:<id>"."""
    return f"{db}:{category_id}"


def parse_cat_key(key: str) -> tuple[str, int]:
    db, _, cid = key.partition(":")
    if db not in ("work", "personal") or not cid.isdigit():
        raise HTTPException(422, f"bad category key {key!r}")
    return db, int(cid)


def item_out(it: Item, cats: dict[int, Category], db: str, conflicts: int = 0) -> dict:
    c = cats.get(it.category_id)
    return {
        "uuid": it.uuid, "title": it.title, "description": it.description, "status": it.status,
        "priority": it.priority, "due_date": it.due_date.isoformat() if it.due_date else None,
        "deadline_origin": it.deadline_origin, "source": it.source,
        "category_id": it.category_id, "category_key": cat_key(db, it.category_id), "category": c.name if c else None, "kind": c.kind if c else None,
        "captured_at": _utc(it.captured_at), "updated_at": _utc(it.updated_at),
        "deleted_at": _utc(it.deleted_at),
        "redmine_issue_id": it.redmine_issue_id, "redmine_project_id": it.redmine_project_id,
        "tracker_id": it.tracker_id, "needs_triage": it.needs_triage, "dirty": it.dirty,
        "open_conflicts": conflicts,
    }


def _iso(d):
    return d.isoformat() if d else None


def _utc(dt):
    """Naive-UTC datetime -> ISO string the browser parses as UTC."""
    return dt.isoformat() + "Z" if dt else None


def create_app(settings: Settings | None = None, dbs: Databases | None = None,
               background: bool = True) -> FastAPI:
    settings = settings or get_settings()
    dbs = dbs or Databases(settings.work_db_path, settings.personal_db_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sched = bridge = None
        if background and settings.scheduler_enabled:
            sched = build_scheduler(dbs, settings)
            sched.start()
        if background and settings.mqtt_enabled:
            from eco.mqtt.bridge import MqttBridge
            bridge = MqttBridge(dbs, settings)
            bridge.start()
        yield
        if bridge:
            bridge.stop()
        if sched:
            sched.shutdown(wait=False)

    app = FastAPI(title="Central Ecosystem", lifespan=lifespan)
    app.state.dbs, app.state.settings = dbs, settings

    # --- auth -----------------------------------------------------------------
    def current_user(request: Request) -> User:
        with dbs.session("work") as s:
            user = auth.user_for_token(s, request.cookies.get(settings.session_cookie))
        if user is None:
            raise HTTPException(401, "not logged in")
        return user

    def locate(user: User, item_uuid: str) -> DbKind:
        for kind in ("work", "personal"):
            with dbs.session(kind) as s:
                it = s.get(Item, item_uuid)
                if it is not None and it.user_id == user.id:
                    return kind
        raise HTTPException(404, "item not found")

    def run_poll(user_id: int):
        try:
            return poll_one(dbs, settings, user_id)
        except Exception:
            log.exception("poll failed")
            raise

    @app.post("/api/auth/login")
    def login(body: LoginIn, response: Response, bg: BackgroundTasks):
        with dbs.session("work") as s:
            user = auth.check_login(s, body.email, body.password)
            if user is None:
                raise HTTPException(401, "wrong email or password")
            token = auth.new_session(s, user, settings.session_days)
        response.set_cookie(settings.session_cookie, token, httponly=True, samesite="lax",
                            max_age=settings.session_days * 86400)
        if redmine_key_for(user.id):
            bg.add_task(run_poll, user.id)  # poll on login
        return {"id": user.id, "name": user.name, "email": user.email}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        with dbs.session("work") as s:
            auth.end_session(s, request.cookies.get(settings.session_cookie))
        response.delete_cookie(settings.session_cookie)
        return {"ok": True}

    @app.get("/api/auth/me")
    def me(user: User = Depends(current_user)):
        return {"id": user.id, "name": user.name, "email": user.email,
                "has_redmine": bool(redmine_key_for(user.id)), "dry_run": settings.outbox_dry_run}

    # --- categories & items ---------------------------------------------------------
    def _cats(s, user_id) -> dict[int, Category]:
        return {c.id: c for c in s.scalars(select(Category).where(Category.user_id == user_id))}

    @app.get("/api/categories")
    def categories(user: User = Depends(current_user)):
        out = []
        for kind in ("work", "personal"):
            with dbs.session(kind) as s:
                out += [{"key": cat_key(kind, c.id), "id": c.id, "name": c.name, "kind": c.kind, "db": kind,
                         "syncs_redmine": c.syncs_redmine} for c in _cats(s, user.id).values()]
        return out

    @app.get("/api/items")
    def list_items(category: str | None = None, db: DbKind | None = None,
                   include_closed: bool = False, user: User = Depends(current_user)):
        out = []
        category_id = None
        if category is not None:
            db, category_id = parse_cat_key(category)
        for kind in ("work", "personal"):
            if db and db != kind:
                continue
            with dbs.session(kind) as s:
                cats = _cats(s, user.id)
                q = select(Item).where(Item.user_id == user.id, Item.deleted_at.is_(None))
                if category_id is not None:
                    q = q.where(Item.category_id == category_id)
                pending = {}
                for c in s.scalars(select(SyncConflict).where(SyncConflict.status == "pending")):
                    pending[c.item_uuid] = pending.get(c.item_uuid, 0) + 1
                for it in s.scalars(q.order_by(Item.due_date.is_(None), Item.due_date)):
                    if include_closed or svc.is_open(it):
                        out.append(item_out(it, cats, kind, pending.get(it.uuid, 0)))
        return out

    @app.post("/api/items", status_code=201)
    def create(body: ItemIn, user: User = Depends(current_user)):
        kind, category_id = parse_cat_key(body.category)
        with dbs.session(kind) as s:
            cat = s.get(Category, category_id)
            if cat is not None and cat.user_id == user.id:
                try:
                    it = svc.create_item(s, user.id, title=body.title, category=cat, source="server",
                                         description=body.description, due_date=body.due_date,
                                         priority=body.priority,
                                         redmine_project_id=body.redmine_project_id,
                                         tracker_id=body.tracker_id,
                                         default_deadline_days=settings.default_deadline_days)
                except svc.Invalid as e:
                    raise HTTPException(422, str(e))
                return item_out(it, _cats(s, user.id), kind)
        raise HTTPException(404, "category not found")

    @app.get("/api/items/{item_uuid}")
    def get_item(item_uuid: str, user: User = Depends(current_user)):
        kind = locate(user, item_uuid)
        with dbs.session(kind) as s:
            return item_out(s.get(Item, item_uuid), _cats(s, user.id), kind)

    def _mutate(user: User, item_uuid: str, fn):
        kind = locate(user, item_uuid)
        with dbs.session(kind) as s:
            it = s.get(Item, item_uuid)
            try:
                fn(s, it)
            except (svc.Invalid, ValueError) as e:
                raise HTTPException(422, str(e))
            s.flush()
            return item_out(it, _cats(s, user.id), kind)

    @app.patch("/api/items/{item_uuid}")
    def patch_item(item_uuid: str, body: ItemPatch, user: User = Depends(current_user)):
        changes = body.model_dump(exclude_unset=True)
        reason = changes.pop("reason", None)
        return _mutate(user, item_uuid, lambda s, it: svc.update_item(s, it, changes, reason=reason))

    @app.post("/api/items/{item_uuid}/done")
    def done(item_uuid: str, user: User = Depends(current_user)):
        return _mutate(user, item_uuid, lambda s, it: svc.complete_item(s, it))

    @app.delete("/api/items/{item_uuid}")
    def delete(item_uuid: str, user: User = Depends(current_user)):
        return _mutate(user, item_uuid, lambda s, it: svc.delete_item(s, it))

    @app.post("/api/items/{item_uuid}/triage")
    def triage(item_uuid: str, body: TriageIn, user: User = Depends(current_user)):
        return _mutate(user, item_uuid, lambda s, it: svc.set_triage(
            s, it, redmine_project_id=body.redmine_project_id, tracker_id=body.tracker_id))

    @app.get("/api/triage")
    def triage_queue(user: User = Depends(current_user)):
        with dbs.session("work") as s:
            cats = _cats(s, user.id)
            return [item_out(it, cats, "work") for it in s.scalars(select(Item).where(
                Item.user_id == user.id, Item.needs_triage.is_(True), Item.deleted_at.is_(None)))]

    # --- notes, journals, hours, deadlines ---------------------------------------------
    @app.get("/api/items/{item_uuid}/thread")
    def thread(item_uuid: str, user: User = Depends(current_user)):
        """Local notes plus Redmine journals by other people, oldest first."""
        with dbs.session(locate(user, item_uuid)) as s:
            notes = s.scalars(select(ItemNote).where(ItemNote.item_uuid == item_uuid)).all()
            mine = {n.redmine_journal_id for n in notes if n.redmine_journal_id}
            out = [{"kind": "note", "id": n.id, "author": n.author, "body": n.body,
                    "private": n.private, "at": n.created_at.isoformat() + "Z",
                    "pushed": n.pushed_at is not None, "error": n.error,
                    "redmine_journal_id": n.redmine_journal_id} for n in notes]
            snap = s.get(RedmineSnapshot, item_uuid)
            if snap:
                for j in json.loads(snap.raw_json).get("journals", []):
                    if j["id"] in mine or not (j.get("notes") or "").strip():
                        continue
                    out.append({"kind": "journal", "id": j["id"], "author": j["user"]["name"],
                                "body": j["notes"], "private": j.get("private_notes", False),
                                "at": j["created_on"], "pushed": True, "error": None,
                                "redmine_journal_id": j["id"]})
            return sorted(out, key=lambda e: e["at"])

    @app.post("/api/items/{item_uuid}/notes", status_code=201)
    def add_note(item_uuid: str, body: NoteIn, user: User = Depends(current_user)):
        with dbs.session(locate(user, item_uuid)) as s:
            try:
                n = svc.add_note(s, s.get(Item, item_uuid), user, body.body, body.private)
            except svc.Invalid as e:
                raise HTTPException(422, str(e))
            return {"id": n.id}

    @app.get("/api/items/{item_uuid}/time_entries")
    def time_entries(item_uuid: str, user: User = Depends(current_user)):
        with dbs.session(locate(user, item_uuid)) as s:
            return [{"id": t.id, "hours": t.hours, "spent_on": t.spent_on.isoformat(),
                     "activity_id": t.activity_id, "comment": t.comment,
                     "pushed": t.pushed_at is not None, "error": t.error,
                     "redmine_time_entry_id": t.redmine_time_entry_id}
                    for t in s.scalars(select(TimeEntry).where(TimeEntry.item_uuid == item_uuid))]

    @app.post("/api/items/{item_uuid}/time_entries", status_code=201)
    def add_time(item_uuid: str, body: TimeIn, user: User = Depends(current_user)):
        with dbs.session(locate(user, item_uuid)) as s:
            t = svc.add_time_entry(s, s.get(Item, item_uuid), user, hours=body.hours,
                                   spent_on=body.spent_on, activity_id=body.activity_id,
                                   comment=body.comment)
            return {"id": t.id}

    @app.get("/api/items/{item_uuid}/deadline_events")
    def deadline_events(item_uuid: str, user: User = Depends(current_user)):
        with dbs.session(locate(user, item_uuid)) as s:
            return [{"id": e.id, "event": e.event, "old_due": _iso(e.old_due), "new_due": _iso(e.new_due),
                     "origin": e.origin, "reason": e.reason, "ts": e.ts.isoformat() + "Z"}
                    for e in s.scalars(select(DeadlineEvent).where(DeadlineEvent.item_uuid == item_uuid)
                                       .order_by(DeadlineEvent.id))]

    # --- conflicts (PRs) & outbox -------------------------------------------------------
    @app.get("/api/conflicts")
    def conflicts(status: str = "pending", user: User = Depends(current_user)):
        with dbs.session("work") as s:
            rows = s.execute(select(SyncConflict, Item).join(Item, Item.uuid == SyncConflict.item_uuid)
                             .where(Item.user_id == user.id, SyncConflict.status == status)
                             .order_by(SyncConflict.detected_at)).all()
            return [{"id": c.id, "item_uuid": c.item_uuid, "item_title": it.title,
                     "redmine_issue_id": it.redmine_issue_id, "field": c.field,
                     "base_value": c.base_value, "server_value": c.server_value,
                     "redmine_value": c.redmine_value, "redmine_journal_id": c.redmine_journal_id,
                     "redmine_author": svc._journal_author(s, it, c.redmine_journal_id),
                     "status": c.status, "detected_at": c.detected_at.isoformat() + "Z"}
                    for c, it in rows]

    @app.post("/api/conflicts/{conflict_id}/resolve")
    def resolve(conflict_id: int, body: ResolveIn, user: User = Depends(current_user)):
        with dbs.session("work") as s:
            c = s.get(SyncConflict, conflict_id)
            if c is None or s.get(Item, c.item_uuid).user_id != user.id:
                raise HTTPException(404, "conflict not found")
            try:
                svc.resolve_conflict(s, c, user, body.choice, body.reason)
            except svc.Invalid as e:
                raise HTTPException(422, str(e))
            return {"id": c.id, "status": c.status}

    @app.get("/api/outbox")
    def outbox(user: User = Depends(current_user)):
        with dbs.session("work") as s:
            mine = select(Item.uuid).where(Item.user_id == user.id)
            rows = s.execute(select(Outbox, Item.title).join(Item, Item.uuid == Outbox.item_uuid)
                             .where(Outbox.item_uuid.in_(mine)).order_by(Outbox.id.desc())).all()
            notes = s.execute(select(ItemNote, Item.title).join(Item, Item.uuid == ItemNote.item_uuid)
                              .where(ItemNote.item_uuid.in_(mine), ItemNote.pushed_at.is_(None))).all()
            hours = s.execute(select(TimeEntry, Item.title).join(Item, Item.uuid == TimeEntry.item_uuid)
                              .where(TimeEntry.item_uuid.in_(mine), TimeEntry.pushed_at.is_(None))).all()
            state = s.get(SyncState, user.id)
            return {
                "dry_run": settings.outbox_dry_run,
                "last_poll_at": _utc(state.last_poll_at) if state else None,
                "last_push_at": _utc(state.last_push_at) if state else None,
                "rows": [{"id": o.id, "item_uuid": o.item_uuid, "item_title": t,
                          "payload": json.loads(o.payload_json), "created_at": o.created_at.isoformat() + "Z",
                          "pushed_at": _utc(o.pushed_at), "error": o.error,
                          "state": "pushed" if o.pushed_at else ("error" if o.error else "pending")}
                         for o, t in rows],
                "pending_notes": [{"id": n.id, "item_title": t, "body": n.body, "error": n.error}
                                  for n, t in notes],
                "pending_hours": [{"id": h.id, "item_title": t, "hours": h.hours, "error": h.error}
                                  for h, t in hours],
            }

    @app.post("/api/sync/poll")
    def sync_poll(user: User = Depends(current_user)):
        if not redmine_key_for(user.id):
            raise HTTPException(400, "no Redmine key for this user")
        r = run_poll(user.id)
        return {"created": r.created, "accepted": r.accepted, "conflicts": r.conflicts, "skipped": r.skipped}

    @app.post("/api/sync/push")
    def sync_push(dry_run: bool | None = None, user: User = Depends(current_user)):
        if not redmine_key_for(user.id):
            raise HTTPException(400, "no Redmine key for this user")
        r = push_one(dbs, settings, user.id, dry_run=dry_run)
        return {"dry_run": r.dry_run, "payloads": r.payloads, "sent": len(r.sent), "errors": r.errors}

    # --- Redmine lookups for triage / hours ----------------------------------------------
    @app.get("/api/lookups")
    def lookups(user: User = Depends(current_user)):
        with dbs.session("work") as s:
            activities = [{"id": a.redmine_activity_id, "name": a.name} for a in s.scalars(select(ActivityMap))]
        key = redmine_key_for(user.id)
        if not key:
            return {"projects": [], "trackers": [], "activities": activities}
        gw = RedmineGateway(settings.redmine_url, key)
        return {"projects": [{"id": p["id"], "name": p["name"]} for p in gw.projects()],
                "trackers": [{"id": t["id"], "name": t["name"]} for t in gw.trackers()],
                "activities": activities}

    return app


def _default_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()


# `uvicorn eco.main:app` -- built lazily so importing this module has no side effects.
def __getattr__(name):
    if name == "app":
        global app
        app = _default_app()
        return app
    raise AttributeError(name)
