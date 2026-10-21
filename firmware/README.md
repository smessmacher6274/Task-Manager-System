# Existing ESP32 pad integration

Active firmware lives in `../2040Bluetooth/Code` beside this repository.
The compatibility adapter accepts that firmware's `tablet-001` protocol.
Bluetooth capture, touchscreen checkboxes, flash cache, and offline edits keep
working. The native `eco/...` simulator protocol also remains available.

## Server setup

Provision your user with `server/tools/create_user.py`, then set in `server/.env`
(or the backend container environment):

```dotenv
PAD_ENABLED=true
PAD_USER_ID=1
PAD_DB=personal
```

Use `PAD_DB=work` for work tasks. One pad shows one user's database: up to 50
tasks, open first and then completed tasks when space permits. New pad captures
use the default category. Work captures enter triage; completion uses the existing
outbox/Redmine flow. Reopening sets status to `new`. Existing server titles display
with control characters replaced and at most 120 UTF-8 bytes; full server titles
are preserved. Additional tasks remain on the website. Pad creates are rejected
when the selected database already has 50 nondeleted tasks. Web/Redmine changes
reach the pad within about two seconds while connected.

The updated `infra/mosquitto/acl` grants the required server and pad topics.
Add a password interactively from `infra`:

```powershell
docker compose exec mosquitto mosquitto_passwd /mosquitto/config/passwd tablet-001
```

Reload/restart Mosquitto after ACL/password changes, and restart the backend
after environment changes. Set MQTT_HOST and the server credentials to that
broker. The pad needs its LAN address, never `127.0.0.1`. Stop the old
`2040Bluetooth/LocalMQTT` server before cutover; do not run two authoritative
servers for the same pad topics. Run only one backend process with this adapter.

The default `docker-compose.prod.yml` disables MQTT. The optional
`docker-compose.pad.yml` enables it and adds Mosquitto to the website stack.
For that deployment, set MQTT_PASSWORD, PAD_USER_ID and PAD_DB in the repository
root `.env`; MQTT_PASSWORD must match the broker's `server` user. Create an empty
`infra/mosquitto/passwd` file first if absent (do not overwrite an existing one).
From the repository root, provision passwords before starting:

```powershell
docker compose -f docker-compose.prod.yml -f docker-compose.pad.yml run --rm --no-deps --entrypoint mosquitto_passwd mosquitto /mosquitto/config/passwd server
docker compose -f docker-compose.prod.yml -f docker-compose.pad.yml run --rm --no-deps --entrypoint mosquitto_passwd mosquitto /mosquitto/config/passwd tablet-001
docker compose -f docker-compose.prod.yml -f docker-compose.pad.yml up --build -d
```

The backend retains its existing scheduler and Redmine dry-run settings. If using
a separate broker instead, pass MQTT_ENABLED/HOST/PORT/USERNAME/PASSWORD and PAD_*
through the backend container environment. A host `.env` alone does not inject
arbitrary settings into a container.

## Pad setup

From `2040Bluetooth/Code`:

```powershell
python setup_task_server.py --host YOUR_SERVER_LAN_IP
```

Enter the password created for `tablet-001`. This updates only the ignored local
MQTT settings in `main/network_config.h`, preserving Wi-Fi credentials. Build
and flash using your activated ESP-IDF environment and working sdkconfig.
No firmware protocol migration is required. OTA topics are not granted.

### First cutover from LocalMQTT

Before switching, reconnect to the old server and let pending pad edits finish.
Preserve any old tasks you need from its web UI/database; this adapter does not
import that database. The pad caches its old revision. A lower new-server revision
will not replace it, so reset **only** the `task_store` partition before first
connection to the new server, using ESP-IDF's `parttool.py` and the actual flashed
partition table (`erase_partition --partition-name task_store`). This deletes
cached tasks and pending edits: preserve them first. Do not erase all flash;
firmware and calibration live elsewhere. Changing PAD_DB/PAD_USER_ID also requires
a deliberate cache cutover. No device data has been erased by this change.

## Protocol and verification

Topics: `notepad/v1/devices/tablet-001/{commands,results,state,presence}`.
State is retained; results are not. SQLite stores per-item revision fingerprints
and exact command receipts atomically with pad mutations. Stale checkbox edits
return `conflict`; retries return the original result. The device identity is
fixed: use a separate broker for another pad/user.

After installing `.[dev]`, run from `server`:

```powershell
python -m pytest -m "not redmine and not mqtt"
```

Hardware check: create via BLE, verify on the website, complete/reopen from both
sides, disconnect Wi-Fi and queue an edit, reconnect, then reboot pad and server.
Change a task on the web while the pad is offline to verify stale edits lose to
the server. Delivery alone does not confirm that the display rendered the list.
