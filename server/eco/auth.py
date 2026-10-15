"""Password hashing (argon2) and server-side web sessions (stored in the work DB)."""
from __future__ import annotations

import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from eco.models import Session, User, utcnow

_ph = PasswordHasher()


def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def check_login(s: DbSession, email: str, pw: str) -> User | None:
    user = s.scalar(select(User).where(User.email == email.strip().lower()))
    if user is None or not user.password_hash:
        _ph.hash(pw)  # keep timing similar for unknown users
        return None
    try:
        _ph.verify(user.password_hash, pw)
    except VerificationError:
        return None
    return user


def new_session(s: DbSession, user: User, days: int) -> str:
    token = secrets.token_urlsafe(32)
    s.add(Session(token=token, user_id=user.id, expires_at=utcnow() + timedelta(days=days)))
    return token


def user_for_token(s: DbSession, token: str | None) -> User | None:
    if not token:
        return None
    sess = s.get(Session, token)
    if sess is None or sess.expires_at < utcnow():
        return None
    return s.get(User, sess.user_id)


def end_session(s: DbSession, token: str | None) -> None:
    if token:
        s.execute(delete(Session).where(Session.token == token))
