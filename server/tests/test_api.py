import pytest
from fastapi.testclient import TestClient

from eco.main import create_app


@pytest.fixture
def client(dbs, settings, monkeypatch):
    monkeypatch.delenv("REDMINE_KEY_USER_1", raising=False)  # no poll-on-login against real Redmine
    with TestClient(create_app(settings, dbs, background=False)) as c:
        yield c


def login(c):
    r = c.post("/api/auth/login", json={"email": "me@eco.test", "password": "pw"})
    assert r.status_code == 200


def test_requires_login(client):
    assert client.get("/api/items").status_code == 401
    assert client.post("/api/auth/login", json={"email": "me@eco.test", "password": "nope"}).status_code == 401


def test_item_lifecycle(client):
    login(client)
    cats = {c["kind"]: c for c in client.get("/api/categories").json()}
    r = client.post("/api/items", json={"title": "Write plan", "category": cats["personal"]["key"],
                                        "due_date": "2026-10-10"})
    assert r.status_code == 201
    uuid = r.json()["uuid"]
    assert client.patch(f"/api/items/{uuid}", json={"due_date": "2026-10-12"}).status_code == 422
    r = client.patch(f"/api/items/{uuid}", json={"due_date": "2026-10-12", "reason": "sick day"})
    assert r.json()["due_date"] == "2026-10-12"
    events = client.get(f"/api/items/{uuid}/deadline_events").json()
    assert [e["event"] for e in events] == ["set", "extended"]
    assert client.post(f"/api/items/{uuid}/done").json()["status"] == "closed"
    assert client.get("/api/items").json() == []
    assert len(client.get("/api/items", params={"include_closed": True}).json()) == 1


def test_work_item_triage_flow(client):
    login(client)
    work = next(c for c in client.get("/api/categories").json() if c["kind"] == "work")
    uuid = client.post("/api/items", json={"title": "Untriaged", "category": work["key"]}).json()["uuid"]
    assert [i["uuid"] for i in client.get("/api/triage").json()] == [uuid]
    client.patch(f"/api/items/{uuid}", json={"title": "Untriaged, edited"})
    assert client.get("/api/outbox").json()["rows"] == []  # not queued until triaged
    r = client.post(f"/api/items/{uuid}/triage", json={"redmine_project_id": 1, "tracker_id": 1})
    assert r.json()["needs_triage"] is False
    assert client.get("/api/triage").json() == []
    assert client.get("/api/outbox").json()["rows"][0]["state"] == "pending"


def test_logout(client):
    login(client)
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401


def test_work_and_personal_categories_are_distinct(client):
    """Each DB numbers categories from 1; keys must keep Work and Personal apart."""
    login(client)
    cats = {c["kind"]: c for c in client.get("/api/categories").json()}
    assert cats["work"]["id"] == cats["personal"]["id"] == 1
    assert cats["work"]["key"] != cats["personal"]["key"]
    p = client.post("/api/items", json={"title": "Groceries", "category": cats["personal"]["key"]}).json()
    w = client.post("/api/items", json={"title": "Report", "category": cats["work"]["key"]}).json()
    assert (p["kind"], p["needs_triage"]) == ("personal", False)
    assert (w["kind"], w["needs_triage"]) == ("work", True)
    personal = client.get("/api/items", params={"category": cats["personal"]["key"]}).json()
    work = client.get("/api/items", params={"category": cats["work"]["key"]}).json()
    assert [i["title"] for i in personal] == ["Groceries"]
    assert [i["title"] for i in work] == ["Report"]
