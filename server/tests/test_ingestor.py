import json
import uuid
from datetime import date

from sqlalchemy import func, select

from eco.models import IngestLog, Item, ItemNote
from eco.mqtt.bridge import Ingestor

UP = "eco/user/1/device/sim-device-1/up"
DOWN = "eco/user/1/device/sim-device-1/down"


def msg(type_, body, msg_uuid=None, ts="2026-10-07T17:00:00-04:00"):
    return json.dumps({"msg_uuid": msg_uuid or str(uuid.uuid4()), "type": type_, "ts": ts, "body": body}).encode()


def decode(out):
    return [(t, json.loads(p)) for t, p in out]


def test_create_work_item_goes_to_triage_in_work_db(dbs, settings):
    ing = Ingestor(dbs, settings)
    item_uuid = str(uuid.uuid4())
    out = decode(ing.handle(UP, msg("item.create", {"uuid": item_uuid, "title": "Call vendor",
                                                     "category": "work", "notes": "re: invoice"})))
    assert out[0][0] == DOWN and out[0][1]["type"] == "ack" and "errors" not in out[0][1]["body"]
    with dbs.session("work") as s:
        it = s.get(Item, item_uuid)
        assert it.needs_triage and it.source == "tablet" and it.due_date is not None
        # offline capture time = envelope ts (converted to UTC)
        assert it.captured_at.isoformat() == "2026-10-07T21:00:00"
        assert s.scalar(select(ItemNote.body)) == "re: invoice"


def test_personal_item_goes_to_personal_db(dbs, settings):
    ing = Ingestor(dbs, settings)
    item_uuid = str(uuid.uuid4())
    ing.handle(UP, msg("item.create", {"uuid": item_uuid, "title": "Groceries", "category": "Personal",
                                        "due_date": "2026-10-09"}))
    with dbs.session("personal") as s:
        it = s.get(Item, item_uuid)
        assert not it.needs_triage and it.due_date == date(2026, 10, 9)
    with dbs.session("work") as s:
        assert s.get(Item, item_uuid) is None


def test_duplicate_message_ignored_but_acked(dbs, settings):
    ing = Ingestor(dbs, settings)
    m = msg("item.create", {"uuid": str(uuid.uuid4()), "title": "Once", "category": "personal"})
    first, second = decode(ing.handle(UP, m)), decode(ing.handle(UP, m))
    assert first[0][1]["type"] == "ack" and second[0][1]["type"] == "ack"
    assert second[0][1]["body"]["duplicate"] is True
    with dbs.session("personal") as s:
        assert s.scalar(select(func.count(Item.uuid))) == 1
    with dbs.session("work") as s:
        assert s.scalar(select(func.count(IngestLog.msg_uuid))) == 1


def test_update_delete_and_snapshot(dbs, settings):
    ing = Ingestor(dbs, settings)
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    ing.handle(UP, msg("item.create", {"uuid": a, "title": "A", "category": "personal"}))
    ing.handle(UP, msg("item.create", {"uuid": b, "title": "B", "category": "work"}))
    ing.handle(UP, msg("item.update", {"uuid": a, "status": "closed"}))
    ing.handle(UP, msg("item.delete", {"uuid": b}))
    out = decode(ing.handle(UP, msg("snapshot.request", {})))
    assert [o[1]["type"] for o in out] == ["ack", "snapshot"]
    assert out[1][1]["body"]["items"] == []  # A closed, B deleted
    with dbs.session("work") as s:
        assert s.get(Item, b).status == "rejected" and s.get(Item, b).deleted_at is not None


def test_bad_update_is_acked_with_error(dbs, settings):
    out = decode(Ingestor(dbs, settings).handle(UP, msg("item.update", {"uuid": str(uuid.uuid4()), "title": "x"})))
    assert out[0][1]["type"] == "ack" and out[0][1]["body"]["errors"]


def test_unknown_topic_and_user_ignored(dbs, settings):
    ing = Ingestor(dbs, settings)
    assert ing.handle("eco/other", b"{}") == []
    assert ing.handle("eco/user/42/device/x/up", msg("snapshot.request", {})) == []
