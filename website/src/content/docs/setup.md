---
title: "Set up on your computer"
description: "Install Hermes Router on Windows, macOS, Linux or Docker, connect a provider, and verify your first request."
---

This guide gets you from a fresh install to your first AI reply. For a machine
you connect to remotely, use [Server setup](/deployment/).

Choose **one** method below. Docker users do not need Python or Node.js.
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

Download the [source ZIP](https://github.com/Shaf2665/Hermes-router/archive/refs/heads/main.zip),
right-click it and choose **Extract All**. Open the extracted folder containing
`install.ps1`, `start.ps1` and `router.py`. Right-click inside that folder and
choose **Open in Terminal**. Use a PowerShell tab.

If you prefer Git, run this instead of downloading the ZIP:

```powershell
git clone https://github.com/Shaf2665/Hermes-router.git Hermes-router
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
git clone https://github.com/Shaf2665/Hermes-router.git Hermes-router
cd Hermes-router
bash install.sh
./hermes-router start
```

Without Git, download and extract the [source ZIP](https://github.com/Shaf2665/Hermes-router/archive/refs/heads/main.zip),
open Terminal in the folder containing `install.sh`, then run the last two commands.
Leave the terminal open; **Ctrl+C** stops Hermes. The optional `hr` shortcut added
by the installer may need a new terminal before it is available.

These steps also work in **WSL** on Windows, which gives you the full `hr` command set.

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
use [native Windows Server setup](/deployment/#windows-server) there.

### 2. Start Hermes

Download the source as in the [Windows](#2-download-hermes) or
[Mac/Linux](#2-download-install-and-start) steps (the ZIP or `git clone`; you do not
need to run the installer). Then, in the folder containing `docker-compose.yml`:

```bash
docker compose up -d --build
docker compose ps
```

Docker builds Hermes from that source, so it always matches the code you downloaded.
Look for a running `hermes-router` service. The Compose volume keeps keys, settings
and usage when the container is replaced, and the port is available only on your
computer. The dashboard's **Restart Now** button works too: the Compose file lets
Hermes restart its own container.

Published Docker Hub images can lag behind the source. Use one only if its release
notes list the features you need, and keep its `-v hermes-data:/app/data` volume.

### 3. Sign in

Open [http://localhost:8319](http://localhost:8319). In the same folder, get the
generated administrator key with this one-line command:

```bash
docker compose exec hermes-router python -c "from pathlib import Path; print(next(x.split('=',1)[1] for x in Path('/app/data/.env').read_text().splitlines() if x.startswith('ADMIN_API_KEY=')))"
```

Paste it into the sign-in screen. Continue below. No initial `.env` is required;
settings are saved in the mounted `/app/data`.

## Connect a provider and test it

These steps are the same for native, Docker and server installations.

### 1. Add your AI provider

1. Sign in to the dashboard with your **administrator key**.
2. Open **Providers** and choose a provider.
3. Click **Get API key** to open its website. Sign in/create your provider account
   and obtain a key. Check the available models and any billing requirements.
4. Paste the key into Hermes. Keep **Model selection** on *Automatically add free
   chat models*, or choose *Choose models manually*, click **Refresh model list**
   (or use the suggestions) and select at least one model your account can use.
5. Click **Save provider**.

With automatic model selection, Hermes looks up the provider's free chat models in
the background and uses them without a restart. Changing models, rotation and
settings later also applies immediately. One case still needs a restart: saving a
**new key with models chosen manually**. The dashboard then shows a banner; click
**Restart Now**, or restart Hermes the way you started it.

Saving a provider can send small background checks, so it may use provider quota.
Hermes is free software; the selected provider/model may charge for requests.

**Using Ollama or LM Studio?** Start the model server and load/download a model there
first. In **Providers**, choose **Local model (Ollama / LM Studio)**. Enter its
OpenAI-compatible URL, tick **No API key** if appropriate, select the model and
save; it applies immediately. Native Hermes on the same computer can usually use
`http://localhost:11434/v1` for Ollama or `http://localhost:1234/v1` for LM Studio.
Any other OpenAI-compatible server can be added as a **Custom provider**. See the
[provider guide](/providers/#custom-openai-compatible-providers).

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

Follow the [VS Code tutorial](/vscode-extension/), or use the values in Connect.
On the same computer, an OpenAI-compatible app uses:

```text
Base URL: http://localhost:8319/v1
Model: hermes-router
API key: your application key
```

The extension's Base URL is `http://localhost:8319` (a trailing `/v1` is accepted).
Anthropic clients use `http://localhost:8319` without `/v1`. See [API examples](/usage/).

## Stop, start and run after a reboot

| Installation | Stop | Start again |
| --- | --- | --- |
| Windows native | Ctrl+C in its terminal | Run `start.ps1` as shown above |
| Mac/Linux native | Ctrl+C in its terminal | `./hermes-router start` in the source folder |
| Docker Compose | `docker compose stop` | `docker compose start` in the source folder |
| Docker run (prebuilt image) | `docker stop hermes-router` | `docker start hermes-router` |

Docker's restart policy works when Docker starts; Docker Desktop must be running.
An explicitly stopped container stays stopped until you start it. For native
startup, see [Linux service](/deployment/#linux-without-docker),
[Windows Task Scheduler](/deployment/#windows-server), or
[macOS login startup](/deployment/#macos-login-startup).

## Version notes

These steps use the current `main` source. Update the router's dashboard and Python
modules together; do not copy individual new files into an older install.
Restarting an old container does not install new code: rebuild it from new source.

The published [VS Code 0.9.7 VSIX](https://github.com/Shaf2665/Hermes-router/releases/download/vscode-v0.9.7/hermes-router-0.9.7.vsix)
includes administrator controls and the dashboard-loading fix, but not context sync.
For dashboard-to-extension context sync, build **extension 0.9.10** from this source
as described in [VS Code Extension](/vscode-extension/), confirm your model
capacities, then follow [Verify after upgrading](/context-sync/#verify-after-upgrading).
**Settings → Context length** sets a minimum context; choose **Auto** to keep
ordinary routing until you want one.

The broader OmniRoute parity work remains staged. This source does not claim support
for every provider, Responses/Gemini/media endpoint, memory engine or agent protocol
in that roadmap.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| Python opens the Store or is not found | Install Python, enable PATH if offered, and open a new terminal. |
| Docker cannot connect to its engine | Start Docker Desktop/Engine; retry `docker info`. |
| Port already in use | Stop the other copy. On Windows use `start.ps1 -Port 8320`; for Docker Compose put `PORT=8320` in a `.env` file next to `docker-compose.yml` and run `docker compose up -d` again. Use port 8320 in the browser and app too. |
| Browser cannot open localhost:8319 | Check the terminal for errors, or run `docker compose logs --tail 30`. Keep the native process running. |
| Dashboard says unauthorized | Use `ADMIN_API_KEY` from this installation, not a provider/application key. |
| No model replies | Check the provider card in Providers (discovery result, selected models), its key and quota, then retry the Connect test. |
| Dashboard shows a restart banner | Click **Restart Now**. Saved settings are kept; browser sessions end, so sign in again. |
| Restart Now says restart is unsupported | Restart using your terminal (`start.ps1` / `./hermes-router start`), `docker compose restart`, or your service manager. A Docker container needs `HR_CONTAINER_RESTART=1` and a restart policy for the button; the Compose file sets both. |
| App says 401 | Copy a current application key from Connect into the app. |
| App says 429 | Check its limits in Application access and wait for the retry period. |
| Context-length error | Lower the extension's setting or configure a model with sufficient known capacity. See [context settings](/configuration/#context-length-and-the-vs-code-extension). |

Before changing versions, follow [Backup and update](/deployment/#backup-and-update).
