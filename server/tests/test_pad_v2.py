"""The v2 tablet exposes isolated work and personal lists."""
import json
import uuid

from eco import items as svc
from eco.models import Item
from eco.mqtt.pad import PadAdapter, PadListAdapter


def request(op, **body):
    return {"op": op, "command_id": str(uuid.uuid4()), **body}


def send(adapter, value):
    output = adapter.handle(json.dumps(value).encode())
    assert len(output) == 2
    return json.loads(output[0][1]), json.loads(output[1][1])


def adapters(dbs, settings):
    settings.pad_user_id = 1
    # v2's two lists always bind to their named DB; PAD_DB is legacy-only.
    settings.pad_db = "work"
    return (PadListAdapter(dbs, settings, "personal"),
            PadListAdapter(dbs, settings, "work"))


def test_dual_lists_are_isolated(dbs, settings):
    personal, work = adapters(dbs, settings)
    p_id, w_id = str(uuid.uuid4()), str(uuid.uuid4())
    pr, ps = send(personal, request("create", request_id=p_id, title="Personal"))
    wr, ws = send(work, request("create", request_id=w_id, title="Work"))
    assert pr["status"] == wr["status"] == "applied"
    assert ps["list_id"] == "personal" and ws["list_id"] == "work"
    assert [i["id"] for i in ps["items"]] == [p_id]
    assert [i["id"] for i in ws["items"]] == [w_id]
    assert ps["revision"] == ws["revision"] == 1
    assert ps["items"][0]["revision"] == ws["items"][0]["revision"] == 1


def test_v2_result_keeps_snapshot_and_item_revisions_separate(dbs, settings):
    personal, _ = adapters(dbs, settings)
    first_id, other_id = str(uuid.uuid4()), str(uuid.uuid4())
    _, state = send(personal, request("create", request_id=first_id, title="First"))
    item = state["items"][0]
    _, state = send(personal, request("create", request_id=other_id, title="Other"))
    assert state["revision"] == 2
    noop = request("update", id=first_id, base_revision=item["revision"], completed=False)
    noop_result, _ = send(personal, noop)
    assert noop_result["revision"] == 2 and noop_result["item_revision"] == 1
    assert send(personal, noop)[0] == noop_result
    result, _ = send(personal, request("update", id=first_id,
                                       base_revision=item["revision"], completed=True))
    assert result["status"] == "applied"
    assert result["revision"] == 3
    assert result["item_revision"] == 3
    result, state = send(personal, request("update", id=first_id,
                                          base_revision=result["item_revision"], completed=False))
    assert result["status"] == "applied"
    assert next(row for row in state["items"] if row["id"] == first_id)["completed"] is False


def test_v1_receipt_cannot_be_replayed_as_v2(dbs, settings):
    settings.pad_user_id = 1
    settings.pad_db = "personal"
    legacy = PadAdapter(dbs, settings)
    v2 = PadListAdapter(dbs, settings, "personal")
    task_id, command_id = str(uuid.uuid4()), str(uuid.uuid4())
    command = {"op": "create", "request_id": task_id, "title": "One protocol only",
               "command_id": command_id}
    legacy_result, _ = send(legacy, command)
    v2_result, state = send(v2, command)
    assert legacy_result["status"] == "applied"
    assert v2_result["status"] == "rejected"
    assert "item_revision" not in v2_result
    assert sum(row["id"] == task_id for row in state["items"]) == 1


def test_work_create_enters_triage(dbs, settings):
    _, work = adapters(dbs, settings)
    item_id = str(uuid.uuid4())
    result, state = send(work, request("create", request_id=item_id, title="Untriaged work"))
    assert result["status"] == "applied"
    with dbs.session("work") as s:
        item = s.get(Item, item_id)
        assert item is not None and item.needs_triage
        assert svc.is_open(item)
    assert state["list_id"] == "work" and state["items"][0]["id"] == item_id


def test_bridge_routes_both_lists_and_ignores_retained_commands(dbs, settings):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from eco.mqtt.bridge import MqttBridge
    from eco.mqtt.pad import V2_PREFIX

    settings.pad_enabled = True
    bridge = MqttBridge(dbs, settings)
    client = Mock()
    bridge._on_message(client, None, SimpleNamespace(topic=V2_PREFIX + "presence", payload=b'{}'))
    assert {call.args[0] for call in client.publish.call_args_list} == {
        V2_PREFIX + "lists/personal/state", V2_PREFIX + "lists/work/state"}
    assert all(call.kwargs["retain"] for call in client.publish.call_args_list)
    client.reset_mock()
    adapter = bridge.pad_lists[1]
    command = request("create", request_id=str(uuid.uuid4()), title="MQTT work")
    message = SimpleNamespace(topic=adapter.commands_topic, payload=json.dumps(command).encode(), retain=True)
    bridge._on_message(client, None, message)
    client.publish.assert_not_called()
    assert json.loads(adapter.snapshot())["items"] == []
    message.retain = False
    bridge._on_message(client, None, message)
    calls = client.publish.call_args_list
    assert len(calls) == 2
    assert calls[0].args[0] == adapter.results_topic and calls[0].kwargs["retain"] is False
    assert calls[1].args[0] == adapter.state_topic and calls[1].kwargs["retain"] is True
    assert json.loads(calls[1].args[1])["items"][0]["title"] == "MQTT work"

