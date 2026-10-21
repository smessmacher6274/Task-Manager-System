"""Compatibility with the durable 2040Bluetooth tablet-001 firmware.

The selected database owns both mutations and receipts in one SQLite transaction.
Polling also sees web/Redmine changes; revisions survive server restarts. The
firmware's fixed identity means only one pad/user binding is supported per broker.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy import func, select, text

from eco import items as svc
from eco.models import CLOSED_STATUSES, Item, PadReceipt, PadState, User

PREFIX = "notepad/v1/devices/tablet-001/"
STATE = PREFIX + "state"
COMMANDS = PREFIX + "commands"
RESULTS = PREFIX + "results"
PRESENCE = PREFIX + "presence"


def encode(value):
    # ASCII escaping also allows a malformed surrogate title to receive a durable
    # rejection instead of failing while SQLite stores its original command.
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def wire_title(title):
    # Server titles may be longer than the pad's 120 UTF-8 byte buffer.
    title = "".join(c if ord(c) >= 32 and ord(c) != 127 else " " for c in title)
    return title.encode("utf-8")[:120].decode("utf-8", errors="ignore").strip() or "(untitled)"


def valid_uuid(value):
    return isinstance(value, str) and len(value) == 36 and str(uuid.UUID(value)) == value.lower()


class PadAdapter:
    def __init__(self, dbs, settings, *, list_id="inbox", schema_version=1, prefix=PREFIX):
        self.dbs, self.settings = dbs, settings
        self.user_id = settings.pad_user_id
        self.kind = settings.pad_db if list_id == "inbox" else list_id
        self.list_id, self.schema_version, self.prefix = list_id, schema_version, prefix
        self.state_topic = prefix + "state"
        self.commands_topic = prefix + "commands"
        self.results_topic = prefix + "results"
        self.presence_topic = prefix + "presence"

    def _state(self, s):
        if s.get(User, self.user_id) is None:
            raise svc.NotFound(f"pad user {self.user_id} is not provisioned")
        state = s.get(PadState, self.user_id)
        if state is None:
            state = PadState(user_id=self.user_id, revision=0, ledger_json="{}")
            s.add(state)
        return state

    def _snapshot(self, s, state):
        rows = s.scalars(select(Item).where(Item.user_id == self.user_id).order_by(Item.uuid)).all()
        previous = json.loads(state.ledger_json)
        ledger = {}
        changed = False
        for item in rows:
            # Include all item columns so unseen server edits invalidate stale toggles.
            fingerprint = hashlib.sha256(encode({c.name: str(getattr(item, c.name))
                                                for c in Item.__table__.columns}).encode()).hexdigest()
            old = previous.get(item.uuid)
            revision = old[1] if old and old[0] == fingerprint else state.revision + 1
            ledger[item.uuid] = [fingerprint, revision]
            changed |= old != ledger[item.uuid]
        changed |= ledger.keys() != previous.keys()
        if changed:
            state.revision += 1
            if state.revision > 2147483647:
                raise RuntimeError("pad revision exhausted")
            state.ledger_json = encode(ledger)
        visible = [it for it in rows if it.deleted_at is None]
        # Open items first; keep completed rows when room permits for reopening.
        visible.sort(key=lambda it: (it.status in CLOSED_STATUSES, it.created_at, it.uuid))
        return {"schema_version": self.schema_version, "device_id": "tablet-001", "list_id": self.list_id,
                "revision": state.revision,
                "items": [{"id": it.uuid, "title": wire_title(it.title),
                           "completed": it.status in CLOSED_STATUSES,
                           "revision": ledger[it.uuid][1]}
                          for it in visible[:50]]}

    def snapshot(self):
        with self.dbs.session(self.kind) as s:
            s.execute(text("BEGIN IMMEDIATE"))
            return encode(self._snapshot(s, self._state(s))).encode()

    def handle(self, payload):
        if len(payload) > 4096:
            return []
        try:
            request = json.loads(payload)
            if not isinstance(request, dict) or not valid_uuid(request.get("command_id")):
                return []
        except (ValueError, TypeError, UnicodeError):
            return []
        command_id = request["command_id"]
        # Receipts share a table across adapters in each DB. Keep the legacy
        # encoding for v1 compatibility, while making v2 command IDs a separate
        # namespace so a v1 receipt can never masquerade as a v2 result.
        canonical = encode(request) if self.schema_version == 1 else encode(
            {"schema_version": self.schema_version, "request": request})
        with self.dbs.session(self.kind) as s:
            s.execute(text("BEGIN IMMEDIATE"))
            state = self._state(s)
            snapshot = self._snapshot(s, state)
            receipt = s.get(PadReceipt, (self.user_id, command_id))
            if receipt:
                # Reusing an id with a different body never performs a second mutation.
                result = (json.loads(receipt.result_json) if receipt.request_json == canonical else
                          {"command_id": command_id, "status": "rejected", "revision": state.revision})
            else:
                status = "rejected"
                try:
                    if request.get("op") == "create":
                        item_id, title = request.get("request_id"), request.get("title")
                        if (not valid_uuid(item_id) or not isinstance(title, str) or
                                not title.strip() or len(title.encode()) > 120 or
                                any(ord(c) < 32 or ord(c) == 127 for c in title)):
                            raise svc.Invalid("invalid pad task")
                        # Never adopt/overwrite a UUID belonging to an existing row.
                        if s.get(Item, item_id) is not None:
                            raise svc.Invalid("task UUID already exists")
                        count = s.scalar(select(func.count(Item.uuid)).where(
                            Item.user_id == self.user_id, Item.deleted_at.is_(None)))
                        if count >= 50:
                            raise svc.Invalid("pad list is full; manage tasks on the website")
                        category = svc.category_for(s, self.user_id, self.kind, self.kind)
                        svc.create_item(s, self.user_id, title=title, category=category,
                                        source="tablet", item_uuid=item_id,
                                        default_deadline_days=self.settings.default_deadline_days)
                        status = "applied"
                    elif request.get("op") == "update":
                        item_id = request.get("id")
                        if (not valid_uuid(item_id) or type(request.get("completed")) is not bool or
                                type(request.get("base_revision")) is not int or request["base_revision"] < 0):
                            raise svc.Invalid("invalid pad update")
                        item = svc.get_item(s, self.user_id, item_id)
                        revision = json.loads(state.ledger_json)[item_id][1]
                        if item.deleted_at is not None or request["base_revision"] != revision:
                            status = "conflict"
                        else:
                            completed = item.status in CLOSED_STATUSES
                            if completed != request["completed"]:
                                svc.update_item(s, item, {"status": "closed" if request["completed"] else "new"})
                            status = "applied"
                except (svc.NotFound, svc.Invalid, ValueError, TypeError):
                    status = "rejected"
                s.flush()
                snapshot = self._snapshot(s, state)
                revision = state.revision
                result = {"command_id": command_id, "status": status, "revision": revision}
                if status == "applied":
                    # V1 used item revision in `revision`; v2 keeps snapshot and item
                    # revisions separate so unrelated list changes cannot confuse either.
                    item_revision = json.loads(state.ledger_json)[item_id][1]
                    if self.schema_version == 1:
                        result["revision"] = item_revision
                    else:
                        result["item_revision"] = item_revision
                s.add(PadReceipt(user_id=self.user_id, command_id=command_id,
                                 request_json=canonical, result_json=encode(result)))
            output = [(self.results_topic, encode(result).encode()),
                      (self.state_topic, encode(snapshot).encode())]
        # Session commits before the caller can publish a receipt.
        return output


V2_PREFIX = "notepad/v2/devices/tablet-001/"


class PadListAdapter(PadAdapter):
    """One v2 list backed by its matching database for the fixed tablet user."""
    def __init__(self, dbs, settings, list_id):
        if list_id not in ("personal", "work"):
            raise ValueError("list_id must be personal or work")
        super().__init__(dbs, settings, list_id=list_id, schema_version=2,
                         prefix=V2_PREFIX + "lists/" + list_id + "/")
        self.presence_topic = V2_PREFIX + "presence"
