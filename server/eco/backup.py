"""Nightly SQLite backups via the online backup API, plus restore."""
from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path


def backup_db(src: Path, dest_dir: Path, name: str, today: date | None = None) -> Path:
    today = today or date.today()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{name}-{today:%Y%m%d}.db"
    with closing(sqlite3.connect(src)) as s, closing(sqlite3.connect(dest)) as d:
        s.backup(d)
    return dest


def prune(dest_dir: Path, keep_days: int, today: date | None = None) -> list[Path]:
    cutoff = (today or date.today()) - timedelta(days=keep_days)
    removed = []
    for p in dest_dir.glob("*-????????.db"):
        try:
            stamp = date(int(p.stem[-8:-4]), int(p.stem[-4:-2]), int(p.stem[-2:]))
        except ValueError:
            continue
        if stamp < cutoff:
            p.unlink()
            removed.append(p)
    return removed


def backup_all(paths: dict[str, Path], dest_dir: Path, keep_days: int,
               today: date | None = None) -> list[Path]:
    out = [backup_db(p, dest_dir, name, today) for name, p in paths.items()]
    prune(dest_dir, keep_days, today)
    return out


def restore(backup: Path, live: Path) -> None:
    """Copy a backup over the live DB. Stop the server first."""
    with closing(sqlite3.connect(backup)) as b:
        if b.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"{backup} failed integrity_check")
    for suffix in ("-wal", "-shm"):
        Path(str(live) + suffix).unlink(missing_ok=True)
    shutil.copyfile(backup, live)
