"""Create/update a user in both DBs with default Work/Personal categories.

  .venv\\Scripts\\python tools\\create_user.py --id 1 --name "Me" --email me@eco.test --password secret
Then put that user's Redmine key in server/.env as REDMINE_KEY_USER_<id>.
"""
import argparse

from eco.config import get_settings
from eco.db import Databases
from eco.users import add_category, ensure_user

p = argparse.ArgumentParser()
p.add_argument("--id", type=int, required=True)
p.add_argument("--name", required=True)
p.add_argument("--email", required=True)
p.add_argument("--password")
p.add_argument("--custom", action="append", default=[], help="extra custom tab name (repeatable)")
a = p.parse_args()

st = get_settings()
dbs = Databases(st.work_db_path, st.personal_db_path)
ensure_user(dbs, user_id=a.id, name=a.name, email=a.email, password=a.password)
for name in a.custom:
    add_category(dbs, user_id=a.id, name=name, kind="custom")
print(f"user {a.id} ready in {st.work_db_path} and {st.personal_db_path}")
