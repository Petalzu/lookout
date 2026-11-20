# Deployment

Use Fedora with Rootless Podman and user-level Quadlet.
Lookout runs the website, API and collectors in one container. The host schedules updates and backups.
The web port binds to loopback by default. A separate proxy handles HTTPS, authentication and external access.

## Requirements

| Requirement | Check or responsibility |
|---|---|
| Linux x86_64, cgroup v2, Landlock ABI 6+ and seccomp | The published image targets linux/amd64. Installation probes the native RPM sandbox. Unsupported isolation stops startup. |
| Rootless Podman, Skopeo, Quadlet, a user systemd manager and Python 3.11+ | The service account needs subordinate UID/GID ranges and linger. The image supplies application dependencies. |
| Writable local persistent storage | Keep the same data directory across updates. It contains SQLite, the SPEC repository and provider caches. Do not use tmpfs or a network filesystem. |
| A trusted image and reachable providers | Public GHCR, GitHub, OBS, Git and upstream providers must be reachable. |
| Private configuration and backups | Maintain free space, backup retention and an independent backup copy. Local backups do not protect against host loss. |

Only one application may write a data directory. The image runs as UID/GID 10001.
Rootless keep-id maps the service account to that identity. Configuration and the
container root are read-only. Do not add privileged mode, unconfined seccomp,
world-writable permissions or a container-engine socket mount.

## Install on Linux

### 1. Prepare the service account

The host administrator installs prerequisites and enables linger, replacing
`SERVICE_USER` with the dedicated non-root account:

```sh
# Fedora
sudo dnf install skopeo podman python3 curl
# Ubuntu
# sudo apt install skopeo podman uidmap passt dbus-user-session python3 curl
sudo loginctl enable-linger SERVICE_USER
```

As that account, these checks must report `true` and `yes`:

```sh
podman info --format '{{.Host.Security.Rootless}}'
loginctl show-user "$USER" --property=Linger --value
```

Run the following blocks in the same Bash session as the service account.
Do not run rootless installation as root. Use a login session for the service account.
The tools select that account's runtime directory and user bus.
A distribution name or kernel version does not prove sandbox support. The native worker probe must pass.

### 2. Obtain the image and host tools

Choose a new instance directory. For Docker, use `ENGINE=docker` in this block,
then follow [Docker](#docker) instead of the Podman installation steps.

```sh
set -euo pipefail
umask 077
ENGINE=podman
NAME=openruyi-lookout
PORT=18730
IMAGE=ghcr.io/openruyi-project/lookout:main
ROOT="$HOME/.local/share/lookout"
TOOLS="$ROOT/tools"
UNIT="$HOME/.config/containers/systemd/$NAME.container"
mkdir -p "$ROOT"
test ! -e "$ROOT/tools"
"$ENGINE" pull "$IMAGE"
IMAGE_ID=$("$ENGINE" image inspect --format '{{.Id}}' "$IMAGE")
HELPER="lookout-tools-$$"
"$ENGINE" create --name "$HELPER" --network none --entrypoint /bin/true "$IMAGE_ID"
trap '"$ENGINE" rm "$HELPER" >/dev/null' EXIT
"$ENGINE" cp "$HELPER:/app/deploy" "$ROOT/tools"
"$ENGINE" rm "$HELPER"
trap - EXIT
```

Private GHCR packages require engine registry credentials, not a GitHub API key.

### 3. Install

```sh
python3 "$TOOLS/install.py" \
  --engine podman --image "$IMAGE_ID" --name "$NAME" \
  --directory "$ROOT" --port "$PORT" --network pasta \
  --auto-update "$IMAGE" | tee "$ROOT/installation.json"
```

The installer creates private configuration and persistent data, checks runtime and Quadlet, then waits for readiness.
It refuses existing destinations. On failure, it retains its files.
Read the error and journal before recovery. Do not initialize the same destination again.

```text
$ROOT/tools/            initial installation tools
$ROOT/automation/current active host tools (atomic pointer)
$ROOT/config/           operator configuration
$ROOT/data/             database, SPEC repository, caches
$ROOT/backups/          backups and upgrade records
$ROOT/installation.json initial image and paths
$UNIT                   application Quadlet
~/.config/systemd/user/$NAME-{update,backup}.{service,timer}
```

The image update timer checks GitHub every five minutes, with jitter: about
12 requests/hour, below the 60/hour anonymous limit for an unshared address.
Only a successful manual **Release** (`checks.yml`) run on `main` selects its
published `sha-<commit>` image. Ordinary pushes run CI without publishing images. No new revision means no GHCR requests, pulls or restarts. If GitHub returns an error or rate limit, the updater leaves the service unchanged.
The updater saves the retry deadline in `backups/publication.json`. Timer events before that deadline make no GitHub request.
Authentication failures remain distinct from quota waits. A successful response clears the wait.
Unattended GHCR `main`/`latest` updates require access to the publication workflow.
The backup timer runs daily. The application schedules its collectors.

To adopt existing state, stop its writer. Back up the data. Add
`--data /absolute/existing/data --config /absolute/reviewed/config` to installation.
The installer reuses that data without copying it. Use image upgrades, not installation,
for subsequent releases.

### Listener and GitHub credentials

`--port` selects the host port. The container web port remains 8080.
For direct LAN access, add `--bind-address 10.230.50.181 --port 18730` during installation,
using the server's actual IP. Restrict inbound access with the host or network firewall.
This option does not configure DNS, HTTPS or authentication. Avoid `0.0.0.0` unless all interfaces are intended.
Image upgrades retain the listener. For Podman, edit `PublishPort` in the installed `.container` file,
then run `systemctl --user daemon-reload` and restart that service.

For authenticated GitHub reads, create a dedicated, public-read-only fine-grained PAT with an expiry date.
Store it outside the source, configuration and backup directories:

```sh
CREDENTIAL="$HOME/.config/lookout/github.env"
install -d -m 700 "$(dirname "$CREDENTIAL")"
test ! -e "$CREDENTIAL"
(umask 077; read -rsp 'GitHub token: ' TOKEN; printf '\n' >&2
 printf 'LOOKOUT_GITHUB_TOKEN=%s\n' "$TOKEN" > "$CREDENTIAL")
```

Add `--github-env-file "$CREDENTIAL"` to installation. The file must contain only that assignment,
be owned by the service account, and have mode 0600 or 0400. Symlinks are rejected.
Both the collector and host updater use it. No additional repository write permission is needed.
Environment credentials remain visible to the container owner and root. Do not share engine access.

For an existing Podman instance, add `EnvironmentFile=/absolute/path/github.env` under `[Container]`,
reload the user manager and restart the service. Use tools from a release that supports this option.
When rotating the token, replace the file with the same owner and permissions, then restart the application.
The updater reads it on its next eligible check. Never include the file in support reports.
For organization-managed credentials, evaluate a GitHub App instead of a person's PAT.
App installation-token renewal is not implemented by these tools.

A rejected GitHub token falls back to the public API for one hour. The updater
and activity collector retain this cooldown across restarts. At the next heartbeat
after it expires, they try the token without advancing the anonymous schedule.
A successful probe resumes authenticated requests; a rejected probe leaves the
anonymous delay and cursor unchanged. Rate-limit responses
do not switch credentials: each client waits until GitHub permits another request.
Anonymous access uses the server IP quota, which other applications can also consume.

### 4. Check the service and take the first backup

```sh
systemctl --user status "$NAME.service" "$NAME-update.timer" "$NAME-backup.timer" --no-pager
curl --fail "http://127.0.0.1:$PORT/livez"
curl --fail "http://127.0.0.1:$PORT/readyz"
curl --fail "http://127.0.0.1:$PORT/api/v2/status"
python3 "$TOOLS/maintain.py" --unit "$UNIT" --backup-dir "$ROOT/backups"
python3 "$TOOLS/maintain.py" --unit "$UNIT" --status "$ROOT/backups"
python3 "$TOOLS/diagnose.py" --unit "$UNIT" --backups "$ROOT/backups"
```

`diagnose.py` reads the running image, ports, HTTP probes, collection coverage, backup status and upgrade wait.
It does not print container environment values or change the instance.
`monitor_coverage` separates pending, successful and failed observations.
`failure_groups` groups repeated errors by monitor and counts affected packages.
A finding count of zero does not establish absence while its checks remain pending or failed.

The service and both timers must be active. The backup status must report
`"ok": true`. Check provider timestamps again after their next scheduled polls.

| Endpoint | Meaning |
|---|---|
| `/livez` | Node → FastAPI request chain works. |
| `/readyz`, or compatibility `/healthz` | The snapshot and its projection are readable. HTTP 200 may still report `degraded`. |
| `/api/v2/status` | Collection timestamps, coverage and failures. |

Initial collection is asynchronous. The installer waits a limited time for readiness to change from 503.
On the target host, check provider access and service recovery after logout and reboot.

## Operations

Use the instance variables from installation. If an upgrade replaced the host tools, set
`TOOLS` to the active script directory in the update/backup units, not the old copy.

### Configuration and ports

**Image updates replace release catalogs, not operator settings.** The default
installation references `/app/config/` for version rules, monitor identities and
distribution mappings. Operator overrides remain in `/config`. See [Configuration](../config/README.md).
Explicit local catalogs remain local.
GitHub repository defaults follow the release distribution catalog. An explicit
`[github]` in `tracker.toml` takes precedence, including an empty repository map
to disable collection. See [GitHub activity configuration](../config/README.md#github-activity).

SQLite stays in the same data directory. Compatible updates reuse observations
and indexes. Before a supported storage-format change, the upgrader backs up the database.
It then migrates the data. If the storage-format change is unsupported, the upgrader refuses the upgrade.

| Setting | Entry point |
|---|---|
| Port | `install.py --port`. Later, change `PublishPort=127.0.0.1:PORT:8080` in `$UNIT` |
| Resource limits | `--memory 8g --cpus 4 --pids-limit 512`. Later, edit the Quadlet |
| GitHub activity token (optional) | `LOOKOUT_GITHUB_TOKEN` in the container environment, with read-only repository access |
| Monitor proxy | `--env TRACKER_MONITOR_PROXY=URL` |
| Host-local proxy | `--network pasta:-T,7890 --env TRACKER_MONITOR_PROXY=http://127.0.0.1:7890` |
| OBS, Git and monitor schedules | `tracker.toml` in the active `/config` mount |
| Default upstream rules and monitor identities | Image `/app/config/` catalogs |
| Administrator identity/rule exceptions | Explicit override files in the active `/config` mount |
| Provider credentials | Private keyfiles, not frontend `PUBLIC_*` values |

Before changing the Quadlet or application configuration,
[pause scheduled jobs](#pause-scheduled-jobs). Edit the active config path recorded in the `/config:ro,Z` mount, not an old copy.
For a local replacement, remove `Label=org.openruyi.catalog-image=…` from the Quadlet.
Change its `/config:ro,Z` volume path. Keep `/data` unchanged.
Set `PORT` to the selected host port, then run:

```sh
systemctl --user daemon-reload
systemctl --user restart "$NAME.service"
curl --fail "http://127.0.0.1:$PORT/readyz"
```

Resume only the timers that were active before the change. Leave the update timer
stopped if the image is deliberately pinned.

Resource limits are defaults, not measured minimums. Directory paths cannot
contain colons. Installer unit values cannot contain whitespace, quotes or
systemd `%` specifiers. Colons are valid in proxy URLs and network parameters.
The installer does not load `.env` files implicitly.

`TRACKER_MONITOR_PROXY` does not configure Git, OBS or host-side registry pulls.
Configure each proxy at its boundary. For a host-local registry proxy, run:

```sh
systemctl --user edit "$NAME-update.service"
```

Add the required proxy addresses, for example:

```ini
[Service]
Environment=HTTP_PROXY=http://127.0.0.1:7890
Environment=HTTPS_PROXY=http://127.0.0.1:7890
```

Then run `systemctl --user daemon-reload`. Keep credentials in private files,
not shell history or public environment values.

The container publishes only web port 8080 on host loopback. API port 18731 stays internal. The
[Caddy example](../deploy/Caddyfile.example) is for a host proxy. Port or ingress
changes do not require a frontend rebuild.

Security queries public OSV and NVD APIs. It needs no scanner or local vulnerability database. Reviewed CPE identities are in `packages.toml`.
Provider errors remain errors, not a claim of no advisories. The existing monitor
heartbeat refreshes unchanged versions too. Operators can override refresh timing
in `[monitors.refresh.security]` without changing query identities.

### Copied catalog migration

The upgrader migrates copied defaults only with a recorded original installation
image. New default installations record that identity. Explicit `--config`
installations remain operator-owned. The last running image is not necessarily the original baseline. Unverified local catalogs remain unchanged.

If the original image is unavailable, keep the local catalog until its ownership
can be reviewed. Do not supply a guessed baseline.

The stopped-state transaction backs up SQLite, prepares a new private config,
checks it, then selects the new image **and** config together. Local rule/identity
edits become explicit overrides. Deleted tracks remain excluded. Migration preserves credentials, schedules and data. Ambiguous deletions reject migration. Failure
reselects the old config/image pair after checking database compatibility.

The prior config stays untouched. `catalogs.json` records both paths. The container mount identifies the active one.
Keep active config and rollback copies when cleaning backups. Resume paused timers after acceptance.

### Image upgrades

`main` and `latest` name the same successful CI build. Registry transport failures
(EOF/reset/timeout) receive at most three attempts within the original request
budget. Authorization failures stop retries. A release tag `vX.Y.Z`, a
`sha-<full-commit>` tag, or `@sha256:<registry-digest>` selects a published build.
Unchanged images do not restart the application. A changed image requires a short **service interruption**.

For a fixed image, enter its published release, commit tag or digest reference:

```sh
read -r -p 'Published image reference: ' SELECTED_IMAGE
systemctl --user stop "$NAME-update.timer"
python3 "$TOOLS/upgrade.py" --image "$SELECTED_IMAGE" \
  --unit "$UNIT" --backups "$ROOT/backups" --apply
```

If a backup or update holds the lock, the command fails. Retry after that job finishes.
The upgrade preserves the data mount, port, environment and resource limits.
The updater pins each successful GHCR `main`/`latest` publication to its commit tag and checks the image revision.
Retries reuse the cached candidate. For other explicit image references, the upgrader compares registry metadata during that manual invocation.
Discovery errors abort without changing the service.
Catalog migration selects a new config mount. Other updates retain it.
Upgrade records under `$ROOT/backups/upgrade-*` contain the prior image, unit and backup.
If the new image fails, the old image resumes only when it can read the resulting
data. Otherwise the instance stays stopped. An arbitrary downgrade may be incompatible. Restoring an older database requires a separate decision because it loses newer data.

To resume the configured registry channel:

```sh
systemctl --user start "$NAME-update.timer"
```

### Backups and failures

Repeat the backup and status commands from [installation verification](#4-check-the-service-and-take-the-first-backup)
for an on-demand backup. Copy the database backups, private configuration and
image references to independent storage. Set retention and monitor free space.
Do not use `cp` to back up a running SQLite database. API export is not a database backup.

Inspect failures without triggering collection or upgrades:

```sh
journalctl --user -u "$NAME.service" -u "$NAME-update.service" \
  -u "$NAME-backup.service" --no-pager -n 80
systemctl --user status "$NAME-update.timer" "$NAME-backup.timer" --no-pager
```

| Observed failure | Administrator action |
|---|---|
| Image changes, but copied catalogs still stay under `/config` | Check whether the installed launcher delegates to the release. See [Copied catalog migration](#copied-catalog-migration). Do not replace catalogs without a recorded original installation image. |
| Pull, lock or provider/network failure | Read the job journal. Fix the reported problem. Retry after any active job finishes. Do not reinitialize data. |
| Upgrade record says `failed`, rollback `ready` | The old image/config pair resumed. Resolve the configuration conflict or missing original image before retrying. |
| Upgrade record says `stopped`, rollback `failed` | Keep the instance stopped. Check storage compatibility or restore into a separate instance. Do not force an old reader onto new data. |

### Pause scheduled jobs

Before replacing host tools or cutting over a recovery instance, stop new jobs
and check that existing jobs finished:

```sh
UPDATE_WAS_ACTIVE=$(systemctl --user is-active "$NAME-update.timer" || true)
BACKUP_WAS_ACTIVE=$(systemctl --user is-active "$NAME-backup.timer" || true)
if ! systemctl --user stop "$NAME-update.timer" "$NAME-backup.timer"; then
  printf 'Timer stop returned an error; checking state.\n' >&2
fi
for TIMER in "$NAME-update.timer" "$NAME-backup.timer"; do
  case "$(systemctl --user show "$TIMER" --property=ActiveState --value)" in
    inactive|failed) ;;
    *) printf '%s is not paused; do not edit the instance.\n' "$TIMER" >&2; exit 1 ;;
  esac
done
for JOB in "$NAME-update.service" "$NAME-backup.service"; do
  case "$(systemctl --user show "$JOB" --property=ActiveState --value)" in
    inactive|failed) ;;
    *) printf 'Wait for %s to finish, then retry.\n' "$JOB" >&2; exit 1 ;;
  esac
done
```

### Host automation

Scheduled updates and backups execute `$ROOT/automation/current`, an atomic
pointer to a complete versioned tool directory. A successful image upgrade
selects that image's tools. Failed upgrades keep the previous pointer. Previous tool directories remain available for review.

Trusting the image channel authorizes its host-side upgrade code. No container
receives the engine socket. Changing the tool pointer does not change application configuration, data, port or proxy settings.

### Restore into a separate instance

Use a verified SQLite backup, its matching configuration and a compatible
published image. Keep the original data intact. The following creates an
independent recovery instance on a free loopback port:

```sh
read -r -p 'SQLite backup path: ' BACKUP
read -r -p 'Matching configuration directory: ' RESTORE_CONFIG
read -r -p 'Matching published image reference: ' RESTORE_IMAGE
read -r -p 'Free recovery port: ' RESTORE_PORT
test -f "$BACKUP"
test -f "$RESTORE_CONFIG/tracker.toml"
podman pull "$RESTORE_IMAGE"
RESTORE_ID=$(podman image inspect --format '{{.Id}}' "$RESTORE_IMAGE")
RESTORE_DIGEST=$(podman image inspect --format '{{ index .RepoDigests 0 }}' "$RESTORE_IMAGE")
RECOVERY=$(mktemp -d "$ROOT/recovery.XXXXXXXX")
RECOVERY_NAME="$NAME-recovery-$(basename "$RECOVERY" | cut -d. -f2)"
mkdir -p "$RECOVERY/data/state"
install -m 0600 "$BACKUP" "$RECOVERY/data/state/tracker.sqlite3"
HELPER="lookout-recovery-tools-$$"
podman create --name "$HELPER" --network none --entrypoint /bin/true "$RESTORE_ID"
trap 'podman rm "$HELPER" >/dev/null' EXIT
podman cp "$HELPER:/app/deploy" "$RECOVERY/tools"
podman rm "$HELPER"
trap - EXIT
python3 - "$RECOVERY/data/state/tracker.sqlite3" <<'PY'
from contextlib import closing
from pathlib import Path
import sqlite3, sys
with closing(sqlite3.connect(Path(sys.argv[1]).resolve().as_uri() + '?mode=ro', uri=True)) as db:
    if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise SystemExit('backup integrity check failed')
PY
python3 "$RECOVERY/tools/install.py" \
  --engine podman --image "$RESTORE_ID" --name "$RECOVERY_NAME" \
  --directory "$RECOVERY/instance" --data "$RECOVERY/data" \
  --config "$RESTORE_CONFIG" --port "$RESTORE_PORT" --network pasta \
  --auto-update "$RESTORE_DIGEST"
curl --fail "http://127.0.0.1:$RESTORE_PORT/readyz"
```

The installer checks storage compatibility and the native runtime before startup.
Check the recovered observations, not just HTTP status. Supply the same required
proxy/network options as the original installation. Recovery jobs use the resolved
image digest and their own backup directory. Changing their channel is a separate
operator choice.

For a production cutover, [pause the original scheduled jobs](#pause-scheduled-jobs),
then stop the original service and edit the recovery port:

```sh
systemctl --user stop "$NAME.service"
RECOVERY_UNIT="$HOME/.config/containers/systemd/$RECOVERY_NAME.container"
vi "$RECOVERY_UNIT"
```

Change `PublishPort` to `127.0.0.1:$PORT:8080`, substituting the original numeric
port. Edit the Quadlet, not the generated `.service`. Then:

```sh
systemctl --user daemon-reload
systemctl --user restart "$RECOVERY_NAME.service"
curl --fail "http://127.0.0.1:$PORT/readyz"
```

Record the recovery instance's new name, paths and backup directory. Preserve the
stopped original until acceptance. Restoring this older snapshot loses observations
made after the backup. Do not overwrite the original database or start its timers.

## Docker

Obtain the tools using `ENGINE=docker` in the [extraction block](#2-obtain-the-image-and-host-tools).
Docker uses explicit config/data/backup volumes rather than the Podman instance
paths. Access to the Docker daemon is administrative.

```sh
python3 "$TOOLS/install.py" --engine docker --image "$IMAGE_ID" \
  --name "$NAME" --port "$PORT"
mkdir -p "$ROOT/backups"
docker inspect --format '{{.State.Status}} {{.Config.User}}' "$NAME"
curl --fail "http://127.0.0.1:$PORT/readyz"
python3 "$TOOLS/maintain.py" --container "$NAME" --backup-dir "$ROOT/backups"
python3 "$TOOLS/maintain.py" --container "$NAME" --status "$ROOT/backups"
```

For an image update:

```sh
python3 "$TOOLS/upgrade.py" --image "$IMAGE" \
  --container "$NAME" --backups "$ROOT/backups" --apply
```

For configuration or a port change, use `maintain.py --container "$NAME"` with
`--config /absolute/reviewed/config` or `--port PORT`. The application runs as UID
10001. Only new-volume initialization uses a constrained root helper. Preserve
`$NAME-config`, `$NAME-data` and `$NAME-backups`. Do not delete volumes or substitute
anonymous ones during upgrades.

Docker does not install the Podman timers. Schedule `upgrade.py` and `maintain.py`
on the host as the same engine owner, with stable `HOME`/`XDG_STATE_HOME`. See
[Image checks](../CONTRIBUTING.md#image-checks) for contributor validation.

## Acceptance and cleanup

Run `deploy/check-image.sh` and `deploy/smoke-image.py` with `CONTAINER_ENGINE=podman` on each target host.
Use a tested image ID, not a moving tag. These commands use temporary resources.
The Docker release test is `python3 deploy/smoke-release.py IMAGE`.
Passing Docker tests does not establish rootless Podman, SELinux or AppArmor compatibility.

On Fedora and Ubuntu, run `python3 deploy/smoke-host.py IMAGE` as the rootless service account.
It installs a disposable Quadlet, starts the native worker, checks HTTP, restarts the service and reads a backup.
It does not reboot the host or test a different image upgrade.
On a disposable host of each supported distribution, install with a separate name and directories.
Check the configured address from another machine. Log out and check again.
Reboot, then check the service, both timers, data generation and backup readability.
Upgrade with the same data directory. Run the documented restore procedure in an independent directory.
Do not reboot a production host as a test without its operator's approval.

To inspect unused images from this instance's successful upgrade records:

```sh
python3 "$TOOLS/cleanup.py" --unit "$UNIT" --backups "$ROOT/backups"
# After review, repeat with --apply.
```

Cleanup keeps the running image, one rollback image, and images referenced by any container.
It never uses force or global prune. It does not remove volumes, backups or unrecorded images.
Set backup retention separately after a restore test. Keep an independent backup copy.

The manual `Host deployment checks` workflow runs these checks on dedicated non-root runners.
Each runner needs the labels `linux`, `lookout-disposable` and either `fedora` or `ubuntu`,
plus the prerequisites above. Use disposable machines, not production servers.
Supply a GHCR digest. An unconfigured or queued runner is not a passing host check.
Reboot and different-image upgrade acceptance remain separate operator checks.

## Publish a release

In GitHub Actions, select **Release → Run workflow**, choose `main`, and select
`patch` (default), `minor`, or `major`. Use `patch` for compatible fixes,
`minor` for features, and `major` for a new major version.
The workflow increments the highest version from the project declaration and
stable release tags. It shows the selected version in the publication job title and run summary.
The workflow pins the selected commit, runs the complete image checks, and
publishes that same image as `v<VERSION>` and `sha-<commit>`. The container's OCI
version label records the release version. A commit can be released only once.

The workflow creates the tag and release. Do not create them in advance.
Production consumers follow successful Release runs. The `main` and `latest`
image tags advance only during releases, not on ordinary pushes.
Configuration and persistent data remain on the host.

A failed run does not authorize an automatic upgrade. Inspect its logs and any
draft release before retrying. Do not move a published version tag or reuse its
version. Use a new version and commit for corrections.

### Commit evidence storage upgrade

Storage format 4 adds the optional commits batch collections. Formats 0, 2 and 3
require the existing stopped-instance verified backup/migration procedure. Old
images cannot read format 4; rollback uses the verified pre-upgrade database
backup. Do not mount NFS as production data or bypass the native sandbox to run
the new phase. See [Commit evidence](commits.md).
