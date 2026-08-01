# Deployment & Platform Support

This guide shows you **exactly how to run hermes-router**, step by step, no prior server
experience needed. Pick the path that matches your computer and follow it top to bottom.

---

## First, the big picture

hermes-router has two pieces. Knowing this makes everything below make sense:

1. **The router (`router.py`)** — the actual program. It's plain Python, so it runs on
   **Windows, macOS, and Linux** equally well. This is the part that does the work.
2. **The `hr` command** — a friendly helper (`hr setup`, `hr auth add`, `hr status`, …). It's
   written in **bash**, so it works on Linux/macOS (and Windows via WSL2), but
   **not** in a plain Windows Command Prompt / PowerShell.

> Takeaway: the engine runs everywhere. If you're on Windows and don't want bash, you either
> use **Docker** or run the Python program directly — both are covered below.

---

## Which path should I pick?

| Your situation | Go to |
|---|---|
| "Just give me the easiest thing that works on any OS" | [Docker](#path-1--docker-easiest-any-os) |
| I'm on **Linux or macOS** | [Linux/macOS install](#path-2--linux--macos-the-hr-way) |
| I'm on **Windows** | [Windows](#path-3--windows) |
| I want to host it **online** | [Hugging Face Space](#path-4--hugging-face-space-host-it-online) |

After any path, jump to [Check it's working](#check-its-working) and
[Troubleshooting](#troubleshooting).

---

## Before you start: do you have the tools?

You only need the ones for *your* chosen path — don't install everything.

**Check if Python is installed** (needed for everything except the Docker path):

```bash
python3 --version      # macOS/Linux
python --version       # Windows
```

You want **3.10 or newer**. If it says "command not found" or an older version:
- **Windows:** download from [python.org](https://www.python.org/downloads/) and, on the
  first install screen, **tick "Add python.exe to PATH"**.
- **macOS:** `brew install python` (or grab it from python.org).
- **Linux:** `sudo apt install python3 python3-venv python3-pip` (Debian/Ubuntu).

**Check if Docker is installed** (only for the Docker path):

```bash
docker --version
```

If not, install **Docker Desktop** (Windows/macOS) or Docker Engine (Linux) from
[docker.com](https://www.docker.com/products/docker-desktop/).

For cloud inference you need at least one provider key; a configured local model needs no key.
See [providers.md](providers.md) for current access notes. Gemini
([aistudio.google.com](https://aistudio.google.com)) is one common starting point where its
free tier is available.

---

## Path 1 — Docker (easiest, any OS)

Works the same on Windows, macOS, and Linux. Nothing to install but Docker.

### Fastest: the prebuilt image (no clone, no build)

A multi-arch image (amd64 + arm64) is published on Docker Hub as
[`shafiq735/hermes-router`](https://hub.docker.com/r/shafiq735/hermes-router):

```bash
docker run -d -p 8319:8319 \
  -e GEMINI_API_KEYS=your-gemini-key \
  -e PROXY_API_KEYS=choose-a-secret-password \
  shafiq735/hermes-router
```

Add more `-e <PROVIDER>_API_KEYS=…` for other providers (see [providers.md](providers.md)).
Then `curl http://localhost:8319/health`. The plain image is designed for environment-based
configuration. For CLI-managed keys and persistent mutable configuration, use the `:cli`
image with its `/app/data` volume as shown in the Windows/VS Code section below.

> **Windows (PowerShell / Command Prompt): put it on one line.** The `\` line-continuation
> above is Linux/macOS shell syntax — on Windows it errors with `docker: invalid reference
> format` and `'-e' is not recognized`. Use a single line instead:
>
> ```powershell
> docker run -d --name hermes-router -p 8319:8319 -e GEMINI_API_KEYS=your-gemini-key -e PROXY_API_KEYS=replace-with-a-long-random-secret shafiq735/hermes-router
> ```
>
> (PowerShell's line-continuation character is a backtick `` ` ``, not `\`, if you want to split
> it across lines.) Set the VS Code extension's `hermesRouter.apiKey` to the same secret.

### Or build from source with Compose

**Step 1 — get the code.**

```bash
git clone https://github.com/Shaf2665/Hermes-router.git
cd Hermes-router
```

(No git? Download the ZIP from the GitHub page → "Code" → "Download ZIP", unzip, and `cd`
into the folder.)

**Step 2 — put your keys in a `.env` file.** Create a file named `.env` next to
`docker-compose.yml` with at least one provider key and your own proxy password:

```
GEMINI_API_KEYS=paste-your-gemini-key-here
PROXY_API_KEYS=choose-a-secret-password
```

`PROXY_API_KEYS` is the password **your app** will use to talk to the router — pick anything.

**Step 3 — start it.**

```bash
docker compose up -d
```

The first run downloads and builds the image (a minute or two). `-d` runs it in the
background.

**Step 4 — confirm it's alive.**

```bash
curl http://localhost:8319/health
```

You should see `{"status":"ok",...}`. Done — skip to [Check it's working](#check-its-working).

**Useful Docker commands:**

```bash
docker compose logs -f      # watch the logs
docker compose restart      # restart after changing .env
docker compose down         # stop it
```

---

## Path 2 — Linux / macOS (the `hr` way)

This gives you the full `hr` helper experience.

**Step 1 — one-line install.**

```bash
curl -fsSL https://raw.githubusercontent.com/Shaf2665/Hermes-router/main/get.sh | bash
```

This clones the repo, creates an isolated Python environment, installs dependencies, and
puts the `hr` command on your PATH — all at once.

**Step 2 — run the setup wizard.** It walks you through adding your first key and starting
the router:

```bash
hr setup
```

**Step 3 — confirm it's alive.**

```bash
hr status
```

You should see a dashboard of providers. That's it.

> Prefer to do it manually? `git clone` the repo, `cd` in, run `./install.sh`, then
> `hr setup`.

**Day-to-day commands:** `hr auth add <provider>` (add a key), `hr status` (health),
`hr restart` (apply changes), `hr update` (upgrade). Full list in the
[README](../README.md#commands).

---

## Path 3 — Windows

The router runs natively on Windows; you just choose how you want to manage it. Here are the
three options, easiest first.

### 3a. Docker Desktop (recommended)

1. Install **Docker Desktop** from [docker.com](https://www.docker.com/products/docker-desktop/)
   and start it (wait until the whale icon says "running").
2. Open **PowerShell** and run the prebuilt image **as a single line** (the `\` multi-line form
   in [Path 1](#path-1--docker-easiest-any-os) is Linux syntax and PowerShell rejects it):

   ```powershell
   docker run -d --name hermes-router -p 8319:8319 -e GEMINI_API_KEYS=your-gemini-key -e PROXY_API_KEYS=replace-with-a-long-random-secret shafiq735/hermes-router
   ```
3. In Docker Desktop → **Containers**, check that the row shows **Port(s) = `8319:8319`**. If
   that column is **blank**, the container has no published port (the `-p` was dropped) and
   nothing on your PC can reach it — remove it and re-run the command above (you can't add a port
   to an existing container):

   ```powershell
   docker rm -f hermes-router
   ```
4. Browse to <http://localhost:8319/health> — you should see `{"status":"ok",...}`.

This is the smoothest Windows experience — no Python, no bash, nothing to configure.

#### Connecting the VS Code extension to this container

The [VS Code extension](vscode-extension.md) talks to the router over HTTP, so it works against
your Docker container. In the extension settings:

- **`hermesRouter.baseUrl`** → `http://localhost:8319`
- **`hermesRouter.apiKey`** → the **same value as the container's `PROXY_API_KEYS`**.

> A red **`401 unauthorized — check hermesRouter.apiKey`** in the dashboard means those two
> values don't match — fix `apiKey` to equal `PROXY_API_KEYS` and click **Refresh**.

**Want the Add key / Restart / Model buttons to work too?** The plain image has no `hr` inside it,
so use the **`:cli`** image variant with a volume, and tell the extension the container name:

```powershell
docker run -d --name hermes-router -p 8319:8319 -v hermes-data:/app/data -e PROXY_API_KEYS=replace-with-a-long-random-secret shafiq735/hermes-router:cli
```

Then set **`hermesRouter.dockerContainer`** to `hermes-router`. Now the buttons run via
`docker exec`/`docker restart` against the container, and the `/app/data` volume keeps your keys
and settings across restarts. Full details: [vscode-extension.md](vscode-extension.md).

(Sticking with the plain image? Add providers by re-running with another `-e <PROVIDER>_API_KEYS=…`
and "restart" with `docker restart hermes-router`.)

### 3b. WSL2 (full `hr` experience)

WSL2 runs a real Ubuntu inside Windows, so everything behaves exactly like Linux.

1. Open **PowerShell as Administrator** and run:
   ```powershell
   wsl --install
   ```
   Restart when prompted; it installs Ubuntu and asks you to create a username/password.
2. Open **Ubuntu** from the Start menu (this is your Linux shell).
3. Inside Ubuntu, follow [Path 2 — Linux/macOS](#path-2--linux--macos-the-hr-way). `hr` and
   all its commands now work.

To reach the router from a Windows app, use `http://localhost:8319` — WSL2 forwards
localhost automatically.

### 3c. Native Python (no Docker, no bash)

Run the router directly. You won't have the `hr` command, but the router works fully.

**Step 1 — get the code** (PowerShell):

```powershell
git clone https://github.com/Shaf2665/Hermes-router.git
cd Hermes-router
```

**Step 2 — create an isolated environment and install dependencies:**

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

You'll see `(venv)` at the start of your prompt — that means it's active.

**Step 3 — set your keys** (for this terminal session):

```powershell
$env:GEMINI_API_KEYS = "paste-your-gemini-key"
$env:PROXY_API_KEYS  = "choose-a-secret-password"
```

**Step 4 — start the router:**

```powershell
python router.py
```

Leave this window open — it's now running on `http://localhost:8319`. Open a **second**
PowerShell window to test it (see [Check it's working](#check-its-working)).

> **Making keys stick:** the `$env:` lines only last for that window. To set them
> permanently, use Windows "Edit the system environment variables", or create a `.env` file
> in the folder (the router reads it on startup):
> ```
> GEMINI_API_KEYS=paste-your-gemini-key
> PROXY_API_KEYS=choose-a-secret-password
> ```
> You can also manage keys by editing `auth.json` directly:
> ```json
> { "providers": { "gemini": ["key1"], "openrouter": ["key2"] } }
> ```

---

## Path 4 — Hugging Face Space (host it online)

Want the router running in the cloud instead of your own computer? A **Docker** Space can host
it. Hugging Face currently requires an eligible paid account/organization plan to create
Docker Spaces, even when CPU Basic hardware itself is listed as free. Check the current
[Spaces overview](https://huggingface.co/docs/hub/spaces-overview) before choosing this path.
Follow these steps carefully — the **port** is the #1 configuration detail people miss.

**Step 1 — create the Space.** On [huggingface.co](https://huggingface.co) → your profile →
**New Space**, and fill the form like this:

| Field on the "Create a new Space" screen | Choose |
|---|---|
| **Owner / Space name** | your account / e.g. `hermes-router` (this becomes your URL) |
| **Short description** | optional — e.g. "Free-tier AI load balancer" |
| **License** | `mit` (matches the project) |
| **Select the Space SDK** | **Docker** — ⚠️ *not* Gradio/Static; it's a web server, not a Gradio app |
| **Choose a Docker template** | **Blank** — ⚠️ *not* Streamlit/Shiny/etc. We ship our own `Dockerfile`, so you want the empty starting point |
| **Space hardware** | **CPU Basic**, when available for your account — the router needs no GPU |
| **Storage Bucket** | leave **off** (keys go in Secrets, not files — see Step 4) |
| **Space Dev Mode** | leave **off** (PRO-only, not needed) |
| **Visibility** | **Public** — the app URL must be reachable; this is why Step 5 (a strong proxy key) matters |

> **Why Public?** A Public Space's app URL is openly reachable, which is what lets your app
> connect to it. *Private* Spaces require an HF token on every request (awkward for an agent),
> so Public + a strong `PROXY_API_KEYS` is the practical combo. Your secrets are **not** in the
> public repo (they live in Settings → Secrets), so they stay private either way.

Then click **Create Space**.

**Step 2 — add hermes-router's files (replacing HF's placeholder).** When the Space is
created, Hugging Face shows a "Get started with your Docker Space!" page with an example
**FastAPI app on port 7860**. **Ignore that example** — you're bringing hermes-router
instead. You need exactly three files, all from the
[GitHub repo](https://github.com/Shaf2665/Hermes-router): `router.py`, `requirements.txt`,
and `Dockerfile`.

Pick whichever way you're comfortable with:

*Easiest — upload in the browser (no git, no SSH key needed):*
1. Download the three files from the GitHub repo (open each file → the **download/raw**
   button).
2. On your Space page, open the **Files** tab → **Add file → Upload files**.
3. Drop in `router.py`, `requirements.txt`, and `Dockerfile`.
4. Write a short commit message and click **Commit changes to main**.

*Or with git (clone the Space, add the files, push):*
```bash
git clone https://<your-hf-username>@huggingface.co/spaces/<your-hf-username>/hermes-router
cd hermes-router
# copy router.py, requirements.txt and Dockerfile into this folder, then:
git add router.py requirements.txt Dockerfile
git commit -m "Add hermes-router"
git push
```
When git asks for a password, paste a **write** access token from
[huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) (not your account
password). SSH works too, but only after you add an SSH key under
**Settings → SSH and GPG Keys** — the token route above avoids that.

> Both methods keep the Space's own `README.md` (the one HF created), which you'll edit in the
> next step. Don't overwrite it with the GitHub repo's README — that one has no Space config.

**Step 3 — fix the port (don't skip this!).** This is the critical bit. Hugging Face serves
your app on **one** port, set by `app_port` (default **7860**), but the router listens on
**8319**. The README that HF auto-generates has **no `app_port` line at all** — so you must
add it, or the Space hangs on "Starting" forever. Edit the **Space's own `README.md`** and
make the YAML block at the very top look like this (the new line is `app_port: 8319`):

```yaml
---
title: Hermes Router
emoji: 🔀
colorFrom: indigo
colorTo: blue
sdk: docker
app_port: 8319
pinned: false
---
```

The `sdk: docker` and `app_port: 8319` lines are the ones that matter; the rest is cosmetic.

(Alternatively, leave `app_port` alone and add a Space variable `PORT=7860` so the router
listens where HF expects. Either works — just don't skip this step, or the page will show
"connection refused" / nothing.)

**Step 4 — add your keys as Secrets.** A Space's storage is wiped on every rebuild, so
**don't** rely on `auth.json`. Go to **Settings → Variables and secrets** and add (as
*Secrets*):

```
GEMINI_API_KEYS    = your-gemini-key
PROXY_API_KEYS     = a-strong-secret-you-choose
ROUTER_STATE_FILE  = /tmp/router_state.json
```

Add more provider keys (`OPENROUTER_API_KEYS`, etc.) the same way. `ROUTER_STATE_FILE` points
the ratings cache at `/tmp`, which is writable on a Space.

**Step 5 — 🔒 lock it down.** Your Space URL is **public** — anyone who finds it can spend
your quota. So `PROXY_API_KEYS` **must** be a strong secret you choose.

**Step 6 — wait for it to build,** then open your Space URL:
`https://<your-username>-<space-name>.hf.space/health` — you should see
`{"status":"ok",...}`.

**Step 7 — connect your app** to the public URL:

```python
from openai import OpenAI
client = OpenAI(
    base_url="https://<your-username>-<space-name>.hf.space/v1",
    api_key="<your PROXY_API_KEYS value>",
)
resp = client.chat.completions.create(
    model="hermes-router",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)
```

> **Heads-up:** free Spaces **go to sleep** when idle. The first request after a nap takes a
> while to wake the Space and may time out — just retry, or have your app retry automatically.

---

## Check it's working

Whichever path you took, verify with these three quick checks.

**1. Is it alive?**

```bash
curl http://localhost:8319/health
```
Expected: `{"status":"ok",...}`. (No `curl`? Paste `http://localhost:8319/health` into a web
browser.)

**2. Can it answer a real question?** Export your `PROXY_API_KEYS` value first:

```bash
export HERMES_ROUTER_KEY='replace-with-your-router-key'
curl http://localhost:8319/v1/chat/completions \
  -H "Authorization: Bearer $HERMES_ROUTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"hermes-router","messages":[{"role":"user","content":"Say hi in one word"}]}'
```
Expected: a JSON reply with the model's answer inside `choices[0].message.content`.

**3. Point your app at it.** Use base URL `http://localhost:8319/v1`, API key = your
`PROXY_API_KEYS`, model `hermes-router`. See [usage.md](usage.md) for full examples
(OpenAI SDK, Anthropic SDK, etc.).

**4. Open the dashboard.** Browse to **`http://localhost:8319/`** for the live monitoring
dashboard — provider health, request log, cache stats, per-key usage, and the **Instances** tab.
It asks for your proxy key once and remembers it in the browser. With Docker the mapped port
(`-p 8319:8319`) already exposes it to your host. Running on a remote box bound to
`HOST=127.0.0.1`? Tunnel it: `ssh -L 8319:127.0.0.1:8319 user@server`, then open
`http://localhost:8319/` locally. See [monitoring.md](monitoring.md) for the full dashboard tour.

The **Instances** tab can register other already-running routers, or launch Docker-managed
routers on additional host ports such as `8320` and `8321`. To use the Docker launch flow, the
manager host needs Docker access and an image matching `HERMES_INSTANCE_IMAGE` (default
`hermes-router:latest`). If you are working from a clone, build it once:

```bash
docker build -t hermes-router:latest .
```

For packaged installs, either tag/pull the image name you want and set `HERMES_INSTANCE_IMAGE`,
or create instances manually with Docker and register them as **Connect existing**.

---

## Troubleshooting

**Hugging Face Space stuck on "Starting" forever**
- This is the #1 issue, and it's the **port**. Open the **Logs → Container** tab: if you see
  `Serving on http://0.0.0.0:8319`, the router is fine — HF just isn't looking there. The
  auto-generated `README.md` does **not** include `app_port`, so HF probes its default 7860
  and never sees the app. Fix: add `app_port: 8319` to the README metadata block (Step 3) and
  commit. It'll rebuild and flip to "Running".

**`Connection refused` / page won't load**
- Is the router actually running? (Docker: `docker compose ps`; native: is the `python
  router.py` window still open?)
- On a **Hugging Face Space**, this is almost always the **port** — re-check Step 3 above
  (`app_port: 8319` or `PORT=7860`).

**Logs say `No providers configured` (Hugging Face Space)**
- You haven't added any keys yet. Go to **Settings → Variables and secrets** and add your
  provider keys + `PROXY_API_KEYS` as **Secrets** (Step 4). The only Space setting you need to
  touch is "Variables and secrets" — leave hardware, sleep, visibility, etc. at their
  defaults.

**`401 Unauthorized`**
- Your app's API key doesn't match `PROXY_API_KEYS`. Make them the same and try again.

**`All providers exhausted` (503)**
- No keys loaded, or all of them are rate-limited. Confirm a key is set (`hr auth list`, or
  check your `.env`/secrets), and add more — see [providers.md](providers.md).

**`hr: command not found`**
- The `hr` helper is Linux/macOS/WSL only. On native Windows use Docker or `python
  router.py` (Path 3). On Linux/macOS, re-run `./install.sh` or open a new terminal so PATH
  refreshes.

**Port `8319` already in use**
- Something else is using it. Set a different port: `PORT=8320` in `.env` (and point your app
  at the new port), then restart.

**Windows: `python` isn't recognized**
- Python isn't on your PATH. Reinstall from python.org and tick **"Add python.exe to PATH"**,
  or use the Docker path instead.

Still stuck? Run `hr doctor` (Linux/macOS/WSL) for an automated diagnosis, or check the logs
(`docker compose logs -f`, or `router.log`).

---

## Keep it running (survive reboots)

> **Important:** a plain `hr start` (or `hr setup`'s "start now") runs the router as a normal
> background process — it does **not** come back after a server reboot. To survive reboots, install
> it as a service. Pick the line that matches how you run it:

**`hr` (Linux):**

```bash
hr service install      # installs a systemd unit + enables it on boot
```

That's it — the router now starts on boot and restarts automatically if it crashes, and `hr restart`
manages it. `hr setup` also **offers this as a step**. Run as root (or with `sudo`) for a system
service; without either it installs a per-user service and enables *lingering* so it still starts at
boot. Check it with `hr service status`; remove it with `hr service uninstall`. (The unit name is
`hermes-router`, overridable with `HERMES_ROUTER_SERVICE`.)

**Docker:** already handled — `docker-compose.yml` sets `restart: unless-stopped`, so the container
comes back on reboot as long as Docker itself starts on boot (the default; `sudo systemctl enable
docker` if not).

**macOS:** there's no systemd — `hr service install` prints a ready-to-paste **launchd** plist you
drop in `~/Library/LaunchAgents` and `launchctl load -w`.

**Hugging Face Space:** free CPU hardware sleeps after inactivity; a later visit wakes it.
Paid hardware normally stays running unless you configure sleep. This is platform lifecycle
behavior, not a router restart guarantee.
