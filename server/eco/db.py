"""Two databases (work, personal) sharing one schema.

Redmine sync, ingest_log dedupe, users and web sessions live in the work DB;
users are mirrored into the personal DB so foreign keys hold there too.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Literal

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from eco.models import Base

DbKind = Literal["work", "personal"]


def _engine(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    return engine


class Databases:
    def __init__(self, work_path: Path, personal_path: Path):
        self.paths = {"work": Path(work_path), "personal": Path(personal_path)}
        self.engines = {k: _engine(p) for k, p in self.paths.items()}
        self._makers = {k: sessionmaker(e, expire_on_commit=False) for k, e in self.engines.items()}
        for e in self.engines.values():
            Base.metadata.create_all(e)

    @contextmanager
    def session(self, kind: DbKind) -> Iterator[Session]:
        s = self._makers[kind]()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def dispose(self) -> None:
        for e in self.engines.values():
            e.dispose()
