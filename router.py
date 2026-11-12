#!/usr/bin/env python3
"""
hermes-router — Multi-provider AI router with automatic key rotation.

A lightweight OpenAI-compatible proxy that:
  - Rotates across multiple API keys per provider automatically
  - Cascades to the next provider when one is exhausted or rate-limited
  - Strips thinking/reasoning fields that break non-Claude providers
  - Handles 413 (payload too large) by cascading instead of crashing
  - Caches identical responses to preserve free-tier quota
  - Routes short requests to low-latency providers first (optional)
  - Tracks per-provider latency and error rates

Supported providers (configure via .env or auth.json):
  API keys: Gemini · OpenRouter · SambaNova · GitHub Models · Cerebras · Groq ·
            Mistral · Cohere · Z.ai · Naga · NVIDIA · Hugging Face · Kimi ·
            OpenCode Zen/Go · OpenAI · Anthropic
  OAuth: Codex (ChatGPT) via `hr auth import-codex`
  Local: OpenAI-compatible servers such as Ollama or LM Studio

Quick start:
  Use install.ps1/start.ps1 on Windows or install.sh on Linux/macOS.
  See documentation/setup.md for the first provider and request.
"""

import json, os, time, threading, logging, hashlib, hmac, itertools, re, sqlite3, subprocess, secrets, uuid, math, shutil
from pathlib import Path
from functools import wraps
from collections import deque, OrderedDict, defaultdict
from flask import Flask, request, jsonify, Response, stream_with_context, redirect, g, has_request_context
from urllib.parse import urlparse, urlunparse
from provider_catalog import PROVIDER_PRESETS, ADDITIONAL_DEFAULTS
import context_policy
import model_discovery
import responses_api

import requests

# ── Config ─────────────────────────────────────────────────────────────────────

def _load_env(path: str = ".env"):
    """Load key=value pairs from a .env file into os.environ (no-op if missing)."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

_load_env(os.environ.get("HR_ENV_FILE", ".env"))

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("hermes-router")

# Shared HTTP session — reuses TCP/TLS connections to each provider host across
# requests (HTTP keep-alive), so we don't pay a fresh ~100–300ms handshake on
# every call. Thread-safe for sending; pool_maxsize covers our worker threads.
# max_retries=0 because the cascade handles retries, not urllib3.
_HTTP = requests.Session()
_http_adapter = requests.adapters.HTTPAdapter(
    pool_connections=20,
    pool_maxsize=max(32, int(os.environ.get("WORKER_THREADS", 16)) * 2),
    max_retries=0,
)
_HTTP.mount("https://", _http_adapter)
_HTTP.mount("http://", _http_adapter)

PORT              = int(os.environ.get("PORT", 8319))
# Bind address. Default 0.0.0.0 (needed for Docker port mapping). Set HOST=127.0.0.1
# to expose the router to localhost only — recommended on a shared/VPS host where
# you reach it via localhost or an SSH tunnel rather than a public port.
HOST              = os.environ.get("HOST", "0.0.0.0")

# Well-known placeholder values shipped in .env.example / the old hardcoded
# fallback. Application inference keys must also be unique per installation.
_KNOWN_DEFAULT_PROXY_KEYS = {"sk-router-1", "sk-my-router-key-1"}


def _ensure_secret(name: str, prefix: str, env_path: str) -> str:
    current = os.environ.get(name, "").strip()
    if current and current not in _KNOWN_DEFAULT_PROXY_KEYS:
        return current
    value = prefix + secrets.token_urlsafe(32)
    path = Path(env_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [line for line in lines if not line.strip().startswith(name + "=")]
    lines.append(name + "=" + value)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text("\n".join(lines) + "\n")
    try:
        os.chmod(temp, 0o600)
    except OSError:
        pass
    os.replace(temp, path)
    os.environ[name] = value
    log.warning("Generated %s; retrieve it locally from %s", name, path)
    return value


def _ensure_real_proxy_key(env_path: str | None = None) -> list[str]:
    current = [k.strip() for k in os.environ.get("PROXY_API_KEYS", "").split(",") if k.strip()]
    if current and all(k in _KNOWN_DEFAULT_PROXY_KEYS for k in current):
        os.environ.pop("PROXY_API_KEYS", None)
    return [k.strip() for k in _ensure_secret("PROXY_API_KEYS", "sk-router-", env_path or os.environ.get("HR_ENV_FILE", ".env")).split(",") if k.strip()]


PROXY_API_KEYS = _ensure_real_proxy_key()
ADMIN_API_KEY = _ensure_secret("ADMIN_API_KEY", "sk-admin-", os.environ.get("HR_ENV_FILE", ".env"))
if ADMIN_API_KEY in PROXY_API_KEYS:
    raise ValueError("ADMIN_API_KEY must differ from application keys")
_ACCESS_LOCK = threading.RLock()
ROUTER_MODEL      = os.environ.get("ROUTER_MODEL_ID", "hermes-router")
_CONTEXT_CONFIG = context_policy.validate_config(json.loads(os.environ.get("ROUTER_CONTEXT_CONFIG", "{}")))
_DISCOVERED_CONTEXT_LIMITS = {}
MODEL_POLICY_FILE = Path(os.environ.get("ROUTER_MODEL_POLICY_FILE") or
                         str(Path(os.environ.get("HR_ENV_FILE", ".env")).with_name("model-policy.json")))


def _load_disabled_models(path):
    if not path.exists():
        return frozenset()
    doc = json.loads(path.read_text())
    if not isinstance(doc, dict) or set(doc) != {"disabled_models"}:
        raise ValueError("Invalid model routing policy")
    entries = doc["disabled_models"]
    if not isinstance(entries, list) or len(entries) > 10000:
        raise ValueError("Invalid model routing policy")
    for pair in entries:
        if (not isinstance(pair, list) or len(pair) != 2 or
                any(not isinstance(s, str) or not s or len(s) > 512 or
                    any(ord(c) < 32 for c in s) for s in pair)):
            raise ValueError("Invalid model routing policy")
    return frozenset(tuple(pair) for pair in entries)


# Immutable snapshots let concurrent request threads see a complete policy.
# Invalid persisted policy fails startup rather than silently enabling models.
_DISABLED_MODELS = _load_disabled_models(MODEL_POLICY_FILE)


def _model_enabled(provider, model):
    return (provider, model) not in _DISABLED_MODELS


def _model_policy_revision(policy=None):
    return hashlib.sha256(json.dumps(sorted(_DISABLED_MODELS if policy is None else policy)).encode()).hexdigest()


CACHE_TTL         = int(os.environ.get("CACHE_TTL_SECONDS", 300))   # 0 = disabled
CACHE_MAX_SIZE    = int(os.environ.get("CACHE_MAX_SIZE", 100))
# Persistent cache: mirror the in-memory response/semantic cache to a SQLite file
# so it survives restarts (opt-in). Use a writable path; on read-only hosts (e.g.
# HF Spaces) point CACHE_DB_PATH at /tmp/..., like ROUTER_STATE_FILE.
CACHE_PERSIST     = os.environ.get("CACHE_PERSIST", "0").strip().lower() not in ("0", "", "false", "no", "off")
CACHE_DB_PATH     = os.environ.get("CACHE_DB_PATH", "./cache.db")
FAST_ROUTE_TOKENS = int(os.environ.get("FAST_ROUTE_THRESHOLD", 0))  # 0 = disabled
# Optional startup model discovery. Kept opt-in because some gateways list paid
# models alongside free ones, and some expose very large catalogs.
AUTO_DISCOVER_MODELS = os.environ.get("AUTO_DISCOVER_MODELS", "0").strip().lower() not in ("0", "", "false", "no", "off")
AUTO_DISCOVER_MODEL_LIMIT = max(1, int(os.environ.get("AUTO_DISCOVER_MODEL_LIMIT", "8")))
AUTO_DISCOVER_FREE_MODELS = os.environ.get("AUTO_DISCOVER_FREE_MODELS", "1").strip().lower() not in ("0", "", "false", "no", "off")
_DISCOVERY_STATUS = {}
_DISCOVERY_QUEUE = OrderedDict()
_DISCOVERY_GENERATIONS = defaultdict(int)
_DISCOVERY_WORKER_RUNNING = False
# Semantic cache: serve a cached answer for a *similar* (not just identical) prompt,
# by embedding prompts and comparing cosine similarity. Opt-in (needs an embedding
# provider); falls back to exact match when off or unavailable.
SEMANTIC_CACHE     = os.environ.get("SEMANTIC_CACHE", "0").strip().lower() not in ("0", "", "false", "no", "off")
SEMANTIC_THRESHOLD = float(os.environ.get("SEMANTIC_CACHE_THRESHOLD", "0.95"))
# Cost display: USD is always the canonical figure. Set COST_FX_RATE (e.g. 83) and
# COST_CURRENCY (e.g. INR) to ALSO surface a converted amount in /v1/usage etc.
COST_CURRENCY      = os.environ.get("COST_CURRENCY", "USD").strip().upper() or "USD"
try:    COST_FX_RATE = float(os.environ.get("COST_FX_RATE", 0) or 0)
except (TypeError, ValueError): COST_FX_RATE = 0.0
# How keys are picked within a provider:
#   round-robin — spread requests evenly across all keys (keys deplete together)
#   sequential  — drain one key until it rate-limits, then move on (keeps reserves fresh)
ROTATION_MODE     = os.environ.get("ROTATION_MODE", "round-robin").strip().lower()
if ROTATION_MODE not in ("round-robin", "sequential"):
    ROTATION_MODE = "round-robin"   # unknown value → safe default
STATE_FILE        = Path(os.environ.get("ROUTER_STATE_FILE", "./router_state.json"))
STATE_TTL_HOURS   = int(os.environ.get("ROUTER_STATE_TTL_HOURS", 24))  # 0 = re-probe every start
AUTH_FILE         = Path(os.environ.get("ROUTER_AUTH_FILE", "./auth.json"))  # router's own key store
INSTANCE_FILE     = Path(os.environ.get("HERMES_INSTANCES_FILE", "./instances.json"))
INSTANCE_DOCKER_IMAGE = os.environ.get("HERMES_INSTANCE_IMAGE", "hermes-router:latest")
INSTANCE_CONTAINER_PORT = int(os.environ.get("HERMES_INSTANCE_CONTAINER_PORT", "8319"))
INSTANCE_DOCKER_PREFIX = os.environ.get("HERMES_INSTANCE_DOCKER_PREFIX", "hermes-router")
REQUEST_TIMEOUT = max(0.1, float(os.environ.get("ROUTER_REQUEST_TIMEOUT_SECONDS", "180")))
SEMANTIC_EMBED_TIMEOUT = max(0.1, float(os.environ.get("SEMANTIC_EMBED_TIMEOUT_SECONDS", "3")))
# In-memory request log: last N requests kept in a ring buffer. Pure RAM, no disk
# writes. Set REQUEST_LOG_SIZE=0 to disable. Exposed via GET /v1/logs.
REQUEST_LOG_SIZE  = max(0, int(os.environ.get("REQUEST_LOG_SIZE", "500")))


def _load_auth_json() -> dict[str, list[str]]:
    """Load provider API keys from auth.json — the router's own credential store,
    managed by `hr auth add`. This makes the router self-contained: keys live with
    the router, independent of any host application.

      Format: {"providers": {"openrouter": ["key1", "key2"], "gemini": ["key"]}}

    Returns {provider_name: [keys]}. A missing or invalid file is non-fatal —
    the router simply falls back to keys from .env (see _keys_for)."""
    if not AUTH_FILE.exists():
        return {}
    try:
        doc = json.loads(AUTH_FILE.read_text())
        out: dict[str, list[str]] = {}
        for name, keys in doc.get("providers", {}).items():
            if isinstance(keys, list):
                out[name] = [str(k).strip() for k in keys if str(k).strip()]
        return out
    except Exception as e:
        log.warning(f"Could not read {AUTH_FILE}: {e}")
        return {}

_AUTH_KEYS = _load_auth_json()


# ── Codex (ChatGPT OAuth) credentials ──────────────────────────────────────────
# Codex authenticates with ChatGPT-subscription OAuth tokens, not static API keys.
# Access tokens are short-lived JWTs; we mint fresh ones from the long-lived refresh
# token. Accounts live in auth.json under "codex_accounts" (written by
# `hr auth import-codex`), separate from the plain-string provider keys so the
# normal credential pool is untouched.
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"


def _jwt_exp(token: str) -> int:
    """Read the `exp` (unix seconds) claim from a JWT without verifying it."""
    try:
        import base64
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)        # pad base64url
        return int(json.loads(base64.urlsafe_b64decode(payload)).get("exp", 0))
    except Exception:
        return 0


def _load_codex_accounts() -> list[dict]:
    """Load Codex accounts from auth.json's "codex_accounts" list."""
    if not AUTH_FILE.exists():
        return []
    try:
        doc = json.loads(AUTH_FILE.read_text())
        accts = doc.get("codex_accounts", [])
        return [a for a in accts if isinstance(a, dict)
                and a.get("refresh_token") and a.get("account_id")]
    except Exception as e:
        log.warning(f"Could not read codex accounts from {AUTH_FILE}: {e}")
        return []


class CodexCredentials:
    """Holds Codex OAuth accounts and hands out fresh access tokens, refreshing
    via the refresh token when a token is missing or near expiry. Refreshed
    tokens are persisted back to auth.json so they survive restarts."""
    REFRESH_SKEW = 300   # refresh this many seconds before the JWT expires

    def __init__(self, accounts: list[dict]):
        self.lock = threading.Lock()
        self.accounts = {a["account_id"]: dict(a) for a in accounts}

    def account_ids(self) -> list[str]:
        return list(self.accounts.keys())

    def get_access_token(self, account_id: str) -> str | None:
        """Return a valid access token for the account, refreshing if needed."""
        with self.lock:
            acct = self.accounts.get(account_id)
            if not acct:
                return None
            tok = acct.get("access_token", "")
            if tok and _jwt_exp(tok) - self.REFRESH_SKEW > time.time():
                return tok
            return self._refresh(acct)

    def _refresh(self, acct: dict) -> str | None:
        try:
            r = _HTTP.post(CODEX_TOKEN_URL, json={
                "client_id":     CODEX_CLIENT_ID,
                "grant_type":    "refresh_token",
                "refresh_token": acct["refresh_token"],
            }, timeout=30)
        except requests.exceptions.RequestException as e:
            log.error(f"  codex token refresh network error: {e}")
            return None
        if r.status_code != 200:
            log.error(f"  codex token refresh failed: HTTP {r.status_code}")
            return None
        data = r.json()
        acct["access_token"] = data.get("access_token", acct.get("access_token", ""))
        if data.get("refresh_token"):           # refresh tokens can rotate
            acct["refresh_token"] = data["refresh_token"]
        acct["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._persist()
        log.info(f"  codex token refreshed for account ...{acct['account_id'][-6:]}")
        return acct["access_token"]

    def _persist(self):
        """Write current accounts back to auth.json (best-effort, 0600)."""
        try:
            doc = json.loads(AUTH_FILE.read_text()) if AUTH_FILE.exists() else {}
        except Exception:
            doc = {}
        if not isinstance(doc, dict):
            doc = {}
        doc["codex_accounts"] = list(self.accounts.values())
        try:
            _atomic_write_text(AUTH_FILE, json.dumps(doc, indent=2) + "\n")
            os.chmod(AUTH_FILE, 0o600)
        except Exception as e:
            log.warning(f"  could not persist codex tokens: {e}")


codex_creds = CodexCredentials(_load_codex_accounts())


# Circuit-breaker knobs — a provider that fails health repeatedly is tripped out
# of rotation for a cooldown, then probed again (half-open). Overridable via env.
BREAKER_WINDOW      = int(os.environ.get("BREAKER_WINDOW", 8))          # recent outcomes to weigh
BREAKER_MIN_SAMPLES = int(os.environ.get("BREAKER_MIN_SAMPLES", 4))     # min samples before it can trip
BREAKER_ERROR_RATE  = float(os.environ.get("BREAKER_ERROR_RATE", 0.5))  # trip at >= this health-fail fraction
BREAKER_COOLDOWN    = int(os.environ.get("BREAKER_COOLDOWN", 60))       # seconds the breaker stays open

# Providers known for low-latency inference — promoted for short requests
_FAST_PROVIDERS = {"groq", "cerebras", "sambanova", "mistral"}

# Per-request counter for round-robin among equally-rated providers.
# itertools.count().__next__ is atomic in CPython, so it's thread-safe.
_rr_counter = itertools.count()

# ── Smart routing: capability ratings ─────────────────────────────────────────
# 1=outstanding  2=best  3=good  4=fair  5=basic  (lower = more capable)
# Recommended base model: set ROUTER_BASE_MODEL_PROVIDER + ROUTER_BASE_MODEL
# e.g. ROUTER_BASE_MODEL_PROVIDER=openai  ROUTER_BASE_MODEL=gpt-4o-mini
KNOWN_MODEL_RATINGS: dict = {
    # 1 — Outstanding
    "gpt-5.3-codex": 1, "gpt-5-codex": 1, "gpt-5.5": 1, "gpt-4o": 1, "o1": 1, "o3": 1,
    "claude-opus-4": 1, "claude-opus": 1, "gemini-2.5-pro": 1,
    "nemotron-3-ultra": 1,
    "gpt-4.5": 1, "claude-3-7": 1, "gemini-2.0-ultra": 1,
    "deepseek-r2": 1, "qwen3-235b": 1, "qwen3-72b": 1,
    # 2 — Best
    "gemini-2.5-flash": 2, "gemini-2.0-flash": 2,
    "llama-3.3-70b": 2, "llama-3.1-70b": 2,
    "mistral-large": 2, "mistral-medium": 2,
    "command-r-plus": 2, "command-a": 2, "nvidia/nemotron-3-super": 2, "nemotron": 2,
    "mimo-v2.5": 2, "north-mini-code": 2, "big-pickle": 2,
    "deepseek-v4-flash": 2, "deepseek-v4": 2,  # capable but slow cold-start → "best", not first-choice
    "deepseek-v3": 2, "deepseek-v2": 2,
    "claude-sonnet": 2, "claude-3-5": 2, "grok-2": 2,
    "qwen2.5-72b": 2, "qwen-72b": 2, "qwen3-32b": 2,
    "phi-4": 2, "phi-4-reasoning": 2,
    "mixtral-8x22b": 2, "wizardlm-2-8x22b": 2,
    "yi-large": 2, "moonshot-v1": 2,
    "llama-4-maverick": 2, "llama-4-scout": 2,
    # 3 — Good
    "gemini-2.5-flash-lite": 3, "gemini-1.5-flash": 3,
    "gpt-4o-mini": 3, "gpt-oss-120b": 3,
    "mistral-small": 3, "glm-4.5-flash": 3, "glm-4.7-flash": 3,
    "llama-3.1-8b-instant": 3,
    "qwen2.5-32b": 3, "qwen3-14b": 3, "qwen3-8b": 3,
    "phi-3.5": 3, "phi-3-medium": 3,
    "mixtral-8x7b": 3, "wizardlm-2-7b": 3,
    "yi-medium": 3, "yi-6b": 3,
    # 4 — Fair
    "command-r7b": 4, "command-r7b-12-2024": 4,
    "llama-3.2-3b": 4, "mistral-7b": 4,
    "qwen2.5-7b": 4, "qwen3-4b": 4, "phi-3-mini": 4,
    "phi-3.5-mini": 4, "yi-mini": 4,
}
_RATING_PATTERNS: list = [
    (1, ["pro-exp", "ultra", "opus", "o3", "o1-pro", "405b", "671b", "r1-zero"]),
    (2, ["70b", "large", "plus", "pro", "turbo", "super", "sonnet", "72b", "32b", "maverick", "scout", "phi-4", "wizardlm"]),
    (3, ["flash", "small", "mini", "medium", "120b", "8b-instant", "glm-4", "14b", "22b", "mixtral", "qwen", "yi-m", "phi-3"]),
    (4, ["7b", "8b", "lite", "fast", "r7b", "nano", "3b", "phi-3-mini", "phi-3.5-mini", "yi-mini", "4b"]),
    (5, ["micro", "tiny", "1b"]),
]
_COMPLEXITY_LABELS = {1: "critical", 2: "complex", 3: "standard", 4: "simple", 5: "trivial"}

# Approximate list prices (USD per 1M tokens) as (input, output), for cost
# ESTIMATION only. Substring match like _rate_model (longest key wins). Anything
# not listed — every free provider, and subscription plans like Codex (ChatGPT)
# and the Kimi coding plan — is treated as $0. Prices drift; treat as estimates
# and override/extend with MODEL_PRICES_FILE (JSON: {"model-substr": [in, out]}).
MODEL_PRICES: dict = {
    "gpt-4o-mini":      (0.15, 0.60),
    "gpt-4o":           (2.50, 10.00),
    "gpt-4.1-mini":     (0.40, 1.60),
    "gpt-4.1":          (2.00, 8.00),
    "o1-mini":          (1.10, 4.40),
    "o1":               (15.00, 60.00),
    "o3-mini":          (1.10, 4.40),
    "claude-opus":      (15.00, 75.00),
    "claude-sonnet":    (3.00, 15.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "claude-haiku":     (0.80, 4.00),
    "mistral-large":    (2.00, 6.00),
    "mistral-medium":   (0.40, 2.00),
    "mistral-small":    (0.10, 0.30),
    "command-a":        (2.50, 10.00),
    "command-r-plus":   (2.50, 10.00),
    "command-r":        (0.15, 0.60),
    "kimi-k2":          (0.60, 2.50),
}

# Tie-breakers for equally priced/capable models. Lower is better. Ratings still
# carry the broad capability class; this table nudges known strong model families
# ahead when multiple candidates cost the same (common for free/subscription pools).
MODEL_QUALITY_RANKS: dict = {
    "big-pickle": 5,
    "gpt-5": 10, "gpt-4o": 15, "o3": 15, "o1": 20,
    "claude-opus": 10, "claude-sonnet": 20,
    "gemini-2.5-pro": 15, "gemini-2.5-flash": 35,
    "nemotron-3-ultra": 20, "nemotron-3-super": 35,
    "deepseek-v4": 30, "deepseek-v3": 40,
    "llama-4": 35, "llama-3.3-70b": 45,
    "mistral-large": 40, "mistral-medium": 55,
    "command-a": 45, "gpt-oss-120b": 55,
}
PROVIDER_QUALITY_RANKS: dict = {
    "opencode": 10, "codex": 15, "openai": 20, "anthropic": 25,
    "gemini": 30, "openrouter": 35, "cerebras": 40, "nvidia": 45,
    "groq": 50, "mistral": 55, "cohere": 60, "sambanova": 65,
    "kimi": 70, "zai": 75, "naga": 80, "github_models": 85,
    "huggingface": 90, "local": 95,
}
_provider_state: dict = {}   # populated at startup by _initialize_ratings()
_INITIALIZATION_STATE = {"state": "initializing"}
# Per-(provider, model) capability — rating + tool/reasoning support. Keyed by
# (provider_name, model). Lets smart routing treat each model in a provider's
# comma-separated list as its own candidate, instead of inheriting the primary's.
_model_state: dict = {}


def _keys(env_var: str) -> list[str]:
    """Collect all keys for a provider from three naming conventions (combined + de-duped):
      1. Singular:  MISTRAL_API_KEY=k1
      2. Plural:    MISTRAL_API_KEYS=k1,k2,k3   (comma-separated)
      3. Numbered:  MISTRAL_API_KEY_2=k2, MISTRAL_API_KEY_3=k3, ...
    The plural form is the canonical multi-key env var; singular and numbered are
    convenience aliases that are merged in automatically.
    """
    collected = []
    # singular (drop the trailing S if the caller passed the plural form)
    singular = env_var[:-1] if env_var.endswith("S") else env_var
    if singular != env_var:
        single = os.environ.get(singular, "").strip()
        if single:
            collected.append(single)
    # plural / comma-separated
    for piece in os.environ.get(env_var, "").split(","):
        piece = piece.strip()
        if piece:
            collected.append(piece)
    # numbered suffixes on the singular name (_2, _3, ...)
    i = 2
    while True:
        nv = os.environ.get(f"{singular}_{i}", "").strip()
        if not nv:
            break
        collected.append(nv)
        i += 1
    seen, out = set(), []
    for k in collected:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _keys_for(provider_name: str, env_var: str) -> list[str]:
    """All keys for a provider: auth.json entries first (the primary store that
    `hr auth add` writes to), then any matching .env keys as a fallback. Deduped,
    order preserved. A provider with keys in EITHER source is enabled."""
    merged = list(_AUTH_KEYS.get(provider_name, []))
    merged += _keys(env_var)
    seen, out = set(), []
    for k in merged:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _int_env(env_var: str, default: int = 0) -> int:
    """Parse an integer env var, falling back to default on missing/invalid."""
    try:
        return int(os.environ.get(env_var, default))
    except (TypeError, ValueError):
        return default


def _parse_retry_after(value, default: int = 60) -> int:
    """Parse a Retry-After header value. RFC 9110 allows either delay-seconds
    or an HTTP date; some providers also send fractional seconds. Anything we
    can't read as a number falls back to the default cooldown."""
    try:
        return max(1, int(float(value)))
    except (TypeError, ValueError):
        return default


# ── Per-provider exclude list ─────────────────────────────────────────────────


def _excluded_models(provider_name: str) -> set[str]:
    """Case-insensitive exact model IDs listed in {PROVIDER}_EXCLUDE_MODELS.

    Excluded models are stripped from a provider's active roster whether
    they come from config or auto-discovery.
    """
    raw = os.environ.get(f"{provider_name.upper()}_EXCLUDE_MODELS", "")
    return {m.strip().lower() for m in raw.split(",") if m.strip()}


def _filter_excluded(provider_name: str, models: list[str]) -> list[str]:
    """Drop models blocked by {PROVIDER}_EXCLUDE_MODELS (exact, case-insensitive)."""
    excl = _excluded_models(provider_name)
    if not excl:
        return models
    return [m for m in models if m.lower() not in excl]


def _provider_models(provider: dict) -> list[str]:
    """Usable model IDs for a provider. Empty means skip for routing/probes.

    Prefer an explicit ``models`` list (including empty after exclude filtering)
    over falling back to ``model``, so ``models=[]`` / ``model=""`` does not
    become a phantom candidate with an empty model string.
    """
    if "models" in provider:
        return [m for m in (provider.get("models") or []) if m]
    m = provider.get("model") or ""
    return [m] if m else []


def _registered_models(provider):
    """Keep disabled models visible and their credentials available for re-enable."""
    return list(dict.fromkeys(_provider_models(provider) +
                             ([provider["embed_model"]] if provider.get("embed_model") else [])))


def _enabled_provider_models(provider):
    return [m for m in _provider_models(provider) if _model_enabled(provider["name"], m)]


# ── Provider definitions ───────────────────────────────────────────────────────

def _build_providers() -> list[dict]:
    providers = []

    gemini_keys = _keys_for("gemini", "GEMINI_API_KEYS")
    if gemini_keys:
        providers.append({
            "name":     "gemini",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
            "model":    os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite"),
            "keys":     gemini_keys,
        })

    openrouter_keys = _keys_for("openrouter", "OPENROUTER_API_KEYS")
    if openrouter_keys:
        providers.append({
            "name":     "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "model":    os.environ.get("OPENROUTER_MODEL", "nvidia/nemotron-3-super-120b-a12b:free"),
            "keys":     openrouter_keys,
            "headers":  {
                "HTTP-Referer": os.environ.get("OPENROUTER_SITE_URL", "https://github.com/Shaf2665/hermes-router"),
                "X-Title":      os.environ.get("OPENROUTER_APP_NAME", "hermes-router"),
            },
        })

    sambanova_keys = _keys_for("sambanova", "SAMBANOVA_API_KEYS")
    if sambanova_keys:
        providers.append({
            "name":     "sambanova",
            "base_url": "https://api.sambanova.ai/v1",
            "model":    os.environ.get("SAMBANOVA_MODEL", "DeepSeek-V3.2"),
            "keys":     sambanova_keys,
        })

    github_keys = _keys_for("github_models", "GITHUB_MODELS_TOKENS")
    if github_keys:
        providers.append({
            "name":     "github_models",
            "base_url": "https://models.inference.ai.azure.com",
            "model":    os.environ.get("GITHUB_MODELS_MODEL", "gpt-4o"),
            "keys":     github_keys,
        })

    cerebras_keys = _keys_for("cerebras", "CEREBRAS_API_KEYS")
    if cerebras_keys:
        providers.append({
            "name":     "cerebras",
            "base_url": "https://api.cerebras.ai/v1",
            "model":    os.environ.get("CEREBRAS_MODEL", "gpt-oss-120b"),
            "keys":     cerebras_keys,
        })

    groq_keys = _keys_for("groq", "GROQ_API_KEYS")
    if groq_keys:
        providers.append({
            "name":     "groq",
            "base_url": "https://api.groq.com/openai/v1",
            "model":    os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
            "keys":     groq_keys,
        })

    mistral_keys = _keys_for("mistral", "MISTRAL_API_KEYS")
    if mistral_keys:
        providers.append({
            "name":     "mistral",
            "base_url": "https://api.mistral.ai/v1",
            "model":    os.environ.get("MISTRAL_MODEL", "mistral-medium-3-5"),
            "keys":     mistral_keys,
        })

    cohere_keys = _keys_for("cohere", "COHERE_API_KEYS")
    if cohere_keys:
        providers.append({
            "name":     "cohere",
            "base_url": "https://api.cohere.ai/compatibility/v1",
            "model":    os.environ.get("COHERE_MODEL", "command-a-03-2025"),
            "keys":     cohere_keys,
        })

    zai_keys = _keys_for("zai", "GLM_API_KEYS")
    if zai_keys:
        providers.append({
            "name":     "zai",
            "base_url": "https://api.z.ai/api/paas/v4",
            "model":    os.environ.get("ZAI_MODEL", "glm-4.7-flash"),
            "keys":     zai_keys,
        })

    naga_keys = _keys_for("naga", "NAGA_API_KEYS")
    if naga_keys:
        providers.append({
            "name":     "naga",
            "base_url": "https://api.naga.ac/v1",
            "model":    os.environ.get("NAGA_MODEL", "nemotron-3-super-120b-a12b:free"),
            "keys":     naga_keys,
        })

    nvidia_keys = _keys_for("nvidia", "NVIDIA_API_KEYS")
    if nvidia_keys:
        providers.append({
            "name":     "nvidia",
            "base_url": "https://integrate.api.nvidia.com/v1",
            "model":    os.environ.get("NVIDIA_MODEL", "deepseek-ai/deepseek-v4-flash"),
            "keys":     nvidia_keys,
        })

    # Hugging Face Inference Providers — one OpenAI-compatible endpoint fronting
    # the models currently served by participating partners. Eligible accounts
    # receive monthly credit; the amount and catalog can change. The `:cheapest`
    # suffix routes to the cheapest eligible partner. Use a
    # token from huggingface.co/settings/tokens (with Inference Providers access).
    huggingface_keys = _keys_for("huggingface", "HUGGINGFACE_API_KEYS")
    if huggingface_keys:
        providers.append({
            "name":     "huggingface",
            "base_url": "https://router.huggingface.co/v1",
            "model":    os.environ.get("HUGGINGFACE_MODEL", "openai/gpt-oss-120b:cheapest"),
            "keys":     huggingface_keys,
        })

    # Kimi (Moonshot) — the "Kimi coding plan" subscription exposes an
    # OpenAI-compatible endpoint and authenticates with a normal API key (sk-...),
    # so it drops in like any other provider. Model id `kimi-for-coding`. Get a key
    # from platform.kimi.ai / platform.moonshot.ai.
    kimi_keys = _keys_for("kimi", "KIMI_API_KEYS")
    if kimi_keys:
        providers.append({
            "name":     "kimi",
            "base_url": os.environ.get("KIMI_BASE_URL", "https://api.kimi.com/coding/v1"),
            "model":    os.environ.get("KIMI_MODEL", "kimi-for-coding"),
            "keys":     kimi_keys,
        })

    # OpenCode Zen — an OpenAI-compatible gateway for coding models with a pool of
    # models currently marked free (default below). One API key (`hr auth add opencode`);
    # paid premium models (claude/gpt/gemini/…) are reachable too via OPENCODE_MODEL.
    opencode_keys = _keys_for("opencode", "OPENCODE_API_KEYS")
    if opencode_keys:
        providers.append({
            "name":     "opencode",
            "base_url": os.environ.get("OPENCODE_BASE_URL", "https://opencode.ai/zen/v1"),
            "model":    os.environ.get(
                "OPENCODE_MODEL",
                "deepseek-v4-flash-free,nemotron-3-ultra-free,"
                "mimo-v2.5-free,north-mini-code-free",
            ),
            "keys":     opencode_keys,
        })

    # OpenCode Go — the same OpenCode key + an OpenAI-compatible endpoint, but a
    # paid subscription tier. Enabled only when an
    # `opencode_go` key is configured (signals you've turned on Go billing), so it
    # never adds dead attempts before you subscribe.
    opencode_go_keys = _keys_for("opencode_go", "OPENCODE_GO_API_KEYS")
    if opencode_go_keys:
        providers.append({
            "name":     "opencode_go",
            "base_url": os.environ.get("OPENCODE_GO_BASE_URL", "https://opencode.ai/zen/go/v1"),
            "model":    os.environ.get("OPENCODE_GO_MODEL", "deepseek-v4-flash,kimi-k2.7-code,mimo-v2.5"),
            "keys":     opencode_go_keys,
        })

    openai_keys = _keys_for("openai", "OPENAI_API_KEYS")
    if openai_keys:
        providers.append({
            "name":     "openai",
            "base_url": "https://api.openai.com/v1",
            "model":    os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            "keys":     openai_keys,
        })

    anthropic_keys = _keys_for("anthropic", "ANTHROPIC_API_KEYS")
    if anthropic_keys:
        providers.append({
            "name":     "anthropic",
            "base_url": "https://api.anthropic.com/v1",
            "model":    os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
            "keys":     anthropic_keys,
            "protocol": "anthropic",   # triggers format translation in forward()
        })

    # Codex — ChatGPT-subscription OAuth (not API keys). Accounts come from
    # `hr auth import-codex` and are keyed by account_id; forward() resolves each
    # to a fresh access token. Speaks the Responses API, so it needs translation.
    codex_ids = codex_creds.account_ids()
    if codex_ids:
        providers.append({
            "name":     "codex",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "model":    os.environ.get("CODEX_MODEL", "gpt-5.5"),
            "keys":     codex_ids,     # account_ids, resolved to tokens at send time
            "protocol": "codex",
        })

    # Local model — Ollama / LM Studio / llama.cpp / any OpenAI-compatible server
    # running on your own machine. Free, private, and fast. Local servers are
    # keyless, but the rotation pool needs ≥1 entry, so we use a sentinel key
    # (LOCAL_API_KEY, default "local") that forward() sends as a harmless Bearer
    # header the server ignores. Enabled by setting LOCAL_BASE_URL or LOCAL_MODEL.
    if os.environ.get("LOCAL_BASE_URL") or os.environ.get("LOCAL_MODEL"):
        providers.append({
            "name":     "local",
            "base_url": os.environ.get("LOCAL_BASE_URL", "http://localhost:11434/v1"),
            "model":    os.environ.get("LOCAL_MODEL", "llama3.1"),
            "keys":     [os.environ.get("LOCAL_API_KEY", "local")],
        })

    for name, default in ADDITIONAL_DEFAULTS.items():
        keys = _keys_for(name, name.upper() + "_API_KEYS")
        if keys:
            providers.append({"name": name, "base_url": PROVIDER_PRESETS[name]["base_url"],
                              "model": os.environ.get(name.upper() + "_MODEL", default), "keys": keys})

    if not providers:
        log.warning("No providers configured — set GEMINI_API_KEYS, OPENROUTER_API_KEYS, etc. in .env")

    # Multi-model support: a provider's model string may be a comma-separated list
    # (e.g. GEMINI_MODEL=gemini-2.5-flash-lite,gemini-2.5-flash). The router
    # fails over across a provider's models before cascading to the next provider;
    # upstream services may still enforce shared account/project limits. The first
    # entry is the "primary" model used for probing, rating, and status display.
    for p in providers:
        models = [m.strip() for m in str(p.get("model", "")).split(",") if m.strip()]
        filtered = _filter_excluded(p["name"], models)
        if models and not filtered:
            log.warning(f"{p['name']}: all models excluded via "
                        f"{p['name'].upper()}_EXCLUDE_MODELS — provider has no usable models")
        p["models"] = filtered
        p["model"]  = filtered[0] if filtered else ""
        p["_manual_models"] = list(filtered)

    # Per-provider "skip when the request is too big" ceiling. Some providers
    # reject large payloads outright, so trying them with a big prompt just wastes
    # a round-trip before cascading. When the estimated request size exceeds a
    # provider's ceiling, that provider is skipped entirely.
    #   Configure via  {PROVIDER}_SKIP_TOKENS_OVER  (0 = never skip).
    # Defaults are intentionally conservative and can be overridden as upstream
    # model/account limits change.
    _skip_defaults = {"groq": 5500, "sambanova": 30000, "github_models": 6000}
    for p in providers:
        env_var = f"{p['name'].upper()}_SKIP_TOKENS_OVER"
        p["skip_if_tokens_over"] = _int_env(env_var, _skip_defaults.get(p["name"], 0))

    # Per-provider output-token ceiling. Some providers 400 the whole request when
    # max_tokens exceeds their output cap, so we clamp it down in forward().
    #   Configure via  {PROVIDER}_MAX_OUTPUT_TOKENS  (0 = no clamp).
    #   • cohere        command-a caps output at 8192
    #   • github_models gpt-4o here rejects very large max_tokens (e.g. 65536)
    _max_out_defaults = {"cohere": 8192, "github_models": 16384}
    for p in providers:
        env_var = f"{p['name'].upper()}_MAX_OUTPUT_TOKENS"
        p["max_output_tokens"] = _int_env(env_var, _max_out_defaults.get(p["name"], 0))

    # Per-provider embedding model. Only providers with a non-empty embed model
    # take part in /v1/embeddings routing (OpenRouter, Groq, etc. are chat-only).
    # Each uses the same base_url with an /embeddings path; the wire format is
    # OpenAI-compatible, so no translation is needed. Configure or enable more
    # via {PROVIDER}_EMBED_MODEL (empty string disables a provider for embeds).
    # NVIDIA is intentionally omitted: its embedding models are "asymmetric" and
    # require an input_type (query/passage) parameter that the OpenAI embeddings
    # format doesn't carry, so they can't be served by clean passthrough. Enable
    # one explicitly with NVIDIA_EMBED_MODEL if you know it accepts OpenAI format.
    _embed_defaults = {
        "gemini":  "gemini-embedding-001",
        "mistral": "mistral-embed",
        "openai":  "text-embedding-3-small",
        "cohere":  "embed-v4.0",
    }
    for p in providers:
        env_var = f"{p['name'].upper()}_EMBED_MODEL"
        p["embed_model"] = os.environ.get(env_var, _embed_defaults.get(p["name"], ""))

    return providers


PROVIDERS = _build_providers()

# Providers whose /models endpoint mixes paid models in with the free ones.
# When auto-discovering a replacement model for these, restrict to :free ids so
# a probe can never silently promote the router onto a paid model.
_FREE_ONLY_DISCOVERY = {"openrouter", "naga", "opencode"}
_MODEL_DISCOVERY_SKIP = {"anthropic", "codex", "local", "huggingface"} | set(ADDITIONAL_DEFAULTS)


def _is_free_model_id(model: str) -> bool:
    m = (model or "").lower()
    return m.endswith(":free") or m.endswith("-free") or "/free" in m

# ── Config-write support (web dashboard "Add key" / "Set model" / add-on toggles) ──
# Mirrors the canonical provider lists + env-var mappings already used by the `hr`
# CLI scripts (scripts/auth.sh, scripts/model.sh), so the dashboard and CLI agree
# on what's valid. Kept as plain data here (not shelling out to bash) so it works
# identically in Docker, where those scripts aren't necessarily present.

# Providers that take a plain API key (excludes "codex" — OAuth via `hr auth
# import-codex` — and "local", which is keyless).
KEY_SETTABLE_PROVIDERS = [
    "gemini", "openrouter", "sambanova", "github_models", "cerebras", "groq",
    "mistral", "cohere", "zai", "naga", "nvidia", "huggingface", "kimi",
    "opencode", "opencode_go", "openai", "anthropic",
]

PROVIDER_KEY_ENV = {
    "gemini": "GEMINI_API_KEYS",
    "openrouter": "OPENROUTER_API_KEYS",
    "sambanova": "SAMBANOVA_API_KEYS",
    "github_models": "GITHUB_MODELS_TOKENS",
    "cerebras": "CEREBRAS_API_KEYS",
    "groq": "GROQ_API_KEYS",
    "mistral": "MISTRAL_API_KEYS",
    "cohere": "COHERE_API_KEYS",
    "zai": "GLM_API_KEYS",
    "naga": "NAGA_API_KEYS",
    "nvidia": "NVIDIA_API_KEYS",
    "huggingface": "HUGGINGFACE_API_KEYS",
    "kimi": "KIMI_API_KEYS",
    "opencode": "OPENCODE_API_KEYS",
    "opencode_go": "OPENCODE_GO_API_KEYS",
    "openai": "OPENAI_API_KEYS",
    "anthropic": "ANTHROPIC_API_KEYS",
}

# Providers whose model(s) can be overridden — a superset of the above (codex and
# local don't take a key here, but do have a settable model).
PROVIDER_MODEL_ENV = {
    "gemini": "GEMINI_MODEL", "openrouter": "OPENROUTER_MODEL",
    "sambanova": "SAMBANOVA_MODEL", "github_models": "GITHUB_MODELS_MODEL",
    "cerebras": "CEREBRAS_MODEL", "groq": "GROQ_MODEL", "mistral": "MISTRAL_MODEL",
    "cohere": "COHERE_MODEL", "zai": "ZAI_MODEL", "naga": "NAGA_MODEL",
    "nvidia": "NVIDIA_MODEL", "huggingface": "HUGGINGFACE_MODEL", "kimi": "KIMI_MODEL",
    "opencode": "OPENCODE_MODEL", "opencode_go": "OPENCODE_GO_MODEL",
    "openai": "OPENAI_MODEL", "anthropic": "ANTHROPIC_MODEL",
    "codex": "CODEX_MODEL", "local": "LOCAL_MODEL",
}

# Built-in default model per provider — shown as a placeholder in the dashboard;
# "reset" just deletes the .env override line, so the code's own default (set in
# _build_providers via os.environ.get(..., default)) takes over on restart. This
# table is display-only and must stay in sync with those inline defaults.
PROVIDER_MODEL_DEFAULT = {
    "gemini": "gemini-2.5-flash-lite", "openrouter": "nvidia/nemotron-3-super-120b-a12b:free",
    "sambanova": "DeepSeek-V3.2", "github_models": "gpt-4o", "cerebras": "gpt-oss-120b",
    "groq": "openai/gpt-oss-120b", "mistral": "mistral-medium-3-5",
    "cohere": "command-a-03-2025", "zai": "glm-4.7-flash",
    "naga": "nemotron-3-super-120b-a12b:free", "nvidia": "deepseek-ai/deepseek-v4-flash",
    "huggingface": "openai/gpt-oss-120b:cheapest", "kimi": "kimi-for-coding",
    "opencode": "deepseek-v4-flash-free,nemotron-3-ultra-free,mimo-v2.5-free,north-mini-code-free",
    "opencode_go": "deepseek-v4-flash,kimi-k2.7-code,mimo-v2.5", "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5-20251001", "codex": "gpt-5.5", "local": "llama3.1",
}

KEY_SETTABLE_PROVIDERS.extend(ADDITIONAL_DEFAULTS)
PROVIDER_KEY_ENV.update({p: p.upper() + "_API_KEYS" for p in ADDITIONAL_DEFAULTS})
PROVIDER_MODEL_ENV.update({p: p.upper() + "_MODEL" for p in ADDITIONAL_DEFAULTS})
PROVIDER_MODEL_DEFAULT.update(ADDITIONAL_DEFAULTS)

ENV_FILE_PATH = Path(os.environ.get("HR_ENV_FILE", ".env"))


def _env_read_line(key: str) -> str | None:
    """Current value of KEY in .env (last occurrence wins), or None if unset."""
    if not ENV_FILE_PATH.exists():
        return None
    val = None
    for line in ENV_FILE_PATH.read_text().splitlines():
        if line.strip().startswith(f"{key}="):
            val = line.split("=", 1)[1]
    return val


def _env_write_line(key: str, value: str | None) -> None:
    """Upsert (or, if value is None, delete) a KEY=VALUE line in .env, preserving
    every other line untouched. Mirrors scripts/model.sh's write_env / scripts/
    features.sh's set_env so the CLI and dashboard produce identical files."""
    lines = ENV_FILE_PATH.read_text().splitlines() if ENV_FILE_PATH.exists() else []
    found, out = False, []
    for line in lines:
        if line.strip().startswith(f"{key}="):
            found = True
            if value is not None:
                out.append(f"{key}={value}")
            # value is None → delete this line (skip appending it)
        else:
            out.append(line)
    if not found and value is not None:
        out.append(f"{key}={value}")
    _atomic_write_text(ENV_FILE_PATH, "\n".join(out) + "\n")


def _atomic_write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temp.write_text(text)
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _locked_access(fn):
    @wraps(fn)
    def locked(*args, **kwargs):
        with _ACCESS_LOCK:
            return fn(*args, **kwargs)
    return locked


_env_write_line = _locked_access(_env_write_line)


def _read_auth_doc():
    doc = json.loads(AUTH_FILE.read_text()) if AUTH_FILE.exists() else {}
    if not isinstance(doc, dict):
        raise ValueError("auth.json must contain an object")
    return doc


@_locked_access
def _auth_json_add_key(provider: str, key: str) -> tuple[bool, int]:
    """Append `key` to auth.json's providers[provider] list (creating the file/
    section as needed). Returns (added, total_count) — added=False on duplicate.
    Mirrors scripts/auth.sh's append_key."""
    doc = _read_auth_doc()
    providers = doc.setdefault("providers", {})
    keys = providers.setdefault(provider, [])
    if key in keys:
        return False, len(keys)
    keys.append(key)
    _atomic_write_text(AUTH_FILE, json.dumps(doc, indent=2) + "\n")
    try:
        os.chmod(AUTH_FILE, 0o600)   # keys are secrets — owner read/write only
    except OSError:
        pass
    return True, len(keys)


# ── Proxy (access) key management ───────────────────────────────────────────────
# "Proxy keys" are the credential CALLERS use to authenticate to the router
# itself (PROXY_API_KEYS) — distinct from provider keys above, which the router
# uses to authenticate to upstream providers. Lets the dashboard mint new keys
# for teammates/other apps, with optional per-key budgets, without hand-editing
# .env/auth.json. Management requires the separate owner credential/session.

def _generate_proxy_key() -> str:
    """A new, cryptographically random proxy key. Shown once at creation time —
    only its last-6-char tail is ever displayed again, matching every other key
    in this codebase."""
    return "sk-router-" + secrets.token_urlsafe(24)


def _read_proxy_api_keys_live() -> list[str]:
    """Runtime state is authoritative; dashboard mutations update it under lock."""
    with _ACCESS_LOCK:
        return list(PROXY_API_KEYS)


def _read_proxy_keys_meta() -> dict:
    """Fresh-read auth.json's proxy_keys metadata (name + limits per key)."""
    if not AUTH_FILE.exists():
        return {}
    try:
        doc = json.loads(AUTH_FILE.read_text())
    except Exception:
        return {}
    pk = doc.get("proxy_keys", {})
    return pk if isinstance(pk, dict) else {}


@_locked_access
def _write_proxy_key_meta(key: str, patch: dict) -> None:
    """Merge `patch` into auth.json's proxy_keys[key] (creating it if absent)."""
    doc = _read_auth_doc()
    pk = doc.setdefault("proxy_keys", {})
    spec = pk.get(key, {})
    spec.update(patch)
    pk[key] = spec
    _atomic_write_text(AUTH_FILE, json.dumps(doc, indent=2) + "\n")
    try:
        os.chmod(AUTH_FILE, 0o600)
    except OSError:
        pass


@_locked_access
def _delete_proxy_key_meta(key: str) -> None:
    if not AUTH_FILE.exists():
        return
    doc = _read_auth_doc()
    pk = doc.get("proxy_keys")
    if isinstance(pk, dict) and pk.pop(key, None) is not None:
        _atomic_write_text(AUTH_FILE, json.dumps(doc, indent=2) + "\n")
        try:
            os.chmod(AUTH_FILE, 0o600)
        except OSError:
            pass


def _persist_access_keys(keys):
    doc = _read_auth_doc()
    doc["access_keys"] = keys
    _atomic_write_text(AUTH_FILE, json.dumps(doc, indent=2) + "\n")
    PROXY_API_KEYS[:] = keys
    os.environ["PROXY_API_KEYS"] = ",".join(keys)
    _reload_access_policy()


def _reload_access_policy():
    global KEY_LIMITS, KEY_LIMITS_ON, KEY_PROVIDER_SCOPE, KEY_MODEL_SCOPE
    KEY_LIMITS = _load_key_limits()
    KEY_LIMITS_ON = any(any(v.values()) for v in KEY_LIMITS.values())
    KEY_PROVIDER_SCOPE = _load_key_provider_scope()
    KEY_MODEL_SCOPE = _load_key_model_scope()


def _resolve_proxy_key_by_tail(tail: str, keys: list[str]) -> str | None:
    matches = [k for k in keys if k[-6:] == tail]
    return matches[0] if len(matches) == 1 else None


def _trigger_restart(delay_s: float = 1.2) -> bool:
    """Schedule a restart only for a verified standalone process managed by the CLI."""
    script = Path(__file__).resolve().parent / "scripts" / "restart.sh"
    if os.name == "nt" or not script.exists() or not shutil.which("bash"):
        return False
    # Service/container permissions cannot be established by accepting an HTTP request.
    if os.environ.get("INVOCATION_ID") or Path("/.dockerenv").exists():
        return False
    try:
        if int((script.parent.parent / "router.pid").read_text().strip()) != os.getpid():
            return False
    except (OSError, ValueError):
        return False

    def _go():
        try:
            subprocess.Popen(["/usr/bin/env", "bash", str(script)],
                              cwd=str(script.parent.parent),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True)
        except Exception as e:
            log.error(f"restart trigger failed: {e}")

    threading.Timer(delay_s, _go).start()
    return True


# ── Instance registry / Docker launcher ────────────────────────────────────────
# Instances let one "manager" router keep track of other Hermes Router processes.
# External instances are just monitored by base URL. Managed instances are Docker
# containers created by this process when the host has Docker available.

_INSTANCE_LOCK = threading.Lock()
_INSTANCE_SNAPSHOT_LOCK = threading.Lock()
_INSTANCE_SNAPSHOTS = {}
_INSTANCE_PROBING = set()
_INSTANCE_GENERATIONS = {}
_INSTANCE_PROBE_SLOTS = threading.BoundedSemaphore(4)
_INSTANCE_FRESH_SECONDS = 15


def _instance_identity(entry):
    owner = (os.path.abspath(INSTANCE_FILE), entry.get("id"))
    return (*owner, hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest(),
            _INSTANCE_GENERATIONS.get(owner, 0))


def _invalidate_instance_snapshot(entry):
    owner = (os.path.abspath(INSTANCE_FILE), entry.get("id"))
    with _INSTANCE_SNAPSHOT_LOCK:
        _INSTANCE_GENERATIONS[owner] = _INSTANCE_GENERATIONS.get(owner, 0) + 1
        for identity in list(_INSTANCE_SNAPSHOTS):
            if identity[:2] == owner:
                del _INSTANCE_SNAPSHOTS[identity]


def _refresh_instance(entry, identity, registry_file):
    try:
        snapshot = {"live": _probe_instance(entry), "checked_at": _utc_now(),
                    "checked_ts": time.monotonic()}
        if entry.get("mode") == "docker":
            snapshot["docker"] = _docker_state(entry)
        with _INSTANCE_LOCK:
            if INSTANCE_FILE != registry_file:
                return
            _, current = _find_instance(_read_instances_doc(), entry.get("id"))
            if current is None or _instance_identity(current) != identity:
                return
            with _INSTANCE_SNAPSHOT_LOCK:
                _INSTANCE_SNAPSHOTS[identity] = snapshot
    except Exception:
        log.warning("Instance refresh failed", exc_info=True)
    finally:
        with _INSTANCE_SNAPSHOT_LOCK:
            _INSTANCE_PROBING.discard(identity)
        _INSTANCE_PROBE_SLOTS.release()


def _instance_snapshot(entry):
    identity = _instance_identity(entry)
    with _INSTANCE_SNAPSHOT_LOCK:
        snapshot = dict(_INSTANCE_SNAPSHOTS.get(identity, {}))
        age = max(0, time.monotonic() - snapshot["checked_ts"]) if snapshot else None
        fresh = age is not None and age < _INSTANCE_FRESH_SECONDS
        if not fresh and identity not in _INSTANCE_PROBING and _INSTANCE_PROBE_SLOTS.acquire(False):
            _INSTANCE_PROBING.add(identity)
            # Copy nested env data before handing mutable configuration to a worker.
            thread = threading.Thread(target=_refresh_instance,
                                      args=(json.loads(json.dumps(entry)), identity, INSTANCE_FILE), daemon=True)
            try:
                thread.start()
            except Exception:
                _INSTANCE_PROBING.discard(identity)
                _INSTANCE_PROBE_SLOTS.release()
                raise
        checking = identity in _INSTANCE_PROBING
    snapshot.pop("checked_ts", None)
    snapshot.setdefault("live", {"status": "unknown", "health_ok": False, "auth_ok": None,
                                  "latency_ms": None, "message": "Awaiting background check"})
    if entry.get("mode") == "docker":
        snapshot.setdefault("docker", {"status": "unknown", "available": None,
                                       "exists": None, "running": None, "message": "Awaiting background check"})
    snapshot["freshness"] = {"state": "fresh" if fresh else "checking" if checking else "stale" if age is not None else "pending",
                             "age_seconds": round(age, 1) if age is not None else None,
                             "checked_at": snapshot.pop("checked_at", None)}
    return snapshot


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _slug(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_.-]+", "-", s.strip().lower()).strip("-")
    return s[:48] or "instance"


def _read_instances_doc() -> dict:
    if not INSTANCE_FILE.exists():
        return {"instances": []}
    try:
        doc = json.loads(INSTANCE_FILE.read_text())
    except Exception as e:
        log.warning(f"Could not read {INSTANCE_FILE}: {e}")
        return {"instances": []}
    if not isinstance(doc, dict):
        return {"instances": []}
    instances = doc.get("instances")
    if not isinstance(instances, list):
        doc["instances"] = []
    return doc


def _write_instances_doc(doc: dict) -> None:
    previous = _read_instances_doc().get("instances", [])
    INSTANCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    INSTANCE_FILE.write_text(json.dumps(doc, indent=2) + "\n")
    try:
        os.chmod(INSTANCE_FILE, 0o600)
    except OSError:
        pass
    current = {e.get("id"): e for e in doc.get("instances", [])}
    for entry in previous:
        if current.get(entry.get("id")) != entry:
            _invalidate_instance_snapshot(entry)
    identities = {_instance_identity(e) for e in doc.get("instances", [])}
    with _INSTANCE_SNAPSHOT_LOCK:
        for identity in list(_INSTANCE_SNAPSHOTS):
            if identity not in identities:
                del _INSTANCE_SNAPSHOTS[identity]


def _normalize_instance_base_url(raw: str | None, host_port: int | None = None) -> tuple[str | None, str | None]:
    value = (raw or "").strip()
    if not value and host_port:
        value = f"http://localhost:{host_port}/v1"
    if not value:
        return None, "missing 'base_url'"
    parsed = urlparse(value if "://" in value else "http://" + value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None, "base_url must be an http(s) URL"
    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1"
    clean = parsed._replace(path=path, params="", query="", fragment="")
    return urlunparse(clean), None


def _instance_health_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3].rstrip("/")
    clean = parsed._replace(path=(path + "/health") if path else "/health",
                            params="", query="", fragment="")
    return urlunparse(clean)


def _instance_models_url(base_url: str) -> str:
    return base_url.rstrip("/") + "/models"


def _mask_secret(value: str | None) -> dict:
    if not value:
        return {"configured": False, "tail": ""}
    return {"configured": True, "tail": value[-6:]}


def _instance_public(entry: dict, *, include_live: bool = True) -> dict:
    out = {k: v for k, v in entry.items() if k not in ("api_key", "env")}
    out["api_key"] = _mask_secret(entry.get("api_key"))
    env = entry.get("env") if isinstance(entry.get("env"), dict) else {}
    out["env"] = {
        "count": len(env),
        "keys": sorted(env.keys()),
        "secret_keys": sorted(k for k in env if "KEY" in k or "TOKEN" in k or "SECRET" in k),
    }
    if include_live:
        out.update(_instance_snapshot(entry))
    return out


def _find_instance(doc: dict, instance_id: str) -> tuple[int, dict | None]:
    for i, entry in enumerate(doc.get("instances", [])):
        if entry.get("id") == instance_id:
            return i, entry
    return -1, None


def _parse_port(value, field: str, default: int | None = None) -> tuple[int | None, str | None]:
    if value in (None, ""):
        return default, None
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None, f"{field} must be a whole number"
    if port < 1 or port > 65535:
        return None, f"{field} must be between 1 and 65535"
    return port, None


def _parse_instance_env(value) -> tuple[dict, str | None]:
    if value in (None, ""):
        return {}, None
    if not isinstance(value, dict):
        return {}, "'env' must be an object of environment variables"
    out = {}
    for k, v in value.items():
        key = str(k or "").strip()
        val = str(v or "").strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", key):
            return {}, f"invalid env var name: {key}"
        if key in {"HOST", "PORT", "HR_ENV_FILE", "HR_USAGE_DB", "HERMES_INSTANCES_FILE",
                   "ROUTER_AUTH_FILE", "ROUTER_STATE_FILE", "CACHE_DB_PATH"}:
            return {}, f"{key} is managed by the instance launcher"
        if any(c in val for c in "\n\r\0"):
            return {}, f"{key} must be a single-line value"
        if val:
            out[key] = val
    return out, None


def _keys_for_instance_copy(provider: str) -> list[str]:
    env_var = PROVIDER_KEY_ENV.get(provider)
    if not env_var:
        return []
    auth_keys = _load_auth_json().get(provider, [])
    seen, out = set(), []
    for key in [*auth_keys, *_keys(env_var)]:
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _provider_key_counts_for_instance_copy() -> dict:
    return {p: len(_keys_for_instance_copy(p)) for p in KEY_SETTABLE_PROVIDERS}


def _parse_copy_provider_keys(value) -> tuple[list[str], str | None]:
    if value in (None, ""):
        return [], None
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        return [], "'copy_provider_keys' must be a list of provider names"
    requested = list(dict.fromkeys(x.strip() for x in value if x.strip()))
    unknown = [p for p in requested if p not in PROVIDER_KEY_ENV]
    if unknown:
        return [], f"unknown provider(s): {', '.join(unknown)}"
    missing = [p for p in requested if not _keys_for_instance_copy(p)]
    if missing:
        return [], f"no existing key(s) available for: {', '.join(missing)}"
    return requested, None


def _copy_provider_keys_env(providers: list[str]) -> dict:
    out = {}
    for provider in providers:
        keys = _keys_for_instance_copy(provider)
        if keys:
            out[PROVIDER_KEY_ENV[provider]] = ",".join(keys)
    return out


def _build_instance_from_body(body: dict, existing: dict | None = None) -> tuple[dict | None, str | None]:
    existing = existing or {}
    mode = str(body.get("mode", existing.get("mode", "external")) or "external").strip().lower()
    if mode not in ("external", "docker"):
        return None, "mode must be 'external' or 'docker'"

    host_port, err = _parse_port(body.get("host_port", existing.get("host_port")), "host_port")
    if err:
        return None, err
    container_port, err = _parse_port(
        body.get("container_port", existing.get("container_port", INSTANCE_CONTAINER_PORT)),
        "container_port",
        INSTANCE_CONTAINER_PORT,
    )
    if err:
        return None, err
    if mode == "docker" and not host_port:
        return None, "host_port is required for docker instances"

    base_url, err = _normalize_instance_base_url(body.get("base_url", existing.get("base_url")), host_port)
    if err:
        return None, err

    name = str(body.get("name", existing.get("name", "")) or "").strip()[:80]
    if not name:
        return None, "missing 'name'"
    if any(c in name for c in "\n\r"):
        return None, "name must not contain newlines"

    api_key = str(body.get("api_key", existing.get("api_key", "")) or "").strip()
    if any(c in api_key for c in "\n\r\0"):
        return None, "api_key must be a single-line value"
    if mode == "docker" and not api_key:
        api_key = _generate_proxy_key()

    raw_env = body.get("env", existing.get("env", {}))
    if "env" not in body and isinstance(raw_env, dict):
        # Older registry entries could override these paths. Normalize inherited
        # values so an unrelated metadata edit remains possible after upgrading.
        raw_env = {k: v for k, v in raw_env.items()
                   if k not in {"HR_ENV_FILE", "HR_USAGE_DB", "HERMES_INSTANCES_FILE"}}
    env, err = _parse_instance_env(raw_env)
    if err:
        return None, err
    copy_provider_keys, err = _parse_copy_provider_keys(body.get("copy_provider_keys", existing.get("copy_provider_keys", [])))
    if err:
        return None, err
    if copy_provider_keys and mode != "docker":
        return None, "copy_provider_keys is only available for docker instances"
    env.update(_copy_provider_keys_env(copy_provider_keys))

    instance_id = existing.get("id") or uuid.uuid4().hex[:12]
    image = str(body.get("image", existing.get("image", INSTANCE_DOCKER_IMAGE)) or INSTANCE_DOCKER_IMAGE).strip()
    if any(c in image for c in "\n\r\0"):
        return None, "image must be a single-line value"
    container_name = str(body.get("container_name", existing.get("container_name", "")) or "").strip()
    if not container_name:
        container_name = f"{INSTANCE_DOCKER_PREFIX}-{_slug(name)}-{instance_id[:6]}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container_name):
        return None, "container_name contains unsupported characters"

    now = _utc_now()
    entry = {
        "id": instance_id,
        "name": name,
        "mode": mode,
        "base_url": base_url,
        "api_key": api_key,
        "host_port": host_port,
        "container_port": container_port,
        "image": image,
        "container_name": container_name,
        "env": env,
        "copy_provider_keys": copy_provider_keys,
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
    }
    return entry, None


def _probe_instance(entry: dict) -> dict:
    base_url = entry.get("base_url") or ""
    started = time.time()
    result = {"status": "unknown", "health_ok": False, "auth_ok": None,
              "latency_ms": None, "message": ""}
    resp = None
    try:
        resp = _HTTP.get(_instance_health_url(base_url), timeout=1.5)
        result["latency_ms"] = round((time.time() - started) * 1000, 1)
        result["health_ok"] = resp.status_code == 200
        result["status"] = "healthy" if resp.status_code == 200 else "unhealthy"
        result["message"] = f"health HTTP {resp.status_code}"
    except Exception as e:
        result["latency_ms"] = round((time.time() - started) * 1000, 1)
        result["status"] = "unreachable"
        result["message"] = str(e)[:160]
        return result
    finally:
        if resp is not None:
            resp.close()

    api_key = entry.get("api_key") or ""
    if api_key:
        mr = None
        try:
            mr = _HTTP.get(_instance_models_url(base_url),
                           headers={"Authorization": "Bearer " + api_key},
                           timeout=1.5)
            result["auth_ok"] = mr.status_code == 200
            if mr.status_code == 401:
                result["status"] = "auth_error"
                result["message"] = "health ok, API key rejected"
            elif mr.status_code != 200:
                result["message"] = f"health ok, models HTTP {mr.status_code}"
        except Exception as e:
            result["auth_ok"] = False
            result["message"] = f"health ok, models check failed: {str(e)[:120]}"
        finally:
            if mr is not None:
                mr.close()
    return result


def _docker_cmd(args: list[str], timeout: float = 15.0) -> tuple[bool, str, int]:
    try:
        proc = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, "Docker CLI not found on this host.", 127
    except subprocess.TimeoutExpired:
        return False, "Docker command timed out.", 124
    output = (proc.stdout or proc.stderr or "").strip()
    return proc.returncode == 0, output, proc.returncode


def _docker_state(entry: dict) -> dict:
    name = entry.get("container_name") or ""
    ok, out, code = _docker_cmd(["inspect", "-f", "{{.State.Status}}|{{.State.Running}}", name], timeout=5)
    if not ok:
        return {"available": code != 127, "exists": False, "running": False,
                "status": "missing" if code != 127 else "unavailable", "message": out}
    parts = out.split("|")
    status = parts[0] if parts else "unknown"
    running = len(parts) > 1 and parts[1].strip().lower() == "true"
    return {"available": True, "exists": True, "running": running, "status": status, "message": ""}


def _docker_run_instance(entry: dict) -> tuple[bool, str]:
    env = dict(entry.get("env") or {})
    if entry.get("api_key"):
        env["PROXY_API_KEYS"] = entry["api_key"]
    env.update({
        "HOST": "0.0.0.0",
        "PORT": str(entry.get("container_port") or INSTANCE_CONTAINER_PORT),
        # Keep all durable state on the named instance volume, rather than the
        # image's anonymous /app/data volume (which recreation does not reuse).
        "HR_ENV_FILE": "/data/.env",
        "HR_USAGE_DB": "/data/usage.db",
        "HERMES_INSTANCES_FILE": "/data/instances.json",
        "ROUTER_AUTH_FILE": "/data/auth.json",
        "ROUTER_STATE_FILE": "/data/router_state.json",
        "CACHE_DB_PATH": "/data/cache.db",
    })
    volume = f"{entry['container_name']}-data:/data"
    args = [
        "run", "-d",
        "--name", entry["container_name"],
        "--label", "com.hermes-router.managed=true",
        "-p", f"{entry['host_port']}:{entry['container_port']}",
        "-v", volume,
    ]
    for k, v in sorted(env.items()):
        args += ["-e", f"{k}={v}"]
    args.append(entry.get("image") or INSTANCE_DOCKER_IMAGE)
    ok, out, _ = _docker_cmd(args, timeout=30)
    return ok, out


def _docker_action(entry: dict, action: str) -> tuple[bool, str]:
    if entry.get("mode") != "docker":
        return False, "This instance is registered as external; Docker actions are unavailable."
    _invalidate_instance_snapshot(entry)
    try:
        state = _docker_state(entry)
        if action == "start":
            if not state.get("available"):
                return False, state.get("message") or "Docker unavailable."
            if not state.get("exists"):
                return _docker_run_instance(entry)
            ok, out, _ = _docker_cmd(["start", entry["container_name"]], timeout=15)
            return ok, out
        if action == "stop":
            ok, out, _ = _docker_cmd(["stop", entry["container_name"]], timeout=20)
            return ok, out
        if action == "restart":
            if state.get("exists"):
                ok, out, _ = _docker_cmd(["restart", entry["container_name"]], timeout=20)
                return ok, out
            return _docker_run_instance(entry)
        return False, f"unknown Docker action: {action}"

    finally:
        _invalidate_instance_snapshot(entry)

# ── Credential pool ────────────────────────────────────────────────────────────

# ── Smart routing helpers ─────────────────────────────────────────────────────

def _rate_model(model_name: str) -> int:
    mn = model_name.lower()
    for key in sorted(KNOWN_MODEL_RATINGS, key=len, reverse=True):
        if key in mn:
            return KNOWN_MODEL_RATINGS[key]
    for rating, patterns in _RATING_PATTERNS:
        if any(p in mn for p in patterns):
            return rating
    return 3


def _apply_price_overrides():
    """Merge MODEL_PRICES_FILE (JSON {"model-substr": [in, out]}) over the built-in
    price table, so users can correct/extend prices without editing code."""
    path = os.environ.get("MODEL_PRICES_FILE")
    if not path or not os.path.exists(path):
        return
    try:
        doc = json.loads(Path(path).read_text())
        n = 0
        for k, v in (doc or {}).items():
            if isinstance(v, (list, tuple)) and len(v) == 2:
                MODEL_PRICES[k.lower()] = (float(v[0]), float(v[1])); n += 1
        log.info(f"Pricing: loaded {n} override(s) from {path}")
    except Exception as e:
        log.warning(f"Pricing: could not read MODEL_PRICES_FILE {path}: {e}")


def _price_model(model: str) -> tuple:
    """(input, output) USD per 1M tokens for a model; (0, 0) if unpriced/free.
    Longest-substring match, mirroring _rate_model."""
    mn = (model or "").lower()
    for key in sorted(MODEL_PRICES, key=len, reverse=True):
        if key in mn:
            return MODEL_PRICES[key]
    return (0.0, 0.0)


def _price_rank(model: str) -> float:
    """Single sortable price estimate. Unknown/free/subscription models are 0."""
    pin, pout = _price_model(model)
    return float(pin or 0.0) + float(pout or 0.0)


def _quality_rank(provider_name: str, model: str) -> int:
    """Lower is better. Used only after cost/tier tie-breaks, so it never makes a
    known-expensive model beat a cheaper capable one."""
    mn = (model or "").lower()
    for key in sorted(MODEL_QUALITY_RANKS, key=len, reverse=True):
        if key in mn:
            return MODEL_QUALITY_RANKS[key]
    # Fall back to capability rating, then provider rank for stable same-price ties.
    return _rate_model(model) * 100 + PROVIDER_QUALITY_RANKS.get(provider_name, 99)


def _cost(model: str, prompt_toks, completion_toks) -> float:
    """Estimated USD cost of one response from its token usage. Free/unpriced = 0."""
    pin, pout = _price_model(model)
    if not pin and not pout:
        return 0.0
    return (int(prompt_toks or 0) / 1e6) * pin + (int(completion_toks or 0) / 1e6) * pout


def _cost_obj(usd: float) -> dict:
    """Serialize a USD amount for JSON output, adding a converted figure when
    COST_FX_RATE is set (e.g. {"usd": 0.0123, "inr": 1.02})."""
    out = {"usd": round(float(usd or 0), 6)}
    if COST_FX_RATE > 0 and COST_CURRENCY != "USD":
        out[COST_CURRENCY.lower()] = round(float(usd or 0) * COST_FX_RATE, 4)
    return out


_apply_price_overrides()


def _model_env_suffix(model: str) -> str:
    """Sanitize a model id into an env-var fragment: upper-case, non-alnum → '_'.
    e.g. 'gemini-2.5-pro' → 'GEMINI_2_5_PRO' (used for <PROVIDER>_<MODEL>_* overrides)."""
    return re.sub(r"[^A-Za-z0-9]+", "_", model).strip("_").upper()


def _model_caps(name: str, model: str) -> dict:
    """Per-(provider, model) capability with optimistic defaults. Rating is always
    derivable (pattern-based, free); tools default to capable when unknown (mirrors
    _supports_tools' stance) and reasoning defaults to off."""
    st = _model_state.get((name, model))
    if st:
        return st
    return {
        "rating": _rate_model(model),
        "supports_tools": True,
        "tools_confirmed": False,
        "reasoning": False,
    }


def _model_supports_tools(name: str, model: str) -> bool:
    """Whether this specific (provider, model) handles function calling."""
    return bool(_model_caps(name, model).get("supports_tools", True))


def _model_has_confirmed_tool_support(name: str, model: str) -> bool:
    """Whether tool support was explicitly configured or positively probed."""
    caps = _model_state.get((name, model))
    return bool(caps and caps.get("supports_tools") is True and caps.get("tools_confirmed") is True)


def _cached_model_caps_compatible(caps: object) -> bool:
    """Legacy cache entries predate tools_confirmed and must be re-probed."""
    return isinstance(caps, dict) and "tools_confirmed" in caps


# Known vision-capable model families, matched by substring (mirrors _rate_model's
# approach). Unlike tool support — which most modern chat models handle, so
# _model_supports_tools defaults to True — vision support is the exception rather
# than the rule among free-tier/small text models. A real cascade test showed 5 of
# 6 non-vision candidates (mistral, cerebras, groq, huggingface) fail cleanly on an
# image request before reaching a model that works, wasting real latency. So this
# defaults to False, but — exactly like enforce_tool — the caller only enforces the
# filter when at least one matching candidate exists, so an incomplete pattern list
# can never make routing worse than it is today, only skip predictable failures.
_VISION_MODEL_PATTERNS = (
    "gemini", "gpt-4o", "gpt-4.1", "gpt-4-turbo", "gpt-5", "o1", "o3",
    "claude-3", "claude-opus", "claude-sonnet", "claude-haiku",
    "pixtral", "llava", "-vl", "vl-", "llama-4", "grok", "vision",
)


def _model_supports_vision(provider: dict, model: str) -> bool:
    """Whether this specific (provider, model) can accept image input.
    Anthropic and Codex (GPT-4o/5-family via ChatGPT) are natively multimodal;
    everything else is matched by known vision-capable family name patterns."""
    if provider.get("protocol") in ("anthropic", "codex"):
        return True
    mn = model.lower()
    if "embed" in mn:   # e.g. gemini-embedding-001 — matches "gemini" but isn't a chat model
        return False
    return any(p in mn for p in _VISION_MODEL_PATTERNS)


def _payload_has_image(payload: dict) -> bool:
    """Whether any message in this OpenAI-format payload carries image content."""
    for m in payload.get("messages", []):
        content = m.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "image_url":
                    return True
    return False


def _discover_best_model(base_url: str, key: str, extra_headers: dict = None,
                         free_only: bool = False) -> str | None:
    try:
        hdrs = {"Authorization": f"Bearer {key}", **(extra_headers or {})}
        r = _HTTP.get(f"{base_url.rstrip('/')}/models", headers=hdrs, timeout=10)
        if r.status_code != 200:
            return None
        models = [m["id"] for m in r.json().get("data", []) if isinstance(m.get("id"), str)]
        if free_only:
            models = [m for m in models if _is_free_model_id(m)]
        return min(models, key=_rate_model) if models else None
    except Exception:
        return None


def _discover_models(provider: dict, key: str, free_only: bool = False) -> list[str]:
    """Fetch provider models from an OpenAI-compatible /models endpoint.

    Returns a quality-sorted full catalog (caller applies AUTO_DISCOVER_MODEL_LIMIT
    when appending extras). Fail-soft: any provider quirk simply disables discovery
    for that provider on this start.
    """
    try:
        hdrs = {"Authorization": f"Bearer {key}", **provider.get("headers", {})}
        r = _HTTP.get(f"{provider['base_url'].rstrip('/')}/models", headers=hdrs, timeout=10)
        if r.status_code != 200:
            return []
        models = []
        for item in r.json().get("data", []):
            mid = item.get("id") if isinstance(item, dict) else None
            if isinstance(mid, str) and mid.strip():
                normalized = mid.strip()
                # Gemini's OpenAI-compat /models returns ids like models/gemini-2.5-pro.
                if provider["name"] == "gemini" and normalized.startswith("models/"):
                    normalized = normalized[len("models/"):]
                models.append(normalized)
                limits = context_policy.discovered_limits(item)
                if limits:
                    _DISCOVERED_CONTEXT_LIMITS[(provider["name"], normalized)] = limits
        if free_only:
            models = [m for m in models if _is_free_model_id(m)]
        models = list(dict.fromkeys(models))
        models.sort(key=lambda m: (_price_rank(m), _quality_rank(provider["name"], m), m.lower()))
        return models
    except Exception as e:
        log.debug(f"[ratings]   {provider['name']}: model discovery skipped: {e}")
        return []


def _provider_model_discovery_enabled(provider: dict) -> bool:
    name = provider["name"].upper()
    val = os.environ.get(f"{name}_AUTO_DISCOVER_MODELS")
    if val is None:
        return AUTO_DISCOVER_MODELS
    return val.strip().lower() not in ("0", "", "false", "no", "off")


def _free_discovery_enabled(provider):
    value = os.environ.get(provider["name"].upper() + "_AUTO_DISCOVER_FREE_MODELS")
    return AUTO_DISCOVER_FREE_MODELS if value is None else value.lower() not in ("0", "false", "no", "off", "")


def _free_tier_confirmed(provider):
    return os.environ.get(provider["name"].upper() + "_DISCOVERY_FREE_TIER", "0") == "1"


def _read_free_catalog(provider, key):
    if provider.get("protocol") in ("anthropic", "codex"):
        return {}
    items = model_discovery.fetch_catalog(_HTTP, provider, key)
    return model_discovery.free_chat_models(provider["name"], items, _free_tier_confirmed(provider))


def _publish_free_catalog(provider, selected, pool_ref):
    """Publish a completed catalog atomically; never change model on/off choices."""
    name = provider["name"]
    manual = provider.get("_manual_models", _provider_models(provider))
    manual = [m for m in manual if not model_discovery.non_chat_id(m)]
    selected = {m: row for m, row in selected.items() if m not in _excluded_models(name)
                and m.lower() not in _excluded_models(name)}
    models = _filter_excluded(name, list(dict.fromkeys(manual + sorted(selected))))
    metadata = {}
    for model, row in selected.items():
        limits = context_policy.discovered_limits(row) or {}
        parameters = row.get("supported_parameters", [])
        tools = isinstance(parameters, list) and "tools" in parameters
        metadata[model] = {"rating": _rate_model(model), "supports_tools": tools,
                           "tools_confirmed": tools, "reasoning": "reasoning" in parameters
                           if isinstance(parameters, list) else False, **limits}
    with _ACCESS_LOCK:
        for model in models:
            pool_ref.ensure_model(name, model, provider["keys"])
        provider["models"] = models
        provider["model"] = models[0] if models else ""
        provider["_discovered_metadata"] = metadata
        for model, caps in metadata.items():
            _DISCOVERED_CONTEXT_LIMITS[(name, model)] = {
                k: v for k, v in caps.items() if k in ("context_window", "max_output_tokens")}
            # Keep previously confirmed capabilities; catalogs need no inference probes.
            _model_state.setdefault((name, model), caps)
        state = _provider_state.setdefault(name, {})
        state["model"] = provider["model"]
        if selected and "available" not in state:
            state.update(available=True, probe_status="catalog", latency_ms=0)
        _DISCOVERY_STATUS[name] = {"state": "complete", "free_models": len(selected),
            "registered_models": len(models), "free_tier_confirmed": _free_tier_confirmed(provider),
            "message": (f"Added all {len(selected)} confirmed free chat models."
                        if selected else "No confirmed free chat models found. Account plans or prices may be unavailable; choose models manually or confirm a supported free plan."),
            "updated_at": time.time()}


def _refresh_free_models(provider, key, pool_ref):
    try:
        selected = _read_free_catalog(provider, key)
        if _provider_model_discovery_enabled(provider):
            _publish_free_catalog(provider, selected, pool_ref)
    except model_discovery.CatalogError as error:
        _DISCOVERY_STATUS[provider["name"]] = {"state": "error", "message": str(error)}


def _refresh_discovered_models(provider: dict, key: str, pool_ref) -> None:
    """Opt-in model refresh: prune configured models not reported by /models and
    append the best discovered models up to AUTO_DISCOVER_MODEL_LIMIT.

    Configured models that still exist in the API catalog are always kept;
    AUTO_DISCOVER_MODEL_LIMIT only bounds how many extras are appended.

    This is deliberately conservative: unsupported protocols and huge/mixed
    catalogs are skipped unless a per-provider env flag explicitly enables them.
    """
    if not _provider_model_discovery_enabled(provider):
        return
    if _free_discovery_enabled(provider):
        _refresh_free_models(provider, key, pool_ref)
        return
    name = provider["name"]
    if name in _MODEL_DISCOVERY_SKIP and os.environ.get(f"{name.upper()}_AUTO_DISCOVER_MODELS") is None:
        log.info(f"[ratings]   {name}: model discovery skipped by default")
        return
    free_only = name in _FREE_ONLY_DISCOVERY
    discovered = _filter_excluded(name, _discover_models(provider, key, free_only=free_only))
    if not discovered:
        return

    configured = _provider_models(provider)
    discovered_set = set(discovered)
    # Prune only when doing so still leaves a configured model; otherwise the
    # existing invalid-model repair path can try to recover a primary model.
    kept = _filter_excluded(name, [m for m in configured if m in discovered_set])
    if not kept:
        kept = discovered[:1]
    # Never drop valid configured models; only bound appended discoveries.
    extras = [m for m in discovered if m not in kept]
    append_limit = max(0, AUTO_DISCOVER_MODEL_LIMIT - len(kept))
    refreshed = list(dict.fromkeys(kept + extras[:append_limit]))
    if not refreshed or refreshed == configured:
        return

    provider["models"] = refreshed
    old_primary = provider["model"]
    provider["model"] = refreshed[0]
    for m in refreshed:
        try:
            pool_ref.ensure_model(name, m, provider["keys"])
        except AttributeError:
            pass
    if old_primary != provider["model"]:
        pool_ref.rename_model(name, old_primary, provider["model"])
    log.info(f"[ratings]   {name}: discovered models → {', '.join(refreshed)}")


def _probe_anthropic(provider: dict, key: str) -> tuple:
    """Probe Anthropic using the Messages API (not OpenAI-format /chat/completions)."""
    url  = "https://api.anthropic.com/v1/messages"
    hdrs = {"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
    body = {"model": provider["model"], "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}
    t0 = time.time()
    try:
        r = _HTTP.post(url, headers=hdrs, json=body, timeout=12)
        latency = (time.time() - t0) * 1000
        if r.status_code == 200:
            return True, latency, provider["model"], "ok"
        return False, latency, provider["model"], ("auth" if r.status_code in (401, 403) else "http")
    except requests.exceptions.ReadTimeout:
        return True, (time.time() - t0) * 1000, provider["model"], "timeout"
    except Exception:
        return False, (time.time() - t0) * 1000, provider["model"], "network"


def _probe_provider(provider: dict, key: str) -> tuple:
    """Returns (success, latency_ms, model_used, status). Auto-discovers alt model on 400/404.

    A read-timeout means the provider accepted the request and is still
    generating — alive but slow. Large MoE models can cold-start for 30–60s,
    past the probe window, so a read-timeout counts as available rather than
    wrongly dropping a working provider to the back of its rating tier. Only a
    connection failure (host unreachable) counts as down."""
    if not _model_enabled(provider["name"], provider["model"]):
        return False, 0, provider["model"], "disabled"
    if provider.get("protocol") == "anthropic":
        return _probe_anthropic(provider, key)
    if provider.get("protocol") == "codex":
        # Don't spend ChatGPT quota (or risk ToS) on a startup completion —
        # "available" means we can mint a valid access token for the account.
        t0 = time.time()
        ok = bool(codex_creds.get_access_token(key))
        return ok, (time.time() - t0) * 1000, provider["model"], ("ok" if ok else "auth")

    url  = provider["base_url"].rstrip("/") + "/chat/completions"
    hdrs = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
            **provider.get("headers", {})}
    body = {"model": provider["model"],
            "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}
    t0 = time.time()
    try:
        r = _HTTP.post(url, headers=hdrs, json=body, timeout=12)
        latency = (time.time() - t0) * 1000
        if r.status_code == 200:
            return True, latency, provider["model"], "ok"
        if r.status_code == 429:
            return False, latency, provider["model"], "rate_limited"
        if r.status_code in (401, 403):
            return False, latency, provider["model"], "auth"
        if r.status_code in (400, 404) and provider["name"] not in ADDITIONAL_DEFAULTS:
            # Providers that list paid models alongside free ones — never let
            # auto-discovery silently pick something that costs credits.
            alt = _discover_best_model(provider["base_url"], key, provider.get("headers", {}),
                                       free_only=provider["name"] in _FREE_ONLY_DISCOVERY)
            if alt and _model_enabled(provider["name"], alt):
                body["model"] = alt
                t0 = time.time()
                r2 = _HTTP.post(url, headers=hdrs, json=body, timeout=12)
                if r2.status_code == 200:
                    return True, (time.time() - t0) * 1000, alt, "ok"
        return False, (time.time() - t0) * 1000, provider["model"], "http"
    except requests.exceptions.ReadTimeout:
        # Connected, still generating — alive, just slow (cold MoE start).
        return True, (time.time() - t0) * 1000, provider["model"], "timeout"
    except Exception:
        return False, (time.time() - t0) * 1000, provider["model"], "network"


_TOOL_PROBE = [{"type": "function", "function": {
    "name": "get_weather", "description": "Get the current weather for a city",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                   "required": ["city"]}}}]


def _probe_tools(provider: dict, key: str, model: str) -> bool | None:
    """Detect whether a provider's model supports function calling. Sends a tiny
    request that forces a tool call (tool_choice=required, falling back to auto
    for providers that reject 'required') and checks whether the model actually
    emits one. Anthropic providers always support tools.

    Returns True/False on a conclusive (HTTP 200) response, or None when neither
    attempt got one — network error, timeout, or a non-200 on both (e.g. the
    provider's free-tier RPM was already spent by earlier probes in this same
    startup pass). None means "couldn't determine", NOT "doesn't support tools":
    caching a transient probe failure as a confident False would silently and
    persistently (for STATE_TTL_HOURS) exclude a capable model from tool-aware
    routing. Callers should treat None as unknown and keep the optimistic default.
    """
    if provider.get("protocol") in ("anthropic", "codex"):
        return True   # both support function calling
    url  = provider["base_url"].rstrip("/") + "/chat/completions"
    hdrs = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
            **provider.get("headers", {})}
    base = {"model": model, "max_tokens": 64, "tools": _TOOL_PROBE,
            "messages": [{"role": "user", "content": "What is the weather in Paris? Use the get_weather tool."}]}
    got_response = False
    for choice in ("required", "auto"):
        if not _model_enabled(provider["name"], model):
            return None
        try:
            r = _HTTP.post(url, headers=hdrs, json={**base, "tool_choice": choice}, timeout=12)
        except Exception:
            continue   # network hiccup on this attempt — still try the other tool_choice
        if r.status_code != 200:
            continue   # provider may reject tool_choice=required → try auto
        got_response = True
        try:
            msg = (r.json().get("choices") or [{}])[0].get("message") or {}
            if msg.get("tool_calls"):
                return True
        except Exception:
            continue
    return False if got_response else None


def _probe_reasoning(provider: dict, key: str, model: str) -> bool:
    """Detect whether a provider's model is a 'reasoning' model — one that spends
    output tokens on hidden chain-of-thought before answering. These return empty
    content if max_tokens is too small to cover the thinking. We probe with a
    small budget and a trivial prompt: a reasoning model exposes a reasoning field
    or burns the whole budget thinking (empty content, truncated), while a normal
    model just answers. Anthropic's thinking is opt-in, so it's treated as normal."""
    if not _model_enabled(provider["name"], model):
        return False
    if provider.get("protocol") == "anthropic":
        return False
    if provider.get("protocol") == "codex":
        return True   # Codex (GPT-5) is a reasoning model — reserve output headroom
    url  = provider["base_url"].rstrip("/") + "/chat/completions"
    hdrs = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
            **provider.get("headers", {})}
    body = {"model": model, "max_tokens": 24,
            "messages": [{"role": "user", "content": "Reply with just the word: ready"}]}
    try:
        r = _HTTP.post(url, headers=hdrs, json=body, timeout=12)
        if r.status_code != 200:
            return False
        choice = (r.json().get("choices") or [{}])[0]
        msg     = choice.get("message") or {}
        content = (msg.get("content") or "").strip()
        if msg.get("reasoning_content") or msg.get("reasoning"):
            return True
        return not content and choice.get("finish_reason") == "length"
    except Exception:
        return False


def classify_complexity(messages: list) -> int:
    """Heuristic: 1 (critical) → 5 (trivial). No LLM call."""
    content = " ".join(
        m["content"] if isinstance(m.get("content"), str)
        else " ".join(p.get("text", "") for p in m["content"] if isinstance(p, dict))
        for m in messages if m.get("content")
    )
    tokens = len(content) // 4
    cl = content.lower()
    has_code    = "```" in content or any(k in cl for k in ["def ", "function ", "class ", "import "])
    has_complex = any(k in cl for k in ["implement", "design", "architect", "debug", "refactor",
                                         "algorithm", "optimize", "analyze", "build", "develop",
                                         "summarize", "explain how", "compare", "research", "create a plan",
                                         "generate", "convert", "migrate", "write tests", "test cases",
                                         "step by step", "walk me through", "help me understand"])
    has_simple  = any(k in cl for k in ["what is", "who is", "define", "translate", "yes or no",
                                         "how many", "give me a number", "true or false", "in one word",
                                         "spell", "what does", "one sentence", "yes or no answer",
                                         "what year", "what time", "how old"])
    if tokens > 2000 or (has_code and has_complex): return 1
    if tokens > 800  or has_complex:                return 2
    if tokens > 300  or has_code:                   return 3
    if tokens > 100  or (not has_simple):           return 4
    return 5


# Generic workload hints (`X-Hermes-Workload-Hint`) let a caller describe the
# kind of inference it is requesting without coupling the router to an agent
# product or execution runtime.
# routing biases the existing candidate order toward matching capability
# metadata. Purely a hint: unknown values and `general` are dropped, and every
# intent leaves the tier/price/quality ordering and the failover cascade intact.
_WORKLOAD_HINTS = frozenset({"planning", "coding", "review", "debug", "vision", "general"})


def _workload_hint(value: str | None) -> str | None:
    """Validate a workload hint. Missing, unknown, or `general` → None, which
    every downstream term treats as "no hint" and routes exactly as it does today."""
    value = (value or "").strip().lower()
    return value if value in _WORKLOAD_HINTS and value != "general" else None


def _workload_fit(hint: str | None, provider: dict, model: str) -> int:
    """0 = this candidate matches the caller's workload hint, 1 = it doesn't.
    Reads only existing capability metadata — never a model or provider name.
    Returns a constant 0 for no/unknown intent and for `debug` (which reuses the
    existing fast-route preference instead), so ordering is bit-for-bit unchanged."""
    if hint == "coding":            # editing/tool-driven work needs function calling
        return 0 if _model_supports_tools(provider["name"], model) else 1
    if hint in ("review", "planning"):   # both want the stronger reasoning models
        return 0 if _model_caps(provider["name"], model).get("reasoning") else 1
    if hint == "vision":            # only reached when the payload really has an image
        return 0 if _model_supports_vision(provider, model) else 1
    return 0


def _get_smart_ordered(providers: list, complexity: int, est_tokens: int = 0,
                       prefer_local: bool = False, workload_hint: str | None = None) -> list:
    """
    Rank every configured (provider, model) for this complexity: cheapest capable
    model first, then better same-price models, then too-weak as last resort. Never blocks. Returns
    a flat list of candidate dicts {"provider": <provider>, "model": <model str>}.

    Each model in a provider's comma-separated list is its own candidate, scored on
    its OWN rating — so e.g. gemini-2.5-pro can be picked for a hard request while
    gemini-2.5-flash-lite handles easy ones, instead of the extra models only being
    429-failover. Within equal ratings, a provider's models keep their listed order
    (list_index tie-break), so cheapest-first ordering still holds.

    When FAST_ROUTE_THRESHOLD is set and the request is shorter than it, low-latency
    providers win ties. With prefer_local (the `:fast` profile), a configured local
    model leads on easy turns (complexity ≥ 3), with cloud as fallback.

    Round-robin: the PROVIDER list is rotated by a per-request counter before
    flattening, so providers that tie on every criterion spread load; the sort is
    stable, so equal-keyed candidates keep their (rotated) relative order.
    """
    fast_first = FAST_ROUTE_TOKENS > 0 and 0 < est_tokens < FAST_ROUTE_TOKENS
    # `debug` reuses the existing low-latency preference rather than adding a
    # ranking term of its own — a debug turn wants a quick answer.
    if workload_hint == "debug":
        fast_first = True

    def _key(cand):
        p      = cand["provider"]
        model  = cand["model"]
        name   = p["name"]
        rating = _model_caps(name, model)["rating"]
        avail  = _provider_state.get(name, {}).get("available", True)
        fast   = 0 if (fast_first and name in _FAST_PROVIDERS) else 1
        # `:fast` profile: a short/casual turn prefers the local model first.
        local_first = 0 if (prefer_local and name == "local" and complexity >= 3) else 1
        # Health-aware terms — tier/sort_within stay FIRST so capability matching
        # is never overridden by health (a healthy weak model must not outrank the
        # correct-capability one). When every candidate is healthy these two terms
        # are constant (0), leaving the existing round-robin/tie order untouched.
        breaker_open = 1 if stats.breaker_open(name) else 0  # open breakers sink within tier
        health       = stats.health_bucket(name)             # 0 healthy / 1 degraded / 2 bad
        price   = _price_rank(model)
        quality = _quality_rank(name, model)
        if rating <= complexity:
            tier        = 0
            sort_within = complexity - rating   # 0 = perfect match, larger = overkill
        else:
            tier        = 1
            sort_within = rating - complexity   # too weak — closest first
        # local_first leads the key so a preferred local model sorts ahead of all
        # others on easy turns; it's a constant 1 otherwise, leaving order unchanged.
        # intent_fit sits AFTER tier for the same reason tier leads: a capability
        # hint may reorder within the capable tier, but must never promote a
        # too-weak model over one that actually fits the request's complexity.
        # list_index trails so a provider's listed model order breaks rating ties.
        return (local_first, tier, _workload_fit(workload_hint, p, model), price, quality,
                sort_within, breaker_open, health, 0 if avail else 1, fast,
                cand["list_index"])

    n = len(providers)
    offset = next(_rr_counter) % n if n else 0
    rotated = providers[offset:] + providers[:offset]
    candidates = [{"provider": p, "model": m, "list_index": i}
                  for p in rotated
                  for i, m in enumerate(_provider_models(p)) if _model_enabled(p["name"], m)]
    return sorted(candidates, key=_key)


def _env_flag(name: str, suffix: str, model: str):
    """Read a capability override env var, preferring the per-model form
    <PROVIDER>_<MODEL>_<SUFFIX> over the provider-wide <PROVIDER>_<SUFFIX>.
    Returns True/False if set, else None (= not overridden → probe)."""
    val = os.environ.get(f"{name.upper()}_{_model_env_suffix(model)}_{suffix}")
    if val is None:
        val = os.environ.get(f"{name.upper()}_{suffix}")
    if val is None:
        return None
    return val.strip().lower() not in ("0", "false", "no", "")


def _resolve_caps(p: dict, key: str, model: str, ok: bool) -> dict:
    """Capability for one (provider, model): rating (free, pattern-based) plus
    tool/reasoning support. An env override (per-model first, then provider-wide)
    wins; otherwise probe the model when the provider is reachable.

    _probe_tools returns None when the probe itself was inconclusive (network
    error / non-200, often a free-tier RPM cap already hit by earlier probes in
    the same startup pass). Normal routing keeps its optimistic fallback; the
    agent profile separately requires tools_confirmed.
    """
    name = p["name"]
    et = _env_flag(name, "SUPPORTS_TOOLS", model)
    if et is not None:
        supports_tools = et
        tools_confirmed = True
    elif not ok:
        supports_tools = False   # provider unreachable at boot — genuinely unusable
        tools_confirmed = False
    else:
        probed = _probe_tools(p, key, model)
        supports_tools = True if probed is None else probed
        tools_confirmed = probed is True
    er = _env_flag(name, "REASONING", model)
    reasoning = er if er is not None else (_probe_reasoning(p, key, model) if ok else False)
    return {
        "rating": _rate_model(model),
        "supports_tools": supports_tools,
        "tools_confirmed": tools_confirmed,
        "reasoning": reasoning,
        **_DISCOVERED_CONTEXT_LIMITS.get((name, model), {}),
    }


def _initialize_ratings(providers: list, pool_ref):
    """Background: probe all providers, fix bad models, assign ratings, persist state."""
    global _provider_state, _model_state
    if STATE_FILE.exists():
        try:
            cached_doc = json.loads(STATE_FILE.read_text())
            _provider_state = cached_doc.get("providers", {})
            # Per-model caps were persisted as "name::model" keys — restore tuples.
            _model_state = {}
            for k, v in (cached_doc.get("model_state") or {}).items():
                n, _, m = k.partition("::")
                if m:
                    _model_state[(n, m)] = v
            log.info(f"[ratings] Loaded cached state ({len(_provider_state)} providers, "
                     f"{len(_model_state)} models)")
            # Probes cost a real completion per model, so skip them while the state
            # is fresh AND still covers every configured provider and model.
            age = time.time() - cached_doc.get("last_updated_ts", 0)
            models_covered = all(
                _cached_model_caps_compatible(_model_state.get((p["name"], m)))
                for p in providers for m in _provider_models(p)
            )
            discovery_requested = any(_provider_model_discovery_enabled(p) for p in providers)
            if (not discovery_requested
                    and STATE_TTL_HOURS > 0 and age < STATE_TTL_HOURS * 3600
                    and all(p["name"] in _provider_state for p in providers)
                    and models_covered):
                for p in providers:
                    cached_model = _provider_state[p["name"]].get("model")
                    if cached_model and cached_model != p["model"]:
                        old = p["model"]
                        p["model"] = cached_model
                        if p.get("models"):
                            p["models"][0] = cached_model
                        pool_ref.rename_model(p["name"], old, cached_model)
                log.info(f"[ratings] State is {age/3600:.1f}h old (< {STATE_TTL_HOURS}h TTL) "
                         "— skipping startup probes")
                return
        except Exception:
            pass

    log.info("[ratings] Background provider validation starting…")
    new_state = {}
    new_model_state = {}
    cached_models = {
        key: caps for key, caps in _model_state.items() if _cached_model_caps_compatible(caps)
    }
    for p in providers:
        name  = p["name"]
        key   = pool_ref.first_key(name)
        # Automatic setup deliberately persists an empty manual roster. On a
        # restart there are no model buckets yet, but catalog discovery still
        # needs the provider's saved key to rebuild those buckets.
        if not key and _provider_model_discovery_enabled(p) and p.get("keys"):
            key = p["keys"][0]
        if not key:
            new_state[name] = {"rating": _rate_model(p["model"]), "model": p["model"],
                                "available": False, "latency_ms": 0, "overridden": False}
            for m in _provider_models(p):
                new_model_state[(name, m)] = {"rating": _rate_model(m),
                                              "supports_tools": False, "tools_confirmed": False,
                                              "reasoning": False}
            continue
        _refresh_discovered_models(p, key, pool_ref)
        if not _provider_models(p):
            new_state[name] = {"rating": _rate_model(p.get("model") or ""),
                                "model": p.get("model") or "",
                                "available": False, "latency_ms": 0, "overridden": False}
            log.info(f"[ratings]   {name}: skipped — no usable models")
            continue
        if p["model"] in p.get("_discovered_metadata", {}):
            ok, latency, actual, probe_status = True, 0, p["model"], "catalog"
        else:
            ok, latency, actual, probe_status = _probe_provider(p, key)
        # A primary model can be rate-limited, missing tools, or otherwise rejected
        # while the provider/key is still usable for other configured models.
        # Only auth/network failures confidently make every model unusable.
        caps_probe_ok = ok or probe_status not in ("auth", "network")
        original   = p["model"]
        overridden = actual != original
        if overridden:
            log.info(f"[ratings]   {name}: model fixed {original} → {actual}")
            p["model"] = actual
            if p.get("models"):
                p["models"][0] = actual
                p["models"] = list(dict.fromkeys(p["models"]))
            pool_ref.rename_model(name, original, actual)
        # Per-model capabilities for the whole list (primary = models[0] = actual).
        # Reuse a cached entry when present so adding one model doesn't re-probe all.
        for m in _provider_models(p) or ([actual] if actual else []):
            caps = (cached_models.get((name, m)) or p.get("_discovered_metadata", {}).get(m)
                    or _resolve_caps(p, key, m, caps_probe_ok))
            new_model_state[(name, m)] = caps
            log.info(f"[ratings]   {name}/{m}: rating={caps['rating']} "
                     f"tools={'yes' if caps['supports_tools'] else 'no'} "
                     f"reasoning={'yes' if caps['reasoning'] else 'no'}")
        # Provider-level fields mirror the primary model's caps (back-compat).
        prim = new_model_state[(name, actual)]
        available = ok or probe_status in ("rate_limited", "http", "timeout", "disabled")
        log.info(f"[ratings]   {name}: {'✓' if available else '✗'} model={actual} {latency:.0f}ms "
                 f"status={probe_status}")
        new_state[name] = {"rating": prim["rating"], "model": actual, "available": available,
                            "latency_ms": round(latency, 1), "overridden": overridden,
                            "original_model": original, "supports_tools": prim["supports_tools"],
                            "tools_confirmed": prim.get("tools_confirmed", False),
                            "reasoning": prim["reasoning"], "probe_status": probe_status}
    _provider_state = new_state
    _model_state = new_model_state
    try:
        STATE_FILE.write_text(json.dumps({"last_updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                           "last_updated_ts": time.time(),
                                           "providers": new_state,
                                           "model_state": {f"{n}::{m}": v
                                                           for (n, m), v in new_model_state.items()}},
                                          indent=2))
        log.info("[ratings] State persisted to disk")
    except Exception as e:
        log.warning(f"[ratings] Could not persist state: {e}")


def _initialize_providers(providers, pool_ref):
    """One background worker owns capability discovery and its global snapshots."""
    global _INITIALIZATION_STATE
    _INITIALIZATION_STATE = {"state": "initializing"}
    try:
        _initialize_ratings(providers, pool_ref)
    except Exception:
        _INITIALIZATION_STATE = {"state": "error"}
        log.exception("Provider capability initialization failed")
    else:
        _INITIALIZATION_STATE = {"state": "ready"}


def _readiness():
    state = _INITIALIZATION_STATE["state"]
    available = sum(bool(_provider_state.get(p["name"], {}).get("available"))
                    and bool(_enabled_provider_models(p)) for p in PROVIDERS)
    return {"state": state if state != "ready" or available else "unavailable",
            "configured_providers": len(PROVIDERS), "available_providers": available}


class CredentialPool:
    """Thread-safe key pool with per-key cooldown tracking.

    Two selection modes (set once via ROTATION_MODE):
      round-robin — advance every call so load spreads evenly across keys
      sequential  — keep returning the same key until it cools, then advance;
                    this drains one account at a time and keeps the rest fresh"""

    def __init__(self, providers: list[dict], mode: str = None):
        self.lock  = threading.Lock()
        self.mode  = mode or ROTATION_MODE
        # provider -> { model -> deque({key, cool_until}) }. Each model gets its own
        # key deque so rate-limit cooldowns are tracked per (key, model): a 429 on
        # one model never sidelines the provider's other models (separate quotas).
        self.pools: dict[str, dict[str, deque]] = {}
        # (provider, key) -> total times this key has been handed out, across all of
        # the provider's models. Lets /v1/status show whether load is actually
        # spreading across configured keys, not just that rotation "should" work.
        self.key_requests: dict = defaultdict(int)
        for p in providers:
            models = _provider_models(p)
            if p.get("embed_model"):
                models = list(models) + [p["embed_model"]]   # embeddings get their own bucket
            self.pools[p["name"]] = {
                m: deque({"key": k, "cool_until": 0.0} for k in p["keys"])
                for m in dict.fromkeys(models)     # de-dupe, preserve order
            }
            log.info(f"  {p['name']}: {len(p['keys'])} key(s) × {len(self.pools[p['name']])} model(s) loaded")

    def get_key(self, provider_name: str, model: str) -> str | None:
        """Return a ready key for (provider, model) per the active mode, or None."""
        with self.lock:
            pool = self.pools.get(provider_name, {}).get(model, deque())
            now  = time.time()
            if self.mode == "sequential":
                # Stay on the current key until it cools; only advance past cooling ones.
                for _ in range(len(pool)):
                    entry = pool[0]
                    if entry["cool_until"] <= now:
                        self.key_requests[(provider_name, entry["key"])] += 1
                        return entry["key"]      # do NOT rotate — keep draining this key
                    pool.rotate(-1)
                return None
            # round-robin (default): advance every call so load spreads evenly
            for _ in range(len(pool)):
                entry = pool[0]
                pool.rotate(-1)
                if entry["cool_until"] <= now:
                    self.key_requests[(provider_name, entry["key"])] += 1
                    return entry["key"]
            return None

    def key_requests_for(self, provider_name: str, key: str) -> int:
        """Total times this (provider, key) has been handed out via get_key()."""
        return self.key_requests.get((provider_name, key), 0)

    def key_count(self, provider_name: str, model: str) -> int:
        """How many keys exist for (provider, model) — used to bound retry attempts."""
        return len(self.pools.get(provider_name, {}).get(model, ()))

    def first_key(self, provider_name: str) -> str | None:
        """Any key for a provider (from its primary model's deque) — used for probing."""
        for entries in self.pools.get(provider_name, {}).values():
            if entries:
                return entries[0]["key"]
        return None

    def mark_rate_limited(self, provider_name: str, key: str, model: str, retry_after: int = 60):
        """Cool a specific (key, model) — leaves the provider's other models ready."""
        with self.lock:
            for entry in self.pools.get(provider_name, {}).get(model, ()):
                if entry["key"] == key:
                    entry["cool_until"] = time.time() + retry_after
                    log.warning(f"  {provider_name} key ...{key[-6:]} model {model} cooling for {retry_after}s")
                    return

    def mark_key_down(self, provider_name: str, key: str, retry_after: int = 30):
        """Cool a key across ALL of the provider's models — for network/5xx (key/
        provider-health) failures, which aren't specific to one model."""
        with self.lock:
            now = time.time()
            for entries in self.pools.get(provider_name, {}).values():
                for entry in entries:
                    if entry["key"] == key:
                        entry["cool_until"] = now + retry_after
            log.warning(f"  {provider_name} key ...{key[-6:]} cooling {retry_after}s (all models)")

    def rename_model(self, provider_name: str, old: str, new: str):
        """Re-key a model's deque — used when the startup probe auto-discovers a
        replacement for a deprecated/invalid primary model name, so the pool's
        per-model bucket keeps matching the provider's model list."""
        with self.lock:
            prov = self.pools.get(provider_name)
            if prov and old in prov and old != new:
                prov[new] = prov.pop(old)

    def ensure_model(self, provider_name: str, model: str, keys: list[str]):
        """Ensure the pool has a bucket for a newly discovered model."""
        with self.lock:
            prov = self.pools.setdefault(provider_name, {})
            if model not in prov:
                prov[model] = deque({"key": k, "cool_until": 0.0} for k in keys)
            else:
                existing = {entry["key"] for entry in prov[model]}
                prov[model].extend({"key": k, "cool_until": 0.0} for k in keys if k not in existing)


pool = CredentialPool(PROVIDERS)

# Background: validate providers, fix models, assign ratings
threading.Thread(target=_initialize_providers, args=(PROVIDERS, pool), daemon=True).start()

# ── Per-provider stats ─────────────────────────────────────────────────────────

class ProviderStats:
    """Tracks latency and error rates per provider for observability."""

    def __init__(self):
        self.lock   = threading.Lock()
        self._data: dict[str, dict] = {}

    def _ensure(self, name: str):
        if name not in self._data:
            self._data[name] = {"latency_sum": 0.0, "latency_count": 0,
                                "error_count": 0, "request_count": 0,
                                "health": deque(maxlen=BREAKER_WINDOW), "open_until": 0.0}

    def record_success(self, name: str, latency_s: float):
        with self.lock:
            self._ensure(name)
            s = self._data[name]
            s["latency_sum"]   += latency_s
            s["latency_count"] += 1
            s["request_count"] += 1

    def record_error(self, name: str):
        with self.lock:
            self._ensure(name)
            s = self._data[name]
            s["error_count"]   += 1
            s["request_count"] += 1

    # ── Circuit breaker ──────────────────────────────────────────────────────
    def record_health(self, name: str, ok: bool):
        """Record a HEALTH outcome (separate from request stats — breaker only).
        On failure: trip the breaker open once the window has enough samples and
        the health-fail fraction crosses the threshold. On success: half-open
        recovery — close the breaker and wipe the window for a clean slate."""
        with self.lock:
            self._ensure(name)
            s   = self._data[name]
            win = s["health"]
            win.append(ok)
            if ok:
                s["open_until"] = 0.0
                win.clear()
            elif len(win) >= BREAKER_MIN_SAMPLES:
                fails = sum(1 for x in win if not x)
                if fails / len(win) >= BREAKER_ERROR_RATE:
                    s["open_until"] = time.time() + BREAKER_COOLDOWN

    def breaker_open(self, name: str) -> bool:
        with self.lock:
            s = self._data.get(name)
            return bool(s) and time.time() < s.get("open_until", 0.0)

    def breaker_status(self, name: str) -> dict:
        with self.lock:
            s   = self._data.get(name, {})
            now = time.time()
            open_until = s.get("open_until", 0.0)
            win   = s.get("health", ())
            fails = sum(1 for x in win if not x)
            return {"open": now < open_until,
                    "opens_in_s": max(0, round(open_until - now)),
                    "recent_health_fails": fails}

    def health_bucket(self, name: str) -> int:
        """Recent error-rate bucket for routing: 0 healthy / 1 degraded / 2 bad.
        Too few samples → 0 (unknown = healthy; don't penalize new providers)."""
        with self.lock:
            s = self._data.get(name)
            if not s:
                return 0
            win = s.get("health", ())
            if len(win) < BREAKER_MIN_SAMPLES:
                return 0
            err_rate = sum(1 for x in win if not x) / len(win)
            return 0 if err_rate < 0.10 else (1 if err_rate < 0.50 else 2)

    def summary(self, name: str) -> dict:
        with self.lock:
            s  = self._data.get(name, {})
            lc = s.get("latency_count", 0)
            rc = s.get("request_count", 0)
            ec = s.get("error_count", 0)
            return {
                "avg_latency_ms": round(s.get("latency_sum", 0) / lc * 1000) if lc else None,
                "error_rate":     round(ec / rc, 3) if rc else 0.0,
                "total_requests": rc,
                "errors":         ec,
            }

    def all_summaries(self) -> dict:
        with self.lock:
            names = list(self._data)
        return {name: self.summary(name) for name in names}


stats = ProviderStats()

# ── Request ring buffer ─────────────────────────────────────────────────────────
# Per-thread context written by _route_completion so endpoint handlers can read
# back routing metadata (chosen provider, model, cascade count) after the call
# returns without changing _route_completion's return signature.
_req_ctx = threading.local()

# Session affinity is a bounded, in-memory routing hint only. A caller supplies
# an opaque session id; the router remembers the last successful provider/model
# and tries it first next time. Normal failover remains unchanged, and a
# successful fallback replaces the hint. Nothing here exposes execution tools.
_SESSION_AFFINITY_TTL = 3600
_SESSION_AFFINITY_MAX = 256
_session_affinity_lock = threading.Lock()
_session_affinity: OrderedDict[str, tuple[str, str, float]] = OrderedDict()


def _session_affinity_id(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value or len(value) > 200 or not re.fullmatch(r"[A-Za-z0-9._:-]+", value):
        return None
    return value


def _session_affinity_get(session_id: str | None) -> tuple[str, str] | None:
    if session_id is None:
        return None
    now = time.time()
    with _session_affinity_lock:
        entry = _session_affinity.get(session_id)
        if entry is None:
            return None
        provider, model, timestamp = entry
        if now - timestamp > _SESSION_AFFINITY_TTL:
            del _session_affinity[session_id]
            return None
        _session_affinity.move_to_end(session_id)
        return provider, model


def _session_affinity_order(ordered: list[dict], session_id: str | None) -> list[dict]:
    affinity = _session_affinity_get(session_id)
    if affinity is None:
        return ordered
    provider_name, model = affinity
    return sorted(
        ordered,
        key=lambda candidate: 0
        if candidate["provider"]["name"] == provider_name and candidate["model"] == model
        else 1,
    )


def _session_affinity_set(session_id: str | None, provider_name: str, model: str) -> None:
    if session_id is None:
        return
    with _session_affinity_lock:
        _session_affinity[session_id] = (provider_name, model, time.time())
        _session_affinity.move_to_end(session_id)
        while len(_session_affinity) > _SESSION_AFFINITY_MAX:
            _session_affinity.popitem(last=False)


class RequestRingBuffer:
    """Fixed-size in-memory circular log of recent requests.

    Oldest entries are silently dropped when full. Never touches disk.
    Thread-safe: all mutations hold a lock."""

    def __init__(self, maxlen: int = 500):
        self._buf   = deque(maxlen=max(1, maxlen)) if maxlen > 0 else None
        self._lock  = threading.Lock()
        self.maxlen = maxlen

    def append(self, entry: dict) -> None:
        if self._buf is None:
            return
        with self._lock:
            self._buf.append(entry)

    def snapshot(self, limit: int = 100, provider: str | None = None,
                 status: str | None = None, endpoint: str | None = None) -> list:
        if self._buf is None:
            return []
        with self._lock:
            items = list(self._buf)
        if provider:
            items = [e for e in items if e.get("provider") == provider]
        if status:
            items = [e for e in items if e.get("status") == status]
        if endpoint:
            items = [e for e in items if e.get("endpoint") == endpoint]
        items = list(reversed(items))   # most recent first
        return items[:limit]

    def clear(self) -> None:
        if self._buf is None:
            return
        with self._lock:
            self._buf.clear()

    @property
    def size(self) -> int:
        if self._buf is None:
            return 0
        with self._lock:
            return len(self._buf)

    @property
    def enabled(self) -> bool:
        return self._buf is not None


request_log = RequestRingBuffer(maxlen=REQUEST_LOG_SIZE)

# ── Response cache ─────────────────────────────────────────────────────────────

def _cosine(a: list, b: list) -> float:
    """Cosine similarity of two equal-length vectors. Pure Python (vectors are
    short and the cache is bounded, so this is plenty fast); numpy not required."""
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y; na += x * x; nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


class ResponseCache:
    """
    In-memory LRU cache for non-streaming responses.
    Identical requests (same model + messages) return a cached copy,
    saving free-tier quota for novel queries.
    Set CACHE_TTL_SECONDS=0 to disable.

    Optionally backed by a SQLite file (CACHE_PERSIST=1) that mirrors the
    in-memory LRU, so the cache survives restarts. The DB is a durable mirror —
    write-through on set, delete on eviction — so it stays bounded at max_size
    and the runtime data structure (and bounded semantic scan) are unchanged.
    All DB access is fail-soft: an error logs and degrades to in-memory only.
    """

    def __init__(self, ttl: int = 300, max_size: int = 100,
                 persist: bool = False, db_path: str = "./cache.db"):
        self.ttl      = ttl
        self.max_size = max_size
        self.lock     = threading.Lock()
        self._store: OrderedDict = OrderedDict()  # hash -> (data, ts, ns, embedding|None)
        self.hits          = 0
        self.misses        = 0
        self.semantic_hits = 0
        self._db = None
        if persist and ttl > 0:
            self._init_db(db_path)

    def _init_db(self, db_path: str):
        """Open the SQLite mirror, prune expired rows, and preload the most-recent
        fresh entries (≤ max_size) into memory so hits/semantic work after restart."""
        try:
            self._db = sqlite3.connect(db_path, check_same_thread=False)
            self._db.execute("CREATE TABLE IF NOT EXISTS cache "
                             "(hash TEXT PRIMARY KEY, data TEXT, ts REAL, ns TEXT, embedding TEXT)")
            cutoff = time.time() - self.ttl
            self._db.execute("DELETE FROM cache WHERE ts < ?", (cutoff,))
            self._db.commit()
            rows = self._db.execute(
                "SELECT hash, data, ts, ns, embedding FROM cache "
                "ORDER BY ts DESC LIMIT ?", (self.max_size,)).fetchall()
            for h, data, ts, ns, emb in reversed(rows):   # oldest-first → LRU order
                self._store[h] = (json.loads(data), ts, ns,
                                  json.loads(emb) if emb else None)
            log.info(f"Cache: persistent (SQLite {db_path}) — preloaded {len(rows)} entr"
                     f"{'y' if len(rows)==1 else 'ies'}")
        except Exception as e:
            log.warning(f"Cache: could not open persistent store {db_path}: {e} — in-memory only")
            self._db = None

    def _db_upsert(self, key, data, ts, ns, emb):
        if self._db is None:
            return
        try:
            self._db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?,?)",
                             (key, json.dumps(data, default=str), ts, ns,
                              json.dumps(emb) if emb is not None else None))
            self._db.commit()
        except Exception as e:
            log.warning(f"Cache: persist write failed: {e}")

    def _db_delete(self, key):
        if self._db is None:
            return
        try:
            self._db.execute("DELETE FROM cache WHERE hash = ?", (key,))
            self._db.commit()
        except Exception as e:
            log.warning(f"Cache: persist delete failed: {e}")

    def _hash(self, payload: dict, ns: str = "") -> str:
        # Hash the entire request (minus "stream", which doesn't change the
        # answer) so requests differing only in temperature, max_tokens,
        # tools, response_format, etc. never collide. `ns` namespaces the entry
        # to the authenticated caller, so two different API keys never share a
        # cached answer for an identical prompt (multi-tenant isolation).
        relevant = {k: v for k, v in payload.items() if k != "stream"}
        content = json.dumps({"ns": ns, "req": relevant}, sort_keys=True, default=str)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def get(self, payload: dict, ns: str = "") -> dict | None:
        if self.ttl <= 0:
            return None
        key = self._hash(payload, ns)
        with self.lock:
            if key in self._store:
                data, ts, *_ = self._store[key]
                if time.time() - ts < self.ttl:
                    self._store.move_to_end(key)
                    self.hits += 1
                    return data
                del self._store[key]
                self._db_delete(key)          # expired → drop from mirror too
            self.misses += 1
        return None

    def _semantic_signature(self, payload):
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages or messages[-1].get("role") != "user" or not isinstance(messages[-1].get("content"), str):
            return None
        compatible = {**payload, "messages": [*messages[:-1], {**messages[-1], "content": ""}]}
        return self._hash(compatible)

    def set(self, payload: dict, data: dict, ns: str = "", embedding=None):
        if self.ttl <= 0 or self.max_size <= 0:
            return
        if isinstance(embedding, dict):
            embedding = {**embedding, "signature": self._semantic_signature(payload)}
        key = self._hash(payload, ns)
        ts  = time.time()
        with self.lock:
            if key not in self._store and len(self._store) >= self.max_size:
                old, _ = self._store.popitem(last=False)  # evict oldest
                self._db_delete(old)
            self._store[key] = (data, ts, ns, embedding)
            self._store.move_to_end(key)
            self._db_upsert(key, data, ts, ns, embedding)

    def semantic_lookup(self, query_emb, ns: str = "", payload: dict | None = None) -> dict | None:
        """Return the cached response whose stored prompt embedding is most similar
        to query_emb (same namespace, same vector dimension), if it clears
        SEMANTIC_THRESHOLD. Bounded linear scan over the LRU (max_size)."""
        signature = self._semantic_signature(payload or {})
        if self.ttl <= 0 or not signature or not isinstance(query_emb, dict) or not query_emb.get("identity"):
            return None
        now = time.time()
        vector = query_emb.get("vector") or []
        qlen = len(vector)
        best_key, best_data, best_sim = None, None, 0.0
        with self.lock:
            for key, (data, ts, ens, emb) in self._store.items():
                # Legacy vectors lack provenance and stay exact-only after restart.
                if not isinstance(emb, dict) or ens != ns or emb.get("signature") != signature or emb.get("identity") != query_emb["identity"] or len(emb.get("vector", [])) != qlen:
                    continue
                if now - ts >= self.ttl:
                    continue
                sim = _cosine(vector, emb["vector"])
                if sim > best_sim:
                    best_key, best_data, best_sim = key, data, sim
            if best_key is not None and best_sim >= SEMANTIC_THRESHOLD:
                self._store.move_to_end(best_key)
                self.semantic_hits += 1
                log.info(f"  semantic match sim={best_sim:.3f}")
                return best_data
        return None

    @property
    def size(self) -> int:
        with self.lock:
            return len(self._store)

    @property
    def persistent(self) -> bool:
        return self._db is not None

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return round(self.hits / total, 3) if total else 0.0


cache = ResponseCache(ttl=CACHE_TTL, max_size=CACHE_MAX_SIZE,
                      persist=CACHE_PERSIST, db_path=CACHE_DB_PATH)

# ── Per-key budgets & rate limits ("virtual keys" lite) ─────────────────────────
# Each PROXY_API_KEYS entry can carry a requests-per-minute ceiling and per-UTC-day
# request/token budgets, which helps control usage when a team shares a router.
# Application keys cannot administer the router. Limits come
# from auth.json under "proxy_keys" ({ "<key>": {"rpm","req_per_day","tokens_per_day"} }),
# with env-var globals (PROXY_LIMIT_RPM / PROXY_LIMIT_REQ_DAY / PROXY_LIMIT_TOKENS_DAY)
# as defaults. 0/absent everywhere = unlimited → identical to the prior behavior.

def _load_key_limits() -> dict:
    g_rpm    = _int_env("PROXY_LIMIT_RPM", 0)
    g_req    = _int_env("PROXY_LIMIT_REQ_DAY", 0)
    g_tokens = _int_env("PROXY_LIMIT_TOKENS_DAY", 0)
    try:    g_cost = float(os.environ.get("PROXY_LIMIT_COST_DAY", 0) or 0)
    except (TypeError, ValueError): g_cost = 0.0
    per_key = {}
    if AUTH_FILE.exists():
        try:
            doc = json.loads(AUTH_FILE.read_text())
            pk = doc.get("proxy_keys", {})
            if isinstance(pk, dict):
                per_key = pk
        except Exception as e:
            log.warning(f"Could not read proxy_keys from {AUTH_FILE}: {e}")
    limits = {}
    for k in PROXY_API_KEYS:
        spec = per_key.get(k) or {}
        limits[k] = {
            "rpm":            int(spec.get("rpm", g_rpm) or 0),
            "req_per_day":    int(spec.get("req_per_day", g_req) or 0),
            "tokens_per_day": int(spec.get("tokens_per_day", g_tokens) or 0),
            "cost_per_day":   float(spec.get("cost_per_day", g_cost) or 0),
        }
        if any(not math.isfinite(v) or v < 0 for v in limits[k].values()):
            raise ValueError("access-key limits must be finite and nonnegative")
    return limits

# Access-key edits are persisted separately from immutable process environment.
if AUTH_FILE.exists():
    try:
        _saved_keys = json.loads(AUTH_FILE.read_text()).get("access_keys")
        if isinstance(_saved_keys, list) and all(isinstance(k, str) and k for k in _saved_keys):
            if ADMIN_API_KEY in _saved_keys:
                raise ValueError("administrator credential cannot be an application key")
            PROXY_API_KEYS = _saved_keys
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("cannot safely read access-key state") from exc


KEY_LIMITS    = _load_key_limits()
KEY_LIMITS_ON = any(any(v.values()) for v in KEY_LIMITS.values())


def _load_key_provider_scope() -> dict:
    """Per-key provider allow-list (auth.json's proxy_keys[key].allowed_providers),
    set from the dashboard's Access Keys page. None = unrestricted (the default —
    backward compatible with every key that predates this feature); a set means
    that key's requests may only route through those providers."""
    per_key = {}
    if AUTH_FILE.exists():
        try:
            doc = json.loads(AUTH_FILE.read_text())
            pk = doc.get("proxy_keys", {})
            if isinstance(pk, dict):
                per_key = pk
        except Exception:
            pass
    scope = {}
    for k in PROXY_API_KEYS:
        allowed = (per_key.get(k) or {}).get("allowed_providers")
        scope[k] = set(allowed) if isinstance(allowed, list) and allowed else None
    return scope


KEY_PROVIDER_SCOPE = _load_key_provider_scope()


def _load_key_model_scope():
    meta = _read_proxy_keys_meta()
    return {key: set(meta[key]["allowed_models"]) if meta.get(key, {}).get("allowed_models") else None
            for key in PROXY_API_KEYS}


KEY_MODEL_SCOPE = _load_key_model_scope()


def _utc_day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())

def _secs_to_utc_midnight() -> int:
    t = time.gmtime()
    return max(1, 86400 - (t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec))


from usage_store import KeyUsage

key_usage = KeyUsage(os.environ.get("HR_USAGE_DB", "./usage.db"))

# Cumulative tokens + estimated cost served per provider (from provider-reported
# usage). Streaming responses that include a usage chunk are counted too; those
# without usage count toward request totals but not tokens/cost.
_provider_tokens = defaultdict(int)
_provider_cost   = defaultdict(float)
_ptok_lock = threading.Lock()

def _add_provider_tokens(name: str, data: dict, model: str | None = None):
    usage = data.get("usage") or {}
    n = usage.get("total_tokens") or 0
    # Cost uses the prompt/completion split (input and output are priced
    # differently). Prefer the model the provider actually reports serving
    # (authoritative for pricing); fall back to the routed model name.
    cost = _cost(data.get("model") or model or "",
                 usage.get("prompt_tokens"), usage.get("completion_tokens"))
    if n or cost:
        with _ptok_lock:
            if n:
                _provider_tokens[name] += n
            if cost:
                _provider_cost[name] += cost

# ── Thinking field stripping ───────────────────────────────────────────────────
# Some providers (e.g. Gemini 2.5) emit reasoning/thinking fields in responses.
# These fields cause 400 errors on other providers (Groq, Cerebras, OpenRouter).
# We strip them from both outgoing requests and incoming responses.
#
# One family is the exception: DeepSeek's thinking mode (and Moonshot's Kimi
# thinking models, which follow the same contract) REQUIRES the client to pass
# an earlier turn's `reasoning_content` back in every later request that
# carries `tools` — DeepSeek answers 400 ("The `reasoning_content` in the
# thinking mode must be passed back to the API.") when it is missing. For
# those models the field is not a foreign extra but part of the conversation
# the upstream itself demands back, so stripping it breaks every tool loop
# after the first tool call. It is preserved in both directions for them:
# responses keep it (so the client can echo it) and forwarded history keeps
# it when the destination model is in the family. A failover to any other
# provider still strips it, exactly as before.

_REASONING_ECHO_PATTERNS = ("deepseek", "kimi", "moonshot")


def _requires_reasoning_echo(model: str | None) -> bool:
    """Whether this model family's API requires `reasoning_content` to be
    passed back in later tool-carrying requests (DeepSeek / Kimi thinking)."""
    m = (model or "").lower()
    return any(p in m for p in _REASONING_ECHO_PATTERNS)


def _strip_message(msg: dict, keep_reasoning: bool = False):
    """Remove thinking fields from a message dict in-place. With
    `keep_reasoning`, `reasoning_content` survives (see above)."""
    if not keep_reasoning:
        msg.pop("reasoning_content", None)
    msg.pop("reasoning", None)
    msg.pop("think", None)
    if isinstance(msg.get("content"), list):
        msg["content"] = [
            b for b in msg["content"]
            if b.get("type") not in ("thinking", "think")
        ]


def _strip_response(data: dict, keep_reasoning: bool = False):
    """Strip thinking fields from a non-streaming response before returning it."""
    for choice in data.get("choices", []):
        if "message" in choice:
            _strip_message(choice["message"], keep_reasoning=keep_reasoning)


def _choice_has_output(choice: dict) -> bool:
    """True when a chat-completion choice contains user-visible output or a tool
    call. Empty assistant messages are treated as unusable so failover can try
    another provider instead of caching a blank answer."""
    msg = choice.get("message") or {}
    if msg.get("tool_calls"):
        return True
    content = msg.get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text") or part.get("content")
            if isinstance(text, str) and text.strip():
                return True
    return False


def _completion_has_output(data: dict) -> bool:
    choices = data.get("choices")
    return isinstance(choices, list) and any(_choice_has_output(c) for c in choices)


def _streaming_generator(resp: requests.Response, keep_reasoning: bool = False):
    """
    Yield SSE chunks with thinking fields stripped from delta objects.
    Buffers by newline to handle chunks that split across SSE boundaries.
    With `keep_reasoning`, `reasoning_content` deltas survive so the client
    can pass them back on the next turn (see _requires_reasoning_echo).
    """
    buf = b""
    for raw_chunk in resp.iter_content(chunk_size=None):
        buf += raw_chunk
        while b"\n" in buf:
            line_bytes, buf = buf.split(b"\n", 1)
            line = line_bytes.decode("utf-8", errors="replace")
            if line.startswith("data: ") and line != "data: [DONE]":
                try:
                    event = json.loads(line[6:])
                    for choice in event.get("choices", []):
                        delta = choice.get("delta", {})
                        if not keep_reasoning:
                            delta.pop("reasoning_content", None)
                        delta.pop("reasoning", None)
                        delta.pop("think", None)
                    yield ("data: " + json.dumps(event) + "\n").encode("utf-8")
                    continue
                except (json.JSONDecodeError, Exception):
                    pass
            yield (line + "\n").encode("utf-8")
    if buf:
        yield buf


class _ManagedStream:
    """Own cleanup even when a response is closed before its first iteration."""
    def __init__(self, gen, cleanup):
        self.gen, self.cleanup, self.closed = iter(gen), cleanup, False

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed:
            raise StopIteration
        try:
            return next(self.gen)
        except BaseException:
            self.close()
            raise

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                close = getattr(self.gen, "close", None)
                if close:
                    close()
            finally:
                self.cleanup()


def _with_cleanup(resp: requests.Response, gen):
    return _ManagedStream(gen, resp.close)


def _streaming_with_usage(gen, name: str, model: str | None = None):
    from flask import g
    try:
        reservation = getattr(g, "usage_reservation", None)
    except RuntimeError:
        reservation = None
    usage, has_output, completed = {}, False, False
    started = getattr(_req_ctx, "started", time.monotonic())
    deadline = getattr(_req_ctx, "deadline", started + REQUEST_TIMEOUT)

    def iterate():
        nonlocal usage, has_output, completed
        buf = ""
        stream_error = False
        for chunk in gen:
            if time.monotonic() >= deadline:
                raise requests.Timeout("Router request deadline exceeded")
            buf += chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                    if "error" in event:
                        stream_error = True
                    if isinstance(event.get("usage"), dict):
                        usage = event["usage"]
                    has_output = has_output or any(
                        bool((choice.get("delta") or {}).get("content") or (choice.get("delta") or {}).get("tool_calls"))
                        for choice in event.get("choices", []))
                except (ValueError, TypeError):
                    pass
            yield chunk
        # Adapters may report an upstream failure as SSE and finish normally.
        # Exhausting their generator is not evidence of a successful response.
        completed = not stream_error

    def finalize():
        try:
            gen.close()
        finally:
            if usage:
                _add_provider_tokens(name, {"usage": usage}, model)
            if completed and has_output:
                stats.record_success(name, time.monotonic() - started)
                stats.record_health(name, True)
            else:
                stats.record_error(name)
                if not completed:
                    stats.record_health(name, False)
            if reservation is not None:
                _settle_request_usage(reservation, usage or None, model, cached=False,
                                      failed=not completed and not has_output and not usage)
    return _ManagedStream(iterate(), finalize)

# ── Anthropic format translation ──────────────────────────────────────────────
# Anthropic's Messages API uses a different format from OpenAI. These helpers
# translate transparently so the caller never has to know which provider they hit.

def _openai_content_to_anthropic(content) -> list | str:
    """Convert an OpenAI content value (string or list) to Anthropic format.

    OpenAI image_url blocks become Anthropic image blocks (base64 or url source).
    Text blocks are preserved. Thinking/reasoning blocks are dropped (already
    stripped by _strip_message, but safe to double-check here).
    Returns a list when images are present, plain string otherwise.
    """
    if not isinstance(content, list):
        return content or ""
    converted = []
    for block in content:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t in ("thinking", "think"):
            continue
        if t == "text":
            converted.append({"type": "text", "text": block.get("text", "")})
        elif t == "image_url":
            url = (block.get("image_url") or {}).get("url", "")
            if url.startswith("data:"):
                # data:image/jpeg;base64,<data>
                try:
                    header, data = url.split(",", 1)
                    media_type = header.split(";")[0][5:]  # strip "data:"
                    converted.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": data},
                    })
                except Exception:
                    pass  # malformed data URL — skip
            elif url.startswith(("http://", "https://")):
                converted.append({
                    "type": "image",
                    "source": {"type": "url", "url": url},
                })
        # unknown types are silently dropped
    if not converted:
        return ""
    # If it's purely text with no images, return a plain string for cleanliness
    if all(b.get("type") == "text" for b in converted):
        return " ".join(b.get("text", "") for b in converted)
    return converted


def _merge_anthropic_content(existing, incoming) -> list | str:
    """Merge two content values when combining consecutive same-role messages.
    Produces a list when either side contains images, plain string otherwise."""
    def _to_list(c) -> list:
        if isinstance(c, list):
            return c
        return [{"type": "text", "text": c}] if c else []

    if isinstance(existing, list) or isinstance(incoming, list):
        merged = _to_list(existing) + _to_list(incoming)
        # Collapse back to string if no images remain
        if all(b.get("type") == "text" for b in merged):
            return " ".join(b.get("text", "") for b in merged)
        return merged
    # Both plain strings
    return (existing + "\n" + incoming) if existing else incoming


def _to_anthropic_body(payload: dict, model: str) -> dict:
    """Convert an OpenAI chat-completions request body to Anthropic Messages format.
    Image content (image_url blocks) is translated to Anthropic image blocks so
    vision requests work correctly when routed to Claude.
    """
    system_parts = []
    messages = []
    for msg in payload.get("messages", []):
        role = msg.get("role", "")
        content = _openai_content_to_anthropic(msg.get("content", ""))
        if role in ("system", "developer"):
            # System content is always plain text in Anthropic's API
            text = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text") if isinstance(content, list) else content
            system_parts.append(text)
        else:
            if role == "tool":
                if not msg.get("tool_call_id"):
                    raise ValueError("Tool results require tool_call_id")
                role = "user"
                content = [{"type": "tool_result", "tool_use_id": msg["tool_call_id"],
                            "content": content}]
            elif role == "assistant" and msg.get("tool_calls"):
                if not isinstance(msg["tool_calls"], list):
                    raise ValueError("tool_calls must be an array")
                content = list(content) if isinstance(content, list) else (
                    [{"type": "text", "text": content}] if content else [])
                for call in msg["tool_calls"]:
                    if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                        raise ValueError("Tool calls require a function object")
                    fn = call.get("function") or {}
                    if call.get("type", "function") != "function" or not call.get("id") or not fn.get("name"):
                        raise ValueError("Tool calls require a function name and id")
                    args = fn.get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except (ValueError, TypeError):
                            raise ValueError("Tool arguments must be a JSON object") from None
                    if not isinstance(args, dict):
                        raise ValueError("Tool arguments must be a JSON object")
                    content.append({"type": "tool_use", "id": call["id"],
                                    "name": fn["name"], "input": args})
            # Merge consecutive same-role messages (Anthropic requires alternating roles)
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] = _merge_anthropic_content(
                    messages[-1]["content"], content
                )
            else:
                messages.append({"role": role, "content": content})

    body: dict = {
        "model":      model,
        "messages":   messages,
        "max_tokens": payload.get("max_completion_tokens") or payload.get("max_tokens") or 1024,
    }
    if system_parts:
        system_text = "\n".join(system_parts)
        # Anthropic prompt caching: mark system prompt for caching when it's long
        # enough to qualify (≥ 1024 tokens; estimated as ≥ 4096 chars). Cached
        # tokens are billed at 10% on subsequent requests — transparent to the caller.
        if len(system_text) >= 4096:
            body["system"] = [{"type": "text", "text": system_text,
                                "cache_control": {"type": "ephemeral"}}]
        else:
            body["system"] = system_text
    if payload.get("stream"):
        body["stream"] = True
    if payload.get("temperature") is not None:
        body["temperature"] = payload["temperature"]
    if payload.get("tools"):
        if not isinstance(payload["tools"], list):
            raise ValueError("tools must be an array")
        tools = []
        for tool in payload["tools"]:
            if not isinstance(tool, dict) or not isinstance(tool.get("function"), dict):
                raise ValueError("Tools require a function object")
            fn = tool.get("function") or {}
            if tool.get("type") != "function" or not fn.get("name"):
                raise ValueError("Anthropic requires function tool definitions")
            native = {"name": fn["name"], "input_schema": fn.get("parameters") or {
                "type": "object", "properties": {}}}
            if not isinstance(native["input_schema"], dict):
                raise ValueError("Tool parameters must be a JSON schema object")
            if fn.get("description") is not None:
                native["description"] = fn["description"]
            if "strict" in fn:
                native["strict"] = fn["strict"]
            tools.append(native)
        body["tools"] = tools
        choice = payload.get("tool_choice", "auto")
        if choice in (None, "auto", "none", "required"):
            native_choice = {"type": "any" if choice == "required" else (choice or "auto")}
        elif isinstance(choice, dict) and choice.get("type") == "function" and isinstance(choice.get("function"), dict) and choice["function"].get("name"):
            native_choice = {"type": "tool", "name": choice["function"]["name"]}
        else:
            raise ValueError("Unsupported tool_choice")
        if payload.get("parallel_tool_calls") is False and native_choice["type"] != "none":
            native_choice["disable_parallel_tool_use"] = True
        body["tool_choice"] = native_choice
    stop = payload.get("stop")
    if stop:
        body["stop_sequences"] = stop if isinstance(stop, list) else [stop]
    return body


def _from_anthropic_response(data: dict) -> dict:
    """Convert an Anthropic Messages response to OpenAI chat-completion format."""
    content = "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    )
    stop_reason = data.get("stop_reason", "end_turn")
    finish_reason = _anthropic_finish_reason(stop_reason)
    message = {"role": "assistant", "content": content}
    calls = [{"id": b["id"], "type": "function",
              "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}))}}
             for b in data.get("content", []) if b.get("type") == "tool_use"]
    if calls:
        message["tool_calls"] = calls
        message["content"] = content or None
    usage = data.get("usage", {})
    prompt_tokens = usage.get("input_tokens", 0)
    completion_tokens = usage.get("output_tokens", 0)
    out: dict = {
        "id":      data.get("id", "msg_unknown"),
        "object":  "chat.completion",
        "created": int(time.time()),
        "model":   data.get("model", ""),
        "choices": [{
            "index":         0,
            "message":       message,
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens":     prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens":      prompt_tokens + completion_tokens,
        },
    }
    # Pass through Anthropic cache token counts when present so callers can
    # observe cache savings without breaking OpenAI-compatible clients.
    if usage.get("cache_read_input_tokens"):
        out["usage"]["cache_read_input_tokens"] = usage["cache_read_input_tokens"]
    if usage.get("cache_creation_input_tokens"):
        out["usage"]["cache_creation_input_tokens"] = usage["cache_creation_input_tokens"]
    return out


def _anthropic_finish_reason(reason):
    return {"tool_use": "tool_calls", "max_tokens": "length",
            "refusal": "content_filter"}.get(reason, "stop")


def _anthropic_streaming_generator(resp: requests.Response):
    """Translate native text and structured tool events without interpreting text."""
    msg_id = f"chatcmpl-{int(time.time())}"
    model = ""
    created = int(time.time())
    finish_reason = "stop"
    usage = {}
    tools = {}  # Anthropic content index -> contiguous OpenAI tool index/state

    def chunk(delta, finish=None):
        return ("data: " + json.dumps({
            "id": msg_id, "object": "chat.completion.chunk", "created": created,
            "model": model, "choices": [{"index": 0, "delta": delta,
                                          "finish_reason": finish}]
        }) + "\n\n").encode()

    def lines():
        # Keep chunk_size=None for low-latency token delivery, and decode only
        # complete lines so a network split cannot corrupt UTF-8 or tool JSON.
        pending = b""
        for raw in resp.iter_content(chunk_size=None):
            pending += raw
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                yield line
        if pending:
            yield pending

    for raw in lines():
        line = raw.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:].strip())
        except (ValueError, TypeError):
            continue
        etype = event.get("type")
        index = event.get("index")
        if etype == "message_start":
            msg = event.get("message", {})
            msg_id = msg.get("id", msg_id)
            model = msg.get("model", "")
            usage.update(msg.get("usage") or {})
            yield chunk({"role": "assistant", "content": ""})
        elif etype == "content_block_start":
            block = event.get("content_block", {})
            if block.get("type") == "tool_use":
                tool_index = len(tools)
                tools[index] = {"index": tool_index, "input": block.get("input", {}),
                                "has_delta": False}
                yield chunk({"tool_calls": [{"index": tool_index, "id": block["id"],
                    "type": "function", "function": {"name": block["name"], "arguments": ""}}]})
            elif block.get("type") == "text" and block.get("text"):
                yield chunk({"content": block["text"]})
        elif etype == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta":
                yield chunk({"content": delta.get("text", "")})
            elif delta.get("type") == "input_json_delta" and index in tools:
                partial = delta.get("partial_json", "")
                if partial:
                    tools[index]["has_delta"] = True
                    yield chunk({"tool_calls": [{"index": tools[index]["index"],
                                                 "function": {"arguments": partial}}]})
        elif etype == "content_block_stop" and index in tools:
            tool = tools[index]
            if not tool["has_delta"]:
                # Zero-argument calls may have no deltas; some compatible
                # gateways instead put the complete object in the start event.
                yield chunk({"tool_calls": [{"index": tool["index"],
                    "function": {"arguments": json.dumps(tool["input"])}}]})
        elif etype == "message_delta":
            usage.update(event.get("usage") or {})
            reason = event.get("delta", {}).get("stop_reason")
            if reason:
                finish_reason = _anthropic_finish_reason(reason)
        elif etype == "error":
            yield b'data: {"error":{"type":"upstream_error","message":"Anthropic stream failed"}}\n\n'
            yield b"data: [DONE]\n\n"
            return
        elif etype == "message_stop":
            yield chunk({}, finish_reason)
            if usage:
                normalized = {"prompt_tokens": usage.get("input_tokens", 0),
                              "completion_tokens": usage.get("output_tokens", 0)}
                normalized["total_tokens"] = sum(normalized.values())
                for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
                    if usage.get(key):
                        normalized[key] = usage[key]
                yield ("data: " + json.dumps({"choices": [], "usage": normalized}) + "\n\n").encode()
            yield b"data: [DONE]\n\n"
            return
    yield b'data: {"error":{"type":"upstream_error","message":"Anthropic stream ended before completion"}}\n\n'
    yield b"data: [DONE]\n\n"


# ── Anthropic INBOUND translation (accept the Anthropic SDK's /v1/messages) ───
# The mirror image of the helpers above: these let a client using the Anthropic
# SDK talk to the router. An incoming Anthropic request is converted to OpenAI
# format, routed through the normal pipeline, and the response is converted back.

_OPENAI_TO_ANTHROPIC_STOP = {"stop": "end_turn", "length": "max_tokens",
                             "tool_calls": "tool_use", "content_filter": "end_turn"}


def _anthropic_request_to_openai(body: dict) -> dict:
    """Convert an Anthropic /v1/messages request into an OpenAI chat payload.
    The model is deliberately NOT preserved — the router picks a model per
    provider — so an Anthropic-SDK client transparently gets multi-provider
    failover instead of being pinned to whatever model string it sent.

    Tool use is mapped both ways: Anthropic `tools`/`tool_choice`, assistant
    `tool_use` content blocks, and user `tool_result` blocks become the OpenAI
    equivalents (function tools, message `tool_calls`, and `role:"tool"`
    messages)."""
    messages = []
    system = body.get("system")
    if isinstance(system, list):   # Anthropic allows system as a list of text blocks
        system = "\n".join(b.get("text", "") for b in system
                           if isinstance(b, dict) and b.get("type") == "text")
    if system:
        messages.append({"role": "system", "content": system})

    for m in body.get("messages", []):
        role    = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        # List content: text / image / tool_use (assistant calls) / tool_result (user returns).
        text_parts, image_parts, tool_calls, tool_msgs = [], [], [], []
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                text_parts.append(b.get("text", ""))
            elif bt == "image":
                # Anthropic image block → OpenAI image_url block (base64 or url source).
                # Without this, a vision request from the Anthropic SDK loses its image
                # silently — the model answers as if only the text part existed.
                src = b.get("source") or {}
                if src.get("type") == "base64" and src.get("data"):
                    media = src.get("media_type", "image/png")
                    image_parts.append({"type": "image_url",
                                        "image_url": {"url": f"data:{media};base64,{src['data']}"}})
                elif src.get("type") == "url" and src.get("url"):
                    image_parts.append({"type": "image_url", "image_url": {"url": src["url"]}})
            elif bt == "tool_use":
                tool_calls.append({"id": b.get("id"), "type": "function",
                                   "function": {"name": b.get("name", ""),
                                                "arguments": json.dumps(b.get("input", {}))}})
            elif bt == "tool_result":
                rc = b.get("content", "")
                if isinstance(rc, list):
                    rc = "".join(x.get("text", "") for x in rc
                                 if isinstance(x, dict) and x.get("type") == "text")
                tool_msgs.append({"role": "tool", "tool_call_id": b.get("tool_use_id"),
                                  "content": rc if isinstance(rc, str) else json.dumps(rc)})
        # OpenAI carries tool results as standalone role:"tool" messages, not nested.
        if tool_msgs:
            messages.extend(tool_msgs)
            if any(text_parts):
                messages.append({"role": role, "content": "".join(text_parts)})
        elif image_parts:
            # Images present → OpenAI's multimodal content shape: a list of text/image_url
            # blocks, not a plain string.
            parts = [{"type": "text", "text": t} for t in text_parts if t] + image_parts
            msg = {"role": role, "content": parts}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            messages.append(msg)
        else:
            msg = {"role": role, "content": "".join(text_parts) or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            messages.append(msg)

    payload: dict = {"model": ROUTER_MODEL, "messages": messages}
    if body.get("stream"):
        payload["stream"] = True
    for field in ("max_tokens", "temperature", "top_p"):
        if body.get(field) is not None:
            payload[field] = body[field]
    if body.get("stop_sequences"):
        payload["stop"] = body["stop_sequences"]
    if body.get("tools"):
        payload["tools"] = [{"type": "function", "function": {
            "name": t.get("name", ""), "description": t.get("description", ""),
            "parameters": t.get("input_schema", {})}}
            for t in body["tools"] if isinstance(t, dict) and t.get("name")]
    tc = body.get("tool_choice")
    if isinstance(tc, dict):
        ttype = tc.get("type")
        if ttype == "auto":
            payload["tool_choice"] = "auto"
        elif ttype == "any":
            payload["tool_choice"] = "required"
        elif ttype == "tool" and tc.get("name"):
            payload["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
    return payload


def _openai_response_to_anthropic(data: dict) -> dict:
    """Convert an OpenAI chat-completion response to Anthropic Messages format,
    including assistant tool calls (-> tool_use content blocks)."""
    choice  = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    finish  = choice.get("finish_reason") or "stop"
    usage   = data.get("usage") or {}

    blocks = []
    if message.get("content"):
        blocks.append({"type": "text", "text": message["content"]})
    tool_calls = message.get("tool_calls") or []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except Exception:
            args = {}
        blocks.append({"type": "tool_use", "id": tc.get("id"),
                       "name": fn.get("name"), "input": args})
    if not blocks:
        blocks = [{"type": "text", "text": ""}]

    return {
        "id":            data.get("id", "msg_unknown"),
        "type":          "message",
        "role":          "assistant",
        "model":         data.get("model", ROUTER_MODEL),
        "content":       blocks,
        "stop_reason":   "tool_use" if tool_calls else _OPENAI_TO_ANTHROPIC_STOP.get(finish, "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens":  usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _openai_stream_to_anthropic(gen):
    """Translate an OpenAI-format SSE stream (bytes, as yielded by the routing
    pipeline) into the Anthropic Messages SSE event sequence the Anthropic SDK
    expects: message_start → (content_block_start → content_block_delta* →
    content_block_stop)* → message_delta → message_stop.

    Handles both text deltas (text_delta) and streamed tool calls
    (tool_use blocks with input_json_delta). Anthropic allows only one content
    block open at a time, so we close the current block before opening the next
    and give each OpenAI tool-call index its own Anthropic block."""
    msg_id   = f"msg_{int(time.time())}"
    model    = ROUTER_MODEL
    finish   = "stop"
    started  = False           # message_start emitted?
    saw_tool = False
    next_index  = 0            # next Anthropic content-block index to allocate
    open_kind   = None         # None | "text" | "tool"
    open_index  = None         # Anthropic index of the currently open block
    tool_blocks = {}           # OpenAI tool-call index -> Anthropic block index

    def message_start():
        return _sse("message_start", {"type": "message_start", "message": {
            "id": msg_id, "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}}})

    buf = ""
    for chunk in gen:
        if isinstance(chunk, (bytes, bytearray)):
            chunk = chunk.decode("utf-8", errors="replace")
        buf += chunk
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                obj = json.loads(data)
            except Exception:
                continue
            model  = obj.get("model") or model
            choice = (obj.get("choices") or [{}])[0]
            delta  = choice.get("delta") or {}
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]

            if not started:
                started = True
                yield message_start()

            # ---- text delta ----
            piece = delta.get("content")
            if piece:
                if open_kind != "text":
                    if open_kind is not None:
                        yield _sse("content_block_stop", {"type": "content_block_stop", "index": open_index})
                    open_index, open_kind = next_index, "text"
                    next_index += 1
                    yield _sse("content_block_start", {"type": "content_block_start",
                        "index": open_index, "content_block": {"type": "text", "text": ""}})
                yield _sse("content_block_delta", {"type": "content_block_delta",
                    "index": open_index, "delta": {"type": "text_delta", "text": piece}})

            # ---- tool-call deltas ----
            for tc in (delta.get("tool_calls") or []):
                saw_tool = True
                oai_idx = tc.get("index", 0)
                fn = tc.get("function") or {}
                if oai_idx not in tool_blocks:            # first chunk for this tool call
                    if open_kind is not None:
                        yield _sse("content_block_stop", {"type": "content_block_stop", "index": open_index})
                    open_index, open_kind = next_index, "tool"
                    next_index += 1
                    tool_blocks[oai_idx] = open_index
                    yield _sse("content_block_start", {"type": "content_block_start",
                        "index": open_index, "content_block": {
                            "type": "tool_use", "id": tc.get("id") or f"toolu_{msg_id}_{oai_idx}",
                            "name": fn.get("name") or "", "input": {}}})
                if fn.get("arguments"):
                    yield _sse("content_block_delta", {"type": "content_block_delta",
                        "index": tool_blocks[oai_idx],
                        "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]}})

    if not started:
        yield message_start()
    if open_kind is None:        # no content at all — emit an empty text block
        open_index = 0
        yield _sse("content_block_start", {"type": "content_block_start",
            "index": 0, "content_block": {"type": "text", "text": ""}})
    yield _sse("content_block_stop", {"type": "content_block_stop", "index": open_index})
    stop_reason = "tool_use" if saw_tool else _OPENAI_TO_ANTHROPIC_STOP.get(finish, "end_turn")
    yield _sse("message_delta", {"type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": 0}})
    yield _sse("message_stop", {"type": "message_stop"})


def _anthropic_error(message: str) -> dict:
    """Anthropic-format error envelope."""
    return {"type": "error", "error": {"type": "api_error", "message": message}}

# ── Complexity-aware provider ordering ────────────────────────────────────────

# Accurate token counting via tiktoken when available. The encoder is loaded
# lazily on first use (not at import) so startup never blocks on tiktoken's
# one-time vocab download, and any failure (no tiktoken, offline, etc.) falls
# back to the character heuristic — the router always works regardless.
_ENCODER = "uninitialized"  # sentinel; resolves to an encoder or None on first use


def _get_encoder():
    global _ENCODER
    if _ENCODER == "uninitialized":
        try:
            import tiktoken
            _ENCODER = tiktoken.get_encoding("o200k_base")
        except Exception as e:
            log.warning(f"tiktoken unavailable ({e}); using char/4 token estimate")
            _ENCODER = None
    return _ENCODER


def _message_text(m: dict) -> str:
    """Extract plain text from a message whose content is either a string or a
    list of multimodal parts (only text parts contribute to the token count)."""
    content = m.get("content", "")
    if isinstance(content, list):
        return " ".join(
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    return str(content)


def _estimated_tokens(messages: list) -> int:
    """Token count for a message list. Uses tiktoken for an accurate count when
    available, otherwise a characters/4 heuristic. Adds a small per-message
    framing overhead (~4 tokens) plus 3 priming tokens, matching how chat
    models actually bill structured messages."""
    enc = _get_encoder()
    if enc is not None:
        total = 3
        for m in messages:
            total += 4 + len(enc.encode(_message_text(m)))
        return total
    return sum(len(_message_text(m)) for m in messages) // 4


def _ordered_providers(payload: dict, prefer_local: bool = False,
                       workload_hint: str | None = None) -> list[dict]:
    """
    Smart complexity-aware ordering: use cheapest capable model for simple
    tasks, best model for complex ones. With FAST_ROUTE_THRESHOLD set,
    short requests break ties in favour of low-latency providers. With
    prefer_local (the `:fast` profile), a local model leads on easy turns.
    With a workload hint, matching capability metadata breaks ties within tier.
    """
    messages   = payload.get("messages", [])
    # A `vision` hint only means anything when the request genuinely carries an
    # image. Dropping it here keeps a text-only request text-only: the hint alone
    # must never make routing (or `enforce_vision` below) treat it as multimodal.
    if workload_hint == "vision" and not _payload_has_image(payload):
        workload_hint = None
    complexity = classify_complexity(messages)
    ordered    = _get_smart_ordered(PROVIDERS, complexity, _estimated_tokens(messages),
                                    prefer_local, workload_hint)
    log.info(f"→ complexity={complexity} ({_COMPLEXITY_LABELS[complexity]}) "
             f"order={[c['provider']['name'] + '/' + c['model'] for c in ordered]}")
    return ordered

# ── Codex (Responses API) format translation ──────────────────────────────────
# Codex speaks OpenAI's Responses API (not Chat Completions). These helpers
# translate transparently, like the Anthropic ones above.

def _to_codex_body(payload: dict, model: str) -> dict:
    """Convert an OpenAI chat-completions body to a Codex Responses-API request."""
    instructions = []
    input_items = []
    for msg in payload.get("messages", []):
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):       # flatten structured content to text
            content = "".join(p.get("text", "") for p in content
                              if isinstance(p, dict) and p.get("type") in ("text", "input_text", "output_text"))
        content = content or ""
        if role == "system":
            instructions.append(content)
            continue
        # Tool results: the Responses API has no "tool" role — they become
        # function_call_output items linked to the call by call_id.
        if role == "tool":
            input_items.append({"type": "function_call_output",
                                "call_id": msg.get("tool_call_id") or "",
                                "output": content})
            continue
        if role == "assistant":
            # Keep any assistant text, then emit each tool call as its own
            # function_call item (an assistant turn with only tool_calls has no text).
            if content:
                input_items.append({"type": "message", "role": "assistant",
                                    "content": [{"type": "output_text", "text": content}]})
            for i, tc in enumerate(msg.get("tool_calls") or []):
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                if not isinstance(args, str):
                    args = json.dumps(args if args is not None else {})
                input_items.append({"type": "function_call",
                                    "call_id": tc.get("id") or f"call_{i}",
                                    "name": fn.get("name", ""),
                                    "arguments": args or "{}"})
            continue
        input_items.append({"type": "message", "role": role,
                            "content": [{"type": "input_text", "text": content}]})

    body: dict = {
        "model":        model,
        "input":        input_items,
        "store":        False,
        "stream":       True,        # Codex backend always streams (SSE)
        # Codex requires a non-empty `instructions`; use the client's system
        # message(s) or a minimal default so requests without one still work.
        "instructions": "\n".join(instructions) if instructions
                        else "You are a helpful assistant.",
    }

    # tools: OpenAI nests under {"function": {...}}; Responses wants them flat.
    tools = []
    for t in payload.get("tools", []) or []:
        if t.get("type") == "function" and isinstance(t.get("function"), dict):
            fn = t["function"]
            tools.append({"type": "function", "name": fn.get("name", ""),
                          "description": fn.get("description", ""),
                          "strict": False, "parameters": fn.get("parameters", {})})
    if tools:
        body["tools"] = tools
        tc = payload.get("tool_choice")
        body["tool_choice"] = tc if isinstance(tc, str) else "auto"
        body["parallel_tool_calls"] = bool(payload.get("parallel_tool_calls", True))

    # reasoning effort (OpenAI clients pass reasoning_effort; default medium)
    effort = payload.get("reasoning_effort") or "medium"
    body["reasoning"] = {"effort": effort}
    body["include"] = ["reasoning.encrypted_content"]
    return body


def _codex_text_and_tools(data: dict):
    """Pull assistant text and any tool calls out of a Responses `output` array."""
    text_parts, tool_calls = [], []
    for item in data.get("output", []) or []:
        itype = item.get("type")
        if itype == "message":
            for c in item.get("content", []) or []:
                if c.get("type") in ("output_text", "text"):
                    text_parts.append(c.get("text", ""))
        elif itype in ("function_call", "tool_call"):
            tool_calls.append({
                "id":   item.get("call_id") or item.get("id") or f"call_{len(tool_calls)}",
                "type": "function",
                "function": {"name": item.get("name", ""),
                             "arguments": item.get("arguments", "") or "{}"},
            })
    return "".join(text_parts), tool_calls


def _codex_output_source(final: dict, done_items: list) -> dict:
    """The response whose `output` to read: the completed response when it
    carries output items, else the items collected from output_item.done."""
    if final and final.get("output"):
        return final
    return {"output": done_items}


def _from_codex_response(events: list) -> dict:
    """Aggregate a list of Responses SSE event objects into one OpenAI
    chat-completion JSON (used for non-streaming clients)."""
    final = {}
    text_acc = []
    done_items = []
    for ev in events:
        t = ev.get("type", "")
        if t == "response.completed" and isinstance(ev.get("response"), dict):
            final = ev["response"]
        elif t == "response.output_text.delta":
            text_acc.append(ev.get("delta", ""))
        elif t == "response.output_item.done" and isinstance(ev.get("item"), dict):
            done_items.append(ev["item"])
    # With store=false the Codex backend sends an empty `output` in
    # response.completed; the items only arrive as response.output_item.done.
    text, tool_calls = _codex_text_and_tools(_codex_output_source(final, done_items))
    if not text and text_acc:
        text = "".join(text_acc)
    message = {"role": "assistant", "content": text or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    finish = "tool_calls" if tool_calls else "stop"
    usage = (final.get("usage") or {}) if final else {}
    return {
        "id":      final.get("id", "chatcmpl-codex"),
        "object":  "chat.completion",
        "created": int(time.time()),
        "model":   final.get("model", "codex"),
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {
            "prompt_tokens":     usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens":      usage.get("total_tokens", 0),
        },
    }


def _codex_streaming_generator(resp: requests.Response):
    """Translate a Codex Responses SSE stream into OpenAI chat.completion.chunk
    SSE on the fly."""
    cid = "chatcmpl-codex"
    created = int(time.time())

    def chunk(delta: dict, finish=None):
        return "data: " + json.dumps({
            "id": cid, "object": "chat.completion.chunk", "created": created,
            "model": "codex",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }) + "\n\n"

    yield chunk({"role": "assistant"})
    event_type = None
    finish = "stop"
    done_items = []
    for raw in resp.iter_lines():
        if not raw:
            continue
        raw = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        if raw.startswith("event:"):
            event_type = raw[6:].strip()
            continue
        if not raw.startswith("data:"):
            continue
        data_str = raw[5:].strip()
        if data_str == "[DONE]":
            break
        try:
            ev = json.loads(data_str)
        except Exception:
            continue
        etype = ev.get("type") or event_type
        if etype == "response.output_text.delta":
            d = ev.get("delta", "")
            if d:
                yield chunk({"content": d})
        elif etype == "response.output_item.done" and isinstance(ev.get("item"), dict):
            done_items.append(ev["item"])
        elif etype == "response.completed" and isinstance(ev.get("response"), dict):
            usage = ev["response"].get("usage")
            if usage:
                normalized = {"prompt_tokens": usage.get("input_tokens", 0),
                              "completion_tokens": usage.get("output_tokens", 0)}
                normalized["total_tokens"] = usage.get("total_tokens", sum(normalized.values()))
                yield "data: " + json.dumps({"choices": [], "usage": normalized}) + "\n\n"
            _, tcs = _codex_text_and_tools(_codex_output_source(ev["response"], done_items))
            if tcs:
                finish = "tool_calls"
                for i, tc in enumerate(tcs):
                    yield chunk({"tool_calls": [{"index": i, **tc}]})
    yield chunk({}, finish=finish)
    yield "data: [DONE]\n\n"


# ── Request forwarding ─────────────────────────────────────────────────────────

def _resolve_model(provider: dict, payload: dict, model: str | None) -> str:
    """Which model to actually send: the explicit one the failover loop chose,
    else the client's (if it named a real model), else the provider's primary."""
    if model:
        return model
    m = payload.get("model", "")
    if not m or m in ("", ROUTER_MODEL, "auto"):
        return provider["model"]
    return m


def _upstream_timeout(read_seconds=120):
    remaining = getattr(_req_ctx, "deadline", time.monotonic() + REQUEST_TIMEOUT) - time.monotonic()
    if remaining <= 0:
        raise requests.Timeout("Router request deadline exceeded")
    return requests.adapters.TimeoutSauce(total=remaining, connect=min(10, remaining), read=min(read_seconds, remaining))


def forward(provider: dict, key: str, payload: dict, streaming: bool,
            model: str | None = None) -> requests.Response | None:
    # Codex (ChatGPT OAuth) speaks the Responses API — translate and send directly.
    if provider.get("protocol") == "codex":
        token = codex_creds.get_access_token(key)   # key is the account_id
        if not token:
            log.error(f"  codex: no valid token for account ...{key[-6:]}")
            return None
        model = _resolve_model(provider, payload, model)
        cleaned = []
        for msg in payload.get("messages", []):
            m = dict(msg); _strip_message(m); cleaned.append(m)
        body = _to_codex_body({**payload, "messages": cleaned}, model)
        hdrs = {
            "Authorization":      f"Bearer {token}",
            "chatgpt-account-id": key,
            "Content-Type":       "application/json",
            "Accept":             "text/event-stream",
            "originator":         "codex_cli_rs",
            "OpenAI-Beta":        "responses=experimental",
        }
        try:
            return _HTTP.post(provider["base_url"].rstrip("/") + "/responses",
                              headers=hdrs, json=body, stream=True, timeout=_upstream_timeout(180))
        except requests.exceptions.RequestException as e:
            log.error(f"  Network error → codex: {e}")
            return None

    # Anthropic uses a different wire format — translate and send directly.
    if provider.get("protocol") == "anthropic":
        model = _resolve_model(provider, payload, model)
        cleaned = []
        for msg in payload.get("messages", []):
            m = dict(msg)
            _strip_message(m)
            cleaned.append(m)
        body = _to_anthropic_body({**payload, "messages": cleaned}, model)
        if provider.get("max_output_tokens"):
            body["max_tokens"] = min(body["max_tokens"], provider["max_output_tokens"])
        hdrs = {"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
        try:
            return _HTTP.post("https://api.anthropic.com/v1/messages",
                              headers=hdrs, json=body, stream=streaming, timeout=_upstream_timeout())
        except requests.exceptions.RequestException as e:
            log.error(f"  Network error → anthropic: {e}")
            return None

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
        **provider.get("headers", {}),
    }

    body = dict(payload)

    # Use the model the failover loop chose (else placeholder → provider's primary)
    body["model"] = _resolve_model(provider, payload, model)

    # Strip thinking fields from conversation history before forwarding.
    # History bound for a reasoning-echo model family keeps its
    # reasoning_content: those upstreams 400 a tool-carrying request whose
    # earlier turns arrive without it (see _requires_reasoning_echo).
    if "messages" in body:
        keep_reasoning = _requires_reasoning_echo(body["model"])
        cleaned = []
        for msg in body["messages"]:
            m = dict(msg)
            _strip_message(m, keep_reasoning=keep_reasoning)
            cleaned.append(m)
        body["messages"] = cleaned

    # Strip top-level thinking fields (Gemini sometimes adds these)
    body.pop("think", None)
    body.pop("thinking", None)

    # Reasoning models spend output tokens on hidden chain-of-thought, so a small
    # client max_tokens can be entirely consumed by thinking — leaving empty
    # content. Give reasoning models extra headroom on top of what the client
    # asked for, so the actual answer still fits. (The model stops when done, so
    # short answers stay short.) Tune/disable with REASONING_TOKEN_RESERVE.
    # Per-model: only the actual model being sent gets the reserve.
    # Synchronized and coding-client requests already reserve output within their total context.
    # Hidden reasoning must share that ceiling instead of expanding it afterward.
    context_bound = has_request_context() and (
        getattr(g, "context_client_pool", None) is not None
        or getattr(g, "strict_client_tools", False)
    )
    if not context_bound and _model_caps(provider["name"], body["model"]).get("reasoning"):
        reserve = _int_env("REASONING_TOKEN_RESERVE", 4096)
        if reserve > 0:
            for field in ("max_tokens", "max_completion_tokens"):
                if isinstance(body.get(field), int):
                    body[field] += reserve

    # Clamp the requested output length to this provider's hard ceiling. Some
    # providers (e.g. Cohere caps output at 8192) reject the ENTIRE request with
    # a 400 when max_tokens exceeds their limit — so a client default like
    # max_tokens=65536 would fail every call. Capping it lets the request through;
    # the model still produces up to its real maximum.
    out_cap = provider.get("max_output_tokens", 0)
    if out_cap:
        for field in ("max_tokens", "max_completion_tokens"):
            if isinstance(body.get(field), int) and body[field] > out_cap:
                log.info(f"  clamping {field} {body[field]}→{out_cap} for {provider['name']}")
                body[field] = out_cap

    # Ask the provider to include usage in the final SSE chunk so _streaming_with_usage
    # can record actual tokens. Non-destructive: merges with any stream_options the
    # client already sent. Most OpenAI-compatible providers support this.
    if streaming:
        body.setdefault("stream_options", {})
        body["stream_options"]["include_usage"] = True

    url = provider["base_url"].rstrip("/") + "/chat/completions"
    try:
        return _HTTP.post(url, headers=headers, json=body, stream=streaming, timeout=_upstream_timeout())
    except requests.exceptions.RequestException as e:
        log.error(f"  Network error → {provider['name']}: {e}")
        return None


def _embed_ordered() -> list[dict]:
    """Embedding-capable providers in a STABLE priority order — deliberately NOT
    round-robined like chat. Different providers return different vector
    dimensions (e.g. gemini 3072, cohere 1536, mistral 1024), and vectors of
    different dimensions can't be compared in one store. So we keep hitting the
    same provider and only fail over (accepting a dimension change) when it's
    actually down. Open breakers and unhealthy providers sink to the back; the
    sort is stable, so healthy providers keep their config order as the priority.

    For STRICT single-dimension guarantees, disable the others' embed models
    (e.g. MISTRAL_EMBED_MODEL= and COHERE_EMBED_MODEL= empty in .env)."""
    try:
        scope = _caller_scope()
        models = _caller_models()
    except RuntimeError:
        scope = None
        models = None
    embed_providers = [p for p in PROVIDERS if p.get("embed_model")
                       and _model_enabled(p["name"], p["embed_model"])
                       and (scope is None or p["name"] in scope)
                       and (models is None or p["embed_model"] in models)]
    return sorted(embed_providers, key=lambda p: (1 if stats.breaker_open(p["name"]) else 0,
                                                  stats.health_bucket(p["name"])))


def _prompt_text(messages: list) -> str:
    """Flatten a chat request's message text for semantic-cache embedding."""
    return " ".join(_message_text(m) for m in messages if m.get("content")).strip()[:8000]


def _embed_text(text: str):
    """Embed text via the internal embeddings pipeline (used by the semantic cache).
    Returns a vector, or None if no embed provider is available / all are cooling."""
    if not text:
        return None
    original = getattr(_req_ctx, "deadline", None)
    _req_ctx.deadline = min(original or float("inf"), time.monotonic() + SEMANTIC_EMBED_TIMEOUT)
    try:
        for provider in _embed_ordered():
            if not _model_enabled(provider["name"], provider["embed_model"]):
                continue
            if time.monotonic() >= _req_ctx.deadline:
                break
            em = provider["embed_model"]
            key = pool.get_key(provider["name"], em)
            if not key:
                continue
            resp = None
            try:
                resp = forward_embeddings(provider, key, {"input": text})
                if resp is not None and 200 <= resp.status_code < 300:
                    vector = resp.json()["data"][0]["embedding"]
                    if isinstance(vector, list) and vector and all(isinstance(n, (int, float)) for n in vector):
                        identity = json.dumps([provider["name"], provider["base_url"], em, provider.get("headers", {})], sort_keys=True)
                        return {"identity": hashlib.sha256(identity.encode()).hexdigest(), "vector": vector}
            except Exception:
                pass
            finally:
                if resp is not None:
                    resp.close()
        return None
    finally:
        if original is None:
            del _req_ctx.deadline
        else:
            _req_ctx.deadline = original


def forward_embeddings(provider: dict, key: str, payload: dict) -> requests.Response | None:
    """POST an OpenAI-format embeddings request to a provider, substituting the
    provider's configured embed model. No streaming, no format translation."""
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
        **provider.get("headers", {}),
    }
    body = dict(payload)
    body["model"] = provider["embed_model"]   # always the provider's real embed model
    url = provider["base_url"].rstrip("/") + "/embeddings"
    try:
        return _HTTP.post(url, headers=headers, json=body, timeout=_upstream_timeout())
    except requests.exceptions.RequestException as e:
        log.error(f"  Network error → {provider['name']} embeddings: {e}")
        return None

# ── Flask app ──────────────────────────────────────────────────────────────────

app = Flask(__name__)
if os.environ.get("HR_TRUST_PROXY") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=0, x_proto=1, x_host=0, x_port=0, x_prefix=0)
# Cap request bodies so a buggy client can't exhaust memory (Flask returns 413)
app.config["MAX_CONTENT_LENGTH"] = _int_env("MAX_REQUEST_BYTES", 10 * 1024 * 1024)
START_TIME = time.time()   # for uptime in /metrics


def _caller_token() -> str:
    """The API key the caller presented (Bearer, or Anthropic's x-api-key)."""
    header = request.headers.get("Authorization", "").strip()
    token  = header[7:].strip() if header[:7].lower() == "bearer " else header
    if not token:
        # The Anthropic SDK sends the key via x-api-key, not Authorization.
        token = request.headers.get("x-api-key", "").strip()
    return token


def _auth_check():
    token = _caller_token()
    with _ACCESS_LOCK:
        if not token or not any(hmac.compare_digest(token.encode(), k.encode()) for k in PROXY_API_KEYS):
            return jsonify({"error": "unauthorized"}), 401
        g.caller_limits = dict(KEY_LIMITS.get(token) or {})
        scope = KEY_PROVIDER_SCOPE.get(token)
        g.caller_scope = set(scope) if scope is not None else None
        models = KEY_MODEL_SCOPE.get(token)
        g.caller_models = set(models) if models is not None else None


def _caller_limits():
    if not has_request_context():
        return {}
    return getattr(g, "caller_limits", KEY_LIMITS.get(_caller_token()) or {})


def _caller_scope():
    if not has_request_context():
        return None
    return getattr(g, "caller_scope", KEY_PROVIDER_SCOPE.get(_caller_token()))


def _caller_models():
    if not has_request_context():
        return None
    return getattr(g, "caller_models", KEY_MODEL_SCOPE.get(_caller_token()))


_SESSION_COOKIE = "hermes_admin_session"
_SESSION_TTL = max(60, int(os.environ.get("HR_ADMIN_SESSION_TTL", "3600")))
_ADMIN_SESSIONS = {}
_SESSION_LOCK = threading.Lock()


def _same_origin():
    origin = request.headers.get("Origin")
    if not origin:
        return request.headers.get("Sec-Fetch-Site", "") not in ("cross-site", "same-site")
    return origin.rstrip("/") == request.host_url.rstrip("/")


def _session_record():
    sid = request.cookies.get(_SESSION_COOKIE, "")
    with _SESSION_LOCK:
        now = time.time()
        for expired in [key for key, value in _ADMIN_SESSIONS.items() if value["expires"] <= now]:
            _ADMIN_SESSIONS.pop(expired, None)
        return _ADMIN_SESSIONS.get(sid)


def _admin_check():
    token = _caller_token()
    if token:
        if not hmac.compare_digest(token.encode(), ADMIN_API_KEY.encode()):
            return jsonify({"error": "unauthorized"}), 401
    else:
        session = _session_record()
        if not session:
            return jsonify({"error": "unauthorized"}), 401
        if not _same_origin():
            return jsonify({"error": "cross-origin administration denied"}), 403
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            csrf = request.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(csrf.encode(), session["csrf_token"].encode()):
                return jsonify({"error": "CSRF token required"}), 403
    if request.method in ("POST", "PUT", "PATCH") and request.data and not isinstance(request.get_json(force=True, silent=True), dict):
        return jsonify({"error": {"message": "request body must be a JSON object", "type": "invalid_request_error"}}), 400


@app.route("/v1/admin/session", methods=["POST", "GET", "DELETE"])
def admin_session():
    if request.method == "POST":
        if not _same_origin():
            return jsonify({"error": "cross-origin administration denied"}), 403
        body = request.get_json(silent=True)
        key = body.get("api_key", "") if isinstance(body, dict) else ""
        if not isinstance(key, str) or not hmac.compare_digest(key.encode(), ADMIN_API_KEY.encode()):
            return jsonify({"error": "unauthorized"}), 401
        sid = secrets.token_urlsafe(32)
        record = {"csrf_token": secrets.token_urlsafe(32), "expires": time.time()+_SESSION_TTL}
        with _SESSION_LOCK:
            _ADMIN_SESSIONS.pop(request.cookies.get(_SESSION_COOKIE, ""), None)
            # Bounded process-local sessions; restarting safely signs browsers out.
            if len(_ADMIN_SESSIONS) >= 256:
                _ADMIN_SESSIONS.pop(next(iter(_ADMIN_SESSIONS)))
            _ADMIN_SESSIONS[sid] = record
        response = jsonify({"authenticated": True, "csrf_token": record["csrf_token"]})
        response.set_cookie(_SESSION_COOKIE, sid, max_age=_SESSION_TTL, httponly=True,
                            secure=request.is_secure or os.environ.get("HR_ADMIN_COOKIE_SECURE") == "1",
                            samesite="Strict", path="/")
    else:
        err = _admin_check()
        if err:
            return err
        if request.method == "DELETE":
            with _SESSION_LOCK:
                _ADMIN_SESSIONS.pop(request.cookies.get(_SESSION_COOKIE, ""), None)
            response = jsonify({"authenticated": False})
            response.delete_cookie(_SESSION_COOKIE, path="/")
        else:
            record = _session_record()
            response = jsonify({"authenticated": True, "csrf_token": record["csrf_token"] if record else None})
    response.headers["Cache-Control"] = "no-store"
    return response


def _cache_ns() -> str:
    """Key and current scope isolate responses after restrictive policy changes."""
    token = _caller_token()
    scope = _caller_scope()
    models = _caller_models()
    policy = json.dumps([sorted(scope) if scope is not None else None,
                         sorted(models) if models is not None else None,
                         _model_policy_revision()])
    return hashlib.sha256((token + policy).encode()).hexdigest()


def _known_price(model):
    return (any(part in str(model).lower() for part in MODEL_PRICES)
            and all(math.isfinite(v) and v >= 0 for v in _price_model(model)))


def _output_bound(payload, provider=None, model=None):
    values = [payload[field] for field in ("max_tokens", "max_completion_tokens") if field in payload]
    if not values or any(not isinstance(v, int) or isinstance(v, bool) or v <= 0 for v in values):
        return None
    bound = max(values)
    if provider and _model_caps(provider["name"], model).get("reasoning"):
        bound += max(0, _int_env("REASONING_TOKEN_RESERVE", 4096))
    if provider and provider.get("max_output_tokens"):
        bound = min(bound, provider["max_output_tokens"])
    return bound


def _budget_candidate_allowed(provider, model, payload):
    try:
        limits = _caller_limits()
        models = _caller_models()
    except RuntimeError:
        return True
    if models is not None and model not in models:
        return False
    if (limits.get("cost_per_day") or limits.get("tokens_per_day")) and (
            provider.get("name") == "codex" or _output_bound(payload, provider, model) is None):
        # The Codex subscription protocol does not enforce an output token ceiling.
        return False
    if not limits.get("cost_per_day"):
        return True
    # Only plain-text token billing has a conservative bound in this release.
    return (provider.get("name") not in ("codex", "anthropic") and _known_price(model)
            and _output_bound(payload, provider, model) is not None)


@_locked_access
def _admit_request(token: str, payload=None, embedding=False):
    payload = payload if payload is not None else request.get_json(silent=True) or {}
    if not embedding and (not isinstance(payload.get("messages", []), list) or
            any(not isinstance(message, dict) for message in payload.get("messages", []))):
        return jsonify({"error": {"message": "messages must be an array of objects", "type": "invalid_request_error"}}), 400
    g.caller_limits = dict(KEY_LIMITS.get(token) or {})
    scope = KEY_PROVIDER_SCOPE.get(token)
    g.caller_scope = set(scope) if scope is not None else None
    models = KEY_MODEL_SCOPE.get(token)
    g.caller_models = set(models) if models is not None else None
    limits = _caller_limits()
    money = bool(limits.get("cost_per_day"))
    token_limit = bool(limits.get("tokens_per_day"))
    bound = _output_bound(payload)
    if (money or token_limit) and (embedding or bound is None or payload.get("tools") or
            payload.get("n", 1) != 1 or payload.get("best_of", 1) != 1 or
            any(not isinstance(m.get("content", ""), str) for m in payload.get("messages", []) if isinstance(m, dict))):
        return jsonify({"error": {"message": "budget requires plain-text input and an enforceable positive output bound", "type": "invalid_request_error"}}), 400
    input_ceiling = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    tokens = input_ceiling + (bound or 0)
    cost = 0.0
    if money or token_limit:
        candidates = _ordered_providers(payload, str(payload.get("model", "")).endswith(":fast"))
        scope = _caller_scope()
        candidates = [c for c in candidates if (scope is None or c["provider"]["name"] in scope)
                      and _budget_candidate_allowed(c["provider"], c["model"], payload)]
        if not candidates:
            return jsonify({"error": {"message": "no eligible model with an enforceable output bound and required pricing", "type": "invalid_request_error"}}), 400
        tokens = max([input_ceiling + _output_bound(payload, c["provider"], c["model"]) for c in candidates] or [tokens])
        if money:
            cost = max(_cost(c["model"], input_ceiling, _output_bound(payload, c["provider"], c["model"])) for c in candidates)
    # Revocation/policy edits and reservation admission have one serialization point.
    with _ACCESS_LOCK:
        if token not in PROXY_API_KEYS:
            return jsonify({"error": "unauthorized"}), 401
        rid, retry, reason = key_usage.reserve(token, limits, tokens, cost)
    if rid:
        g.usage_reservation = rid
        return None
    response = jsonify({"error": {"message": f"quota exceeded ({reason})", "type": "rate_limit_error"}})
    response.headers["Retry-After"] = str(retry)
    return response, 429


def _settle_request_usage(reservation_id, usage, model, cached=False, failed=False):
    usage = usage if isinstance(usage, dict) else {}
    pt = usage.get("prompt_tokens", usage.get("input_tokens"))
    ct = usage.get("completion_tokens", usage.get("output_tokens"))
    total = usage.get("total_tokens")
    valid_count = lambda value: isinstance(value, int) and not isinstance(value, bool) and value >= 0
    split = valid_count(pt) and valid_count(ct)
    if split:
        total = max(total if valid_count(total) else 0, pt + ct)
    valid = valid_count(total)
    cost = _cost(model, pt, ct) if split and _known_price(model) else None
    key_usage.settle(reservation_id, total if valid else None, cost, failed=failed, cached=cached,
                     source="unknown_price" if not cached and not _known_price(model) else
                            "reported" if valid and cost is not None else "estimated")


def _record_request_tokens(token: str, payload: dict, result):
    if result and result[0] == "stream":
        return  # the stream finalizer owns this reservation
    failed = not result or result[0] == "error"
    data = result[1] if result and result[0] == "json" else {}
    _settle_request_usage(getattr(g, "usage_reservation", None), data.get("usage"),
                          data.get("model") or getattr(_req_ctx, "model", "") or "",
                          cached=getattr(_req_ctx, "cache_hit", False), failed=failed)


_DASHBOARD_HTML = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")


@app.route("/")
def root():
    """Land bare-host visitors on the dashboard so `http://<host>:<port>` just works
    in a browser — no need to know the /dashboard path. API clients use /v1/* and
    never hit this."""
    return redirect("/dashboard", code=302)


@app.route("/dashboard")
def dashboard():
    """Self-contained monitoring dashboard. Opens in any browser.
    Polls /v1/status, /v1/usage, and /v1/logs every 5 seconds.
    Prompts for the proxy API key on first load (stored in localStorage)."""
    return Response(_DASHBOARD_HTML, content_type="text/html; charset=utf-8")


@app.route("/favicon.ico")
def favicon():
    return Response(status=204)


@app.route("/health")
def health():
    """Unauthenticated health check for uptime monitoring."""
    readiness = _readiness()
    return jsonify({"status": "ok", "providers": [p["name"] for p in PROVIDERS],
                    "initializing": readiness["state"] == "initializing",
                    "ready": readiness["state"] == "ready", "readiness": readiness})


@app.route("/v1/models")
def models():
    err = _auth_check()
    if err:
        return err
    data = [{"id": ROUTER_MODEL, "object": "model", "owned_by": "hermes-router"}]
    # Advertise the fast/conversation profile only when a local model is configured,
    # since that's what it routes short turns to.
    if any(p["name"] == "local" and _enabled_provider_models(p) for p in PROVIDERS):
        data.append({"id": f"{ROUTER_MODEL}:fast", "object": "model", "owned_by": "hermes-router"})
    return jsonify({"object": "list", "data": data})


def _context_limits(provider, model, config):
    explicit = config["model_limits"].get(provider["name"] + "::" + model)
    if explicit:
        return explicit
    return (_DISCOVERED_CONTEXT_LIMITS.get((provider["name"], model))
            or context_policy.discovered_limits(_model_caps(provider["name"], model)))


def _context_candidates(config):
    providers, models = _caller_scope(), _caller_models()
    result = []
    for provider in PROVIDERS:
        # The subscription Responses protocol cannot enforce max output tokens.
        # Do not promise a bounded client budget for this otherwise usable pool.
        if provider.get("protocol") == "codex":
            continue
        if providers is not None and provider["name"] not in providers:
            continue
        for model in _enabled_provider_models(provider):
            if has_request_context() and getattr(g, "claude_only", False) and not _claude_candidate(provider, model):
                continue
            if has_request_context() and getattr(g, "strict_client_tools", False) and not _model_has_confirmed_tool_support(provider["name"], model):
                continue
            if models is not None and model not in models:
                continue
            if not _model_supports_tools(provider["name"], model):
                continue
            budget = context_policy.candidate_budget(
                _context_limits(provider, model, config), config["context_window"], provider)
            if budget:
                result.append({"provider": provider, "model": model, "budget": budget})
    return result


def _client_context(config):
    return context_policy.client_config(ROUTER_MODEL, config, _context_candidates(config))


@app.route("/v1/client-config")
@app.route("/claude/v1/client-config")
@app.route("/codex/v1/client-config")
def client_configuration():
    err = _auth_check()
    if err:
        return err
    g.claude_only = request.path.startswith("/claude/")
    g.strict_client_tools = request.path != "/v1/client-config"
    response = jsonify(_client_context(_CONTEXT_CONFIG))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/v1/bridge-info")
def bridge_information():
    err = _auth_check()
    if err:
        return err
    return jsonify({"version": 1, "responses": "stateless",
                    "claude_messages": "/claude/v1/messages", "automatic_handoff": False})


def _claude_candidate(provider, model):
    return (provider.get("name") == "anthropic" or
            (provider.get("protocol") == "anthropic" and model.startswith("claude-")) or
            (provider.get("name") == "openrouter" and model.startswith("anthropic/claude-")))


def _check_client_context(payload):
    # Immutable request snapshot: a settings edit affects the next request,
    # never halfway through an existing request's failover chain.
    g.context_config = _CONTEXT_CONFIG
    revision = request.headers.get("X-Hermes-Config-Revision")
    if revision is None:
        return None
    config = _client_context(g.context_config)
    if revision != config["revision"]:
        return jsonify({"error": {"code": "client_config_changed", "message":
            "Router context settings changed. Refresh model information and retry."}}), 409
    model = config["model"]
    if model is None:
        return jsonify({"error": {"code": "context_unavailable", "message": config["reason"]}}), 503
    # Conservative across tokenizers, including tool schemas and arguments.
    content = {k: payload[k] for k in ("messages", "tools") if k in payload}
    input_bound = len(json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    outputs = [payload.get("max_tokens")]
    if "max_completion_tokens" in payload:
        outputs.append(payload["max_completion_tokens"])
    if any(type(output) is not int or not 0 < output <= model["max_output_tokens"] for output in outputs):
        return jsonify({"error": {"code": "context_output_limit", "message": "Invalid client output budget"}}), 400
    if input_bound > model["max_input_tokens"]:
        return jsonify({"error": {"code": "context_input_limit", "message":
            "Conversation exceeds the current router input budget. Start a shorter conversation."}}), 400
    g.context_client_revision = config["revision"]
    g.context_client_pool = {(c["provider"]["name"], c["model"]) for c in _context_candidates(g.context_config)}
    return None


@app.route("/v1/config/context", methods=["POST"])
@_locked_access
def config_context():
    global _CONTEXT_CONFIG
    err = _admin_check()
    if err:
        return err
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not body or set(body) - {"context_window", "model_limits"}:
        return jsonify({"error": {"message": "Supply context_window and/or model_limits"}}), 400
    try:
        merged = {"context_window": body.get("context_window", _CONTEXT_CONFIG["context_window"]),
                  "model_limits": dict(_CONTEXT_CONFIG["model_limits"])}
        patch = body.get("model_limits", {})
        if not isinstance(patch, dict):
            raise ValueError("model_limits must be an object")
        known = {p["name"] + "::" + m for p in PROVIDERS for m in _provider_models(p)}
        for key, value in patch.items():
            if key not in known and not (value is None and key in merged["model_limits"]):
                raise ValueError("unknown provider/model")
            if value is None:
                merged["model_limits"].pop(key, None)
            else:
                merged["model_limits"][key] = value
        clean = context_policy.validate_config(merged)
    except ValueError as exc:
        return jsonify({"error": {"message": str(exc)}}), 400
    # One atomic persisted value for the minimum and all confirmed capacities.
    encoded = json.dumps(clean, separators=(",", ":"))
    _env_write_line("ROUTER_CONTEXT_CONFIG", encoded)
    os.environ["ROUTER_CONTEXT_CONFIG"] = encoded
    _CONTEXT_CONFIG = clean
    return jsonify({"context_window": clean["context_window"], "restart_required": False})


def _route_completion(payload: dict, streaming: bool, ns: str = ""):
    """Core routing + failover pipeline, shared by /v1/chat/completions and the
    Anthropic-compatible /v1/messages. Takes an OpenAI-format payload and returns
    one of:
        ("json",   data_dict)            non-streaming success (OpenAI format)
        ("stream", generator, provider)  streaming success; generator yields
                                         OpenAI-format SSE regardless of upstream
        ("error",  error_dict, status)   every provider exhausted
    """
    # Seed per-thread routing context so endpoint handlers can read it back
    # after this call returns (provider chosen, cascade count, cache-hit flag).
    _req_ctx.provider  = None
    _req_ctx.model     = None
    _req_ctx.cache_hit = False
    _req_ctx.attempts  = 0   # total forward() calls made (cascades = attempts-1)
    _req_ctx.started = time.monotonic()
    _req_ctx.deadline = _req_ctx.started + REQUEST_TIMEOUT

    # Generic routing hints on the existing completion endpoint. `:fast`
    # prefers a local model; a tool loop can request strict tool transport,
    # cache isolation, and session affinity while retaining normal failover.
    prefer_local = False
    tool_loop = False
    session_affinity_id = None
    workload_hint = None
    try:
        tool_loop = request.headers.get("X-Hermes-Tool-Loop", "").strip().lower() in {"1", "true", "yes"}
        session_affinity_id = _session_affinity_id(request.headers.get("X-Hermes-Session-Affinity"))
        workload_hint = _workload_hint(request.headers.get("X-Hermes-Workload-Hint"))
    except RuntimeError:
        pass  # called outside a request context (e.g. tests)
    if str(payload.get("model") or "").endswith(":fast"):
        prefer_local = True
        payload = {**payload, "model": ROUTER_MODEL}

    messages = payload.get("messages", [])

    context_config = getattr(g, "context_config", _CONTEXT_CONFIG) if has_request_context() else _CONTEXT_CONFIG
    context_pool = getattr(g, "context_client_pool", None) if has_request_context() else None
    # Bound requests cannot reuse a legacy response with a larger reasoning
    # allowance or a response from an earlier eligible pool.
    if context_pool is not None:
        ns += ":client:" + g.context_client_revision
    # A response cached under an old policy cannot satisfy a new policy.
    ns += ":context:" + hashlib.sha256(json.dumps(context_config, sort_keys=True).encode()).hexdigest()
    # Retain this exact snapshot: even an A→B→A edit must not cache a response
    # routed through the temporary pool under the original pool's namespace.
    model_policy = _DISABLED_MODELS
    ns += ":models:" + _model_policy_revision(model_policy)
    claude_only = has_request_context() and getattr(g, "claude_only", False)
    if claude_only:
        ns += ":claude-only"
    strict_client = has_request_context() and getattr(g, "strict_client_tools", False)
    if strict_client and payload.get("tools"):
        tool_loop = True

    # Cache check (non-streaming only): exact match first (cheap), then optional
    # semantic match. query_emb is reused to store the response so future similar
    # prompts can match it.
    query_emb = None
    if not streaming and not tool_loop and not strict_client:
        cached = cache.get(payload, ns)
        if cached is not None:
            log.info("↩ cache hit")
            _req_ctx.cache_hit = True
            return ("json", cached)
        try:
            limits = _caller_limits()
        except RuntimeError:
            limits = {}
        if SEMANTIC_CACHE and not (limits.get("cost_per_day") or limits.get("tokens_per_day")) and _embed_ordered():
            query_emb = _embed_text(_prompt_text(messages))
            if query_emb:
                hit = cache.semantic_lookup(query_emb, ns, payload)
                if hit is not None:
                    log.info("↩ semantic cache hit")
                    _req_ctx.cache_hit = True
                    return ("json", hit)

    est_tokens = _estimated_tokens(messages)
    if strict_client:
        # Include tool definitions and tool arguments in the context estimate.
        est_tokens = max(est_tokens, _estimated_tokens([{"role": "user", "content":
            json.dumps({k: payload[k] for k in ("messages", "tools") if k in payload}, ensure_ascii=False)}]))
    ordered    = _ordered_providers(payload, prefer_local, workload_hint)
    if claude_only:
        ordered = [c for c in ordered if _claude_candidate(c["provider"], c["model"])]
    ordered = _session_affinity_order(ordered, session_affinity_id)

    # Tool-aware routing: when the request carries tools, prefer (provider, model)
    # candidates whose MODEL actually supports function calling — otherwise a model
    # that silently ignores tools would return plain text instead of the tool call.
    # Default requests keep the compatibility fallback when capability metadata
    # has no tool candidate. Explicit tool loops are strict and fail early.
    needs_tools = bool(payload.get("tools"))
    tool_capable = _model_has_confirmed_tool_support if tool_loop else _model_supports_tools
    any_tool_candidate = any(
        tool_capable(c["provider"]["name"], c["model"]) for c in ordered
    )
    if tool_loop and not needs_tools:
        return (
            "error",
            {"error": {"message": "Tool-loop mode requires tool definitions", "type": "invalid_request_error"}},
            400,
        )
    if tool_loop and not any_tool_candidate:
        return (
            "error",
            {"error": {"message": "No tool-capable models available", "type": "router_error"}},
            503,
        )
    enforce_tool = needs_tools and (tool_loop or any_tool_candidate)

    # Vision-aware routing: when the request carries an image, prefer candidates
    # whose MODEL is known to accept image input — otherwise the request cascades
    # through every text-only model's clean 400/403 rejection first, wasting real
    # latency before reaching one that actually works. SAFETY — same fallback as
    # tools: only enforce when at least one vision-capable candidate exists.
    needs_vision  = _payload_has_image(payload)
    enforce_vision = needs_vision and any(
        _model_supports_vision(c["provider"], c["model"]) for c in ordered)

    # Provider scoping: an access key can be restricted to specific providers
    # from the dashboard's Access Keys page. Unlike tool/vision detection above,
    # this is an explicit admin restriction, not a heuristic — so there is
    # deliberately NO safety-net fallback. If none of the caller's allowed
    # providers are viable right now, the request should fail rather than
    # silently route through a provider it was scoped away from.
    caller_providers = None
    try:
        caller_providers = _caller_scope()
    except RuntimeError:
        pass  # called outside a request context (e.g. tests)

    # Circuit breaker: skip providers whose breaker is open. SAFETY — if EVERY
    # candidate is open, treat them all as half-open probes (skip none) so we
    # always make forward progress instead of hard-failing while options remain.
    any_closed = any(not stats.breaker_open(c["provider"]["name"]) for c in ordered)

    # Per-(provider, model) failover: walk the ranked candidate list, rotating keys
    # within each candidate. A whole provider is taken out of the running for this
    # request (skip_providers) on auth / payload / unexpected errors — those won't
    # be fixed by another of its models.
    skip_providers: set = set()
    for cand in ordered:
        provider = cand["provider"]
        name     = provider["name"]
        model    = cand["model"]

        if name in skip_providers or not _model_enabled(name, model):
            continue

        if context_pool is not None and (name, model) not in context_pool:
            continue
        limits = _context_limits(provider, model, context_config)
        if strict_client and limits is None:
            continue
        if context_config["context_window"]:
            if limits is None or limits["context_window"] < context_config["context_window"]:
                continue
        if limits:
            requested_output = payload.get("max_tokens", payload.get("max_completion_tokens", 0))
            if type(requested_output) is not int or requested_output < 0:
                continue
            if requested_output > limits["max_output_tokens"] or est_tokens + requested_output > limits["context_window"]:
                continue

        # Caller's access key is scoped to specific providers — skip anything else.
        if caller_providers is not None and name not in caller_providers:
            skip_providers.add(name)
            continue

        # Breaker open → skip the whole provider (unless all are open, then probe).
        if any_closed and stats.breaker_open(name):
            log.info(f"⨂ skipping {name} (circuit open)")
            skip_providers.add(name)
            continue

        # Skip providers whose payload ceiling this request would exceed
        # (e.g. Groq's free TPM) — avoids a guaranteed 413 round-trip. Provider-wide.
        cap = provider.get("skip_if_tokens_over", 0)
        if cap and est_tokens > cap:
            log.info(f"⤳ skipping {name} (~{est_tokens} tok > {cap} cap)")
            skip_providers.add(name)
            continue

        # Tool request → skip candidates whose MODEL can't do function calling
        # (per-model; another model on the same provider may still qualify).
        if enforce_tool and not tool_capable(name, model):
            log.info(f"⚒ skipping {name}/{model} (no tool support)")
            continue

        # Vision request → skip candidates whose MODEL isn't known to accept images.
        if enforce_vision and not _model_supports_vision(provider, model):
            log.info(f"🖼 skipping {name}/{model} (no vision support)")
            continue

        if not _budget_candidate_allowed(provider, model, payload):
            continue
        attempts = pool.key_count(name, model) or 1
        for _ in range(attempts):
            # A switch changed during a previous upstream attempt also applies
            # to retries/failover from this already-running request.
            if not _model_enabled(name, model):
                break
            if time.monotonic() >= _req_ctx.deadline:
                return ("error", {"error": {"message": "Router request deadline exceeded", "type": "router_timeout"}}, 504)
            key = pool.get_key(name, model)
            if not key:
                break   # all keys for this (provider, model) are cooling → next candidate

            log.info(f"→ Trying {name}/{model} ...{key[-6:]}")
            _req_ctx.attempts += 1
            t0   = time.time()
            try:
                resp = forward(provider, key, payload, streaming, model)
            except ValueError as exc:
                return ("error", {"error": {"message": str(exc), "type": "invalid_request_error"}}, 400)
            elapsed = time.time() - t0

            if resp is None:
                stats.record_error(name)
                stats.record_health(name, False)   # network/timeout = provider health failure
                pool.mark_key_down(name, key, retry_after=30)
                continue

            handed_off = False
            try:
                if time.monotonic() >= _req_ctx.deadline:
                    return ("error", {"error": {"message": "Router request deadline exceeded", "type": "router_timeout"}}, 504)
                if resp.status_code == 429:
                    stats.record_error(name)
                    # 429 is NOT a health failure — per-(key,model) cooldown handles it.
                    retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                    pool.mark_rate_limited(name, key, model, retry_after=retry_after)
                    log.warning(f"  {name}/{model} 429 — cooldown {retry_after}s, trying next")
                    continue

                if resp.status_code in (401, 403):
                    stats.record_error(name)
                    btxt = (resp.text or "")[:300]
                    # Some gateways (e.g. OpenCode) return a MODEL-level rejection as a
                    # 401 — an ended free promo, an unsupported/paywalled model. That's
                    # not a credential problem, so skip just this model and try the
                    # provider's next one instead of disabling the whole provider.
                    if re.search(r"modelerror|not supported|promotion has ended|subscrib|no payment|credits", btxt, re.I):
                        log.warning(f"  {name}/{model} {resp.status_code} model-level — skipping this model: {btxt[:160]}")
                        break
                    # Genuine auth/permission failure — won't work for any model here.
                    # Also count it against the circuit breaker: record_error() alone only
                    # feeds /v1/usage stats, not the breaker (that's record_health-only). A
                    # provider with a permanently bad/unsubscribed key would otherwise be
                    # retried and rejected on every single future request forever, instead
                    # of tripping the breaker and cooling down like any other unhealthy
                    # provider (e.g. a key configured for a paid tier the account never
                    # actually enabled, like OpenCode Go without Go billing turned on).
                    log.error(f"  {name} {resp.status_code} — auth, skipping provider: {btxt[:200]}")
                    stats.record_health(name, False)
                    skip_providers.add(name)
                    break

                if resp.status_code in (400, 404):
                    stats.record_error(name)
                    # model-specific (e.g. bad model name) — just skip this candidate.
                    log.warning(f"  {name}/{model} {resp.status_code} — skipping this model: {resp.text[:150]}")
                    break

                if resp.status_code == 413:
                    stats.record_error(name)
                    # payload-specific — bigger model won't help; cascade providers.
                    log.warning(f"  {name} 413 — payload too large, cascading")
                    skip_providers.add(name)
                    break

                if resp.status_code >= 500:
                    stats.record_error(name)
                    stats.record_health(name, False)   # 5xx = provider health failure
                    pool.mark_key_down(name, key, retry_after=15)
                    continue

                if not (200 <= resp.status_code < 300):
                    stats.record_error(name)
                    stats.record_health(name, False)   # unexpected non-2xx = health failure
                    log.warning(f"  {name} unexpected {resp.status_code} — skipping provider")
                    skip_providers.add(name)
                    break

                # Success
                log.info(f"  ✓ {name}/{model} {resp.status_code} ({elapsed*1000:.0f}ms)")
                _req_ctx.provider = name
                _req_ctx.model    = model
                is_anthropic = provider.get("protocol") == "anthropic"
                is_codex     = provider.get("protocol") == "codex"
                if is_codex:
                    # Codex backend always streams SSE. Stream it through, or
                    # aggregate it into one response for non-streaming clients.
                    if streaming:
                        if session_affinity_id is not None:
                            _session_affinity_set(session_affinity_id, name, model)
                        handed_off = True
                        return ("stream", _streaming_with_usage(_with_cleanup(resp, _codex_streaming_generator(resp)), name, model), name)
                    events = []
                    for raw in resp.iter_lines():
                        if not raw:
                            continue
                        raw = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
                        if raw.startswith("data:"):
                            ds = raw[5:].strip()
                            if ds and ds != "[DONE]":
                                try: events.append(json.loads(ds))
                                except Exception: pass
                    data = _from_codex_response(events)
                    if not _completion_has_output(data):
                        stats.record_error(name)
                        stats.record_health(name, False)
                        log.warning(f"  {name}/{model} empty completion — cascading")
                        pool.mark_rate_limited(name, key, model, retry_after=30)
                        break
                    _add_provider_tokens(name, data, model)
                    if not tool_loop and not strict_client and _DISABLED_MODELS is model_policy:
                        cache.set(payload, data, ns, query_emb)
                    _session_affinity_set(session_affinity_id, name, model)
                    stats.record_success(name, elapsed)
                    stats.record_health(name, True)
                    return ("json", data)
                if streaming:
                    if session_affinity_id is not None:
                        _session_affinity_set(session_affinity_id, name, model)
                    gen = (_anthropic_streaming_generator(resp) if is_anthropic
                           else _streaming_generator(
                               resp, keep_reasoning=_requires_reasoning_echo(model)))
                    wrapped = _streaming_with_usage(_with_cleanup(resp, gen), name, model)
                    handed_off = True
                    return ("stream", wrapped, name)
                else:
                    try:
                        raw = resp.json()
                    except Exception:
                        raw = None
                    data = (_from_anthropic_response(raw) if (is_anthropic and isinstance(raw, dict))
                            else raw)
                    # Guard: a 2xx that carries no usable completion (no `choices`) — e.g.
                    # a gateway that wraps an error in an HTTP-200 body (NVIDIA NIM's gRPC
                    # "ResourceExhausted: Worker local total request limit reached"), or a
                    # non-JSON body. Don't surface that to the caller as the answer —
                    # treat it as a provider failure, cool this (key,model), and cascade.
                    if not isinstance(data, dict) or not data.get("choices"):
                        stats.record_error(name)
                        stats.record_health(name, False)
                        err = data.get("error") if isinstance(data, dict) else None
                        emsg = (err.get("message", "") if isinstance(err, dict)
                                else err if isinstance(err, str)
                                else (data.get("message", "") if isinstance(data, dict) else ""))
                        # NVIDIA NIM (and similar gateways) wrap a transient rate-limit /
                        # resource-exhaustion error in an HTTP-200 body. That's expected under
                        # load and is fully handled here (cascade + per-key cooldown), so log it
                        # at debug to keep it out of the logs; only a genuinely unexpected empty
                        # 2xx body warns.
                        _emsg_l = str(emsg).lower()
                        _transient = any(s in _emsg_l for s in (
                            "resourceexhausted", "resource exhausted", "request limit reached",
                            "rate limit", "too many requests", "quota", "overloaded"))
                        (log.debug if _transient else log.warning)(
                            f"  {name}/{model} 2xx without choices — cascading: {str(emsg)[:140]}")
                        pool.mark_rate_limited(name, key, model, retry_after=30)
                        break
                    if not _completion_has_output(data):
                        stats.record_error(name)
                        stats.record_health(name, False)
                        log.warning(f"  {name}/{model} empty completion — cascading")
                        pool.mark_rate_limited(name, key, model, retry_after=30)
                        break
                    if not is_anthropic:
                        _strip_response(
                            data, keep_reasoning=_requires_reasoning_echo(model))
                    _add_provider_tokens(name, data, model)
                    if not tool_loop and not strict_client and _DISABLED_MODELS is model_policy:
                        cache.set(payload, data, ns, query_emb)
                    _session_affinity_set(session_affinity_id, name, model)
                    stats.record_success(name, elapsed)
                    stats.record_health(name, True)
                    return ("json", data)

            except requests.exceptions.RequestException:
                stats.record_error(name)
                stats.record_health(name, False)
                pool.mark_key_down(name, key, retry_after=30)
            finally:
                if not handed_off:
                    resp.close()

    if time.monotonic() >= _req_ctx.deadline:
        return ("error", {"error": {"message": "Router request deadline exceeded", "type": "router_timeout"}}, 504)
    if PROVIDERS and not any(_enabled_provider_models(p) for p in PROVIDERS):
        return ("error", {"error": {"message": "All chat models are disabled. Enable a model in Providers.",
                                    "type": "router_error", "code": "models_disabled"}}, 503)
    return ("error", {"error": {"message": "All providers exhausted", "type": "router_error"}}, 503)


def _log_completion(token: str, endpoint: str, payload: dict, result: tuple, elapsed: float) -> None:
    """Append one entry to the request ring buffer. Never raises."""
    try:
        messages = payload.get("messages", [])
        is_cache = getattr(_req_ctx, "cache_hit", False)
        attempts = getattr(_req_ctx, "attempts", 0)

        if result[0] == "json":
            status = "cache_hit" if is_cache else "success"
            usage  = result[1].get("usage", {}) if isinstance(result[1], dict) else {}
            ptok   = usage.get("prompt_tokens")
            ctok   = usage.get("completion_tokens")
        elif result[0] == "stream":
            status = "success"
            ptok   = ctok = None
        else:
            status = "error"
            ptok   = ctok = None

        request_log.append({
            "ts":               time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "endpoint":         endpoint,
            "caller":           token[-6:] if token else "anon",
            "streaming":        bool(payload.get("stream", False)),
            "complexity":       classify_complexity(messages),
            "est_tokens":       _estimated_tokens(messages),
            "provider":         "cache" if is_cache else getattr(_req_ctx, "provider", None),
            "model":            getattr(_req_ctx, "model", None) or payload.get("model"),
            "latency_ms":       round(elapsed * 1000),
            "cascades":         max(0, attempts - 1),
            "status":           status,
            "prompt_tokens":    ptok,
            "completion_tokens": ctok,
        })
    except Exception:
        pass   # logging must never break the response path


@app.route("/v1/chat/completions", methods=["POST"])
def chat():
    err = _auth_check()
    if err:
        return err

    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": {"message": "request body must be a JSON object",
                                  "type": "invalid_request_error"}}), 400

    context_error = _check_client_context(payload)
    if context_error:
        return context_error
    token  = _caller_token()
    gate   = _admit_request(token)
    if gate:
        return gate

    t_start = time.time()
    result  = _route_completion(payload, payload.get("stream", False), _cache_ns())
    _record_request_tokens(token, payload, result)

    _log_completion(token, "chat", payload, result, time.time() - t_start)

    if result[0] == "json":
        return jsonify(result[1]), 200
    if result[0] == "stream":
        _, gen, name = result
        response = Response(stream_with_context(gen), content_type="text/event-stream",
                            headers={"X-Provider": name})
        response.call_on_close(gen.close)
        return response
    return jsonify(result[1]), result[2]


@app.route("/v1/responses", methods=["POST"])
def responses():
    """Stateless client Responses API; all inference uses existing routing policy."""
    err = _auth_check()
    if err:
        return err
    try:
        payload, custom = responses_api.to_chat(request.get_json(silent=True), ROUTER_MODEL)
    except ValueError as exc:
        return jsonify({"error": {"message": str(exc), "type": "invalid_request_error"}}), 400
    except (TypeError, AttributeError):
        return jsonify({"error": {"message": "Invalid Responses request structure", "type": "invalid_request_error"}}), 400
    context_error = _check_client_context(payload)
    if context_error:
        return context_error
    g.strict_client_tools = True
    payload.setdefault("max_tokens", 4096)
    token = _caller_token()
    gate = _admit_request(token, payload)
    if gate:
        return gate
    started = time.time()
    result = _route_completion(payload, payload["stream"], _cache_ns())
    _record_request_tokens(token, payload, result)
    _log_completion(token, "responses", payload, result, time.time() - started)
    names = [t["function"]["name"] for t in payload.get("tools", [])]
    if result[0] == "json":
        try:
            return jsonify(responses_api.from_chat(result[1], payload["model"], custom, names))
        except (ValueError, TypeError, KeyError, AttributeError, IndexError):
            return jsonify({"error": {"type": "upstream_error", "message": "Invalid provider response"}}), 502
    if result[0] == "stream":
        _, source, name = result
        gen = responses_api.stream(source, payload["model"], custom, names)
        response = Response(stream_with_context(gen), content_type="text/event-stream",
                            headers={"X-Provider": name, "Cache-Control": "no-store", "X-Accel-Buffering": "no"})
        response.call_on_close(gen.close)
        response.call_on_close(source.close)
        return response
    return jsonify(result[1]), result[2]


@app.route("/claude/v1/messages", methods=["POST"])
@app.route("/v1/messages", methods=["POST"])
def anthropic_messages():
    """Anthropic Messages API endpoint — lets the Anthropic SDK use the router
    plug-and-play. The request is translated to OpenAI format, routed through the
    same multi-provider pipeline as /v1/chat/completions, and translated back."""
    err = _auth_check()
    if err:
        return err

    g.claude_only = request.path.startswith("/claude/")
    g.strict_client_tools = g.claude_only

    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict) or "messages" not in body:
        return jsonify(_anthropic_error("request body must be a JSON object with a 'messages' field")), 400

    try:
        payload = _anthropic_request_to_openai(body)
    except (ValueError, TypeError, KeyError, AttributeError):
        return jsonify(_anthropic_error("Invalid Messages request")), 400
    token  = _caller_token()
    gate   = _admit_request(token, payload)
    if gate:
        response, code = gate
        error = response.get_json().get("error", {})
        translated = jsonify(_anthropic_error(error.get("message", "request denied") if isinstance(error, dict) else error))
        if response.headers.get("Retry-After"):
            translated.headers["Retry-After"] = response.headers["Retry-After"]
        return translated, code

    streaming = bool(body.get("stream", False))
    t_start   = time.time()
    result    = _route_completion(payload, streaming, _cache_ns())
    _record_request_tokens(token, payload, result)

    _log_completion(token, "messages", payload, result, time.time() - t_start)

    if result[0] == "json":
        return jsonify(_openai_response_to_anthropic(result[1])), 200
    if result[0] == "stream":
        _, gen, name = result
        response = Response(stream_with_context(_openai_stream_to_anthropic(gen)),
                            content_type="text/event-stream", headers={"X-Provider": name})
        response.call_on_close(gen.close)
        return response
    return jsonify(_anthropic_error(result[1].get("error", {}).get("message", "error"))), result[2]


@app.route("/v1/embeddings", methods=["POST"])
def embeddings():
    model_policy = _DISABLED_MODELS
    _req_ctx.deadline = time.monotonic() + REQUEST_TIMEOUT
    err = _auth_check()
    if err:
        return err

    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict) or "input" not in payload:
        return jsonify({"error": {"message": "request body must be a JSON object with an 'input' field",
                                  "type": "invalid_request_error"}}), 400

    token  = _caller_token()
    gate   = _admit_request(token, payload, embedding=True)
    if gate:
        return gate

    scope = _caller_scope()
    models = _caller_models()
    ordered = [p for p in _embed_ordered() if (scope is None or p["name"] in scope)
               and (models is None or p["embed_model"] in models)]
    if not ordered:
        _settle_request_usage(getattr(g, "usage_reservation", None), None, "", failed=True)
        return jsonify({"error": {"message": "no embedding-capable providers configured "
                                             "(set e.g. GEMINI_API_KEYS or MISTRAL_API_KEYS)",
                                  "type": "router_error"}}), 503

    # Embeddings are deterministic — identical input is a perfect cache hit.
    ns      = _cache_ns()
    t_start = time.time()
    cached  = cache.get(payload, ns)
    if cached is not None:
        log.info("↩ cache hit (embeddings)")
        request_log.append({
            "ts":         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "endpoint":   "embeddings",
            "caller":     token[-6:] if token else "anon",
            "streaming":  False,
            "complexity": None,
            "est_tokens": 0,
            "provider":   "cache",
            "model":      payload.get("model"),
            "latency_ms": round((time.time() - t_start) * 1000),
            "cascades":   0,
            "status":     "cache_hit",
            "prompt_tokens": None,
            "completion_tokens": None,
        })
        _settle_request_usage(getattr(g, "usage_reservation", None), cached.get("usage"), cached.get("model", ""), cached=True)
        return jsonify(cached)

    any_closed = any(not stats.breaker_open(p["name"]) for p in ordered)

    for provider in ordered:
        name = provider["name"]
        if any_closed and stats.breaker_open(name):
            log.info(f"⨂ skipping {name} embeddings (circuit open)")
            continue

        em = provider["embed_model"]
        attempts = pool.key_count(name, em) or 1
        for _ in range(attempts):
            if not _model_enabled(name, em):
                break
            key = pool.get_key(name, em)
            if not key:
                log.warning(f"All {name} keys cooling — skipping provider")
                break

            if time.monotonic() >= _req_ctx.deadline:
                _settle_request_usage(getattr(g, "usage_reservation", None), None, "", failed=True)
                return jsonify({"error": {"message": "Router request deadline exceeded", "type": "router_error"}}), 504
            log.info(f"→ Trying {name} embeddings ({em}) ...{key[-6:]}")
            t0   = time.time()
            resp = forward_embeddings(provider, key, payload)
            elapsed = time.time() - t0

            if resp is None:
                stats.record_error(name); stats.record_health(name, False)
                pool.mark_key_down(name, key, retry_after=30)
                continue
            if resp.status_code == 429:
                stats.record_error(name)
                pool.mark_rate_limited(name, key, em, retry_after=_parse_retry_after(resp.headers.get("Retry-After")))
                log.warning(f"  {name} 429 — cooldown, trying next key")
                resp.close()
                continue
            if resp.status_code in (400, 401, 403, 404):
                stats.record_error(name)   # request/auth/model-specific, not a health failure
                log.error(f"  {name} embeddings {resp.status_code} — skipping provider: {resp.text[:200]}")
                resp.close()
                break
            if resp.status_code >= 500:
                stats.record_error(name); stats.record_health(name, False)
                pool.mark_key_down(name, key, retry_after=15)
                resp.close()
                continue
            if not (200 <= resp.status_code < 300):
                stats.record_error(name); stats.record_health(name, False)
                log.warning(f"  {name} embeddings unexpected {resp.status_code} — skipping provider")
                resp.close()
                break

            stats.record_success(name, elapsed); stats.record_health(name, True)
            log.info(f"  ✓ {name} embeddings ({elapsed*1000:.0f}ms)")
            try:
                data = resp.json()
            finally:
                resp.close()
            _settle_request_usage(getattr(g, "usage_reservation", None), data.get("usage"), em)
            _add_provider_tokens(name, data)
            if _DISABLED_MODELS is model_policy:
                cache.set(payload, data, ns)
            request_log.append({
                "ts":         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "endpoint":   "embeddings",
                "caller":     token[-6:] if token else "anon",
                "streaming":  False,
                "complexity": None,
                "est_tokens": 0,
                "provider":   name,
                "model":      em,
                "latency_ms": round((time.time() - t_start) * 1000),
                "cascades":   0,
                "status":     "success",
                "prompt_tokens": (data.get("usage") or {}).get("total_tokens"),
                "completion_tokens": None,
            })
            return jsonify(data), 200

        log.warning(f"✗ {name} embeddings exhausted — cascading")

    request_log.append({
        "ts":         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "endpoint":   "embeddings",
        "caller":     token[-6:] if token else "anon",
        "streaming":  False,
        "complexity": None,
        "est_tokens": 0,
        "provider":   None,
        "model":      None,
        "latency_ms": round((time.time() - t_start) * 1000),
        "cascades":   0,
        "status":     "error",
        "prompt_tokens": None,
        "completion_tokens": None,
    })
    _settle_request_usage(getattr(g, "usage_reservation", None), None, "", failed=True)
    return jsonify({"error": {"message": "All embedding providers exhausted", "type": "router_error"}}), 503


# ── Feature add-ons ─────────────────────────────────────────────────────────────
# hermes-router separates CORE features (always on — the router's identity) from
# ADD-ONS (opt-in behaviors, each backed by an env var or some config). The
# registry below is the single source of truth: it powers the `features` block in
# /v1/status, the `hr features` CLI (which reads it and toggles the `env` flag in
# .env), and the dashboard. Env vars remain authoritative — this is just a unified
# view + friendly toggle, so behavior is unchanged whether or not you use it.
CORE_FEATURES = [
    "auth", "credential_pool", "key_rotation", "failover", "circuit_breaker",
    "smart_routing", "protocol_translation", "capability_probing", "token_counting",
    "request_guardrails", "usage_cost_tracking",
]

def _features_snapshot() -> dict:
    """Live core/add-on categorization for /v1/status and `hr features`.
    `enabled` is computed from the already-parsed config (env vars stay the source
    of truth). Flag add-ons carry env/on/off so the CLI can toggle them; config
    add-ons carry a `manage` command instead."""
    has_local = any(p["name"] == "local" for p in PROVIDERS)
    addons = [
        {"name": "response_cache", "title": "Response cache", "kind": "flag",
         "enabled": CACHE_TTL > 0, "env": "CACHE_TTL_SECONDS", "on": "300", "off": "0",
         "desc": "Serve identical requests from an in-memory TTL+LRU cache."},
        {"name": "semantic_cache", "title": "Semantic cache", "kind": "flag",
         "enabled": SEMANTIC_CACHE, "env": "SEMANTIC_CACHE", "on": "1", "off": "0",
         "desc": "Also serve cached answers for similar (not just identical) prompts."},
        {"name": "persistent_cache", "title": "Persistent cache", "kind": "flag",
         "enabled": cache.persistent, "env": "CACHE_PERSIST", "on": "1", "off": "0",
         "desc": "Mirror the cache to SQLite so it survives restarts."},
        {"name": "fast_routing", "title": "Fast routing", "kind": "flag",
         "enabled": FAST_ROUTE_TOKENS > 0, "env": "FAST_ROUTE_THRESHOLD", "on": "200", "off": "0",
         "desc": "Short requests prefer low-latency providers on ties."},
        {"name": "model_discovery", "title": "Model discovery", "kind": "flag",
         "enabled": AUTO_DISCOVER_MODELS, "env": "AUTO_DISCOVER_MODELS", "on": "1", "off": "0",
         "desc": "Refresh configured provider model lists from /models at startup, bounded by AUTO_DISCOVER_MODEL_LIMIT."},
        {"name": "metrics_auth", "title": "Metrics auth", "kind": "builtin",
         "enabled": True, "desc": "Administrator authentication is always required on /metrics."},
        {"name": "cost_currency", "title": "Cost currency conversion", "kind": "flag",
         "enabled": COST_FX_RATE > 0, "env": "COST_FX_RATE", "on": "83", "off": "0",
         "desc": "Show a second currency (e.g. INR) alongside USD spend."},
        {"name": "key_budgets", "title": "Per-key budgets & rate limits", "kind": "config",
         "enabled": KEY_LIMITS_ON, "manage": "hr limit set <key> --rpm/--req-day/--tokens-day/--cost-day",
         "desc": "Per-key RPM / daily request / token / cost ceilings."},
        {"name": "local_model", "title": "Local model provider", "kind": "config",
         "enabled": has_local, "manage": "hr model set local <model>",
         "desc": "Route to a model on your own machine (Ollama / LM Studio / llama.cpp)."},
        {"name": "request_log", "title": "Request log", "kind": "flag",
         "enabled": request_log.enabled, "env": "REQUEST_LOG_SIZE", "on": "500", "off": "0",
         "desc": f"In-memory ring buffer of the last {REQUEST_LOG_SIZE} requests. No disk writes. Query via GET /v1/logs."},
        {"name": "dashboard", "title": "Monitoring dashboard", "kind": "builtin",
         "enabled": True,
         "desc": "Browser-based live dashboard at /dashboard — provider health, request log, cache stats, key usage."},
    ]
    return {"core": CORE_FEATURES, "addons": addons}


# ── Config-write endpoints (web dashboard) ──────────────────────────────────────
def _discovery_worker():
    global _DISCOVERY_WORKER_RUNNING
    while True:
        with _ACCESS_LOCK:
            if not _DISCOVERY_QUEUE:
                _DISCOVERY_WORKER_RUNNING = False
                return
            name, (generation, provider) = _DISCOVERY_QUEUE.popitem(last=False)
            snapshot = {**provider, "keys": list(provider.get("keys", []))}
            manual = list(provider.get("_manual_models", _provider_models(provider)))
            confirmed = _free_tier_confirmed(provider)
            _DISCOVERY_STATUS[name] = {"state": "running", "message": "Looking for free chat models…"}
        # Let the startup owner publish its snapshots before live discoveries.
        deadline = time.monotonic() + 30
        while (_INITIALIZATION_STATE.get("state") == "initializing"
               and time.monotonic() < deadline):
            time.sleep(.1)
        try:
            if _INITIALIZATION_STATE.get("state") == "initializing":
                raise RuntimeError("Startup still initializing")
            selected = _read_free_catalog(snapshot, snapshot["keys"][0])
            error = None
        except Exception:
            selected = None
            error = "Could not read the provider catalog. Existing models are unchanged; retry discovery."
        with _ACCESS_LOCK:
            if (generation != _DISCOVERY_GENERATIONS[name]
                    or not any(p is provider for p in PROVIDERS)
                    or provider.get("keys") != snapshot["keys"]
                    or provider.get("_manual_models", _provider_models(provider)) != manual
                    or _free_tier_confirmed(provider) != confirmed
                    or not _provider_model_discovery_enabled(provider)):
                if generation == _DISCOVERY_GENERATIONS[name]:
                    _DISCOVERY_STATUS[name] = {"state": "cancelled", "message": "Configuration changed. Retry discovery."}
                continue
            if error:
                _DISCOVERY_STATUS[name] = {"state": "error", "message": error}
            else:
                try:
                    _publish_free_catalog(provider, selected, pool)
                except Exception:
                    _DISCOVERY_STATUS[name] = {"state": "error", "message": "Could not apply catalog. Retry discovery."}


@_locked_access
def _queue_free_discovery(provider):
    global _DISCOVERY_WORKER_RUNNING
    name = provider["name"]
    _DISCOVERY_GENERATIONS[name] += 1
    _DISCOVERY_QUEUE[name] = (_DISCOVERY_GENERATIONS[name], provider)
    _DISCOVERY_STATUS[name] = {"state": "queued", "message": "Free model discovery queued."}
    if not _DISCOVERY_WORKER_RUNNING:
        _DISCOVERY_WORKER_RUNNING = True
        try:
            threading.Thread(target=_discovery_worker, daemon=True).start()
        except RuntimeError:
            _DISCOVERY_WORKER_RUNNING = False
            _DISCOVERY_STATUS[name] = {"state": "error", "message": "Could not start discovery. Retry."}


@_locked_access
def _activate_discovery_provider(name):
    """Activate a saved key for discovery without rebuilding other providers."""
    global _AUTH_KEYS
    _AUTH_KEYS = _load_auth_json()
    current = next((p for p in PROVIDERS if p["name"] == name), None)
    env = PROVIDER_MODEL_ENV[name]
    if current is None and _env_read_line(env) is None and env not in os.environ:
        # A provider's paid default is not an explicit model selection.
        _env_write_line(env, "")
        os.environ[env] = ""
    fresh = next((p for p in _build_providers() if p["name"] == name), None)
    if fresh is None:
        raise ValueError("Provider has no usable saved key")
    if current is None:
        current = fresh
        PROVIDERS.append(current)
    else:
        current["keys"] = fresh["keys"]
    for model in _registered_models(current):
        pool.ensure_model(name, model, current["keys"])
    return current


@_locked_access
def _configure_free_discovery(name, free_tier=None):
    settings = {name.upper() + "_AUTO_DISCOVER_MODELS": "1",
                name.upper() + "_AUTO_DISCOVER_FREE_MODELS": "1"}
    if free_tier is not None:
        settings[name.upper() + "_DISCOVERY_FREE_TIER"] = "1" if free_tier else "0"
    for env, value in settings.items():
        _env_write_line(env, value)
        os.environ[env] = value


@app.route("/v1/config/providers/<provider>/discover", methods=["POST"])
@_locked_access
def config_discover_free(provider):
    err = _admin_check()
    if err:
        return err
    current = next((p for p in PROVIDERS if p["name"] == provider and p.get("keys")), None)
    if current is None:
        return jsonify({"error": {"message": "Save a provider key first."}}), 404
    body = request.get_json(silent=True)
    if (not isinstance(body, dict) or set(body) - {"free_tier_confirmed"}
            or ("free_tier_confirmed" in body and type(body["free_tier_confirmed"]) is not bool)
            or (body.get("free_tier_confirmed") and provider not in model_discovery.FREE_TIER_CHAT_MODELS)):
        return jsonify({"error": {"message": "Unsupported free-plan confirmation."}}), 400
    _configure_free_discovery(provider, body.get("free_tier_confirmed"))
    _queue_free_discovery(current)
    return jsonify({"provider": provider, "discovery": "queued", "restart_required": False}), 202


# These back the dashboard's "Add key" / "Model" / add-on toggle forms. Same
# administrator access as /v1/status; application inference keys cannot change config.
# Every write is a plain, auditable file edit (.env or auth.json), identical to
# what `hr auth add` / `hr model set` / `hr features enable` already produce —
# nothing here is a new mechanism, just an HTTP front-end for the same files.
# Changes take effect after a restart; the dashboard prompts for one via
# POST /v1/config/restart.

@app.route("/v1/config/providers")
def config_providers():
    """List of providers the dashboard can build add-key / set-model forms for,
    plus which ones accept a plain key vs. model-only (codex/local)."""
    err = _admin_check()
    if err:
        return err
    return jsonify({
        "key_settable": KEY_SETTABLE_PROVIDERS,
        "model_settable": list(PROVIDER_MODEL_ENV.keys()),
        "defaults": PROVIDER_MODEL_DEFAULT,
        "presets": PROVIDER_PRESETS,
        "discovery": dict(_DISCOVERY_STATUS),
        "free_tier_providers": sorted(model_discovery.FREE_TIER_CHAT_MODELS),
        "free_tier_confirmed": {p["name"]: _free_tier_confirmed(p) for p in PROVIDERS},
        "configured_models": {p: _env_read_line(env) or os.environ.get(env, "")
                              for p, env in PROVIDER_MODEL_ENV.items()},
        # Live key count per currently-configured provider, e.g. {"gemini": 6} —
        # informational context for the Access Keys page's provider picker, not
        # an enforced quota split (see monitoring docs on provider scoping).
        "key_counts": {p["name"]: len(p.get("keys", [])) for p in PROVIDERS},
        "copyable_provider_keys": _provider_key_counts_for_instance_copy(),
    })


@app.route("/v1/config/providers/<provider>/models", methods=["POST"])
def config_provider_models(provider):
    """Explicit catalog lookup; does not save keys or enable discovered models."""
    err = _admin_check()
    if err:
        return err
    preset = PROVIDER_PRESETS.get(provider)
    if not preset:
        return jsonify({"error": {"message": "Unknown provider"}}), 404
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": {"message": "Expected an object"}}), 400
    key = body.get("key", "")
    if not isinstance(key, str) or len(key) > 16384 or any(c in key for c in "\r\n"):
        return jsonify({"error": {"message": "Invalid API key"}}), 400
    key = key.strip()
    if not key:
        keys = _keys_for_instance_copy(provider)
        key = keys[0] if keys else ""
    if not key:
        return jsonify({"error": {"message": "Paste an API key first"}}), 400
    headers = {"Authorization": "Bearer " + key}
    if provider == "anthropic":
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
    try:
        # Fixed public preset host; reject redirects so a key never follows one.
        with _HTTP.get(preset["base_url"].rstrip("/") + "/models", headers=headers,
                       timeout=(3, 8), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                return jsonify({"error": {"message": "Provider could not list models (HTTP "
                    + str(response.status_code) + "). Check the key or use the suggestions below."}}), 502
            chunks, size = [], 0
            deadline = time.monotonic() + 12
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 2_000_000 or time.monotonic() > deadline:
                    raise ValueError("catalog limit")
                chunks.append(chunk)
            payload = json.loads(b"".join(chunks))
        items = payload.get("data", [])
        if not isinstance(items, list):
            raise ValueError("invalid catalog")
        models = set()
        for item in items[:5000]:
            model = item.get("id") if isinstance(item, dict) else None
            if provider == "gemini" and isinstance(model, str):
                model = model.removeprefix("models/")
            if isinstance(model, str) and len(model) <= 200 and re.fullmatch(r"[A-Za-z0-9@+._\-:/]+", model):
                models.add(model)
        if not models:
            return jsonify({"error": {"message": "No compatible model IDs returned. Use the suggestions or enter a model ID."}}), 502
        return jsonify({"models": sorted(models), "source": "live",
                        "notice": "This list may include paid models. Check prices before selecting."})
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        # Do not return upstream bodies or exception text: either may echo secrets.
        return jsonify({"error": {"message": "Could not load the provider catalog. Use the suggestions or enter a model ID."}}), 502


@app.route("/v1/config/models/enabled", methods=["POST"])
@_locked_access
def config_model_enabled():
    """Persist one model switch and publish it live; never restart the router."""
    global _DISABLED_MODELS
    err = _admin_check()
    if err:
        return err
    body = request.get_json(silent=True)
    if (not isinstance(body, dict) or set(body) != {"provider", "model", "enabled"}
            or not isinstance(body["provider"], str) or not isinstance(body["model"], str)
            or any(not body[k] or len(body[k]) > 512 or any(ord(c) < 32 for c in body[k])
                   for k in ("provider", "model"))
            or type(body["enabled"]) is not bool):
        return jsonify({"error": {"message": "Supply provider, model and a boolean enabled"}}), 400
    provider = next((p for p in PROVIDERS if p["name"] == body["provider"]), None)
    if provider is None or body["model"] not in _registered_models(provider):
        return jsonify({"error": {"message": "Unknown registered provider/model"}}), 404
    disabled = set(_DISABLED_MODELS)
    identity = (provider["name"], body["model"])
    if body["enabled"]:
        disabled.discard(identity)
    else:
        disabled.add(identity)
    if len(disabled) > 10000:
        return jsonify({"error": {"message": "Model routing policy is full"}}), 400
    if disabled != _DISABLED_MODELS:
        try:
            _atomic_write_text(MODEL_POLICY_FILE,
                               json.dumps({"disabled_models": sorted(disabled)}) + "\n")
        except OSError:
            log.error("Could not save model routing policy")
            return jsonify({"error": {"message": "Could not save model switch. Routing is unchanged."}}), 503
        _DISABLED_MODELS = frozenset(disabled)
    return jsonify({**body, "restart_required": False, "routing_revision": _model_policy_revision()})


@app.route("/v1/config/keys/<provider>", methods=["POST"])
@_locked_access
def config_add_key(provider):
    """Add one API key for a provider to auth.json. Body: {"key": "..."}"""
    err = _admin_check()
    if err:
        return err
    if provider not in KEY_SETTABLE_PROVIDERS:
        return jsonify({"error": {"message": f"unknown or non-key provider: {provider}",
                                  "type": "invalid_request_error"}}), 400
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": {"message": "Expected an object"}}), 400
    discover = body.get("discover_free", AUTO_DISCOVER_MODELS and AUTO_DISCOVER_FREE_MODELS)
    free_tier = body.get("free_tier_confirmed")
    if (type(discover) is not bool or (free_tier is not None and type(free_tier) is not bool)
            or (free_tier and provider not in model_discovery.FREE_TIER_CHAT_MODELS)):
        return jsonify({"error": {"message": "Invalid discovery or free-plan option."}}), 400
    key = body.get("key")
    if not isinstance(key, str):
        return jsonify({"error": {"message": "key must be text"}}), 400
    key = key.strip()
    if not key:
        return jsonify({"error": {"message": "missing 'key'", "type": "invalid_request_error"}}), 400
    if len(key) > 16384 or "\n" in key or "\r" in key:
        return jsonify({"error": {"message": "key must not contain newlines", "type": "invalid_request_error"}}), 400
    added, total = _auth_json_add_key(provider, key)
    if discover:
        _configure_free_discovery(provider, free_tier)
        current = _activate_discovery_provider(provider)
        _queue_free_discovery(current)
        return jsonify({"provider": provider, "added": added, "total_keys": total,
                        "duplicate": not added, "discovery": "queued", "restart_required": False})
    return jsonify({"provider": provider, "added": added, "total_keys": total,
                    "duplicate": not added, "restart_required": added})


@app.route("/v1/config/model/<provider>", methods=["POST", "DELETE"])
@_locked_access
def config_model(provider):
    """POST {"model": "m1,m2"} to override a provider's model(s); DELETE to reset
    to the built-in default (just removes the .env override line)."""
    err = _admin_check()
    if err:
        return err
    env_var = PROVIDER_MODEL_ENV.get(provider)
    if not env_var:
        return jsonify({"error": {"message": f"unknown provider: {provider}",
                                  "type": "invalid_request_error"}}), 400

    if request.method == "DELETE":
        _env_write_line(env_var, None)
        _cancel_free_discovery(provider)
        return jsonify({"provider": provider, "reset": True, "restart_required": True})

    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": {"message": "Expected an object"}}), 400
    model = body.get("model")
    if not isinstance(model, str):
        return jsonify({"error": {"message": "model must be text"}}), 400
    model = model.strip()
    if not model:
        return jsonify({"error": {"message": "missing 'model'", "type": "invalid_request_error"}}), 400
    if any(c in model for c in "\n\r"):
        return jsonify({"error": {"message": "model must not contain newlines", "type": "invalid_request_error"}}), 400
    if not re.fullmatch(r"[A-Za-z0-9@+._\-:/, ]+", model):
        return jsonify({"error": {"message": "model contains unsupported characters",
                                  "type": "invalid_request_error"}}), 400
    _env_write_line(env_var, model)
    _cancel_free_discovery(provider)
    return jsonify({"provider": provider, "model": model, "restart_required": True})


def _cancel_free_discovery(name):
    """A pending manual edit must win over a catalog fetched earlier."""
    _DISCOVERY_GENERATIONS[name] += 1
    _DISCOVERY_QUEUE.pop(name, None)
    _DISCOVERY_STATUS[name] = {"state": "cancelled", "message": "Model selection changed."}


@app.route("/v1/config/features/<name>", methods=["POST"])
def config_feature(name):
    """Toggle a flag-kind add-on on/off. Body: {"enabled": true|false}. Config-kind
    add-ons (per-key budgets, local model) aren't simple flag writes — use their
    own command (`hr limit`, `hr model set local ...`), matching `hr features`."""
    err = _admin_check()
    if err:
        return err
    addon = next((a for a in _features_snapshot()["addons"] if a["name"] == name), None)
    if not addon:
        return jsonify({"error": {"message": f"unknown add-on: {name}", "type": "invalid_request_error"}}), 404
    if addon.get("kind") != "flag":
        return jsonify({"error": {"message": f"'{name}' isn't a simple toggle — manage it with: {addon.get('manage', '(see docs)')}",
                                  "type": "invalid_request_error"}}), 400

    global AUTO_DISCOVER_MODELS
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or type(body.get("enabled")) is not bool:
        return jsonify({"error": {"message": "enabled must be true or false"}}), 400
    enabled = body["enabled"]
    _env_write_line(addon["env"], addon["on"] if enabled else addon["off"])
    if name == "model_discovery":
        AUTO_DISCOVER_MODELS = enabled
        for provider in list(PROVIDERS):
            if _provider_model_discovery_enabled(provider) and _free_discovery_enabled(provider):
                _queue_free_discovery(provider)
        return jsonify({"name": name, "enabled": enabled, "restart_required": False})
    return jsonify({"name": name, "enabled": enabled, "restart_required": True})


@app.route("/v1/config/rotation", methods=["POST"])
def config_rotation():
    """Set key rotation mode from the dashboard. Body: {"mode": "round-robin"|"sequential"}."""
    err = _admin_check()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    mode = (body.get("mode") or "").strip().lower()
    aliases = {"roundrobin": "round-robin", "round_robin": "round-robin", "rr": "round-robin",
               "seq": "sequential"}
    mode = aliases.get(mode, mode)
    if mode not in ("round-robin", "sequential"):
        return jsonify({"error": {"message": "mode must be 'round-robin' or 'sequential'",
                                  "type": "invalid_request_error"}}), 400
    _env_write_line("ROTATION_MODE", mode)
    return jsonify({"mode": mode, "restart_required": True})


@app.route("/v1/config/restart", methods=["POST"])
def config_restart():
    """Restart the router so config changes take effect. Responds immediately;
    the actual restart happens ~1s later so this response reaches the client."""
    err = _admin_check()
    if err:
        return err
    if not _trigger_restart():
        return jsonify({"supported": False, "error": {"message": "automatic restart is unavailable; restart using your process manager or start.ps1", "type": "unsupported_operation"}}), 409
    return jsonify({"status": "restarting", "message": "Router restarting — this page will reconnect shortly."})


def _parse_limit_fields(body: dict) -> tuple[dict, str | None]:
    """Validate rpm/req_per_day/tokens_per_day/cost_per_day from a request body.
    Returns (limits, error) — only includes fields the caller actually sent, so a
    partial update doesn't zero out fields left unset (0 itself is a valid,
    meaningful 'unlimited' value and is kept distinct from 'not provided')."""
    out: dict = {}
    for f in ("rpm", "req_per_day", "tokens_per_day"):
        if f in body and body[f] not in (None, ""):
            try:
                v = int(body[f])
            except (TypeError, ValueError):
                return {}, f"'{f}' must be a whole number"
            if isinstance(body[f], bool) or str(v) != str(body[f]).strip():
                return {}, f"'{f}' must be a whole number"
            if v < 0:
                return {}, f"'{f}' must not be negative"
            out[f] = v
    if "cost_per_day" in body and body["cost_per_day"] not in (None, ""):
        try:
            v = float(body["cost_per_day"])
        except (TypeError, ValueError):
            return {}, "'cost_per_day' must be a number"
        if not __import__("math").isfinite(v) or v < 0:
            return {}, "'cost_per_day' must be finite and not negative"
        out["cost_per_day"] = v
    return out, None


def _parse_allowed_providers(body: dict) -> tuple[dict, str | None]:
    """Validate 'allowed_providers' from a request body. Returns a patch dict
    (empty if the field wasn't sent at all — leaves any existing value untouched
    on an update) and an error message or None. An explicit empty list / null
    means 'unrestricted', matching KEY_PROVIDER_SCOPE's loader semantics."""
    if "allowed_providers" not in body:
        return {}, None
    val = [] if body["allowed_providers"] is None else body["allowed_providers"]
    if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
        return {}, "'allowed_providers' must be a list of provider names"
    known = set(PROVIDER_MODEL_ENV.keys())
    unknown = [x for x in val if x not in known]
    if unknown:
        return {}, f"unknown provider(s): {', '.join(unknown)}"
    return {"allowed_providers": val}, None


def _parse_allowed_models(body):
    if "allowed_models" not in body:
        return {}, None
    value = body["allowed_models"]
    if value is None:
        value = []
    if not isinstance(value, list) or any(not isinstance(m, str) or not m.strip() or len(m) > 200 or any(c in m for c in "\r\n") for m in value):
        return {}, "'allowed_models' must be a list of model IDs"
    return {"allowed_models": value}, None


@app.route("/v1/config/proxy-keys")
def config_list_proxy_keys():
    """List every proxy (access) key — the credential CALLERS use to authenticate
    to this router, distinct from the provider keys under /v1/config/keys. Shows
    tail, optional name, limits, and live usage. Reads fresh from .env/auth.json
    (not the process's own stale PROXY_API_KEYS) so a just-created or just-revoked
    key shows immediately, flagged pending until a restart actually applies it."""
    err = _admin_check()
    if err:
        return err
    live_keys = _read_proxy_api_keys_live()
    meta = _read_proxy_keys_meta()
    active_now = set(PROXY_API_KEYS)
    out = []
    for k in live_keys:
        spec = meta.get(k, {})
        out.append({
            "key_tail": k[-6:],
            "name": spec.get("name", ""),
            "limits": {
                "rpm":            spec.get("rpm", 0) or 0,
                "req_per_day":    spec.get("req_per_day", 0) or 0,
                "tokens_per_day": spec.get("tokens_per_day", 0) or 0,
                "cost_per_day":   spec.get("cost_per_day", 0) or 0,
            },
            "allowed_providers": spec.get("allowed_providers") or [],
            "allowed_models": spec.get("allowed_models") or [],
            "usage": key_usage.snapshot(k),
            "pending_restart": k not in active_now,
        })
    return jsonify({"keys": out})


@app.route("/v1/config/proxy-keys", methods=["POST"])
@_locked_access
def config_create_proxy_key():
    """Mint a new proxy key for a teammate/other app to call the router with.
    Body: {"name": "...", "rpm": N, "req_per_day": N, "tokens_per_day": N,
    "cost_per_day": N, "allowed_providers": ["gemini",...]} — all optional;
    omitted limits fall back to the PROXY_LIMIT_* env defaults (0/unset =
    unlimited); an empty/omitted allowed_providers means unrestricted (can use
    any configured provider). Returns the plaintext key ONCE — like every other
    key in this codebase, only its tail is shown again."""
    err = _admin_check()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    name = str(body.get("name") or "").strip()[:80]
    if "\n" in name or "\r" in name:
        return jsonify({"error": {"message": "name must not contain newlines", "type": "invalid_request_error"}}), 400
    limits, verr = _parse_limit_fields(body)
    if verr:
        return jsonify({"error": {"message": verr, "type": "invalid_request_error"}}), 400
    scope, serr = _parse_allowed_providers(body)
    if serr:
        return jsonify({"error": {"message": serr, "type": "invalid_request_error"}}), 400

    model_scope, model_error = _parse_allowed_models(body)
    if model_error:
        return jsonify({"error": {"message": model_error, "type": "invalid_request_error"}}), 400
    patch = {**limits, **scope, **model_scope}
    live_keys = _read_proxy_api_keys_live()
    new_key = _generate_proxy_key()
    while new_key in live_keys:   # astronomically unlikely; stay correct anyway
        new_key = _generate_proxy_key()
    live_keys.append(new_key)
    _env_write_line("PROXY_API_KEYS", ",".join(live_keys))

    if name:
        patch["name"] = name
    if patch:
        _write_proxy_key_meta(new_key, patch)

    _persist_access_keys(live_keys)
    return jsonify({"key": new_key, "key_tail": new_key[-6:], "name": name,
                    "limits": limits, "allowed_providers": scope.get("allowed_providers", []),
                    "allowed_models": model_scope.get("allowed_models", []),
                    "restart_required": False})


@app.route("/v1/config/proxy-keys/<tail>", methods=["POST"])
@_locked_access
def config_update_proxy_key(tail):
    """Update the name/limits of an existing proxy key, found by its last-6-char
    tail. Body: same shape as create. Only fields present in the body change —
    others keep their current value."""
    err = _admin_check()
    if err:
        return err
    live_keys = _read_proxy_api_keys_live()
    key = _resolve_proxy_key_by_tail(tail, live_keys)
    if not key:
        return jsonify({"error": {"message": f"no proxy key ending in '{tail}'",
                                  "type": "invalid_request_error"}}), 404

    body = request.get_json(force=True, silent=True) or {}
    limits, verr = _parse_limit_fields(body)
    if verr:
        return jsonify({"error": {"message": verr, "type": "invalid_request_error"}}), 400
    scope, serr = _parse_allowed_providers(body)
    if serr:
        return jsonify({"error": {"message": serr, "type": "invalid_request_error"}}), 400

    model_scope, model_error = _parse_allowed_models(body)
    if model_error:
        return jsonify({"error": {"message": model_error, "type": "invalid_request_error"}}), 400
    patch = {**limits, **scope, **model_scope}
    if "name" in body:
        name = str(body.get("name") or "").strip()[:80]
        if "\n" in name or "\r" in name:
            return jsonify({"error": {"message": "name must not contain newlines",
                                      "type": "invalid_request_error"}}), 400
        patch["name"] = name
    if patch:
        _write_proxy_key_meta(key, patch)
    _reload_access_policy()
    return jsonify({"key_tail": tail, "restart_required": False})


@app.route("/v1/config/proxy-keys/<tail>", methods=["DELETE"])
@_locked_access
def config_delete_proxy_key(tail):
    """Revoke an application key immediately; owner access is independent."""
    err = _admin_check()
    if err:
        return err
    live_keys = _read_proxy_api_keys_live()
    key = _resolve_proxy_key_by_tail(tail, live_keys)
    if not key:
        return jsonify({"error": {"message": f"no proxy key ending in '{tail}'",
                                  "type": "invalid_request_error"}}), 404
    live_keys.remove(key)
    _env_write_line("PROXY_API_KEYS", ",".join(live_keys))
    _persist_access_keys(live_keys)
    _delete_proxy_key_meta(key)
    return jsonify({"key_tail": tail, "revoked": True, "restart_required": False})


@app.route("/v1/instances")
def list_instances():
    """List registered Hermes Router instances and live health/Docker state.
    Secrets are never returned; callers only see key tails and env var names."""
    err = _admin_check()
    if err:
        return err
    with _INSTANCE_LOCK:
        doc = _read_instances_doc()
        entries = list(doc.get("instances", []))
    return jsonify({"instances": [_instance_public(e) for e in entries]})


@app.route("/v1/instances", methods=["POST"])
def create_instance():
    """Register an external instance or define/start a managed Docker instance.
    Body: {name, mode, base_url, host_port, container_port, image, api_key, env,
    start}. Docker mode generates an API key when api_key is omitted."""
    err = _admin_check()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    entry, verr = _build_instance_from_body(body)
    if verr:
        return jsonify({"error": {"message": verr, "type": "invalid_request_error"}}), 400
    with _INSTANCE_LOCK:
        doc = _read_instances_doc()
        doc.setdefault("instances", []).append(entry)
        _write_instances_doc(doc)

    action_result = None
    if entry["mode"] == "docker" and bool(body.get("start")):
        ok, msg = _docker_action(entry, "start")
        action_result = {"ok": ok, "message": msg}
    provided_api_key = str(body.get("api_key") or "").strip()
    return jsonify({
        "instance": _instance_public(entry),
        "generated_api_key": entry["api_key"] if entry["mode"] == "docker" and not provided_api_key else None,
        "action": action_result,
    }), 201


@app.route("/v1/instances/<instance_id>", methods=["POST"])
def update_instance(instance_id):
    """Update a registered instance. Fields omitted from the body keep their
    previous values. Existing Docker containers are not recreated automatically."""
    err = _admin_check()
    if err:
        return err
    body = request.get_json(force=True, silent=True) or {}
    with _INSTANCE_LOCK:
        doc = _read_instances_doc()
        idx, current = _find_instance(doc, instance_id)
        if current is None:
            return jsonify({"error": {"message": f"unknown instance: {instance_id}",
                                      "type": "invalid_request_error"}}), 404
        entry, verr = _build_instance_from_body(body, current)
        if verr:
            return jsonify({"error": {"message": verr, "type": "invalid_request_error"}}), 400
        doc["instances"][idx] = entry
        _write_instances_doc(doc)
    return jsonify({"instance": _instance_public(entry)})


@app.route("/v1/instances/<instance_id>", methods=["DELETE"])
def delete_instance(instance_id):
    """Remove an instance from the registry. Pass ?remove_container=1 to also
    remove a managed Docker container."""
    err = _admin_check()
    if err:
        return err
    remove_container = request.args.get("remove_container", "").lower() in ("1", "true", "yes")
    removed = None
    with _INSTANCE_LOCK:
        doc = _read_instances_doc()
        idx, entry = _find_instance(doc, instance_id)
        if entry is None:
            return jsonify({"error": {"message": f"unknown instance: {instance_id}",
                                      "type": "invalid_request_error"}}), 404
        removed = doc["instances"].pop(idx)
        _write_instances_doc(doc)

    action_result = None
    if remove_container and removed.get("mode") == "docker":
        ok, msg, _ = _docker_cmd(["rm", "-f", removed["container_name"]], timeout=20)
        action_result = {"ok": ok, "message": msg}
    return jsonify({"deleted": True, "id": instance_id, "action": action_result})


@app.route("/v1/instances/<instance_id>/<action>", methods=["POST"])
def instance_action(instance_id, action):
    """Start, stop, or restart a managed Docker instance."""
    err = _admin_check()
    if err:
        return err
    if action not in ("start", "stop", "restart"):
        return jsonify({"error": {"message": "action must be start, stop, or restart",
                                  "type": "invalid_request_error"}}), 400
    with _INSTANCE_LOCK:
        doc = _read_instances_doc()
        _, entry = _find_instance(doc, instance_id)
    if entry is None:
        return jsonify({"error": {"message": f"unknown instance: {instance_id}",
                                  "type": "invalid_request_error"}}), 404
    ok, msg = _docker_action(entry, action)
    if not ok:
        return jsonify({"ok": False, "message": msg, "instance": _instance_public(entry)}), 409
    return jsonify({"ok": True, "message": msg, "instance": _instance_public(entry)})


@app.route("/v1/status")
def status():
    """Show key cooldown state, latency/error stats, and cache metrics."""
    err = _admin_check()
    if err:
        return err

    now  = time.time()
    keys = {}
    with pool.lock:
        for name, model_pools in pool.pools.items():
            # Representative key status from the provider's primary model bucket
            # (insertion order → models[0]); per-model buckets share the same keys.
            primary = next(iter(model_pools), None)
            entries = model_pools.get(primary, []) if primary else []
            keys[name] = [
                {
                    "key_tail": e["key"][-6:],
                    "status":   "cooling" if e["cool_until"] > now else "ready",
                    "ready_in": max(0, round(e["cool_until"] - now)),
                    "requests": pool.key_requests_for(name, e["key"]),
                }
                for e in entries
            ]

    provider_stats = {}
    for p in PROVIDERS:
        entry = {
            "discovery": _DISCOVERY_STATUS.get(p["name"]),
            "model_states": [{"model": m, "enabled": _model_enabled(p["name"], m),
                              "kind": "chat" if m in _provider_models(p) else "embeddings"}
                             for m in _registered_models(p)],
            "keys":  keys.get(p["name"], []),
            "stats": stats.summary(p["name"]),
            "breaker": stats.breaker_status(p["name"]),
            "tokens": _provider_tokens.get(p["name"], 0),
            "cost_usd": round(_provider_cost.get(p["name"], 0.0), 6),
        }
        # Surface the internal routing signals (rating + probe latency + model)
        # so dashboards can show them. Added only when known, so un-probed
        # providers still fall back to the dashboard's "?"/"—" placeholders.
        st = _provider_state.get(p["name"], {})
        if st.get("rating") is not None:
            entry["rating"] = st["rating"]
        if st.get("latency_ms"):
            entry["latency_ms"] = st["latency_ms"]
        if st.get("model"):
            entry["model"] = st["model"]
        if p.get("models"):
            entry["models"] = p["models"]
            # Per-model capability breakdown (rating + tool/reasoning support), so
            # dashboards can show why a non-primary model gets picked for hard turns.
            entry["model_caps"] = [
                {"model": m, **_model_caps(p["name"], m),
                 "supports_vision": _model_supports_vision(p, m)} for m in p["models"]]
        if "available" in st:
            entry["available"] = st["available"]
        if "supports_tools" in st:
            entry["supports_tools"] = st["supports_tools"]
        if "tools_confirmed" in st:
            entry["tools_confirmed"] = st["tools_confirmed"]
        if "reasoning" in st:
            entry["reasoning"] = st["reasoning"]
        if p.get("skip_if_tokens_over"):
            entry["skip_if_tokens_over"] = p["skip_if_tokens_over"]
        if p.get("max_output_tokens"):
            entry["max_output_tokens"] = p["max_output_tokens"]
        provider_stats[p["name"]] = entry

    return jsonify({
        "providers": provider_stats,
        "context_window": _CONTEXT_CONFIG["context_window"],
        "context_models": [
            {"id": p["name"] + "::" + m, "provider": p["name"], "model": m,
             "limits": _context_limits(p, m, _CONTEXT_CONFIG),
             "overridden": p["name"] + "::" + m in _CONTEXT_CONFIG["model_limits"]}
            for p in PROVIDERS for m in _provider_models(p)],
        "readiness": _readiness(),
        "cache": {
            "enabled":    CACHE_TTL > 0,
            "ttl_s":      CACHE_TTL,
            "size":       cache.size,
            "max_size":   CACHE_MAX_SIZE,
            "persistent": cache.persistent,
            "hits":       cache.hits,
            "misses":     cache.misses,
            "hit_rate":   cache.hit_rate,
            "semantic": {
                "enabled":   SEMANTIC_CACHE,
                "threshold": SEMANTIC_THRESHOLD,
                "hits":      cache.semantic_hits,
            },
        },
        "fast_routing": {
            "enabled":         FAST_ROUTE_TOKENS > 0,
            "threshold_tokens": FAST_ROUTE_TOKENS,
            "fast_providers":  sorted(_FAST_PROVIDERS),
        },
        "rotation": {
            "mode": ROTATION_MODE,
        },
        "limits": {
            "enabled": KEY_LIMITS_ON,
            "keys": ([
                {"key_tail": k[-6:], "limits": KEY_LIMITS[k], "usage": key_usage.snapshot(k)}
                for k in PROXY_API_KEYS
            ] if KEY_LIMITS_ON else []),
        },
        "circuit_breaker": {
            "window":      BREAKER_WINDOW,
            "min_samples": BREAKER_MIN_SAMPLES,
            "error_rate":  BREAKER_ERROR_RATE,
            "cooldown_s":  BREAKER_COOLDOWN,
        },
        "features": _features_snapshot(),
    })


@app.route("/v1/usage")
def usage():
    """Owner sees global totals; application callers see only their own ledger."""
    if _caller_token() != ADMIN_API_KEY and not (not _caller_token() and _session_record()):
        err = _auth_check()
        if err:
            return err
        own = key_usage.snapshot(_caller_token())
        return jsonify({"keys": [{"key_tail": _caller_token()[-6:], **own}], "providers": {},
                        "totals": {"tokens": own["tokens_total"], "cost": _cost_obj(own["cost_total"])},
                        "budget_policy": "estimated-spend admission; not an invoice guarantee"})
    err = _admin_check()
    if err:
        return err

    providers = {}
    for p in PROVIDERS:
        s = stats.summary(p["name"])
        providers[p["name"]] = {
            "requests": s["total_requests"],
            "errors":   s["errors"],
            "tokens":   _provider_tokens.get(p["name"], 0),
            "cost":     _cost_obj(_provider_cost.get(p["name"], 0.0)),
        }
    keys = [{"key_tail": k[-6:], **key_usage.snapshot(k)} for k in PROXY_API_KEYS]

    return jsonify({
        "uptime_s":  round(time.time() - START_TIME),
        "totals":    {"tokens": sum(_provider_tokens.values()),
                      "cost":   _cost_obj(sum(_provider_cost.values()))},
        "providers": providers,
        "keys":      keys,
        "cache": {
            "hits":          cache.hits,
            "misses":        cache.misses,
            "hit_rate":      cache.hit_rate,
            "semantic_hits": cache.semantic_hits,
        },
    })


@app.route("/v1/logs")
def logs():
    """In-memory request log — last REQUEST_LOG_SIZE entries, most recent first.

    Never writes to disk. Returns an empty list when REQUEST_LOG_SIZE=0.

    Query params (all optional):
      limit=N          Max entries to return (default 100, capped at REQUEST_LOG_SIZE)
      provider=name    Filter by provider name (e.g. "gemini", "anthropic", "cache")
      status=s         Filter by status: success | error | cache_hit
      endpoint=e       Filter by endpoint: chat | messages | embeddings
    """
    err = _admin_check()
    if err:
        return err

    try:
        limit = min(int(request.args.get("limit", 100)), max(1, REQUEST_LOG_SIZE))
    except (TypeError, ValueError):
        limit = 100
    provider = request.args.get("provider") or None
    status   = request.args.get("status")   or None
    endpoint = request.args.get("endpoint") or None

    valid_statuses  = {"success", "error", "cache_hit"}
    valid_endpoints = {"chat", "messages", "embeddings"}
    if status and status not in valid_statuses:
        return jsonify({"error": {"message": f"status must be one of {sorted(valid_statuses)}",
                                  "type": "invalid_request_error"}}), 400
    if endpoint and endpoint not in valid_endpoints:
        return jsonify({"error": {"message": f"endpoint must be one of {sorted(valid_endpoints)}",
                                  "type": "invalid_request_error"}}), 400

    entries = request_log.snapshot(limit=limit, provider=provider,
                                   status=status, endpoint=endpoint)
    return jsonify({
        "buffer_size": REQUEST_LOG_SIZE,
        "stored":      request_log.size,
        "returned":    len(entries),
        "entries":     entries,
    })


@app.route("/metrics")
def metrics():
    """Prometheus text-format metrics for scraping (Grafana, etc.). Exposes
    operational labels including provider names and proxy-key tails, but never
    request content or full keys. Administrator authentication is required."""
    err = _admin_check()
    if err:
        return err

    out: list[str] = []

    def emit(name, mtype, help_, samples):
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} {mtype}")
        for labels, val in samples:
            tag = ("{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}") if labels else ""
            out.append(f"{name}{tag} {val}")

    emit("hermes_router_uptime_seconds", "gauge", "Seconds since the router started",
         [({}, round(time.time() - START_TIME))])
    emit("hermes_router_providers", "gauge", "Number of configured providers",
         [({}, len(PROVIDERS))])

    req, errs, lat, brk = [], [], [], []
    for p in PROVIDERS:
        name = p["name"]
        s = stats.summary(name)
        req.append(({"provider": name}, s["total_requests"]))
        errs.append(({"provider": name}, s["errors"]))
        if s["avg_latency_ms"] is not None:
            lat.append(({"provider": name}, s["avg_latency_ms"]))
        brk.append(({"provider": name}, 1 if stats.breaker_open(name) else 0))
    emit("hermes_router_requests_total", "counter", "Total requests routed per provider", req)
    emit("hermes_router_errors_total", "counter", "Total errored requests per provider", errs)
    emit("hermes_router_avg_latency_ms", "gauge", "Mean successful-request latency in ms per provider", lat)
    emit("hermes_router_circuit_breaker_open", "gauge", "1 if the provider's circuit breaker is open, else 0", brk)

    emit("hermes_router_cache_hits_total", "counter", "Response-cache hits", [({}, cache.hits)])
    emit("hermes_router_cache_misses_total", "counter", "Response-cache misses", [({}, cache.misses)])
    emit("hermes_router_cache_size", "gauge", "Entries currently in the response cache", [({}, cache.size)])
    emit("hermes_router_semantic_cache_hits_total", "counter", "Semantic-cache hits", [({}, cache.semantic_hits)])

    emit("hermes_router_tokens_total", "counter", "Total tokens served per provider (non-streaming)",
         [({"provider": n}, v) for n, v in _provider_tokens.items()])
    emit("hermes_router_cost_usd_total", "counter", "Estimated USD cost served per provider",
         [({"provider": n}, round(v, 6)) for n, v in _provider_cost.items()])
    emit("hermes_router_key_requests_total", "counter", "Total requests per proxy key (by key tail)",
         [({"key": k[-6:]}, key_usage.snapshot(k)["req_total"]) for k in PROXY_API_KEYS])

    return Response("\n".join(out) + "\n", content_type="text/plain; version=0.0.4")


if __name__ == "__main__":
    log.info(f"hermes-router starting on {HOST}:{PORT}")
    log.info(f"Providers: {[p['name'] for p in PROVIDERS]}")
    _embed = {p["name"]: p["embed_model"] for p in PROVIDERS if p.get("embed_model")}
    log.info(f"Embeddings (/v1/embeddings): {_embed if _embed else 'no embed-capable providers'}")
    log.info(f"Cache: {'enabled' if CACHE_TTL > 0 else 'disabled'} (TTL={CACHE_TTL}s, max={CACHE_MAX_SIZE}"
             f"{', persistent' if cache.persistent else ''})")
    log.info(f"Fast routing: {'enabled' if FAST_ROUTE_TOKENS > 0 else 'disabled'} (threshold={FAST_ROUTE_TOKENS} tokens)")
    log.info(f"Key rotation: {ROTATION_MODE}")
    log.info(f"Dashboard: http://{'localhost' if HOST in ('0.0.0.0','') else HOST}:{PORT}/dashboard")
    _skips = {p["name"]: p["skip_if_tokens_over"] for p in PROVIDERS if p.get("skip_if_tokens_over")}
    if _skips:
        log.info(f"Large-payload skip ceilings: {_skips}")
    try:
        from waitress import serve
        log.info("Serving with waitress (production WSGI)")
        serve(app, host=HOST, port=PORT, threads=int(os.environ.get("WORKER_THREADS", 16)))
    except ImportError:
        log.warning("waitress not installed — falling back to Flask dev server")
        app.run(host=HOST, port=PORT, threaded=True)
