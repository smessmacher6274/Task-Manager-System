"""User provisioning: a user row (mirrored in both DBs) plus default categories."""
from __future__ import annotations

from sqlalchemy import select

from eco.auth import hash_password
from eco.db import Databases
from eco.models import Category, User

DEFAULT_CATEGORIES = {
    "work": [("Work", "work", True)],
    "personal": [("Personal", "personal", False)],
}


def ensure_user(dbs: Databases, *, user_id: int, name: str, email: str,
                password: str | None = None, tz: str = "America/New_York") -> None:
    pw_hash = hash_password(password) if password else None
    for kind in ("work", "personal"):
        with dbs.session(kind) as s:
            u = s.get(User, user_id) or User(id=user_id)
            u.name, u.email, u.tz = name, email.strip().lower(), tz
            if pw_hash:
                u.password_hash = pw_hash
            s.add(u)
            s.flush()
            for cname, ckind, syncs in DEFAULT_CATEGORIES[kind]:
                if not s.scalar(select(Category).where(Category.user_id == user_id, Category.name == cname)):
                    s.add(Category(user_id=user_id, name=cname, kind=ckind, syncs_redmine=syncs))


def add_category(dbs: Databases, *, user_id: int, name: str, kind: str) -> int:
    """Custom tabs live in the personal DB; work-kind categories in the work DB."""
    db = "work" if kind == "work" else "personal"
    with dbs.session(db) as s:
        c = Category(user_id=user_id, name=name, kind=kind, syncs_redmine=False)
        s.add(c)
        s.flush()
        return c.id
