#!/usr/bin/env python3
"""Launch an official local coding CLI against Hermes without editing its login.

Standard library only. Keys stay in the child environment/temporary private
settings file, never in saved configuration or command-line arguments.
"""
import argparse
import getpass
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid

VERSION = "0.1.0"
KEY_ENV = "HERMES_ROUTER_API_KEY"


def config_path():
    base = Path(os.environ.get("APPDATA") or Path.home() / ".config")
    return base / "hermes-router" / "bridge.json"


def base_url(value):
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("Enter a valid router URL")
    parsed = urllib.parse.urlsplit(value)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.scheme not in {"http", "https"}):
        raise ValueError("Router URL must use HTTPS and contain no credentials, query, or fragment")
    try:
        loopback = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname == "localhost"
    if parsed.scheme == "http" and not loopback:
        raise ValueError("HTTP is allowed only for localhost; use HTTPS for your VM")
    try:
        parsed.port
    except ValueError:
        raise ValueError("Invalid router port") from None
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def model_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:+-]{0,199}", value):
        raise ValueError("Invalid router model name")
    return value


def save_config(path, url, model):
    value = {"version": 1, "url": base_url(url), "model": model_name(model)}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".bridge-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return value


def read_config(path):
    try:
        if path.stat().st_size > 8192:
            raise ValueError("Configuration is too large")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Run configure first to save your router URL") from None
    if not isinstance(value, dict) or set(value) != {"version", "url", "model"} or value["version"] != 1:
        raise ValueError("Invalid bridge configuration")
    return {"version": 1, "url": base_url(value["url"]), "model": model_name(value["model"])}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an application key to a redirect destination.
        return None


def request_json(url, key):
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + key,
                                                "Accept": "application/json"})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
            data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ValueError("Router response is too large")
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError("Invalid router response")
            return value
    except urllib.error.HTTPError as exc:
        raise ValueError(f"Router returned HTTP {exc.code}; check its URL, application key, and bridge version") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise ValueError("Could not read router configuration; check connectivity and TLS") from None


def preflight(config, key, client):
    info = request_json(config["url"] + "/v1/bridge-info", key)
    if info.get("version") != 1 or info.get("responses") != "stateless" or info.get("claude_messages") != "/claude/v1/messages":
        raise ValueError("This router needs the coding-client bridge update")
    prefix = "/claude" if client == "claude" else "/codex"
    settings = request_json(config["url"] + prefix + "/v1/client-config", key)
    model = settings.get("model")
    if not isinstance(model, dict):
        raise ValueError("No enabled, tool-capable model has confirmed context limits for this client")
    for field in ("max_input_tokens", "max_output_tokens", "context_window"):
        if type(model.get(field)) is not int or not 0 < model[field] <= 100_000_000:
            raise ValueError("Router returned invalid context limits")
    if model["context_window"] != model["max_input_tokens"] + model["max_output_tokens"]:
        raise ValueError("Router returned inconsistent context limits")
    return model


def command_for(client, config, limits, resume=None, picker=False):
    """Only explicit UUIDs or the client's picker; never guess the last session."""
    if resume:
        try:
            resume = str(uuid.UUID(resume))
        except ValueError:
            raise ValueError("Use the exact session UUID or --pick-session") from None
    if client == "codex":
        values = {
            "model_provider": "hermes", "model": config["model"],
            "model_providers.hermes.name": "Hermes Router", "model_providers.hermes.base_url": config["url"] + "/v1",
            "model_providers.hermes.env_key": KEY_ENV, "model_providers.hermes.wire_api": "responses",
            "model_providers.hermes.requires_openai_auth": False,
            "model_providers.hermes.supports_websockets": False,
            "model_providers.hermes.request_max_retries": 0,
            "model_providers.hermes.stream_max_retries": 0,
            "model_context_window": limits["context_window"],
            "model_auto_compact_token_limit": max(1, limits["max_input_tokens"] * 3 // 4),
            "web_search": "disabled",
        }
        args = [client]
        for key, value in values.items():
            args.extend(["-c", key + "=" + json.dumps(value, ensure_ascii=False)])
        if resume or picker:
            args.append("resume")
            if resume:
                args.append(resume)
        return args
    args = [client, "--model", config["model"]]
    if resume or picker:
        args.append("--resume")
        if resume:
            args.append(resume)
    return args


def child_environment(client, config, limits, key):
    env = os.environ.copy()
    if client == "codex":
        env[KEY_ENV] = key
        return env, None
    # Use private per-launch settings because a settings-file env block beats
    # shell exports. Do not modify existing settings or native credentials.
    overrides = {
        "ANTHROPIC_BASE_URL": config["url"] + "/claude", "ANTHROPIC_AUTH_TOKEN": key,
        "ANTHROPIC_API_KEY": "", "CLAUDE_CODE_OAUTH_TOKEN": "",
        "CLAUDE_CODE_USE_BEDROCK": "0", "CLAUDE_CODE_USE_VERTEX": "0", "CLAUDE_CODE_USE_FOUNDRY": "0",
        "ANTHROPIC_MODEL": config["model"],
        "ANTHROPIC_DEFAULT_OPUS_MODEL": config["model"],
        "ANTHROPIC_DEFAULT_SONNET_MODEL": config["model"],
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": config["model"],
        "CLAUDE_CODE_SUBAGENT_MODEL": config["model"],
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(limits["max_output_tokens"]),
    }
    env.update(overrides)
    return env, {"env": overrides}


def launch(args, env, settings=None):
    executable = shutil.which(args[0])
    if executable is None:
        raise ValueError(f"Install the official {args[0]} CLI first")
    prefix = [executable]
    if Path(executable).suffix.lower() in {".cmd", ".bat"}:
        # Windows can run batch wrappers through cmd.exe even with shell=False.
        # Invoke the known npm entry point through Node so URLs/model names
        # remain literal argv values instead of shell expressions.
        entry = ("@openai/codex/bin/codex.js" if args[0] == "codex" else
                 "@anthropic-ai/claude-code/cli.js")
        script = Path(executable).parent / "node_modules" / entry
        node = shutil.which("node")
        if not script.is_file() or not node or Path(node).suffix.lower() in {".cmd", ".bat"}:
            raise ValueError("Cannot safely launch this Windows wrapper; install the official native CLI or npm package")
        prefix = [node, str(script)]
    args = [*prefix, *args[1:]]
    if settings is None:
        return subprocess.call(args, env=env)
    with tempfile.TemporaryDirectory(prefix="hermes-bridge-") as folder:
        path = Path(folder) / "settings.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(settings, handle)
        return subprocess.call([*args, "--settings", str(path)], env=env)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--config", type=Path, default=config_path())
    commands = parser.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("configure", help="Save URL and model only; never saves keys")
    configure.add_argument("--url", required=True)
    configure.add_argument("--model", default="hermes-router")
    status = commands.add_parser("status", help="Check authentication and compatible enabled models without inference")
    status.add_argument("--client", choices=["codex", "claude"], default="codex")
    for client in ("codex", "claude"):
        command = commands.add_parser(client, help=f"Launch {client} with Hermes for this process only")
        group = command.add_mutually_exclusive_group()
        group.add_argument("--resume", metavar="SESSION_UUID")
        group.add_argument("--pick-session", action="store_true")
        command.add_argument("--dry-run", action="store_true", help="Validate and show command without starting a client")
    args = parser.parse_args(argv)
    try:
        if args.command == "configure":
            save_config(args.config, args.url, args.model)
            print("Saved router URL and model. Set HERMES_ROUTER_API_KEY or enter it at launch.")
            return 0
        config = read_config(args.config)
        key = os.environ.get(KEY_ENV, "")
        if not key and sys.stdin.isatty():
            key = getpass.getpass("Hermes application key (not your subscription login): ")
        if not key or any(ord(c) < 32 for c in key):
            raise ValueError("Set HERMES_ROUTER_API_KEY to a Hermes application key")
        client = args.client if args.command == "status" else args.command
        limits = preflight(config, key, client)
        if args.command == "status":
            print(json.dumps({"ready": True, "client": client, "context_window": limits["context_window"],
                              "max_output_tokens": limits["max_output_tokens"], "automatic_handoff": False}))
            return 0
        command = command_for(client, config, limits, args.resume, args.pick_session)
        env, settings = child_environment(client, config, limits, key)
        if args.dry_run:
            print(json.dumps({"command": command, "private_settings": settings is not None,
                              "context_window": limits["context_window"]}, indent=2))
            return 0
        print("Using Hermes provider quotas. Resume opens your saved session without resending the last prompt.", flush=True)
        return launch(command, env, settings)
    except (ValueError, OSError) as exc:
        # OSError may include process/env paths; never dump env, headers or key.
        print("Hermes: " + (str(exc) if isinstance(exc, ValueError) else "Could not start the client or write configuration"), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
