"""Tests for the legacy tablet-001 MQTT protocol adapter."""
from __future__ import annotations

import json
import uuid

from sqlalchemy import func, select

from eco import items as svc
from eco.models import Item, PadReceipt
from eco.mqtt.pad import RESULTS, STATE, PadAdapter
from eco.users import ensure_user


def command(op, **body):
    return {"op": op, **body, "command_id": str(uuid.uuid4())}


def send(adapter, request):
    out = adapter.handle(json.dumps(request, ensure_ascii=False).encode("utf-8"))
    assert [topic for topic, _ in out] == [RESULTS, STATE]
    return json.loads(out[0][1]), json.loads(out[1][1])


def create_request(title="Pad task", *, item_id=None, command_id=None):
    request = command("create", request_id=item_id or str(uuid.uuid4()), title=title)
    if command_id:
        request["command_id"] = command_id
    return request


def make_adapter(dbs, settings):
    settings.pad_user_id = 1
    settings.pad_db = "personal"
    return PadAdapter(dbs, settings)


def test_create_and_complete_then_reopen(dbs, settings):
    adapter = make_adapter(dbs, settings)
    create = create_request("Finish the report")
    result, state = send(adapter, create)
    assert result["status"] == "applied"
    item = next(i for i in state["items"] if i["id"] == create["request_id"])
    assert item["title"] == "Finish the report" and item["completed"] is False

    complete, state = send(adapter, command("update", id=item["id"],
                                            base_revision=item["revision"], completed=True))
    assert complete["status"] == "applied"
    item = next(i for i in state["items"] if i["id"] == item["id"])
    assert item["completed"] is True

    reopen, state = send(adapter, command("update", id=item["id"],
                                         base_revision=item["revision"], completed=False))
    assert reopen["status"] == "applied"
    assert next(i for i in state["items"] if i["id"] == item["id"])["completed"] is False


def test_web_edit_makes_tablet_revision_stale(dbs, settings):
    adapter = make_adapter(dbs, settings)
    create = create_request("Before web edit")
    _, state = send(adapter, create)
    item = next(i for i in state["items"] if i["id"] == create["request_id"])
    old_revision = item["revision"]

    with dbs.session("personal") as s:
        row = s.get(Item, item["id"])
        svc.update_item(s, row, {"title": "Changed on the web"})

    result, state = send(adapter, command("update", id=item["id"],
                                          base_revision=old_revision, completed=True))
    assert result["status"] == "conflict"
    updated = next(i for i in state["items"] if i["id"] == item["id"])
    assert updated["title"] == "Changed on the web" and updated["completed"] is False
    assert updated["revision"] != old_revision


def test_duplicate_receipt_survives_database_reopen(dbs, settings):
    adapter = make_adapter(dbs, settings)
    request = create_request("Exactly once")
    first, _ = send(adapter, request)
    dbs.dispose()

    from eco.db import Databases
    reopened = Databases(settings.work_db_path, settings.personal_db_path)
    try:
        repeated, _ = send(make_adapter(reopened, settings), request)
        assert repeated == first
        with reopened.session("personal") as s:
            assert s.scalar(select(func.count(Item.uuid)).where(Item.uuid == request["request_id"])) == 1
            assert s.scalar(select(func.count(PadReceipt.command_id))) == 1
    finally:
        reopened.dispose()


def test_malformed_commands_are_ignored_or_rejected_without_mutation(dbs, settings):
    adapter = make_adapter(dbs, settings)
    assert adapter.handle(b"not json") == []
    assert adapter.handle(json.dumps({"op": "create", "command_id": "bad"}).encode()) == []

    malformed = command("update", id=str(uuid.uuid4()), base_revision=0, completed="yes")
    result, state = send(adapter, malformed)
    assert result["status"] == "rejected"
    assert state["items"] == []

    unknown = command("rename", id=str(uuid.uuid4()))
    result, state = send(adapter, unknown)
    assert result["status"] == "rejected"
    assert state["items"] == []


def test_pad_titles_obey_utf8_byte_limit(dbs, settings):
    adapter = make_adapter(dbs, settings)
    accepted = create_request("é" * 60)  # exactly 120 UTF-8 bytes
    result, state = send(adapter, accepted)
    assert result["status"] == "applied"
    assert next(i for i in state["items"] if i["id"] == accepted["request_id"])["title"] == "é" * 60

    rejected = create_request("é" * 61)
    result, state = send(adapter, rejected)
    assert result["status"] == "rejected"
    assert all(i["id"] != rejected["request_id"] for i in state["items"])


def test_invalid_surrogate_gets_durable_rejection(dbs, settings):
    adapter = make_adapter(dbs, settings)
    request = create_request("\ud800")
    payload = json.dumps(request).encode()
    first = adapter.handle(payload)
    assert json.loads(first[0][1])["status"] == "rejected"
    assert adapter.handle(payload) == first


def test_noop_completion_can_chain_after_unrelated_changes(dbs, settings):
    adapter = make_adapter(dbs, settings)
    request = create_request()
    _, state = send(adapter, request)
    item = state["items"][0]
    send(adapter, create_request("Unrelated task"))
    result, _ = send(adapter, command("update", id=item["id"],
                                     base_revision=item["revision"], completed=False))
    assert result["status"] == "applied"
    result, state = send(adapter, command("update", id=item["id"],
                                         base_revision=result["revision"], completed=True))
    assert result["status"] == "applied"
    assert next(i for i in state["items"] if i["id"] == item["id"])["completed"]


def test_web_long_titles_fit_firmware_snapshot(dbs, settings):
    adapter = make_adapter(dbs, settings)
    with dbs.session("personal") as s:
        category = svc.category_for(s, 1, "personal", "personal")
        for _ in range(51):
            svc.create_item(s, 1, title="é" * 200, category=category, source="server")
    payload = adapter.snapshot()
    assert len(payload) <= 24576
    state = json.loads(payload)
    assert len(state["items"]) == 50
    assert all(len(it["title"].encode()) <= 120 for it in state["items"])


def test_pad_rejects_creation_after_fifty_items(dbs, settings):
    adapter = make_adapter(dbs, settings)
    for index in range(50):
        result, _ = send(adapter, create_request(f"Task {index + 1}"))
        assert result["status"] == "applied"

    overflow = create_request("Task 51")
    result, state = send(adapter, overflow)
    assert result["status"] == "rejected"
    assert len(state["items"]) == 50
    with dbs.session("personal") as s:
        assert s.scalar(select(func.count(Item.uuid))) == 50


def test_commands_are_scoped_to_configured_user(dbs, settings):
    ensure_user(dbs, user_id=2, name="Other User", email="other@eco.test")
    with dbs.session("personal") as s:
        category = svc.category_for(s, 2, "personal", "personal")
        other_item = svc.create_item(s, 2, title="User two item", category=category, source="server")
        other_uuid = other_item.uuid
    adapter = make_adapter(dbs, settings)
    state = json.loads(adapter.snapshot())
    assert all(i["id"] != other_uuid for i in state["items"])
    update = command("update", id=other_uuid, base_revision=0, completed=True)
    result, state = send(adapter, update)
    assert result["status"] == "rejected"
    assert all(i["id"] != other_uuid for i in state["items"])

    with dbs.session("personal") as s:
        assert s.get(Item, other_uuid).user_id == 2
        assert s.get(Item, other_uuid).status == "new"
