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

The current v2 firmware shows personal and work lists together. Both lists use
`PAD_USER_ID`; personal always maps to the personal database and work always maps
to the work database. `PAD_DB` only selects the database for legacy v1 firmware.
Each list shows up to 50 tasks, open first and then completed tasks when space
permits. New captures use the list's default category. Work captures enter triage;
completion uses the existing outbox/Redmine flow. Reopening sets status to `new`.
Existing server titles display with control characters replaced and at most 120
UTF-8 bytes; full server titles are preserved. Additional tasks remain on the
website. Creates are rejected when that list's database already has 50
nondeleted tasks. Web/Redmine changes reach the pad within about two seconds
while connected.

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
The current firmware uses the v2 dual-list MQTT topics. OTA topics are not
granted.

### Flashing current firmware

The current firmware stores list data in the `task_v2` NVS partition at offset
`0x330000`, size `0x20000`. Keep the old `task_store` partition and its legacy
cache intact; the new firmware does not import that cache. Flash the complete
USB firmware image, including its partition table, so the new partition layout
is installed. The cache cutover does not require an erase command.

### Legacy v1 cache cutover

This applies only when continuing to use legacy v1 firmware and changing its
server, `PAD_DB`, or `PAD_USER_ID` binding. Before switching, reconnect to the
old server and let pending pad edits finish. Preserve any old tasks you need from
its web UI/database; the adapter does not import that database. Legacy v1 caches
its old revision, so a lower new-server revision may not replace it. If a cache
reset is needed, erase only `task_store` with ESP-IDF `parttool.py` using the
actual flashed partition table (`erase_partition --partition-name task_store`).
That deletes cached tasks and pending edits; preserve them first. Do not erase
all flash.

## Protocol and verification

Legacy topics: `notepad/v1/devices/tablet-001/{commands,results,state,presence}`.
V2 snapshots use `notepad/v2/devices/tablet-001/lists/{personal,work}/state`,
each with matching `commands` and `results`; presence is shared at
`notepad/v2/devices/tablet-001/presence`. States are retained; results are not.
SQLite stores per-item revision fingerprints and exact command receipts
atomically with pad mutations. V2 state has a list-wide `revision` and each
snapshot item has its own `revision`; successful results include the new
`item_revision` for chaining edits. Stale checkbox edits return `conflict`;
retries return the original result. The device identity is fixed: use a separate
broker for another pad/user.

After installing `.[dev]`, run from `server`:

```powershell
python -m pytest -m "not redmine and not mqtt"
```

Hardware check: create via BLE, verify on the website, complete/reopen from both
sides, disconnect Wi-Fi and queue an edit, reconnect, then reboot pad and server.
Change a task on the web while the pad is offline to verify stale edits lose to
the server. Delivery alone does not confirm that the display rendered the list.
