"""Pretend to be the ESP32 tablet over MQTT (contract: CentralEcosystemPlan.md §4).

  python tools\\sim_tablet.py create --title "Call vendor" --category work
  python tools\\sim_tablet.py update --uuid <uuid> --status closed
  python tools\\sim_tablet.py delete --uuid <uuid>
  python tools\\sim_tablet.py reconnect             # snapshot.request, prints items
  python tools\\sim_tablet.py dup --title "Twice"   # same msg_uuid sent twice
  python tools\\sim_tablet.py offline-burst -n 5    # queue locally, flush on connect, drop on ack

Queue file (stand-in for the microSD queue): tools/.sim_queue.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt

QUEUE = Path(__file__).with_name(".sim_queue.json")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def env(msg_type: str, body: dict, msg_uuid: str | None = None) -> dict:
    return {"msg_uuid": msg_uuid or str(uuid.uuid4()), "type": msg_type, "ts": now_iso(), "body": body}


class Tablet:
    def __init__(self, a):
        self.up = f"eco/user/{a.user}/device/{a.device}/up"
        self.down = f"eco/user/{a.user}/device/{a.device}/down"
        self.acked: set[str] = set()
        self.snapshots: list[dict] = []
        self.received: list[dict] = []
        self._cv = threading.Condition()
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sim-{a.device}")
        self.c.username_pw_set(a.device, a.password)
        self.c.on_message = self._on_message
        self.c.connect(a.host, a.port, keepalive=30)
        self.c.subscribe(self.down, qos=1)
        self.c.loop_start()
        time.sleep(0.3)

    def _on_message(self, client, userdata, m):
        msg = json.loads(m.payload)
        with self._cv:
            self.received.append(msg)
            if msg["type"] == "ack":
                self.acked.update(msg["body"]["msg_uuids"])
                if msg["body"].get("errors"):
                    print("server rejected:", msg["body"]["errors"])
            elif msg["type"] == "snapshot":
                self.snapshots.append(msg["body"])
            self._cv.notify_all()

    def send(self, message: dict, wait: float = 5.0) -> bool:
        self.c.publish(self.up, json.dumps(message), qos=1).wait_for_publish(5)
        deadline = time.time() + wait
        with self._cv:
            while message["msg_uuid"] not in self.acked:
                left = deadline - time.time()
                if left <= 0:
                    return False
                self._cv.wait(left)
        return True

    def wait_snapshot(self, wait: float = 5.0) -> dict | None:
        deadline = time.time() + wait
        with self._cv:
            while not self.snapshots and time.time() < deadline:
                self._cv.wait(deadline - time.time())
            return self.snapshots[-1] if self.snapshots else None

    def close(self):
        self.c.loop_stop()
        self.c.disconnect()


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--user", type=int, default=1)
    p.add_argument("--device", default="sim-device-1")
    p.add_argument("--password", default=os.environ.get("SIM_DEVICE_PASSWORD", "sim-device-pass"))
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create"); c.add_argument("--title", required=True)
    c.add_argument("--category", default="personal"); c.add_argument("--due"); c.add_argument("--notes")
    u = sub.add_parser("update"); u.add_argument("--uuid", required=True)
    u.add_argument("--title"); u.add_argument("--status"); u.add_argument("--due"); u.add_argument("--reason")
    d = sub.add_parser("delete"); d.add_argument("--uuid", required=True)
    sub.add_parser("reconnect")
    dp = sub.add_parser("dup"); dp.add_argument("--title", default="duplicate test")
    dp.add_argument("--category", default="personal")
    ob = sub.add_parser("offline-burst"); ob.add_argument("-n", type=int, default=3)
    ob.add_argument("--category", default="personal")
    a = p.parse_args(argv)

    if a.cmd == "offline-burst":
        # "Wi-Fi off": queue to the local file without connecting.
        queue = json.loads(QUEUE.read_text()) if QUEUE.exists() else []
        for i in range(a.n):
            queue.append(env("item.create", {"uuid": str(uuid.uuid4()), "title": f"offline item {i + 1}",
                                             "category": a.category}))
        QUEUE.write_text(json.dumps(queue, indent=1))
        print(f"queued {len(queue)} message(s) offline; reconnecting...")

    t = Tablet(a)
    try:
        # Flush offline queue first; delete from queue only on ack.
        if QUEUE.exists():
            queue = json.loads(QUEUE.read_text())
            remaining = [m for m in queue if not t.send(m)]
            QUEUE.write_text(json.dumps(remaining, indent=1)) if remaining else QUEUE.unlink()
            print(f"flushed {len(queue) - len(remaining)} queued message(s), {len(remaining)} left")

        if a.cmd == "create":
            body = {"uuid": str(uuid.uuid4()), "title": a.title, "category": a.category}
            if a.due: body["due_date"] = a.due
            if a.notes: body["notes"] = a.notes
            ok = t.send(env("item.create", body))
            print(json.dumps({"uuid": body["uuid"], "acked": ok}))
        elif a.cmd == "update":
            body = {"uuid": a.uuid}
            for k, v in (("title", a.title), ("status", a.status), ("due_date", a.due), ("reason", a.reason)):
                if v: body[k] = v
            print(json.dumps({"acked": t.send(env("item.update", body))}))
        elif a.cmd == "delete":
            print(json.dumps({"acked": t.send(env("item.delete", {"uuid": a.uuid}))}))
        elif a.cmd == "dup":
            m = env("item.create", {"uuid": str(uuid.uuid4()), "title": a.title, "category": a.category})
            first, second = t.send(m), None
            t.acked.discard(m["msg_uuid"])
            second = t.send(m)
            print(json.dumps({"uuid": m["body"]["uuid"], "msg_uuid": m["msg_uuid"],
                              "first_ack": first, "second_ack": second}))
        if a.cmd in ("reconnect", "offline-burst"):
            t.c.publish(t.up, json.dumps(env("snapshot.request", {})), qos=1)
            snap = t.wait_snapshot()
            print(json.dumps(snap, indent=1) if snap else "no snapshot received")
            if snap is None:
                return 1
    finally:
        t.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
