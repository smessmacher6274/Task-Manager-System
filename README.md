# Central Ecosystem

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

## Web UI (Phase 2)

```powershell
cd web
npm install
npm run dev     # http://localhost:5173, proxies /api to :8000
```
