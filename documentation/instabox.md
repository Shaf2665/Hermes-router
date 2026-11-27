# Host Hermes Router on InstaBox

This deployment runs the dashboard and API on an existing Docker-capable InstaBox VM,
behind a small Nginx gateway and InstaBox's public HTTPS address. The router image is
built on the VM from the source you clone, so the deployment always matches that code.
You still need your own upstream provider account.

## Install

Run these commands in the VM terminal. Use a new directory for a fresh installation;
keep an existing `data` directory when upgrading.

```bash
git clone --depth 1 https://github.com/Shaf2665/Hermes-router.git /data/home/hermes-source
mkdir -p /data/home/hermes-router/data
chmod 700 /data/home/hermes-router /data/home/hermes-router/data
cp /data/home/hermes-source/deploy/instabox/compose.yaml /data/home/hermes-router/
cp /data/home/hermes-source/deploy/instabox/default.conf.template /data/home/hermes-router/
cd /data/home/hermes-router
instabox-port publish 8319 --name Hermes-Router --public --json
```

Copy the **hostname only** from the returned HTTPS URL (without `https://` or a path)
into `.env` beside `compose.yaml`:

```dotenv
HERMES_PUBLIC_HOST=8319-YOUR-MACHINE.instabox.tech
```

Use the actual hostname supplied by InstaBox, not the example above. If you cloned the
source somewhere other than `/data/home/hermes-source`, also add
`HERMES_SOURCE=/path/to/your/clone`. Then build and start:

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

Open the published HTTPS URL with `/dashboard` appended. First startup generates
separate administrator and application keys in `data/.env`. To read them privately
in your VM terminal:

```bash
docker compose exec -T hermes python -c "from pathlib import Path; print('\n'.join(x for x in Path('/app/data/.env').read_text().splitlines() if x.startswith(('ADMIN_API_KEY=', 'PROXY_API_KEYS='))))"
```

Use the value of `ADMIN_API_KEY` to sign in. In **Providers**, add a provider key,
choose its models, and save. If the dashboard asks for a restart, use its **Restart**
action: the supplied Compose file sets `HR_CONTAINER_RESTART=1` together with
`restart: unless-stopped`, so Hermes exits and Docker starts it again with the saved
settings. Keep both settings together. The router only restarts its own container when
it is the container's main process (PID 1); it needs no access to the Docker socket.
A restart interrupts in-flight requests and ends browser sessions; sign in again after
a few seconds. For a terminal fallback:

```bash
cd /data/home/hermes-router
docker compose restart hermes
```

For coding tools, use the value of `PROXY_API_KEYS`, the published URL followed by
`/v1`, and model `hermes-router`. Keep the administrator key for dashboard management.

## Set a context minimum

Open **Settings → Context length** and select **16K**, **128K**, or another size.
Hermes uses only models with at least that known capacity. Saves apply immediately
and survive a container update. **Auto** adds no server minimum.

## Ports and HTTPS

- Publish **8319**, HTTP protocol, through InstaBox's **Ports** tab or the command above.
- InstaBox supplies the public HTTPS URL. No separate public ports 80 or 443 are needed.
- SSH uses InstaBox's connection details, normally gateway port 2222; it is separate
  from the web app. Do not publish the Docker socket or internal Hermes container.

The gateway fixes the upstream Host to `HERMES_PUBLIC_HOST`. Waitress uses its
[documented HTTPS scheme setting](https://docs.pylonsproject.org/projects/waitress/en/latest/reverse-proxy.html),
so browser sign-in can validate the actual public origin even when InstaBox rewrites
request headers. Browser origin and CSRF checks remain enabled, and the session cookie
is marked `Secure`. Hermes itself has no host port mapping; only the gateway exposes
8319. This configuration assumes access through InstaBox's HTTPS URL, not plain HTTP.

## Storage, restarts, and updates

Settings, provider credentials, usage, and cache files live in
`/data/home/hermes-router/data`. Keep that directory private and back it up privately;
it contains secrets. For a consistent backup, stop this Compose deployment, copy the
whole directory, then start it again. Do not put the backup in a public repository.

```bash
cd /data/home/hermes-router
docker compose logs --tail 100
docker compose restart hermes
```

Containers have a restart policy. This cannot override InstaBox pausing or stopping
the VM. To update Hermes, pull the source and rebuild:

```bash
git -C /data/home/hermes-source pull --ff-only
cd /data/home/hermes-router
docker compose up -d --build
docker compose restart gateway
```

Restarting the gateway after recreating Hermes refreshes its internal Docker address.
Your provider settings remain in the same data directory. If the update changed
`deploy/instabox/`, copy the new `compose.yaml` and `default.conf.template` first.

## Free-plan limits

InstaBox's [published limits](https://instabox.sh/docs/limits) currently specify a
30-minute idle pause and an eight-hour maximum continuous run for free machines.
This is suitable for personal use and trials, but is not guaranteed always-on hosting.
Published URLs can wake an idle-paused VM; if you manually stop or pause it, start it
in InstaBox before connecting. See [published-port behavior](https://instabox.sh/docs/api/ports).
