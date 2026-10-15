"""Restore a DB from a backup. Stop the server first.

  .venv\\Scripts\\python tools\\restore.py backups\\work-20261007.db --db work
"""
import argparse
from pathlib import Path

from eco.backup import restore
from eco.config import get_settings

p = argparse.ArgumentParser()
p.add_argument("backup", type=Path)
p.add_argument("--db", choices=["work", "personal"], required=True)
a = p.parse_args()

st = get_settings()
live = st.work_db_path if a.db == "work" else st.personal_db_path
restore(a.backup, live)
print(f"restored {a.backup} -> {live}")
