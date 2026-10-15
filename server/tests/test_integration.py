"""md §5 step 8 scenarios against the local Docker stack (Redmine, Mosquitto, Mailpit).

  docker compose -f ../infra/docker-compose.yml up -d   (then seed, see README)
  .venv\\Scripts\\python -m pytest -m "redmine or mqtt"

Edits are made in Redmine with the `coworker` key, on the server via eco.items.
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import date
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from eco import items as svc
from eco.jobs import push_one
from eco.models import Category, Item, Outbox, SyncConflict, User
from eco.redmine_client import RedmineGateway
from eco.sync.outbox import push_user
from eco.sync.poller import poll_user

URL = os.environ.get("REDMINE_URL", "http://127.0.0.1:3000")
KEY_ME = os.environ.get("REDMINE_KEY_ME") or os.environ.get("REDMINE_KEY_USER_1")
KEY_CO = os.environ.get("REDMINE_KEY_COWORKER")
MAILPIT = "http://127.0.0.1:8025"


def _up(url):
    try:
        return httpx.get(url, timeout=3).status_code < 500
    except httpx.HTTPError:
        return False


needs_redmine = pytest.mark.skipif(not (KEY_ME and KEY_CO and _up(URL)),
                                   reason="local Redmine not running / keys not in server/.env")


@pytest.fixture
def me():
    return RedmineGateway(URL, KEY_ME)


@pytest.fixture
def co():
    return RedmineGateway(URL, KEY_CO)


@pytest.fixture
def project(me):
    return next(p for p in me.projects() if p["name"] == "Eco Test")


@pytest.fixture
def tracker(me):
    return {t["name"]: t["id"] for t in me.trackers()}


@pytest.fixture
def new_issue(co, me, project, tracker):
    created = []

    def make(subject=None, tracker_name="Support", **extra):
        issue = co.create_issue({"project_id": project["id"], "tracker_id": tracker[tracker_name],
                                 "subject": subject or f"it-{uuid.uuid4().hex[:8]}",
                                 "assigned_to_id": me.me()["id"], **extra})
        created.append(issue["id"])
        time.sleep(1.1)  # Redmine updated_on has 1 s resolution
        return issue["id"]

    yield make


def poll(dbs, gw):
    with dbs.session("work") as s:
        return poll_user(s, s.get(User, 1), gw)


def push(dbs, gw, dry_run=False):
    with dbs.session("work") as s:
        return push_user(s, s.get(User, 1), gw, dry_run=dry_run)


def item_for(dbs, iid) -> Item:
    with dbs.session("work") as s:
        return s.scalar(select(Item).where(Item.redmine_issue_id == iid))


def edit(dbs, iid, fn):
    with dbs.session("work") as s:
        fn(s, s.scalar(select(Item).where(Item.redmine_issue_id == iid)))


def co_edit(co, iid, **fields):
    time.sleep(1.1)
    co.update_issue(iid, fields)


pytestmark = pytest.mark.redmine


@needs_redmine
def test_1_new_ticket_appears(dbs, me, new_issue):
    iid = new_issue(due_date="2026-12-01")
    poll(dbs, me)
    it = item_for(dbs, iid)
    assert it is not None and it.deadline_origin == "redmine" and it.due_date == date(2026, 12, 1)


@needs_redmine
def test_author_id_me_resolves(me, project, tracker, dbs):
    """Issues I authored but that are assigned to someone else are still polled."""
    issue = me.create_issue({"project_id": project["id"], "tracker_id": tracker["Support"],
                             "subject": "authored by me", "assigned_to_id": None})
    poll(dbs, me)
    assert item_for(dbs, issue["id"]) is not None


@needs_redmine
def test_2_redmine_only_edit_auto_accepted(dbs, me, co, new_issue):
    iid = new_issue()
    poll(dbs, me)
    co_edit(co, iid, subject="renamed by coworker")
    poll(dbs, me)
    assert item_for(dbs, iid).title == "renamed by coworker"


@needs_redmine
def test_3_server_only_edit_pushed(dbs, me, new_issue):
    iid = new_issue()
    poll(dbs, me)
    edit(dbs, iid, lambda s, it: svc.update_item(s, it, {"title": "server title"}))
    dry = push(dbs, me, dry_run=True)
    assert dry.payloads and me.get_issue(iid)["subject"] != "server title"
    assert push(dbs, me).errors == []
    assert me.get_issue(iid)["subject"] == "server title"


@needs_redmine
@pytest.mark.parametrize("choice", ["take_redmine", "keep_server"])
def test_4_conflict_both_resolutions(dbs, me, co, new_issue, choice):
    iid = new_issue()
    poll(dbs, me)
    edit(dbs, iid, lambda s, it: svc.update_item(s, it, {"title": "mine"}))
    co_edit(co, iid, subject="theirs", notes="I renamed it")
    poll(dbs, me)
    with dbs.session("work") as s:
        c = s.scalar(select(SyncConflict).where(SyncConflict.status == "pending"))
        assert (c.field, c.server_value, c.redmine_value) == ("title", "mine", "theirs")
        svc.resolve_conflict(s, c, s.get(User, 1), choice, reason="agreed with PM")
    assert push(dbs, me).errors == []
    issue = me.get_issue(iid)
    if choice == "take_redmine":
        assert issue["subject"] == "theirs" and item_for(dbs, iid).title == "theirs"
    else:
        assert issue["subject"] == "mine"
        assert "agreed with PM" in issue["journals"][-1]["notes"]


@needs_redmine
def test_5_delete_rejected_done_closed(dbs, me, new_issue):
    a, b = new_issue(), new_issue()
    poll(dbs, me)
    edit(dbs, a, lambda s, it: svc.delete_item(s, it))
    edit(dbs, b, lambda s, it: svc.complete_item(s, it))
    assert push(dbs, me).errors == []
    assert me.get_issue(a)["status"]["name"] == "Rejected"
    assert me.get_issue(b)["status"]["name"] == "Closed"


@needs_redmine
def test_6_note_and_hours(dbs, me, new_issue):
    iid = new_issue()
    poll(dbs, me)
    activity = me.activities()[0]["id"]
    with dbs.session("work") as s:
        it = s.scalar(select(Item).where(Item.redmine_issue_id == iid))
        svc.add_note(s, it, s.get(User, 1), "note from server")
        svc.add_time_entry(s, it, s.get(User, 1), hours=0.75, spent_on=date.today(), activity_id=activity)
    assert push(dbs, me).errors == []
    issue = me.get_issue(iid)
    assert issue["journals"][-1]["notes"] == "note from server"
    entries = me._req("get", "/time_entries.json", params={"issue_id": iid})["time_entries"]
    assert [e["hours"] for e in entries] == [0.75]


@needs_redmine
def test_7_workflow_blocked_error_and_alert(dbs, settings, me, co, new_issue, monkeypatch):
    iid = new_issue(tracker_name="Bug")
    co_edit(co, iid, status_id=next(s["id"] for s in me.statuses() if s["name"] == "Feedback"))
    poll(dbs, me)
    edit(dbs, iid, lambda s, it: svc.delete_item(s, it))
    monkeypatch.setenv("REDMINE_KEY_USER_1", KEY_ME)
    settings.redmine_url = URL
    mailpit_ok = _up(MAILPIT)
    if mailpit_ok:
        httpx.delete(f"{MAILPIT}/api/v1/messages")
    rep = push_one(dbs, settings, 1, dry_run=False)
    assert rep.errors and "workflow" in rep.errors[0]["error"]
    with dbs.session("work") as s:
        assert s.scalar(select(Outbox).where(Outbox.item_uuid == item_for(dbs, iid).uuid)).error
    if mailpit_ok:
        time.sleep(0.5)
        subjects = [m["Subject"] for m in httpx.get(f"{MAILPIT}/api/v1/messages").json()["messages"]]
        assert "Eco: Redmine push FAILED" in subjects


# --- MQTT --------------------------------------------------------------------------
SIM_PW = os.environ.get("SIM_DEVICE_PASSWORD")
SERVER_PW = os.environ.get("MQTT_PASSWORD")


def _broker_up():
    import socket
    try:
        socket.create_connection(("127.0.0.1", 1883), timeout=2).close()
        return True
    except OSError:
        return False


@pytest.mark.mqtt
@pytest.mark.skipif(not (SIM_PW and SERVER_PW and _broker_up()), reason="Mosquitto not running / creds missing")
def test_8_duplicate_mqtt_message_ignored(dbs, settings):
    from eco.mqtt.bridge import MqttBridge
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    from sim_tablet import Tablet, env

    settings.mqtt_password = SERVER_PW
    bridge = MqttBridge(dbs, settings)
    bridge.start()
    time.sleep(1)
    args = type("A", (), {"user": 1, "device": "sim-device-1", "password": SIM_PW,
                          "host": "127.0.0.1", "port": 1883})
    t = Tablet(args)
    try:
        m = env("item.create", {"uuid": str(uuid.uuid4()), "title": "dup over mqtt", "category": "personal"})
        assert t.send(m)
        t.acked.discard(m["msg_uuid"])
        assert t.send(m)  # re-acked
        with dbs.session("personal") as s:
            assert len(s.scalars(select(Item).where(Item.title == "dup over mqtt")).all()) == 1
        dup_acks = [r for r in t.received if r["type"] == "ack" and r["body"].get("duplicate")]
        assert len(dup_acks) == 1
        t.c.publish(t.up, __import__("json").dumps(env("snapshot.request", {})), qos=1)
        snap = t.wait_snapshot()
        assert any(i["title"] == "dup over mqtt" for i in snap["items"])
    finally:
        t.close()
        bridge.stop()
