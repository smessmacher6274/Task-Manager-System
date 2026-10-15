from eco.sync.merge import diff_against_base, three_way

BASE = {"title": "A", "description": "", "status": "new", "priority": "normal", "due_date": None}


def test_no_changes():
    r = three_way(BASE, dict(BASE), dict(BASE))
    assert not r.take_redmine and not r.server_changed and not r.conflicts


def test_redmine_only_change_is_accepted():
    r = three_way(BASE, dict(BASE), {**BASE, "title": "B"})
    assert r.take_redmine == {"title": "B"} and not r.conflicts


def test_server_only_change_goes_to_outbox():
    r = three_way(BASE, {**BASE, "status": "closed"}, dict(BASE))
    assert r.server_changed == {"status"} and not r.take_redmine


def test_both_changed_differently_is_conflict():
    r = three_way(BASE, {**BASE, "title": "S"}, {**BASE, "title": "R"})
    assert [(c.field, c.base, c.server, c.redmine) for c in r.conflicts] == [("title", "A", "S", "R")]


def test_both_changed_identically_is_not_conflict():
    r = three_way(BASE, {**BASE, "title": "X"}, {**BASE, "title": "X"})
    assert not r.conflicts and not r.take_redmine and not r.server_changed


def test_mixed_fields():
    r = three_way(BASE, {**BASE, "status": "closed", "title": "S"}, {**BASE, "due_date": "2026-11-01", "title": "R"})
    assert r.take_redmine == {"due_date": "2026-11-01"}
    assert r.server_changed == {"status"}
    assert [c.field for c in r.conflicts] == ["title"]


def test_diff_skips_conflict_fields():
    assert diff_against_base(BASE, {**BASE, "title": "S", "status": "closed"}, skip={"title"}) == {"status": "closed"}
