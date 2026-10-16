# Raspberry Pi setup — Task-Manager-System

Deploy from the repository root on a Raspberry Pi running **Debian 13 (Trixie), ARM64**. The backend uses Python 3.12, the frontend builds with Node 22 and runs in nginx, and port **8080** serves the site and proxies `/api` to `backend:8000`. SQLite data stays on the Pi through bind mounts. Tailscale provides private access from an iPhone.

> Repository-specific checks: the available conversation does not expose the source tree, ASGI import path, frontend location, or database configuration names. The configuration below is a baseline with explicit checks; verify these items against your checkout before building. Preserve existing working deployment files and compare before replacing them.

## 1. Prepare the Pi

Use a reliable power supply and storage, and enable SSH through your OS setup if needed. Connect the Pi to your network. On the Pi:

```bash
cat /etc/os-release
dpkg --print-architecture
uname -m
sudo apt update
sudo apt upgrade -y
sudo apt install -y ca-certificates curl git
```

Expect Debian 13, `arm64`, and `aarch64`. These instructions target 64-bit Debian; do not force an amd64 image on ARM64. Reboot if the OS upgrade requires it, then reconnect.

## 2. Install Docker Engine and Compose

The following uses Docker's Debian repository. If another Docker installation already exists, review the official installation guide's conflicting-package instructions before changing it. Do not remove a working installation blindly.

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: trixie
Components: stable
Architectures: arm64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker run --rm hello-world
sudo docker compose version
```

This guide uses `sudo docker` throughout. Membership in the `docker` group grants effectively root-level access; it is optional, not required here. See [Docker's Debian installation guide](https://docs.docker.com/engine/install/debian/).

## 3. Open the repository root

For an existing checkout:

```bash
cd ~/Task-Manager-System
pwd
git status
```

For a new checkout, replace the URL below with your actual repository URL:

```bash
git clone <YOUR_REPOSITORY_URL> ~/Task-Manager-System
cd ~/Task-Manager-System
```

All remaining commands run here. Expected deployment files:

```text
Task-Manager-System/
├── Dockerfile.backend
├── Dockerfile.frontend
├── docker-compose.prod.yml
├── .dockerignore
├── .env                     # local; never commit
├── nginx/default.conf
└── server/
    ├── tools/create_user.py
    └── data/                # persistent SQLite files
```

Inspect the actual source layout before creating files:

```bash
find . -maxdepth 3 -name pyproject.toml -not -path './node_modules/*'
find . -maxdepth 3 -name package.json -not -path './node_modules/*'
find server -name '*.py' -not -path '*/.venv/*' | head -40
grep -R -n 'FastAPI\|Starlette\|sqlite\|personal.db\|work.db' server --include='*.py' | head -60
```

Determine the ASGI module and application object from the existing launch command or source, for example the portion after `uvicorn` in the project's documentation. Set `APP_MODULE` to that exact `module:object`; do not guess. Determine which `pyproject.toml` defines the installable Python project, which directory contains `package-lock.json`, and where the frontend build writes its output. Confirm the frontend calls the relative `/api` path, rather than `localhost:8000` (which would point at the visitor's device).

## 4. Backend Dockerfile

Create `Dockerfile.backend` in the root. This baseline assumes `server/pyproject.toml` is the Python project, and that `server/` contains the `eco/` and `tools/` directories. If the project metadata lives in the root, adjust COPY and the pip target accordingly. Retain any additional source files required by the package metadata.

```dockerfile
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/server
COPY server/ /app/server/
RUN python -m pip install --no-cache-dir /app/server uvicorn
EXPOSE 8000
CMD ["sh", "-c", "exec python -m uvicorn \"${APP_MODULE:?Set APP_MODULE to the verified ASGI import path}\" --host 0.0.0.0 --port 8000"]
```

`pip install /app/server` installs the project described by its `pyproject.toml`; `eco` and `tools` are local source directories, not assumed PyPI package names. Keep `server/tools/create_user.py` in the image. If required dependencies use an optional extra, use only the extra actually declared in the project's metadata. Native dependencies may require additional Debian build packages; add those only when the build error identifies them.

## 5. Frontend Dockerfile

Create `Dockerfile.frontend`. This baseline assumes `package.json` and the committed `package-lock.json` are in the repo root and `npm run build` produces `dist/`. If they are in `frontend/`, change both source COPY paths and the build context layout to match; if the output differs, change the final COPY source.

```dockerfile
FROM node:22 AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
COPY nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist/ /usr/share/nginx/html/
EXPOSE 80
```

Do not put credentials in frontend build variables: compiled browser assets are public to anyone who can load the site. For repeatable production builds, record tested image tags or digests after validating this deployment.

## 6. nginx configuration

```bash
mkdir -p nginx server/data
```

Create `nginx/default.conf`:

```nginx
server {
    listen 80;
    server_name _;
    root /usr/share/nginx/html;
    index index.html;

    location /api {
        proxy_pass http://backend:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location / {
        try_files $uri $uri/ /index.html;
    }
}
```

The `proxy_pass` intentionally has no trailing slash: `/api/...` reaches the backend with that prefix intact. If your application routes omit `/api`, adapt this to its verified routing contract. The frontend and API share an origin, avoiding a separate browser API hostname.

## 7. Local environment and Compose

Create a local `.env` with:

```dotenv
# Replace with the verified ASGI import path before building/running.
APP_MODULE=REPLACE_WITH_VERIFIED_MODULE:APP_OBJECT
MQTT_ENABLED=false
SCHEDULER_ENABLED=false
OUTBOX_DRY_RUN=true
```

Add any authentication secrets and database settings required by the existing application, using their exact documented names. Generate strong unique secrets; never reuse example values. Confirm in source that these three feature flags are recognized and parsed correctly. They express the deployment's intended safe initial state: MQTT off, scheduler off, and outbox dry-run on. Verify behavior in logs before connecting real devices or external delivery services.

```bash
chmod 600 .env
```

Create `docker-compose.prod.yml`:

```yaml
services:
  backend:
    build:
      context: .
      dockerfile: Dockerfile.backend
    env_file:
      - .env
    environment:
      MQTT_ENABLED: "false"
      SCHEDULER_ENABLED: "false"
      OUTBOX_DRY_RUN: "true"
    working_dir: /app/server
    volumes:
      - ./server/data:/app/server/data
    expose:
      - "8000"
    restart: unless-stopped

  frontend:
    build:
      context: .
      dockerfile: Dockerfile.frontend
    ports:
      - "8080:80"
    depends_on:
      - backend
    restart: unless-stopped
```

Confirm the application's configured SQLite paths resolve inside `/app/server/data`, including `personal.db` and `work.db`. The working directory accommodates relative `data/...` paths, but does not override absolute paths in the application. Set the actual supported database variables if needed. Mount the directory, not individual SQLite files, so journals, WAL and SHM files persist together. If the application uses additional SQLite directories, bind-mount those too.

The backend has no published host port. `8080:80` exposes the frontend on the Pi's interfaces, including the LAN and Tailscale. Do not forward port 8080 on your router. Docker-published ports can bypass some host firewall rules; use appropriate Docker-aware filtering if LAN access must be restricted. Tailscale does not automatically make this mapping tailnet-only. An alternative is binding to the Pi's specific Tailscale IP after authentication, with startup ordering considered.

Create `.dockerignore` to exclude private/runtime data from both build contexts:

```gitignore
.git
.env
.env.*
!.env.example
**/node_modules
**/dist
**/.venv
**/__pycache__
**/*.db
**/*.db-*
**/*.sqlite
**/*.sqlite3
**/*.sqlite-*
**/*.sqlite3-*
server/data
backups
infra/mosquitto/passwd
```

## 8. Build and initialize securely

```bash
sudo docker compose -f docker-compose.prod.yml config --quiet
sudo docker compose -f docker-compose.prod.yml build
sudo docker compose -f docker-compose.prod.yml run --rm --no-deps backend python tools/create_user.py --help
```

The image stores the script at `/app/server/tools/create_user.py` and runs from `/app/server`. Read its help, then run that same command with only the arguments the script actually supports. This guide deliberately does not invent username, password, role, or database flags. If `--help` is unsupported, inspect `server/tools/create_user.py` and the repo's account-creation documentation before proceeding.

Prefer an interactive password prompt if supported. Avoid placing passwords in command-line arguments, shell history, committed files, screenshots, or copied logs. If only argument-based password input is supported, understand its process-list/history exposure and improve or follow the project's secure initialization mechanism before sharing the service. Use a unique password, minimum privileges, and no shared default account. Confirm the script targets the same mounted database as the running backend. Follow any documented migration/bootstrap ordering before creating the first user.

```bash
sudo docker compose -f docker-compose.prod.yml up -d
sudo docker compose -f docker-compose.prod.yml ps
sudo docker compose -f docker-compose.prod.yml logs --tail=100 backend frontend
```

`depends_on` controls start order, not readiness. If nginx starts before the backend is available, wait for backend startup and restart the frontend if needed. Do not enable MQTT or scheduling as a workaround for unrelated startup failures.

## 9. Verify locally

```bash
curl -I http://127.0.0.1:8080/
hostname -I
sudo docker compose -f docker-compose.prod.yml exec frontend nginx -t
sudo docker compose -f docker-compose.prod.yml exec backend sh -c 'pwd; ls -ld /app/server/data; ls -l /app/server/data'
```

Open `http://<PI_LAN_IP>:8080` in a browser on the same trusted network. Sign in with the user you created. Create a harmless test task, refresh, restart the services, and confirm it remains:

```bash
sudo docker compose -f docker-compose.prod.yml restart
```

Test a documented API endpoint through `http://127.0.0.1:8080/api/...`; do not assume a `/health` endpoint exists. A 401/403 may mean authentication is working; a 404 may mean the chosen path does not exist. Use browser network tools and backend logs to distinguish these from proxy failures. Verify no real outbound actions occur with the initial feature flags.

## 10. Tailscale and iPhone access

Install from Tailscale's Debian 13 package repository:

```bash
curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.noarmor.gpg | sudo tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null
curl -fsSL https://pkgs.tailscale.com/stable/debian/trixie.tailscale-keyring.list | sudo tee /etc/apt/sources.list.d/tailscale.list >/dev/null
sudo apt update
sudo apt install -y tailscale
sudo systemctl enable --now tailscaled
sudo tailscale up
tailscale status
tailscale ip -4
```

Open the authentication URL printed by `tailscale up` on your iPhone or computer, sign in, and approve the Pi if your tailnet requires device approval. Treat login URLs as sensitive. If `tailscale ip -4` says `NeedsLogin`, installation succeeded but authentication is incomplete: run `sudo tailscale up` and finish the browser login.

On the iPhone:

1. Install the official Tailscale app from the App Store using [Tailscale's download page](https://tailscale.com/download).
2. Sign in to the same tailnet/account, approve the VPN configuration, and turn the connection on.
3. Confirm the Pi is visible and allowed by your tailnet access policy.
4. In Safari, open `http://<PI_TAILSCALE_IP>:8080` using the address from `tailscale ip -4`.
5. Sign in to Task-Manager-System separately. Tailscale access does not replace the app's login.
6. Disable Wi-Fi temporarily and repeat over cellular to confirm remote access works.

No router port forwarding, exit node, or subnet router is required for this direct device-to-device connection. Tailscale encrypts traffic between participating devices; plain HTTP on the LAN has no equivalent transport protection. Use the Tailscale address for remote access and trusted devices/accounts with MFA. For HTTPS or secure-cookie requirements, configure a supported TLS arrangement before changing application cookie security settings.

References: [Tailscale Linux installation](https://tailscale.com/docs/install/linux) and [stable package instructions](https://dl.tailscale.com/stable/).

## 11. Persistence and consistent backups

`./server/data` on the host survives container recreation. Images and containers are replaceable; that host directory, local configuration, and authentication secrets are not. Protect them and store encrypted backups off the Pi. `restart: unless-stopped` plus Docker's enabled service normally restarts services after reboot; a manually stopped service remains stopped.

For a simple consistent offline backup, stop all writers first. Include any other process that writes the same databases:

```bash
mkdir -p backups
chmod 700 backups
sudo docker compose -f docker-compose.prod.yml stop backend
sudo tar -czf "backups/sqlite-$(date +%Y%m%d-%H%M%S).tar.gz" server/data
sudo docker compose -f docker-compose.prod.yml start backend
sudo chmod 600 backups/*.tar.gz
```

If archiving fails, restart the backend and resolve the failure before an update. Back up `.env` separately into a secure location. Do not copy only a live `.db` file while SQLite is writing: committed data may still be in its WAL. Use SQLite's supported online backup mechanism if uninterrupted service is required. Never delete live WAL/SHM files to solve a Git or locking issue.

Test restores in a separate directory/instance. For a production restore: stop writers, preserve the current data directory, inspect the archive, restore the complete selected backup at the same paths with correct ownership, then start and verify. Avoid overwriting the only copy of current data. `docker compose down` removes containers/networks but not this bind-mounted host data; deleting `server/data` does remove it. Do not use destructive prune/removal commands as routine maintenance.

## 12. Updates

Take a verified backup first and read release/migration notes. From the root:

```bash
git status
git pull --ff-only
sudo docker compose -f docker-compose.prod.yml build --pull
sudo docker compose -f docker-compose.prod.yml up -d
sudo docker compose -f docker-compose.prod.yml ps
sudo docker compose -f docker-compose.prod.yml logs --tail=100 backend frontend
```

Resolve local changes intentionally before pulling; do not discard them blindly. Test login, API requests and persistence after updates. Database migrations can prevent a simple image rollback: keep the previous Git revision and a compatible pre-update backup. Update Debian/Docker/Tailscale through their package repositories and reboot when needed. Check access after a reboot, including Tailscale login state and any device-key expiry policy.

## 13. Git hygiene

Append missing rules to `.gitignore`; preserve other project-specific entries:

```gitignore
.env
.env.*
!.env.example
*.db
*.db-journal
*.db-wal
*.db-shm
*.sqlite
*.sqlite-*
*.sqlite3
*.sqlite3-*
backups/
.venv/
__pycache__/
*.egg-info/
.pytest_cache/
node_modules/
dist/
infra/mosquitto/passwd
```

Ignore rules do not untrack files already committed. Remove the four previously tracked SQLite sidecars from the Git index only:

```bash
git rm --cached --ignore-unmatch -- server/data/personal.db-shm server/data/personal.db-wal server/data/work.db-shm server/data/work.db-wal
git ls-files server/data
```

If the database files themselves are tracked, also remove them from the index:

```bash
git rm --cached --ignore-unmatch -- server/data/personal.db server/data/work.db
```

`--cached` preserves local files. Staged `deleted:` entries are expected: they remove the files from version control, not from the running Pi. If removal is already staged, skip repeating it. Review other tracked runtime files individually. Removing files now does not remove old database contents or secrets from Git history; rotate exposed secrets and assess history cleanup if private data was published.

Commit the deployment source files and this guide:

```bash
git add .gitignore .dockerignore Dockerfile.backend Dockerfile.frontend docker-compose.prod.yml nginx/default.conf RaspberryPiSetUp.md
git diff --cached --stat
git diff --cached -- .gitignore .dockerignore Dockerfile.backend Dockerfile.frontend docker-compose.prod.yml nginx/default.conf RaspberryPiSetUp.md
git status
git commit -m "Add Raspberry Pi Docker deployment guide and configuration"
```

Review before pushing. Do not stage `.env`, databases, backups, MQTT passwords, private keys, tokens, or generated dependency/build directories. Keep a sanitized `.env.example` if useful.

## 14. Troubleshooting

| Symptom | Check and remedy |
| --- | --- |
| Docker permission denied | Use the documented `sudo docker` commands and confirm Docker is running. |
| `exec format error` | Confirm ARM64 OS and compatible base images; remove any forced amd64 platform setting. |
| pip build fails | Verify the pyproject directory, included source/metadata files, dependency declarations, and any required system build packages. |
| ASGI module cannot import | Verify `APP_MODULE`, package layout and `PYTHONPATH`; use the same import path as the repository's working launch command. |
| `npm ci` fails | Check frontend directory and committed lockfile; resolve package/lockfile mismatch in development. |
| Frontend build output missing | Confirm the build script's real output directory and final Docker COPY source. |
| nginx returns 502 | Inspect backend logs, container status and port 8000 binding; verify the backend listens on `0.0.0.0`, then restart frontend if needed. |
| API returns 404 or HTML | Check `/api` prefix preservation, backend routes and frontend API base URL; rebuild frontend after build-time changes. |
| SQLite readonly / unable to open | Check actual database path, bind mount, directory ownership for the container user, free space and filesystem health. Do not use `chmod 777`. |
| Data disappears after rebuild | Database path is likely outside the bind mount, or a different Compose root/data path was used. Stop and locate existing files before initializing another database. |
| Database locked | Look for concurrent writers/duplicate backends; use the app's supported timeout/concurrency settings. Never delete live WAL/SHM files. |
| Port 8080 already in use | Check `sudo ss -ltnp`; choose another host port if required, and use that port in all browser URLs. |
| Tailscale `NeedsLogin` | Run `sudo tailscale up` and complete the displayed login flow. |
| iPhone cannot connect | Enable Tailscale, confirm same tailnet, device approval/access policy, current Pi address, running containers and port mapping. Test locally first. |
| Login/cookie failures | Check application auth settings, correct database, browser errors and whether secure cookies require HTTPS. Do not disable security settings blindly. |
| Pi slow or build killed | Check `free -h`, `df -h`, power/temperature and logs; build one image at a time or use a suitably resourced ARM64 build environment. |

Useful diagnostics (review for secrets before sharing):

```bash
sudo systemctl status docker tailscaled --no-pager
sudo docker compose -f docker-compose.prod.yml ps
sudo docker compose -f docker-compose.prod.yml logs --tail=150 backend frontend
tailscale status
df -h
```

Avoid sharing `docker compose config` output without review: it can expand environment values containing secrets. Use `config --quiet` for validation.
