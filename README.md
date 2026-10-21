# Central Ecosystem

Existing ESP32 pad integration is available through an optional MQTT compatibility
adapter. See [pad setup and cutover](firmware/README.md) and
`docker-compose.pad.yml` for the website deployment with a broker.

When `PAD_ENABLED=true`, the bridge supports both the legacy v1 topic and the
v2 dual-list protocol. Both v2 lists bind to `PAD_USER_ID`: `personal` always
uses the personal database and `work` always uses the work database, regardless
of `PAD_DB`. Each list has its own retained `state`, `commands`, and `results`
topics under `notepad/v2/devices/tablet-001/lists/{personal,work}/`; the shared
presence topic is `notepad/v2/devices/tablet-001/presence`. A result's
`revision` is the list snapshot revision. Successful results also include
`item_revision`, which clients use to chain later edits to that item. Snapshot
items keep their per-item version in `items[].revision`.

See `../CentralEcosystemPlan.md` for the design. Layout: `server/` (Python), `web/` (React + Vite),
`infra/` (local Docker stack), `firmware/` (later).

## Local stack (Phase 1)

All commands from PowerShell. Never point any of this at production Redmine.

```powershell
cd infra
# Mosquitto users (server + simulated tablet); passwd is gitignored
New-Item -ItemType File mosquitto\passwd -Force
docker compose run --rm mosquitto mosquitto_passwd -b /mosquitto/config/passwd server server-pass
docker compose run --rm mosquitto mosquitto_passwd -b /mosquitto/config/passwd sim-device-1 sim-device-pass
docker compose up -d
# wait ~30 s for Redmine to migrate, then seed it (prints API keys)
docker compose exec redmine bundle exec rails runner /seed/seed.rb
```

Redmine UI: http://localhost:3000 (admin/admin, or me/password123, coworker/password123).
Mailpit inbox: http://localhost:8025.

```powershell
cd ..\server
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
copy .env.example .env      # paste the keys printed by seed.rb
.venv\Scripts\python tools\create_user.py --id 1 --name "Me Tester" --email me@eco.test --password pw

.venv\Scripts\python -m pytest -m "not redmine and not mqtt"   # unit tests, no Docker
.venv\Scripts\python -m pytest                                  # everything, needs the stack
.venv\Scripts\uvicorn eco.main:app --reload                     # API on :8000
.venv\Scripts\python tools\sim_tablet.py create --title "Call vendor" --category work
```

Exit criterion for Phase 1: full `pytest` green with `OUTBOX_DRY_RUN=false`.

Stop any running `uvicorn` before `pytest`: two servers on the same broker both answer the
simulated tablet, and the MQTT test fails.

### Troubleshooting (Windows)
- Use `127.0.0.1`, not `localhost`, for local services: `localhost` tries IPv6 first and each request stalls ~5 s.
- Docker Desktop fails with `remove …\engine.sock` / `…\run\dockerInference: The file cannot be accessed by the system`:
  stale Unix sockets from an unclean shutdown. Quit Docker Desktop, then delete them from WSL
  (`rm /mnt/c/Users/<you>/AppData/Local/docker-secrets-engine/engine.sock /mnt/c/Users/<you>/AppData/Local/Docker/run/*`)
  and start Docker again. Windows tools can't delete them.

## Verification and Git synchronization (2026-10-21)

The user verified the combined local and Raspberry Pi changes and reported that
they work. Automated test output and individual hardware-check results were not
recorded in this verification note.

Local `main` was fast-forwarded to Raspberry Pi commit `884ad59`. Local work was
saved with `git stash push -u`, then reapplied. The `firmware/README.md` conflict
was resolved by retaining the local v2 dual-list documentation. The previously
untracked `docker-compose.pad.yml`, `server/eco/mqtt/pad.py`, and
`server/tests/test_pad.py` matched the incoming files in content; only executable
permissions differed. The local README, environment template, firmware guide,
and `server/tests/test_pad_v2.py` remain for review and commit. Keep the backup
stash until these changes are committed and verified.

To repeat the server checks in WSL, activate the environment from `server/`,
then run:

```bash
source .venv-wsl/bin/activate
python -m pytest tests/test_pad.py tests/test_pad_v2.py -v
python -m pytest -m "not redmine and not mqtt"
```

Hardware checks: create a task on the pad and verify it on the website; complete
and reopen from both sides; check personal/work isolation; queue an edit while
offline and reconnect; reboot and check persistence. Also edit a task on the web
while the pad is offline and confirm stale pad edits do not overwrite it.

## Web UI (Phase 2)

```powershell
cd web
npm install
npm run dev     # http://localhost:5173, proxies /api to :8000
```
