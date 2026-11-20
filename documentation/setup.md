# Set up Hermes on your computer

This guide gets you from a fresh install to your first AI reply. For a machine
you connect to remotely, use [Server setup](deployment.md).

Choose **one** method below. Docker users do not need Python, Git or Node.js.
Native users need Python; Git is optional. Every method serves the dashboard and
API together on port **8319**.

- [Windows without Docker](#windows-without-docker)
- [Mac or Linux without Docker](#mac-or-linux-without-docker)
- [Docker on your computer](#docker-on-your-computer)

## Windows without Docker

### 1. Install Python

Install [Python](https://www.python.org/downloads/windows/) **3.10 or newer**.
If the installer offers **Add Python to PATH**, enable it. Open a new PowerShell
window and run `python --version`. Continue when it shows a version such as
`Python 3.12.x`, not a Store prompt or “command not found”.

### 2. Download Hermes

Download the [source ZIP](https://github.com/Shaf2665/Hermes-router/archive/refs/heads/codex/hermes-upgrade-design.zip),
right-click it and choose **Extract All**. Open the extracted folder containing
`install.ps1`, `start.ps1` and `router.py`. Right-click inside that folder and
choose **Open in Terminal**. Use a PowerShell tab.

If you prefer Git, run this instead of downloading the ZIP:

```powershell
git clone --branch codex/hermes-upgrade-design --single-branch https://github.com/Shaf2665/Hermes-router.git Hermes-router
cd Hermes-router
```

### 3. Install and start

Run these commands one at a time:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
powershell -ExecutionPolicy Bypass -File .\start.ps1
```

The first installs Python libraries into Hermes's own `venv` folder. The second
starts Hermes. Leave this terminal open. **Ctrl+C** stops it. The execution-policy
option affects only that command, not your system policy.

### 4. Sign in

Open [http://localhost:8319](http://localhost:8319). In a **second PowerShell tab**,
open the same Hermes folder and run:

```powershell
Get-Content .env | Select-String '^ADMIN_API_KEY='
```

Copy only the value after `=` into the dashboard sign-in. Keep this key private.
Continue to [Connect a provider and test it](#connect-a-provider-and-test-it).

## Mac or Linux without Docker

### 1. Check Python

Open Terminal and run `python3 --version`. You need **Python 3.10 or newer**.
On a Mac, install it from [python.org](https://www.python.org/downloads/macos/).
On Ubuntu/Debian, install Python and its environment tools with:

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip git
```

### 2. Download, install and start

```bash
git clone --branch codex/hermes-upgrade-design --single-branch https://github.com/Shaf2665/Hermes-router.git Hermes-router
cd Hermes-router
bash install.sh
./hermes-router start
```

Without Git, download and extract the [source ZIP](https://github.com/Shaf2665/Hermes-router/archive/refs/heads/codex/hermes-upgrade-design.zip),
open Terminal in the folder containing `install.sh`, then run the last two commands.
Leave the terminal open; **Ctrl+C** stops Hermes. The optional `hr` shortcut added
by the installer may need a new terminal before it is available.

### 3. Sign in

Open [http://localhost:8319](http://localhost:8319). In a **second terminal**, open
the same Hermes folder and run:

```bash
sed -n 's/^ADMIN_API_KEY=//p' .env
```

Paste the value into the dashboard. Continue below to connect a provider.

## Docker on your computer

### 1. Start Docker

Use [Docker Desktop](https://docs.docker.com/desktop/) on a supported Windows PC or
Mac, or [Docker Engine](https://docs.docker.com/engine/install/) on Linux. Wait for
Docker to start, then run `docker info`. Continue when it shows server information
rather than a connection error.

Hermes uses **Linux containers**. Check Docker's [Windows requirements](https://docs.docker.com/desktop/setup/install/windows-install/).
Docker Desktop is [not supported on Windows Server](https://docs.docker.com/desktop/troubleshoot-and-support/faqs/windowsfaqs/);
use [native Windows Server setup](deployment.md#windows-server) there.

### 2. Start Hermes

Copy this as **one line** into PowerShell or Terminal:

```bash
docker run -d --name hermes-router --restart unless-stopped -e HR_CONTAINER_RESTART=1 -p 127.0.0.1:8319:8319 -v hermes-data:/app/data shafiq735/hermes-router:context-20261104.1
```

Check it with `docker ps`. Look for a running container named `hermes-router`.
The `hermes-data` volume keeps keys, settings and usage after container replacement.
The port is available only on your computer.

If that name already exists, use `docker start hermes-router` to resume it. For an
upgrade, use [Backup and update](deployment.md#backup-and-update) instead of deleting it.

### 3. Sign in

Open [http://localhost:8319](http://localhost:8319). Get the generated administrator
key with this one-line command:

```bash
docker exec hermes-router python -c "from pathlib import Path; print(next(x.split('=',1)[1] for x in Path('/app/data/.env').read_text().splitlines() if x.startswith('ADMIN_API_KEY=')))"
```

Paste it into the sign-in screen. Continue below.

### Optional: build the source yourself

For a source change not in a published image, download the source as described above
and run from the folder containing `docker-compose.yml`:

```bash
docker compose up -d --build
docker compose ps
```

This creates a separate Compose installation and volume. It does not upgrade the
`docker run` installation. Stop that installation first if it already uses port 8319.
To read the Compose installation's key, use the command above with
`docker compose exec hermes-router` instead of `docker exec hermes-router`.
No initial `.env` is required; settings are saved in the mounted `/app/data`.

## Connect a provider and test it

These steps are the same for native, Docker and server installations.

### 1. Add your AI provider

1. Sign in to the dashboard with your **administrator key**.
2. Open **Providers** and choose a provider.
3. Click **Get API key** to open its website. Sign in/create your provider account
   and obtain a key. Check the available models and any billing requirements.
4. Paste the key into Hermes. Click **Refresh model list**, or use the suggestions.
5. Select at least one model your account can use, then click **Save provider**.

Current dashboard changes apply immediately. An older router may ask for a restart.
Saving a provider can send small background checks, so it may use provider quota.
Hermes is free software; the selected provider/model may charge for requests.

**Using Ollama or LM Studio?** Start the model server and load/download a model there
first. In **Providers**, choose **Local model (Ollama / LM Studio)**. Enter its
OpenAI-compatible URL, choose **No API key** if appropriate, select the model and
save. Native Hermes on the same computer can usually use `http://localhost:11434/v1`
for Ollama or `http://localhost:1234/v1` for LM Studio. See the
[provider guide](providers.md#custom-providers-and-local-models).

Inside Docker, `localhost` is the container. Docker Desktop can use
`host.docker.internal` to reach a model on the host if its server accepts that
connection. Linux Docker needs a reachable host address or host-gateway setup.
A remote Hermes server cannot reach your PC's model through its own `localhost`.

### 2. Create an application key

Open **Connect**, enter an application name such as `My VS Code`, and create a key.
Copy it while it is shown. This key goes into your app; the administrator key stays
with the owner. Manage keys under **Settings → Application access**.

### 3. Send a test message

Click **Send test request** in Connect. It sends a real, small AI request.
**A reply means setup is working.** If it fails, check the provider key, model and
quota. A green `/health` response alone does not test your AI provider.

### 4. Connect VS Code or another app

Follow the [VS Code tutorial](vscode-extension.md), or use the values in Connect.
On the same computer, an OpenAI-compatible app uses:

```text
Base URL: http://localhost:8319/v1
Model: hermes-router
API key: your application key
```

The extension's Base URL is `http://localhost:8319` (a trailing `/v1` is accepted).
Anthropic clients use `http://localhost:8319` without `/v1`. See [API examples](usage.md).

## Stop, start and run after a reboot

| Installation | Stop | Start again |
| --- | --- | --- |
| Windows native | Ctrl+C in its terminal | Run `start.ps1` as shown above |
| Mac/Linux native | Ctrl+C in its terminal | `./hermes-router start` in the source folder |
| Docker run | `docker stop hermes-router` | `docker start hermes-router` |
| Docker Compose | `docker compose stop` | `docker compose start` in the source folder |

Docker's restart policy works when Docker starts; Docker Desktop must be running.
An explicitly stopped container stays stopped until you start it. For native
startup, see [Linux service](deployment.md#linux-without-docker),
[Windows Task Scheduler](deployment.md#windows-server), or
[macOS login startup](deployment.md#macos-login-startup).

## Version notes

The examples use the published Docker image **`context-20261104.1`** or the
**`codex/hermes-upgrade-design`** source branch. These include the current dashboard,
separate administrator/application keys and live provider changes. The older
`main/get.sh` installer is not the installation path used in this guide.

Extension **0.9.7** is available as a [downloadable VSIX](https://github.com/Shaf2665/Hermes-router/releases/download/vscode-v0.9.7/hermes-router-0.9.7.vsix).
The Docker image above includes **Settings → Context length**. Extension **0.9.9+**
follows that dashboard setting; older extensions keep their own input budget.
Choose **Auto** to preserve ordinary routing until you want to set a minimum.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| Python opens the Store or is not found | Install Python, enable PATH if offered, and open a new terminal. |
| Docker cannot connect to its engine | Start Docker Desktop/Engine; retry `docker info`. |
| Port already in use | Stop the other copy. On Windows use `start.ps1 -Port 8320`; for Docker change only the host port to `127.0.0.1:8320:8319`. Use port 8320 in the browser and app too. |
| Browser cannot open localhost:8319 | Check the terminal for errors, or run `docker logs --tail 30 hermes-router`. Keep the native process running. |
| Dashboard says unauthorized | Use `ADMIN_API_KEY` from this installation, not a provider/application key. |
| No model replies | Select a model in Providers, check its key/quota, then retry the Connect test. |
| App says 401 | Copy a current application key from Connect into the app. |
| App says 429 | Check its limits in Application access and wait for the retry period. |
| Context-length error | Lower the extension's setting or configure a model with sufficient known capacity. See [context settings](configuration.md#context-windows). |
| Web restart is unsupported | Restart using your terminal, Docker or service manager. Your saved settings remain. |

Before changing versions, follow [Backup and update](deployment.md#backup-and-update).
