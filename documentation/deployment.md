# Run Hermes on a server

Use this guide when Hermes should run on another machine and stay available after
you close your laptop. For installation on your own computer, use [Local setup](setup.md).

You need access to a server and one AI provider account/key or a local model
running where that server can reach it. A server does not include AI access.

| Server | Start here |
| --- | --- |
| Linux with Docker | [Linux with Docker](#linux-with-docker) |
| Linux without Docker | [Linux without Docker](#linux-without-docker) |
| Windows Server | [Windows Server](#windows-server) |
| InstaBox | [InstaBox guide](instabox.md) |

The examples keep port 8319 private. An SSH tunnel lets you use the dashboard and
API from your computer without opening another public server port.

## Linux with Docker

### 1. Connect to your server

Open PowerShell or Terminal **on your computer**:

```bash
ssh YOUR_USER@YOUR_SERVER
```

Replace `YOUR_USER` with your server login and `YOUR_SERVER` with its IP address
or hostname. The following commands run **inside that SSH session, on the server**.

### 2. Install Docker and start Hermes

If `docker info` fails because Docker is missing, follow the official
[Docker Engine installation guide](https://docs.docker.com/engine/install/) for
your distribution ([Ubuntu instructions](https://docs.docker.com/engine/install/ubuntu/)).
Your server account needs permission to use Docker; follow the installation guide's
user-access instructions. You also need Git (`sudo apt install git` on Ubuntu/Debian).
Hermes is built from its source on the server, so it always matches the code you
downloaded:

```bash
git clone https://github.com/Shaf2665/Hermes-router.git
cd Hermes-router
docker compose up -d --build
```

Then check:

```bash
docker compose ps
curl --fail http://127.0.0.1:8319/health
```

Look for a running `hermes-router` service and a JSON response containing
`"status":"ok"`. This confirms Hermes is running, not that a provider is connected.

### 3. Get the administrator key

Still on the server, in the `Hermes-router` folder, run:

```bash
docker compose exec hermes-router python -c "from pathlib import Path; print(next(x.split('=',1)[1] for x in Path('/app/data/.env').read_text().splitlines() if x.startswith('ADMIN_API_KEY=')))"
```

Keep this value private. Next, [open the dashboard through a tunnel](#open-the-dashboard-from-your-computer).

The named volume keeps your settings and usage. The restart policy starts Hermes
when Docker starts, unless you explicitly stopped the container. Check that the
Docker service starts at boot; verify a real request after your next planned reboot.
The dashboard's **Restart Now** button also works here: the Compose file sets
`HR_CONTAINER_RESTART=1`, so Hermes can restart its own container when asked.

## Linux without Docker

### 1. Download and start Hermes

Connect with SSH as above. You need Python 3.10+, Bash and Python's `venv` module.
On Ubuntu/Debian:

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip git
git clone https://github.com/Shaf2665/Hermes-router.git Hermes-router
cd Hermes-router
bash install.sh
./hermes-router start
```

Use the account that will run Hermes, and keep this source directory in place.
The installer sets `HOST=127.0.0.1` for a new installation. Keep that setting for
private access. In a second SSH session, open the same folder and get your key:

```bash
sed -n 's/^ADMIN_API_KEY=//p' .env
```

[Open the dashboard through a tunnel](#open-the-dashboard-from-your-computer),
connect a provider and get a test reply before setting up automatic startup.

### 2. Make it start after reboot

Stop the foreground router with **Ctrl+C**. From its source folder run:

```bash
./hermes-router service install
./hermes-router service status
curl --fail http://127.0.0.1:8319/health
```

On Linux with systemd, the installer creates and starts a service. It may ask for
sudo to install a system service, which runs as the account that invoked it.
Without sudo, it uses a user service and attempts to enable “lingering” so it can
start without a login. If that fails, ask your server administrator to enable
lingering for that account before relying on startup after reboot.

Use `./hermes-router restart` for this service. To inspect logs:

```bash
sudo journalctl -u hermes-router -n 50 --no-pager
```

For a user service, use `journalctl --user -u hermes-router -n 50 --no-pager`.
Check service status and send a test message after your next planned reboot.

## Open the dashboard from your computer

In a **new terminal on your own computer**, run:

```bash
ssh -N -L 8319:127.0.0.1:8319 YOUR_USER@YOUR_SERVER
```

This creates a private connection to the server. No output is normal; leave the
terminal open. Open [http://localhost:8319](http://localhost:8319) in your browser,
sign in with the server's administrator key, and follow
[Connect a provider and test it](setup.md#connect-a-provider-and-test-it).

Use `http://localhost:8319` for the extension, or `http://localhost:8319/v1` for
OpenAI-compatible clients. Both reach the server **while the tunnel is open**.

If port 8319 is already used on your computer, change only the tunnel's local port:

```bash
ssh -N -L 8320:127.0.0.1:8319 YOUR_USER@YOUR_SERVER
```

Then use `http://localhost:8320` in the browser/extension and
`http://localhost:8320/v1` in an OpenAI-compatible app. Ctrl+C closes the tunnel,
but Hermes continues running on the server.

## Windows Server

Docker Desktop is not supported on Windows Server. Install Hermes natively using
the [Windows instructions](setup.md#windows-without-docker), either at the server
console or through Remote Desktop. Use a regular account with Python 3.10+ and
write access to the installation folder. Test a reply in the server's browser first.

For remote access, use the SSH tunnel above if the server has an SSH service, or
the [HTTPS option](#optional-use-an-https-address). Keep `HOST=127.0.0.1` in `.env`
when using a tunnel or a reverse proxy on that server.

### Start automatically with Task Scheduler

1. Stop your foreground copy with **Ctrl+C**.
2. Open **Task Scheduler → Create Task** (not Create Basic Task).
3. Configure these fields, replacing the sample paths with your actual folder:

| Field | Value |
| --- | --- |
| Name | `Hermes Router` |
| User account | The account that owns the installation |
| Security option | Run whether user is logged on or not; leave highest privileges unchecked |
| Trigger | At startup; delay 30 seconds |
| Program/script | Full path to `venv\Scripts\python.exe`, such as `D:\Hermes-router\venv\Scripts\python.exe` |
| Arguments | `"D:\Hermes-router\router.py"` |
| Start in | `D:\Hermes-router` (no quotes) |
| Settings | Restart on failure after 1 minute; do not start a new instance if already running |
| Time limit | Disable “Stop the task if it runs longer than” |

4. Save; provide the account's Windows password if prompted. Your organization's
   policy may require an administrator to grant “Log on as a batch job”.
5. Right-click the task → **Run**, open `http://localhost:8319` on the server, and
   send a test message. Do not also run `start.ps1` while the task is running.

For a laptop, disable the task's AC-power-only condition if needed. Test again
after a planned reboot. Microsoft's [task scheduling reference](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-create)
explains the account and startup options. These steps also work on Windows PCs.

## macOS login startup

After [native Mac setup](setup.md#mac-or-linux-without-docker), run
`./hermes-router service install`. On macOS this prints a `launchd` configuration
and commands for your paths; it does **not** install a systemd service. Follow the
printed instructions after stopping the foreground copy. The LaunchAgent starts
at **user login**, not before anyone logs in after a reboot.

## Optional: use an HTTPS address

For clients that cannot keep an SSH tunnel open, put a reverse proxy on the server.
You need a domain you control, a DNS record pointing to the server, and ports
80/443 reachable by that proxy. Keep Hermes port 8319 private.

For example, after [installing Caddy](https://caddyserver.com/docs/install), place
this in its Caddyfile, replacing `ai.example.com` with your domain:

```text
ai.example.com {
    reverse_proxy 127.0.0.1:8319
}
```

Follow Caddy's [reverse-proxy guide](https://caddyserver.com/docs/quick-starts/reverse-proxy)
to run/reload it and obtain a certificate. Set `HR_TRUST_PROXY=1` in Hermes's
environment file and restart Hermes. For native installs that is the source
folder's `.env`; for Docker it is `/app/data/.env` inside the mounted volume.
Enable this only behind a trusted proxy that controls the forwarding headers,
with no direct public access to Hermes's port.

For Docker Compose, from the `Hermes-router` folder, copy that private file out with
`docker compose cp hermes-router:/app/data/.env ./hermes.env`, edit the setting (replace an
existing value rather than adding duplicates), copy it back with
`docker compose cp ./hermes.env hermes-router:/app/data/.env`, then
`docker compose restart hermes-router`. The copied file contains secrets; store it privately.

The dashboard/extension address is now `https://ai.example.com`; the OpenAI base
URL is `https://ai.example.com/v1`. Give each app its own application key. Do not
share the administrator key. For a fuller example, see [InstaBox hosting](instabox.md).

## Backup and update

Back up **before replacing source or a container**. Keep a copy of the previous
source/image version so you can return to it if an update fails.

### Docker run installation (prebuilt image)

In a private directory on the Docker host, run these one at a time:

```bash
docker stop hermes-router
mkdir hermes-backup
docker cp hermes-router:/app/data/. ./hermes-backup/
docker start hermes-router
```

Use a new backup folder name if one exists. This copy contains credentials and
usage; do not put it in a public/shared folder. On Linux/macOS restrict its access
with `chmod 700 hermes-backup`. On Windows keep it under your private user profile.

To update, pull the **specific new image tag from its release notes**, stop and
remove the old container, then repeat its `docker run` command with the new tag
and the **same volume, ports, environment options and restart policy**. Removing
the container does not remove the named volume; do not delete `hermes-data`.
Pulling alone does not change a running container. Preserve custom options if your
installation differs from this guide.

### Compose installation

From the original source folder, stop it with `docker compose stop`, copy its data
using `docker compose cp hermes-router:/app/data/. ../hermes-backup/`, then resume
with `docker compose start`. Create a private backup folder first as above.
Update the source with `git pull --ff-only`, then run `docker compose up -d --build` from that **same
folder** so it keeps using the same project volume. Never use `docker compose down -v`
when you want to keep your data.

### Native installation

Stop Hermes using its terminal, systemd service or scheduled task. Back up `.env`,
`auth.json`, `usage.db`, `instances.json`, and optional `cache.db`/`router_state.json`
where present. Update source, rerun the platform installer, then restart. Do not
copy live SQLite files while Hermes is writing them.

After any update, check `/health`, sign in again, and send a test message. If you
need to roll back, restore the previous source/image and, if its data format changed,
the matching backup while Hermes is stopped.

For dashboard-managed Docker instances, the launcher uses the named `<container-name>-data` volume mounted at `/data`. New containers keep `.env`, `auth.json`, `usage.db`, `instances.json`, `cache.db` and `router_state.json` there. Containers created with the initial foundation image may instead have `.env`, `usage.db` and `instances.json` in a separate `/app/data` volume. Before removing or recreating one, stop it and privately back up **both** `/data` and `/app/data`; preserve the administrator credential and SQLite ledger when migrating to `/data`. Do not overwrite existing files blindly or copy a live SQLite database. Updating the launcher does not migrate or modify existing containers.

For context sync, keep the persisted environment file containing `ROUTER_CONTEXT_CONFIG`.
If the server has custom changes, preserve and reconcile them before updating to `main`.
Build/install the matching extension and follow [Verify after upgrading](context-sync.md#verify-after-upgrading).

Old Docker installs may have stored `.env` and `auth.json` only under `/app`, outside
the volume. Copy those files out **before** removing that container. New images
read `/app/data`; migrate the saved files there before normal startup. Do not
assume an old unmounted container's settings will survive removal.

## Other hosting services

[InstaBox](instabox.md) has a dedicated Docker/HTTPS walkthrough. On managed Docker
hosts such as [Hugging Face Spaces](https://huggingface.co/docs/hub/spaces-sdks-docker),
check persistent-storage, sleeping and port requirements before installing. The
standard image listens on 8319; the host must route to the same port. Mount durable
storage at `/app/data`, or set the documented data-file environment variables to
that host's writable persistent directory. Ephemeral storage loses dashboard
settings and usage after recreation. A sleeping service cannot provide always-on
inference. A Linux server following this guide is the simpler starting point.

For connection/key errors, use [Troubleshooting](setup.md#troubleshooting).
