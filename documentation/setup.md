# Set up Hermes locally or on a server

These instructions apply to the **foundation preview in this source checkout**. The public `main` branch and previously published Docker images may still contain the earlier version. Use the tested preview source/archive supplied with this change; do not mix its dashboard or Python modules with an older install.

Get the preview from GitHub into a new directory:

```bash
git clone --branch codex/hermes-upgrade-design --single-branch https://github.com/Shaf2665/Hermes-router.git Hermes-router-preview
cd Hermes-router-preview
```

Without Git, download the [preview source ZIP](https://github.com/Shaf2665/Hermes-router/archive/refs/heads/codex/hermes-upgrade-design.zip) and extract it. Run the platform instructions below from that extracted folder. This branch contains the foundation changes; it has not been merged into `main`.

Hermes runs the API and dashboard together. The Vercel website is documentation, not your private router. You need Python 3.10+ for a native installation, or Docker with Compose. You also need one provider account/key or a running local model. Provider usage may cost money; Hermes does not supply a subscription.

| Where you run it | Installation | Starts after reboot |
| --- | --- | --- |
| Windows 10/11 | Native PowerShell or Docker Desktop in Linux-container mode | Task Scheduler for native; Docker after Desktop starts |
| Windows Server | Native PowerShell | Task Scheduler at startup |
| Linux computer/server | Native Python or Docker Engine + Compose | systemd for native; Docker restart policy |
| macOS | Native Python or Docker Desktop | launchd guidance or Docker after Desktop starts |

The images use Linux containers. Docker Desktop is [not supported on Windows Server](https://docs.docker.com/desktop/troubleshoot-and-support/faqs/windowsfaqs/); use the native instructions there, or a Linux VM running Docker. For Windows 10/11 with Docker already installed, start Desktop and select Linux containers. Check [Docker's current Windows requirements](https://docs.docker.com/desktop/setup/install/windows-install/) for supported Windows builds and WSL versions.

For an InstaBox VM, follow the [InstaBox hosting guide](instabox.md) for a public HTTPS dashboard and persistent storage.

## Published packages (2026-11-04)

Docker Hub provides `shafiq735/hermes-router:providers-20261104` and `:providers-20261104-cli`, for Linux AMD64 and ARM64. The `latest` and `cli` tags track this release. Use a dated tag for reproducible installs.

For a fresh install without building source, run this single command in PowerShell or Bash:

```bash
docker run -d --name hermes-router --restart unless-stopped -e HR_CONTAINER_RESTART=1 -p 127.0.0.1:8319:8319 -v hermes-data:/app/data shafiq735/hermes-router:providers-20261104
```

Use `providers-20261104-cli` instead if you need `docker exec hermes-router hr status`. Open `http://localhost:8319` and retrieve the administrator credential with:

```bash
docker exec hermes-router python -c "from pathlib import Path; print(next(x.split('=',1)[1] for x in Path('/app/data/.env').read_text().splitlines() if x.startswith('ADMIN_API_KEY=')))"
```

The matching [VS Code 0.9.7 VSIX](https://github.com/Shaf2665/Hermes-router/releases/download/vscode-v0.9.7/hermes-router-0.9.7.vsix) can be installed using **Extensions: Install from VSIX…**. Use extension version 0.9.6 or newer for the administrator command.

Before upgrading an existing container, back up its configuration and mounted data. Pulling an image does not upgrade a running container: recreate it with the same volume, environment variables, ports and restart policy. Old installs that stored `.env` or `auth.json` only inside `/app` must copy them into the mounted data directory before recreation. Keep application keys for inference; retrieve the separate administrator key for dashboard access. See the backup section below.

## 1. Run on your computer

### Windows (PowerShell)

Install Python 3.10+ with its PATH option enabled. Extract the preview archive to a folder you can write to, then open PowerShell there. For example:

```powershell
cd D:\Hermes-router
powershell -ExecutionPolicy Bypass -File .\install.ps1
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

The execution-policy override applies only to that invocation. The installer creates `venv`, installs requirements and preserves existing settings. `start.ps1` listens only on localhost. Keep that terminal open; Ctrl+C stops Hermes. Use `-Port 8320` if 8319 is occupied.

Open [http://localhost:8319](http://localhost:8319). In another PowerShell window, read your locally stored administrator key:

```powershell
Get-Content .env | Select-String '^ADMIN_API_KEY='
```

Copy only the value after `=` into the dashboard sign-in. This is a local secret; do not put it in an SDK, screenshot or shared workspace settings. Restarting signs browser sessions out.

### macOS / Linux / WSL

From the source folder:

```bash
bash install.sh
./hermes-router start
```

Open [http://localhost:8319](http://localhost:8319). In a second terminal, in the same folder:

```bash
sed -n 's/^ADMIN_API_KEY=//p' .env
```

Paste that value into the dashboard. `install.sh` provides the optional `hr` shortcut; the explicit `./hermes-router` command also works before opening a new shell. Ctrl+C stops a foreground router.

For a native installation that should survive reboot, follow the Linux systemd or Windows Task Scheduler steps below. On macOS, `hr service` prints launchd guidance.

### Docker on your computer

Start Docker Desktop (or Docker Engine), then run from the preview source folder:

```bash
docker compose up -d --build
docker compose ps
docker compose logs --tail 30
```

No pre-existing `.env` is required. Open [http://localhost:8319](http://localhost:8319). Retrieve the generated administrator key locally:

```bash
docker compose exec hermes-router python -c "from pathlib import Path; print(next(x.split('=',1)[1] for x in Path('/app/data/.env').read_text().splitlines() if x.startswith('ADMIN_API_KEY=')))"
```

The named volume `hermes-data` persists credentials, configuration, usage and optional cache. The host port is bound to `127.0.0.1`, so it is not publicly exposed. `docker compose stop` stops the service; `docker compose start` resumes it. Do not use `down -v` if you want to keep your data.

## 2. Connect a provider and send your first request

1. Sign in with the **administrator key**. Existing application keys now authenticate inference only.
2. Open **Providers**, add your provider key and check the selected model. For a local model, configure `LOCAL_BASE_URL` and `LOCAL_MODEL` in the environment file instead (see below).
3. If the dashboard says a restart is required, restart the actual hosting process. Windows: Ctrl+C and run `start.ps1` again. Docker: `docker compose restart`. Native Linux/macOS foreground: Ctrl+C and start again. For a systemd service or CLI-managed background process, use `hr restart`. Browser restart is supported only for a verified CLI-managed standalone process; other hosts show instructions.
4. Open **Connect**, create an **application key**, and save it when it is shown. This is the key for your coding tool or SDK, not the administrator key.
5. Choose **Send test message**. It sends a real, small request through the selected provider and may consume quota. A successful `/health` response alone does not prove inference works.
6. Copy the endpoint, model and application key into your client. On the same computer: OpenAI base URL `http://localhost:8319/v1`, model `hermes-router`. Anthropic clients use `http://localhost:8319`.

For a simple terminal request on Linux/macOS:

```bash
read -rs -p 'Application key: ' HERMES_KEY
echo
curl --fail-with-body http://localhost:8319/v1/chat/completions \
  -H "Authorization: Bearer $HERMES_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"hermes-router","messages":[{"role":"user","content":"Say hello in one short sentence."}],"max_tokens":64}'
unset HERMES_KEY
```

Use the dashboard test on Windows, or these PowerShell commands (the key is prompted rather than embedded in your shell history):

```powershell
$applicationKey = Read-Host 'Application key'
$body = @{ model='hermes-router'; messages=@(@{role='user'; content='Say hello in one short sentence.'}); max_tokens=64 } | ConvertTo-Json -Depth 4
Invoke-RestMethod -Method Post -Uri 'http://localhost:8319/v1/chat/completions' -Headers @{Authorization="Bearer $applicationKey"} -ContentType 'application/json' -Body $body
Remove-Variable applicationKey
```

In VS Code, keep the application key for chat. Use the extension's dedicated administrator connection command only if you want global status or remote management. Admin credentials are stored separately in VS Code SecretStorage.

### Local model instead of a cloud provider

For a native Hermes process and a local OpenAI-compatible server, add the actual URL/model ID to `.env`, then restart:

```dotenv
LOCAL_BASE_URL=http://127.0.0.1:11434/v1
LOCAL_MODEL=your-installed-model-id
```

Inside Docker, `127.0.0.1` means the container. Docker Desktop can use `http://host.docker.internal:11434/v1` if the model server accepts that connection. On Linux, use a reachable private host address or configure a host-gateway mapping explicitly. Local-first routing can still fall back to cloud providers; do not interpret it as a local-only privacy guarantee.

## 3. Run on a server

### Linux with Docker

The simplest server path uses Docker Compose and SSH access. Install Docker Engine and the Compose plugin using [Docker's instructions for your distribution](https://docs.docker.com/engine/install/); for Ubuntu use the [official Ubuntu guide](https://docs.docker.com/engine/install/ubuntu/).

Transfer the preview source archive from your computer using `scp`, or use a published tested release checkout once available. For example, when the supplied archive is named `hermes-foundation.zip`:

```bash
scp hermes-foundation.zip USER@SERVER:~/
ssh USER@SERVER
mkdir -p ~/hermes-router-preview
unzip ~/hermes-foundation.zip -d ~/hermes-router-preview
cd ~/hermes-router-preview
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:8319/health
```

Replace `USER` and `SERVER` with your SSH account and server address. Use a new directory for a first install. The `unzip` utility must be installed. Docker commands require an account authorized to use the daemon; follow your server's administration policy rather than changing socket permissions.

On your **own computer**, open an SSH tunnel:

```bash
ssh -N -L 8319:127.0.0.1:8319 USER@SERVER
```

Leave the tunnel open and visit [http://localhost:8319](http://localhost:8319). If a local Hermes already uses that port, use `-L 8320:127.0.0.1:8319` and visit `http://localhost:8320`. Retrieve the administrator key with the Docker command above in your SSH session, then follow the provider/test steps. Your client base URL uses the **local tunnel port**.

This needs no public router port or domain. `restart: unless-stopped` restarts the container with Docker after a reboot unless you explicitly stopped it; verify the Docker service starts at boot.

### Linux directly (without Docker)

Transfer and extract the same archive to a permanent directory owned by the account that will run Hermes. Use a Python 3.10+ installation with its `venv` module and Bash; on Ubuntu/Debian, install the `python3-venv` package if creating the environment fails.

```bash
cd ~/hermes-router-preview
bash install.sh
./hermes-router start
```

Use the SSH tunnel above, retrieve `ADMIN_API_KEY` from this directory's `.env`, and complete the provider/test flow. Once that works, stop the foreground process with Ctrl+C and install the existing systemd service:

```bash
./hermes-router service install
./hermes-router service status
curl --fail http://127.0.0.1:8319/health
```

Run the installer as the intended non-root service account. It may request sudo to install the system unit; the unit runs as that account. Without sudo, it uses a user unit and attempts to enable lingering for startup without an interactive login. If that fails, an administrator must enable lingering before relying on boot startup. Keep the source directory in place: the service runs from it.

Use `./hermes-router restart` after settings changes. For a system unit, inspect logs with `sudo journalctl -u hermes-router -n 50 --no-pager`; for a user unit, use `journalctl --user -u hermes-router -n 50 --no-pager`. Check the service and a request after the next reboot. Keep `HOST=127.0.0.1`; use the tunnel or HTTPS proxy for remote clients.

### Windows Server directly (also works on Windows 10/11)

Use a regular dedicated account with access to Python 3.10+ and write access to the extracted source directory. Follow the PowerShell install/start instructions above and complete a local dashboard test first. Hermes does not require WSL, Git Bash, Docker, Node, or an administrator account to run.

For unattended startup, use Windows **Task Scheduler → Create Task**. The [Microsoft task scheduling reference](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-create) covers startup triggers and account options.

| Task field | Value |
| --- | --- |
| Name | `Hermes Router` |
| User account | The dedicated account that owns the installation |
| Security option | Run whether user is logged on or not; leave highest privileges unchecked |
| Trigger | At startup, with a 30-second delay |
| Action/program | Full path to `venv\Scripts\python.exe`, for example `D:\Hermes-router\venv\Scripts\python.exe` |
| Action/arguments | `"D:\Hermes-router\router.py"` (replace with your actual source path) |
| Action/start in | `D:\Hermes-router` (your source directory, without quotes) |
| Conditions | Disable the AC-power condition if running on a laptop |
| Settings | Allow on-demand run; restart on failure after 1 minute; do not start a new instance if already running |
| Execution time limit | Disable "Stop the task if it runs longer than" |

Save with the account's Windows credentials when prompted. The account may need the "Log on as a batch job" right under your server's policy. Stop the foreground copy before selecting **Run** on the task; only one process should use the port and data directory. Verify:

```powershell
Get-ScheduledTask -TaskName 'Hermes Router' | Select-Object TaskName, State
Invoke-RestMethod 'http://127.0.0.1:8319/health'
```

Open the dashboard on the server (for example through an RDP session), or use an SSH tunnel if Windows OpenSSH Server is already configured. For shared remote access, configure an HTTPS reverse proxy as below. The default router port remains loopback-only; opening a firewall rule alone will not expose it.

Use Task Scheduler **End**, then **Run** to restart after a configuration change. End stops this foreground Python task; confirm the port is released before starting again. Recheck its running state and `/health` after a reboot. Task history and Event Viewer help diagnose startup failures. Keep `.env`, `auth.json`, and the usage database private to the service account and administrators.

### Optional: HTTPS address for a team

Use a domain you control and a trusted reverse proxy. For example, with Caddy installed on the host, a DNS record pointing at the server, and ports 80/443 directed to Caddy:

```text
ai.example.com {
    reverse_proxy 127.0.0.1:8319
}
```

See [Caddy's reverse-proxy guide](https://caddyserver.com/docs/quick-starts/reverse-proxy) for certificate and service setup. Keep port 8319 loopback-only. Enable `HR_TRUST_PROXY=1` in the router's persisted `/app/data/.env` and restart before using browser sessions through HTTPS. Enable that setting only when the router is reachable exclusively through a trusted proxy which overwrites forwarding headers. Native installs use their own `.env`.

Application base URL: `https://ai.example.com/v1`; administrator dashboard: `https://ai.example.com/dashboard`. Give each client its own application key and limits. Share neither the admin key nor one common app key with the entire team. Key labels identify applications; this release is not an SSO/user-invitation service.

Money budgets are estimated-spend controls. Under a money limit, candidates with unknown pricing or an unbounded output budget are rejected. They do not guarantee that a provider's invoice exactly matches Hermes's estimates.

## 4. Backup, update and recover

For Docker, stop the service briefly and copy its data directory to a private backup directory on the host:

```bash
docker compose stop
mkdir -p ../hermes-backup
chmod 700 ../hermes-backup
docker compose cp hermes-router:/app/data/. ../hermes-backup/
docker compose start
```

The backup contains secrets; keep it private. Before an update, retain the previous source version/image and this backup. Apply the new tested source, then run `docker compose up -d --build`; verify `/health`, admin sign-in and a user-triggered message. Roll back by restoring the previous source/image and, if the storage format changed, its matching backup while the router is stopped. Do not overwrite live SQLite files.

For native installs, stop Hermes and back up `.env`, `auth.json`, `usage.db`, `instances.json`, and optional `cache.db`/`router_state.json` where present. Run the installer again after updating source, then restart. Existing settings are preserved.

Lost admin credential: retrieve `ADMIN_API_KEY` from the local environment file using filesystem/SSH access. To rotate it, replace its value with a new strong random value and restart. If the value is supplied by a service/container environment variable, rotate that source too. Never reset all client keys merely to recover dashboard access.

Application key revoked or replaced: create a new key through the authenticated dashboard and update that client. Revocation rejects new requests immediately; an already admitted response may finish. Use the usage screen to distinguish current, estimated and reported amounts.

## Troubleshooting

| Symptom | Next action |
| --- | --- |
| Docker cannot connect to its engine | Start Docker Desktop/Engine and rerun `docker compose ps`. |
| Port already occupied | Stop the other process or choose another host port; update the browser/client URL too. |
| Dashboard rejects an old proxy key | Use the separate `ADMIN_API_KEY`; your app keeps its inference key. |
| No configured/compatible provider | Save a provider key and models in the dashboard; changes apply live. Check tools/vision/model eligibility. |
| Provider key saved but test still fails | Check the model ID, provider key and request error; saving a key is not a successful connectivity test. |
| 429 from Hermes | Check this application's request/token/money limit and the retry delay. |
| Everything is unavailable | Check provider credentials, quotas and network access; failover needs another eligible provider. |
| Remote dashboard works, client cannot connect | Use the server's HTTPS address or the client's own SSH tunnel; `localhost` always means the machine running that client. |
| Restart is unsupported in the dashboard | Restart using `start.ps1`, the service manager or `docker compose restart` on the host. |

The broader OmniRoute parity work remains staged. This preview does not claim support for every provider, Responses/Gemini/media endpoint, memory engine or agent protocol in that roadmap.
