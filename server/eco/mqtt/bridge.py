"""Tablet <-> server over MQTT (contract: CentralEcosystemPlan.md §4).

`Ingestor` is broker-agnostic (topic + bytes in, list of (topic, bytes) out) so it
can be unit-tested; `MqttBridge` wires it to paho-mqtt.
"""
from __future__ import annotations

import json
import logging
import re
import uuid as uuidlib
import threading
from datetime import date, datetime, timezone

import paho.mqtt.client as mqtt
from sqlalchemy import select
from sqlalchemy.orm import Session

from eco import items as svc
from eco.config import Settings
from eco.db import Databases
from eco.models import Category, Device, IngestLog, Item, User, utcnow
from eco.mqtt.pad import PadAdapter, PadListAdapter, COMMANDS, PRESENCE, STATE

log = logging.getLogger(__name__)

UP_TOPIC = re.compile(r"^eco/user/(\d+)/device/([^/]+)/up$")
UPDATABLE = {"title", "description", "status", "priority", "due_date"}


def down_topic(user_id: int, device_id: str) -> str:
    return f"eco/user/{user_id}/device/{device_id}/down"


def envelope(type_: str, body: dict) -> bytes:
    return json.dumps({"msg_uuid": str(uuidlib.uuid4()), "type": type_,
                       "ts": datetime.now(timezone.utc).isoformat(), "body": body}).encode()


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


class Ingestor:
    def __init__(self, dbs: Databases, settings: Settings):
        self.dbs = dbs
        self.settings = settings

    # --- public -------------------------------------------------------------
    def handle(self, topic: str, payload: bytes) -> list[tuple[str, bytes]]:
        m = UP_TOPIC.match(topic)
        if not m:
            return []
        user_id, device_id = int(m.group(1)), m.group(2)
        down = down_topic(user_id, device_id)
        try:
            msg = json.loads(payload)
            msg_uuid, type_, body = msg["msg_uuid"], msg["type"], msg.get("body") or {}
        except (ValueError, KeyError, TypeError):
            log.warning("bad envelope on %s: %r", topic, payload[:200])
            return []

        with self.dbs.session("work") as w:
            if w.get(User, user_id) is None:
                log.warning("message for unknown user %s", user_id)
                return []
            dev = w.get(Device, device_id) or Device(id=device_id, user_id=user_id)
            dev.last_seen_at = utcnow()
            w.add(dev)
            if w.get(IngestLog, msg_uuid) is not None:
                log.info("duplicate msg %s ignored", msg_uuid)
                return [(down, envelope("ack", {"msg_uuids": [msg_uuid], "duplicate": True}))]

        out: list[tuple[str, bytes]] = []
        error = None
        try:
            if type_ == "item.create":
                self._create(user_id, body, _parse_ts(msg.get("ts")))
            elif type_ == "item.update":
                self._update(user_id, body)
            elif type_ == "item.delete":
                self._delete(user_id, body)
            elif type_ == "snapshot.request":
                out.append((down, envelope("snapshot", self.snapshot(user_id))))
            else:
                error = f"unknown type {type_!r}"
        except (svc.NotFound, svc.Invalid, ValueError, KeyError) as e:
            error = f"{type(e).__name__}: {e}"
            log.warning("msg %s (%s) rejected: %s", msg_uuid, type_, error)

        with self.dbs.session("work") as w:
            w.add(IngestLog(msg_uuid=msg_uuid, device_id=device_id))
        ack = {"msg_uuids": [msg_uuid]}
        if error:
            ack["errors"] = {msg_uuid: error}
        out.insert(0, (down, envelope("ack", ack)))
        return out

    def snapshot(self, user_id: int) -> dict:
        items = []
        for kind in ("work", "personal"):
            with self.dbs.session(kind) as s:
                cats = {c.id: c for c in s.scalars(select(Category).where(Category.user_id == user_id))}
                for it in s.scalars(select(Item).where(Item.user_id == user_id, Item.deleted_at.is_(None))):
                    if not svc.is_open(it):
                        continue
                    c = cats.get(it.category_id)
                    items.append({"uuid": it.uuid, "title": it.title, "status": it.status,
                                  "priority": it.priority, "category": c.name if c else None,
                                  "kind": c.kind if c else None,
                                  "due_date": it.due_date.isoformat() if it.due_date else None,
                                  "needs_triage": it.needs_triage})
        return {"snapshot_id": utcnow().isoformat(), "items": items}

    # --- handlers -----------------------------------------------------------
    def _route(self, user_id: int, ref: str | None) -> tuple[str, int]:
        """Pick (db kind, category id) for a tablet category name/kind."""
        ref = (ref or "personal").strip().lower()
        for kind in ("work", "personal"):
            with self.dbs.session(kind) as s:
                for c in s.scalars(select(Category).where(Category.user_id == user_id)):
                    if c.name.lower() == ref or c.kind == ref:
                        return kind, c.id
        raise svc.NotFound(f"category {ref!r}")

    def _find(self, user_id: int, item_uuid: str) -> str:
        for kind in ("work", "personal"):
            with self.dbs.session(kind) as s:
                it = s.get(Item, item_uuid)
                if it is not None and it.user_id == user_id:
                    return kind
        raise svc.NotFound(item_uuid)

    def _create(self, user_id: int, body: dict, ts: datetime | None) -> None:
        item_uuid = str(uuidlib.UUID(body["uuid"]))
        kind, cat_id = self._route(user_id, body.get("category"))
        with self.dbs.session(kind) as s:
            if s.get(Item, item_uuid) is not None:
                return  # idempotent: already created
            cat = s.get(Category, cat_id)
            due = date.fromisoformat(body["due_date"]) if body.get("due_date") else None
            item = svc.create_item(s, user_id, title=body["title"], category=cat, source="tablet",
                                   due_date=due, item_uuid=item_uuid,
                                   captured_at=_parse_ts(body.get("captured_at")) or ts or utcnow(),
                                   default_deadline_days=self.settings.default_deadline_days)
            if body.get("notes"):
                svc.add_note(s, item, s.get(User, user_id), body["notes"])

    def _update(self, user_id: int, body: dict) -> None:
        kind = self._find(user_id, body["uuid"])
        changes = {k: v for k, v in body.items() if k in UPDATABLE}
        with self.dbs.session(kind) as s:
            svc.update_item(s, svc.get_item(s, user_id, body["uuid"]), changes, reason=body.get("reason"))

    def _delete(self, user_id: int, body: dict) -> None:
        kind = self._find(user_id, body["uuid"])
        with self.dbs.session(kind) as s:
            svc.delete_item(s, svc.get_item(s, user_id, body["uuid"]))


class MqttBridge:
    def __init__(self, dbs: Databases, settings: Settings):
        self.settings = settings
        self.ingestor = Ingestor(dbs, settings)
        self.pad = PadAdapter(dbs, settings) if settings.pad_enabled else None
        self.pad_lists = ([PadListAdapter(dbs, settings, name) for name in ("personal", "work")]
                          if settings.pad_enabled else [])
        self._pad_lock = threading.Lock()
        self._stopped = threading.Event()
        self._pad_thread = None
        self._pad_payload = {}
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=f"eco-server-{uuidlib.uuid4().hex[:8]}")
        self.client.username_pw_set(settings.mqtt_username, settings.mqtt_password)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    def start(self) -> None:
        self.client.connect_async(self.settings.mqtt_host, self.settings.mqtt_port, keepalive=30)
        self.client.loop_start()
        if self.pad:
            self._stopped.clear()
            self._pad_thread = threading.Thread(target=self._poll_pad, name="pad-snapshot", daemon=True)
            self._pad_thread.start()

    def stop(self) -> None:
        self._stopped.set()
        if self._pad_thread:
            self._pad_thread.join()
        self.client.loop_stop()
        self.client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT connect failed: %s", reason_code)
            return
        client.subscribe("eco/user/+/device/+/up", qos=1)
        if self.pad:
            topics = [(COMMANDS, 1), (PRESENCE, 1)]
            for adapter in self.pad_lists:
                topics.append((adapter.commands_topic, 1))
            if self.pad_lists:
                topics.append((self.pad_lists[0].presence_topic, 1))
            client.subscribe(topics)
            # Broker persistence may be disabled, so republish on every connection.
            with self._pad_lock:
                self._pad_payload.clear()
        log.info("MQTT connected, subscribed")

    def _on_message(self, client, userdata, msg):
        try:
            v2_by_command = {a.commands_topic: a for a in self.pad_lists}
            v2_by_presence = {a.presence_topic: a for a in self.pad_lists}
            if self.pad and (msg.topic in (COMMANDS, PRESENCE) or
                             msg.topic in v2_by_command or msg.topic in v2_by_presence):
                with self._pad_lock:
                    command_topic = msg.topic in (COMMANDS, *v2_by_command.keys())
                    if command_topic:
                        adapter = v2_by_command.get(msg.topic) or self.pad
                        if msg.retain:
                            return  # never execute retained commands on server startup
                        for topic, payload in adapter.handle(msg.payload):
                            client.publish(topic, payload, qos=1,
                                           retain=topic == STATE or topic.endswith("/state"))
                    else:
                        adapters = self.pad_lists if msg.topic in v2_by_presence else []
                        if msg.topic == PRESENCE:
                            adapters = [self.pad, *self.pad_lists]
                        for adapter in adapters:
                            client.publish(adapter.state_topic, adapter.snapshot(), qos=1, retain=True)
                return
            for topic, payload in self.ingestor.handle(msg.topic, msg.payload):
                client.publish(topic, payload, qos=1)
        except Exception:
            log.exception("MQTT message handling failed on %s", msg.topic)

    def _poll_pad(self):
        while not self._stopped.is_set():
            try:
                if self.client.is_connected():
                    with self._pad_lock:
                        for adapter in [self.pad, *self.pad_lists]:
                            payload = adapter.snapshot()
                            topic = adapter.state_topic
                            if payload != self._pad_payload.get(topic):
                                info = self.client.publish(topic, payload, qos=1, retain=True)
                                if info.rc == mqtt.MQTT_ERR_SUCCESS:
                                    self._pad_payload[topic] = payload
            except Exception:
                log.exception("Pad snapshot publication failed")
            self._stopped.wait(2)
