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
  pip install -r requirements.txt
  cp .env.example .env   # add your API keys
  python router.py
"""

import json, os, time, threading, logging, hashlib, hmac, itertools, re, sqlite3, subprocess, secrets, uuid
from pathlib import Path
from collections import deque, OrderedDict, defaultdict
from flask import Flask, request, jsonify, Response, stream_with_context, redirect
from urllib.parse import urlparse, urlunparse
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

_load_env()

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
# fallback. PROXY_API_KEYS now gates real config-write power (add provider keys,
# mint/revoke access keys, restart) via the dashboard, not just chat — so an
# install left on one of these would share a publicly-documented credential with
# every other install that never edited it. See _ensure_real_proxy_key below.
_KNOWN_DEFAULT_PROXY_KEYS = {"sk-router-1", "sk-my-router-key-1"}


def _ensure_real_proxy_key(env_path: str = ".env") -> list[str]:
    """If no PROXY_API_KEYS is set, or it's still one of the placeholder values
    above, generate a real random key and persist it to .env — so every install
    gets a unique dashboard/API secret on first boot without the operator needing
    to remember to change it. A no-op once a real key is in place."""
    raw = os.environ.get("PROXY_API_KEYS", "").strip()
    current = [k.strip() for k in raw.split(",") if k.strip()]
    if current and not all(k in _KNOWN_DEFAULT_PROXY_KEYS for k in current):
        return current   # already a real, user-set key (or keys) — leave it alone

    new_key = "sk-router-" + secrets.token_urlsafe(24)
    p = Path(env_path)
    lines = p.read_text().splitlines() if p.exists() else []
    found, out = False, []
    for line in lines:
        if line.strip().startswith("PROXY_API_KEYS="):
            out.append(f"PROXY_API_KEYS={new_key}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"PROXY_API_KEYS={new_key}")
    p.write_text("\n".join(out) + "\n")
    os.environ["PROXY_API_KEYS"] = new_key
    log.warning("=" * 72)
    log.warning("No unique proxy API key was configured — generated one and saved")
    log.warning(f"it to {env_path}. Use this to access the dashboard and the API:")
    log.warning(f"    {new_key}")
    log.warning("=" * 72)
    return [new_key]


PROXY_API_KEYS    = _ensure_real_proxy_key()
ROUTER_MODEL      = os.environ.get("ROUTER_MODEL_ID", "hermes-router")
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
            AUTH_FILE.write_text(json.dumps(doc, indent=2) + "\n")
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

    if not providers:
        log.warning("No providers configured — set GEMINI_API_KEYS, OPENROUTER_API_KEYS, etc. in .env")

    # Multi-model support: a provider's model string may be a comma-separated list
    # (e.g. GEMINI_MODEL=gemini-2.5-flash-lite,gemini-2.5-flash). The router
    # fails over across a provider's models before cascading to the next provider;
    # upstream services may still enforce shared account/project limits. The first
    # entry is the "primary" model used for probing, rating, and status display.
    for p in providers:
        models = [m.strip() for m in str(p.get("model", "")).split(",") if m.strip()]
        models = models or [p.get("model", "")]
        filtered = _filter_excluded(p["name"], models)
        if models and not filtered:
            log.warning(f"{p['name']}: all models excluded via "
                        f"{p['name'].upper()}_EXCLUDE_MODELS — provider has no usable models")
        p["models"] = filtered
        p["model"]  = filtered[0] if filtered else ""

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
_MODEL_DISCOVERY_SKIP = {"anthropic", "codex", "local", "huggingface"}


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
    ENV_FILE_PATH.write_text("\n".join(out) + "\n")


def _auth_json_add_key(provider: str, key: str) -> tuple[bool, int]:
    """Append `key` to auth.json's providers[provider] list (creating the file/
    section as needed). Returns (added, total_count) — added=False on duplicate.
    Mirrors scripts/auth.sh's append_key."""
    doc = {}
    if AUTH_FILE.exists():
        try:
            doc = json.loads(AUTH_FILE.read_text())
        except Exception:
            doc = {}
    if not isinstance(doc, dict):
        doc = {}
    providers = doc.setdefault("providers", {})
    keys = providers.setdefault(provider, [])
    if key in keys:
        return False, len(keys)
    keys.append(key)
    AUTH_FILE.write_text(json.dumps(doc, indent=2) + "\n")
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
# .env/auth.json. Same proxy-key auth as every other /v1/config/* endpoint —
# this project has one flat admin tier, not per-key permission levels.

def _generate_proxy_key() -> str:
    """A new, cryptographically random proxy key. Shown once at creation time —
    only its last-6-char tail is ever displayed again, matching every other key
    in this codebase."""
    return "sk-router-" + secrets.token_urlsafe(24)


def _read_proxy_api_keys_live() -> list[str]:
    """Fresh-read PROXY_API_KEYS from .env (not the process's stale in-memory
    PROXY_API_KEYS global), so a just-created/revoked key is reflected in the
    dashboard immediately, before a restart makes it actually active."""
    raw = _env_read_line("PROXY_API_KEYS")
    if raw is None:
        return list(PROXY_API_KEYS)   # .env has no override line — use the running default
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    return keys or list(PROXY_API_KEYS)


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


def _write_proxy_key_meta(key: str, patch: dict) -> None:
    """Merge `patch` into auth.json's proxy_keys[key] (creating it if absent)."""
    doc = {}
    if AUTH_FILE.exists():
        try:
            doc = json.loads(AUTH_FILE.read_text())
        except Exception:
            doc = {}
    if not isinstance(doc, dict):
        doc = {}
    pk = doc.setdefault("proxy_keys", {})
    spec = pk.get(key, {})
    spec.update(patch)
    pk[key] = spec
    AUTH_FILE.write_text(json.dumps(doc, indent=2) + "\n")
    try:
        os.chmod(AUTH_FILE, 0o600)
    except OSError:
        pass


def _delete_proxy_key_meta(key: str) -> None:
    if not AUTH_FILE.exists():
        return
    try:
        doc = json.loads(AUTH_FILE.read_text())
    except Exception:
        return
    if not isinstance(doc, dict):
        return
    pk = doc.get("proxy_keys")
    if isinstance(pk, dict) and pk.pop(key, None) is not None:
        AUTH_FILE.write_text(json.dumps(doc, indent=2) + "\n")
        try:
            os.chmod(AUTH_FILE, 0o600)
        except OSError:
            pass


def _resolve_proxy_key_by_tail(tail: str, keys: list[str]) -> str | None:
    matches = [k for k in keys if k[-6:] == tail]
    return matches[0] if len(matches) == 1 else None


def _trigger_restart(delay_s: float = 1.2) -> None:
    """Restart the router shortly after this call returns, so the HTTP response
    triggering it has time to reach the client first. Delegates to the same,
    already-tested scripts/restart.sh used by `hr restart` (handles both the
    systemd and standalone-process cases) rather than duplicating that logic."""
    script = Path(__file__).resolve().parent / "scripts" / "restart.sh"
    if not script.exists():
        log.warning("restart requested but scripts/restart.sh not found — skipping")
        return

    def _go():
        try:
            subprocess.Popen(["/usr/bin/env", "bash", str(script)],
                              cwd=str(script.parent.parent),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True)
        except Exception as e:
            log.error(f"restart trigger failed: {e}")

    threading.Timer(delay_s, _go).start()


# ── Instance registry / Docker launcher ────────────────────────────────────────
# Instances let one "manager" router keep track of other Hermes Router processes.
# External instances are just monitored by base URL. Managed instances are Docker
# containers created by this process when the host has Docker available.

_INSTANCE_LOCK = threading.Lock()


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
    INSTANCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    INSTANCE_FILE.write_text(json.dumps(doc, indent=2) + "\n")
    try:
        os.chmod(INSTANCE_FILE, 0o600)
    except OSError:
        pass


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
        out["live"] = _probe_instance(entry)
        if entry.get("mode") == "docker":
            out["docker"] = _docker_state(entry)
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
        if key in {"HOST", "PORT", "ROUTER_AUTH_FILE", "ROUTER_STATE_FILE", "CACHE_DB_PATH"}:
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

    env, err = _parse_instance_env(body.get("env", existing.get("env", {})))
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

    api_key = entry.get("api_key") or ""
    if api_key:
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
        if r.status_code in (400, 404):
            # Providers that list paid models alongside free ones — never let
            # auto-discovery silently pick something that costs credits.
            alt = _discover_best_model(provider["base_url"], key, provider.get("headers", {}),
                                       free_only=provider["name"] in _FREE_ONLY_DISCOVERY)
            if alt:
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
                  for i, m in enumerate(_provider_models(p))]
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
            caps = cached_models.get((name, m)) or _resolve_caps(p, key, m, caps_probe_ok)
            new_model_state[(name, m)] = caps
            log.info(f"[ratings]   {name}/{m}: rating={caps['rating']} "
                     f"tools={'yes' if caps['supports_tools'] else 'no'} "
                     f"reasoning={'yes' if caps['reasoning'] else 'no'}")
        # Provider-level fields mirror the primary model's caps (back-compat).
        prim = new_model_state[(name, actual)]
        available = ok or probe_status in ("rate_limited", "http", "timeout")
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


pool = CredentialPool(PROVIDERS)

# Background: validate providers, fix models, assign ratings
threading.Thread(target=_initialize_ratings, args=(PROVIDERS, pool), daemon=True).start()

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
            return {name: self.summary(name) for name in self._data}


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

    def set(self, payload: dict, data: dict, ns: str = "", embedding: list | None = None):
        if self.ttl <= 0:
            return
        key = self._hash(payload, ns)
        ts  = time.time()
        with self.lock:
            if key not in self._store and len(self._store) >= self.max_size:
                old, _ = self._store.popitem(last=False)  # evict oldest
                self._db_delete(old)
            self._store[key] = (data, ts, ns, embedding)
            self._store.move_to_end(key)
            self._db_upsert(key, data, ts, ns, embedding)

    def semantic_lookup(self, query_emb: list, ns: str = "") -> dict | None:
        """Return the cached response whose stored prompt embedding is most similar
        to query_emb (same namespace, same vector dimension), if it clears
        SEMANTIC_THRESHOLD. Bounded linear scan over the LRU (max_size)."""
        if self.ttl <= 0 or not query_emb:
            return None
        now = time.time()
        qlen = len(query_emb)
        best_key, best_data, best_sim = None, None, 0.0
        with self.lock:
            for key, (data, ts, ens, emb) in self._store.items():
                if emb is None or ens != ns or len(emb) != qlen:
                    continue
                if now - ts >= self.ttl:
                    continue
                sim = _cosine(query_emb, emb)
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
# These limits are not an authorization boundary: every valid proxy key can use
# the router's authenticated configuration endpoints. Limits come
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
    return limits

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


def _utc_day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())

def _secs_to_utc_midnight() -> int:
    t = time.gmtime()
    return max(1, 86400 - (t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec))


class KeyUsage:
    """Thread-safe per-key counters: a rolling 60s window for RPM, plus per-UTC-day
    request and token tallies. In-memory (resets on restart)."""

    def __init__(self):
        self.lock = threading.Lock()
        self._win  = defaultdict(deque)   # key -> deque[timestamps] within the last 60s
        self._day  = defaultdict(lambda: {"day": "", "req": 0, "tokens": 0, "cost": 0.0})
        self._life = defaultdict(lambda: {"req": 0, "tokens": 0, "cost": 0.0})  # since start

    def _roll(self, key, now):
        w = self._win[key]
        cutoff = now - 60
        while w and w[0] < cutoff:
            w.popleft()

    def _day_bucket(self, key):
        d = self._day[key]
        today = _utc_day()
        if d["day"] != today:
            d.update(day=today, req=0, tokens=0, cost=0.0)
        return d

    def check_and_record(self, key, limits):
        """Atomically enforce this key's limits and, if allowed, count the request
        (RPM window + per-day + lifetime). `limits` may be all-zero, in which case
        nothing is gated and the request is simply recorded — so usage analytics
        work whether or not limits are configured. Returns (ok, retry, reason)."""
        rpm     = limits.get("rpm", 0)
        req_day = limits.get("req_per_day", 0)
        tpd     = limits.get("tokens_per_day", 0)
        cpd     = limits.get("cost_per_day", 0)
        now = time.time()
        with self.lock:
            d = self._day_bucket(key)
            self._roll(key, now)
            if cpd and d["cost"] >= cpd:
                return (False, _secs_to_utc_midnight(), f"${cpd:g} cost/day")
            if tpd and d["tokens"] >= tpd:
                return (False, _secs_to_utc_midnight(), f"{tpd} tokens/day")
            if rpm and len(self._win[key]) >= rpm:
                return (False, max(1, int(60 - (now - self._win[key][0]))), f"{rpm} requests/min")
            if req_day and d["req"] >= req_day:
                return (False, _secs_to_utc_midnight(), f"{req_day} requests/day")
            self._win[key].append(now)
            d["req"] += 1
            self._life[key]["req"] += 1
            return (True, 0, "")

    def add_tokens(self, key, n):
        n = int(n or 0)
        if not n:
            return
        with self.lock:
            self._day_bucket(key)["tokens"] += n
            self._life[key]["tokens"] += n

    def add_cost(self, key, usd):
        usd = float(usd or 0)
        if not usd:
            return
        with self.lock:
            self._day_bucket(key)["cost"] += usd
            self._life[key]["cost"] += usd

    def snapshot(self, key):
        with self.lock:
            d = self._day_bucket(key)
            self._roll(key, time.time())
            l = self._life[key]
            return {"req_today": d["req"], "tokens_today": d["tokens"],
                    "cost_today": round(d["cost"], 6),
                    "rpm_window": len(self._win[key]),
                    "req_total": l["req"], "tokens_total": l["tokens"],
                    "cost_total": round(l["cost"], 6)}


key_usage = KeyUsage()

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

def _strip_message(msg: dict):
    """Remove thinking fields from a message dict in-place."""
    msg.pop("reasoning_content", None)
    msg.pop("reasoning", None)
    msg.pop("think", None)
    if isinstance(msg.get("content"), list):
        msg["content"] = [
            b for b in msg["content"]
            if b.get("type") not in ("thinking", "think")
        ]


def _strip_response(data: dict):
    """Strip thinking fields from a non-streaming response before returning it."""
    for choice in data.get("choices", []):
        if "message" in choice:
            _strip_message(choice["message"])


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


def _streaming_generator(resp: requests.Response):
    """
    Yield SSE chunks with thinking fields stripped from delta objects.
    Buffers by newline to handle chunks that split across SSE boundaries.
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


def _with_cleanup(resp: requests.Response, gen):
    """Drive a streaming generator and always release the upstream connection
    when done — including when the client disconnects mid-stream (GeneratorExit).
    Without this, an aborted stream could keep an upstream socket checked out of
    the connection pool until garbage collection."""
    try:
        yield from gen
    finally:
        resp.close()


def _streaming_with_usage(gen, name: str, model: str | None = None):
    """Wrap a streaming generator to capture the usage block from the final SSE
    chunk (present when stream_options.include_usage=true is sent upstream) and
    record tokens + cost in _provider_tokens/_provider_cost. Yields every chunk
    unchanged."""
    usage: dict = {}
    for chunk in gen:
        text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else chunk
        for line in text.split("\n"):
            if line.startswith("data: ") and line != "data: [DONE]":
                try:
                    event = json.loads(line[6:])
                    u = event.get("usage") or {}
                    if u.get("total_tokens"):
                        usage = u
                except Exception:
                    pass
        yield chunk
    if usage:
        _add_provider_tokens(name, {"usage": usage}, model)

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
        if role == "system":
            # System content is always plain text in Anthropic's API
            text = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text") if isinstance(content, list) else content
            system_parts.append(text)
        else:
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
        "max_tokens": payload.get("max_tokens") or 1024,
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
    finish_reason = "stop" if stop_reason in ("end_turn", "stop_sequence") else "length"
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
            "message":       {"role": "assistant", "content": content},
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


def _anthropic_streaming_generator(resp: requests.Response):
    """Translate Anthropic SSE stream to OpenAI SSE format token-by-token."""
    msg_id       = f"chatcmpl-{int(time.time())}"
    model        = ""
    created      = int(time.time())
    finish_reason = "stop"
    first_chunk  = True

    buf = b""
    for raw_chunk in resp.iter_content(chunk_size=None):
        buf += raw_chunk
        while b"\n" in buf:
            line_bytes, buf = buf.split(b"\n", 1)
            line = line_bytes.decode("utf-8", errors="replace").strip()
            if not line.startswith("data: "):
                continue
            data_str = line[6:]
            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            etype = event.get("type")

            if etype == "message_start":
                msg    = event.get("message", {})
                msg_id = msg.get("id", msg_id)
                model  = msg.get("model", "")
                # Emit role chunk
                chunk = {"id": msg_id, "object": "chat.completion.chunk", "created": created,
                         "model": model,
                         "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                                      "finish_reason": None}]}
                yield ("data: " + json.dumps(chunk) + "\n\n").encode()
                first_chunk = False

            elif etype == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    text  = delta.get("text", "")
                    chunk = {"id": msg_id, "object": "chat.completion.chunk", "created": created,
                             "model": model,
                             "choices": [{"index": 0, "delta": {"content": text},
                                          "finish_reason": None}]}
                    yield ("data: " + json.dumps(chunk) + "\n\n").encode()

            elif etype == "message_delta":
                sr = event.get("delta", {}).get("stop_reason", "end_turn")
                finish_reason = "stop" if sr in ("end_turn", "stop_sequence") else "length"

            elif etype == "message_stop":
                chunk = {"id": msg_id, "object": "chat.completion.chunk", "created": created,
                         "model": model,
                         "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]}
                yield ("data: " + json.dumps(chunk) + "\n\n").encode()
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
        # assistant turns use output_text; user/tool use input_text
        ctype = "output_text" if role == "assistant" else "input_text"
        input_items.append({"type": "message", "role": role,
                             "content": [{"type": ctype, "text": content}]})

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


def _from_codex_response(events: list) -> dict:
    """Aggregate a list of Responses SSE event objects into one OpenAI
    chat-completion JSON (used for non-streaming clients)."""
    final = {}
    text_acc = []
    for ev in events:
        t = ev.get("type", "")
        if t == "response.completed" and isinstance(ev.get("response"), dict):
            final = ev["response"]
        elif t == "response.output_text.delta":
            text_acc.append(ev.get("delta", ""))
    text, tool_calls = _codex_text_and_tools(final) if final else ("", [])
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
        elif etype == "response.completed" and isinstance(ev.get("response"), dict):
            _, tcs = _codex_text_and_tools(ev["response"])
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
                              headers=hdrs, json=body, stream=True, timeout=(10, 180))
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
        hdrs = {"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
        try:
            return _HTTP.post("https://api.anthropic.com/v1/messages",
                              headers=hdrs, json=body, stream=streaming, timeout=(10, 120))
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

    # Strip thinking fields from conversation history before forwarding
    if "messages" in body:
        cleaned = []
        for msg in body["messages"]:
            m = dict(msg)
            _strip_message(m)
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
    if _model_caps(provider["name"], body["model"]).get("reasoning"):
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
        return _HTTP.post(url, headers=headers, json=body, stream=streaming, timeout=(10, 120))
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
    embed_providers = [p for p in PROVIDERS if p.get("embed_model")]
    return sorted(embed_providers, key=lambda p: (1 if stats.breaker_open(p["name"]) else 0,
                                                  stats.health_bucket(p["name"])))


def _prompt_text(messages: list) -> str:
    """Flatten a chat request's message text for semantic-cache embedding."""
    return " ".join(_message_text(m) for m in messages if m.get("content")).strip()[:8000]


def _embed_text(text: str) -> list | None:
    """Embed text via the internal embeddings pipeline (used by the semantic cache).
    Returns a vector, or None if no embed provider is available / all are cooling."""
    if not text:
        return None
    body = {"input": text}
    for provider in _embed_ordered():
        em  = provider["embed_model"]
        key = pool.get_key(provider["name"], em)
        if not key:
            continue
        try:
            resp = forward_embeddings(provider, key, body)
        except Exception:
            resp = None
        if resp is not None and 200 <= resp.status_code < 300:
            try:
                return resp.json()["data"][0]["embedding"]
            except Exception:
                pass
    return None


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
        return _HTTP.post(url, headers=headers, json=body, timeout=(10, 120))
    except requests.exceptions.RequestException as e:
        log.error(f"  Network error → {provider['name']} embeddings: {e}")
        return None

# ── Flask app ──────────────────────────────────────────────────────────────────

app = Flask(__name__)
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
    # compare_digest keeps the comparison constant-time per key
    if not any(hmac.compare_digest(token, k) for k in PROXY_API_KEYS):
        return jsonify({"error": "unauthorized"}), 401


def _cache_ns() -> str:
    """Cache namespace = the authenticated caller, so different API keys never
    share a cached response for an identical request."""
    return _caller_token()


def _admit_request(token: str):
    """Enforce this caller's rate/budget limits AND record the request for usage
    analytics (recording happens whether or not limits are set). Returns a Flask
    (response, 429) tuple to short-circuit when over limit, or None to proceed."""
    limits = KEY_LIMITS.get(token) or {}
    ok, retry, reason = key_usage.check_and_record(token, limits)
    if ok:
        return None
    resp = jsonify({"error": {"message": f"quota exceeded ({reason})",
                              "type": "rate_limit_error"}})
    resp.headers["Retry-After"] = str(retry)
    return resp, 429


def _record_request_tokens(token: str, payload: dict, result):
    """Post-flight: add this request's tokens (and estimated cost) to the caller's
    tally (daily + lifetime). Uses provider-reported usage when present, else an
    estimate (e.g. streaming). Cost uses the response model's prompt/completion
    split when available; $0 for free/unpriced models."""
    n = 0
    if result and result[0] == "json":
        data  = result[1]
        usage = data.get("usage") or {}
        n = usage.get("total_tokens") or 0
        cost = _cost(data.get("model") or payload.get("model") or "",
                     usage.get("prompt_tokens"), usage.get("completion_tokens"))
        if cost:
            key_usage.add_cost(token, cost)
    if not n:
        n = _estimated_tokens(payload.get("messages", []))
    key_usage.add_tokens(token, n)


_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hermes Router — Dashboard</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0f1117;--surface:#181c27;--surface2:#1e2333;--border:#2a3050;
  --text:#e2e8f0;--muted:#8892a4;--accent:#6c8cff;--green:#4ade80;
  --yellow:#facc15;--red:#f87171;--orange:#fb923c;--purple:#c084fc;
  --font:'Inter',system-ui,sans-serif;
}
body{background:var(--bg);color:var(--text);font-family:var(--font);font-size:13px;min-height:100vh}

/* ── layout ── */
header{display:flex;align-items:center;justify-content:space-between;padding:14px 20px;
  background:var(--surface);border-bottom:1px solid var(--border);position:sticky;top:0;z-index:10}
header h1{font-size:15px;font-weight:600;letter-spacing:.3px;color:var(--text)}
header h1 span{color:var(--accent)}
.badge{display:inline-flex;align-items:center;gap:5px;font-size:11px;padding:3px 8px;
  border-radius:99px;background:var(--surface2);border:1px solid var(--border);color:var(--muted)}
.badge .dot{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 5px var(--green)}
.badge .dot.err{background:var(--red);box-shadow:0 0 5px var(--red)}
.header-right{display:flex;align-items:center;gap:10px}
#last-update{font-size:11px;color:var(--muted)}
.btn{cursor:pointer;font-size:11px;padding:4px 10px;border-radius:6px;border:1px solid var(--border);
  background:var(--surface2);color:var(--text);transition:.15s}
.btn:hover{border-color:var(--accent);color:var(--accent)}

/* ── app shell / sidebar ── */
.app-shell{display:flex;align-items:stretch;min-height:100vh}
.sidebar{width:200px;flex:0 0 auto;background:var(--surface);border-right:1px solid var(--border);
  display:flex;flex-direction:column;padding:16px 0;position:sticky;top:0;align-self:flex-start;
  height:100vh}
.sidebar-brand{padding:0 16px 14px;font-size:15px;font-weight:600;letter-spacing:.3px;
  border-bottom:1px solid var(--border);margin-bottom:12px}
.sidebar-brand span{color:var(--accent)}
.sidebar-nav{display:flex;flex-direction:column;gap:2px;padding:0 8px}
.nav-item{display:flex;align-items:center;justify-content:space-between;gap:8px;width:100%;
  text-align:left;padding:9px 10px;border-radius:7px;border:none;background:transparent;
  color:var(--muted);font-size:12.5px;font-family:inherit;cursor:pointer;transition:.15s}
.nav-item:hover{background:var(--surface2);color:var(--text)}
.nav-item.active{background:var(--surface2);color:var(--accent);font-weight:600}
.nav-item .nav-dot{width:6px;height:6px;border-radius:50%;background:var(--border);flex:0 0 auto}
.nav-item .nav-dot.warn{background:var(--yellow);box-shadow:0 0 4px var(--yellow)}
.nav-item .nav-dot.bad{background:var(--red);box-shadow:0 0 4px var(--red)}
.app-main{flex:1;min-width:0;display:flex;flex-direction:column}
@media(max-width:760px){
  .app-shell{flex-direction:column}
  .sidebar{width:100%;height:auto;position:static;flex-direction:row;overflow-x:auto;padding:10px 8px}
  .sidebar-brand{display:none}
  .sidebar-nav{flex-direction:row}
  .nav-item{white-space:nowrap}
}

main{padding:18px 20px;display:grid;gap:16px;max-width:1180px;margin:0 auto;width:100%}
.page{display:none}
.page.active{display:grid;gap:16px}
.page-intro{color:var(--muted);line-height:1.45}

/* ── key input overlay ── */
#key-gate{position:fixed;inset:0;background:rgba(0,0,0,.7);display:flex;
  align-items:center;justify-content:center;z-index:100}
#key-gate.hidden{display:none}
.gate-box{background:var(--surface);border:1px solid var(--border);border-radius:12px;
  padding:28px 32px;min-width:320px;text-align:center}
.gate-box h2{margin-bottom:6px;font-size:15px}
.gate-box p{color:var(--muted);font-size:12px;margin-bottom:18px}
.gate-box input{width:100%;padding:8px 12px;border-radius:7px;border:1px solid var(--border);
  background:var(--bg);color:var(--text);font-size:13px;outline:none;margin-bottom:10px}
.gate-box input:focus{border-color:var(--accent)}
.gate-box .btn{width:100%;padding:7px}

/* ── stat cards ── */
.stat-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.stat-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;
  padding:14px 16px}
.stat-card .label{font-size:11px;color:var(--muted);margin-bottom:5px;text-transform:uppercase;
  letter-spacing:.5px}
.stat-card .value{font-size:22px;font-weight:700;color:var(--text)}
.stat-card .sub{font-size:11px;color:var(--muted);margin-top:3px}

/* ── simple overview ── */
.overview-grid{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(280px,.75fr);gap:14px}
@media(max-width:900px){.overview-grid{grid-template-columns:1fr}}
.hero{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:18px}
.hero-top{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
.hero h2{font-size:20px;line-height:1.2;margin-bottom:6px}
.hero-copy{color:var(--muted);line-height:1.45;max-width:760px}
.hero-state{font-size:12px;padding:5px 10px;border-radius:999px;white-space:nowrap}
.hero-state.good{background:rgba(74,222,128,.12);color:var(--green)}
.hero-state.warn{background:rgba(250,204,21,.12);color:var(--yellow)}
.hero-state.bad{background:rgba(248,113,113,.12);color:var(--red)}
.quick-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-top:16px}
.quick-card{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:12px}
.quick-label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px;margin-bottom:5px}
.quick-value{font-size:15px;font-weight:700}
.quick-sub{font-size:11px;color:var(--muted);margin-top:3px;line-height:1.4}
.setup-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px}
.setup-card h3{font-size:14px;margin-bottom:10px}
.setup-list{display:grid;gap:8px}
.setup-step{display:flex;align-items:center;gap:8px;color:var(--muted)}
.setup-step strong{color:var(--text);font-weight:600}
.step-dot{width:9px;height:9px;border-radius:50%;background:var(--border);flex:0 0 auto}
.setup-step.done .step-dot{background:var(--green);box-shadow:0 0 6px var(--green)}
.setup-step.warn .step-dot{background:var(--yellow);box-shadow:0 0 6px var(--yellow)}
.setup-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
.provider-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px;padding:12px}
.provider-card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px}
.provider-card.bad{border-color:rgba(248,113,113,.45)}
.provider-card.warn{border-color:rgba(250,204,21,.45)}
.provider-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:5px}
.provider-name{font-weight:700}
.provider-model{font-size:11px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.provider-meta{display:flex;justify-content:space-between;gap:8px;margin-top:10px;font-size:11px;color:var(--muted)}
.advanced-panel summary{cursor:pointer;list-style:none}
.advanced-panel summary::-webkit-details-marker{display:none}
.advanced-panel .panel-header:after{content:"show";font-size:11px;color:var(--muted)}
.advanced-panel[open] .panel-header:after{content:"hide"}
.advanced-panel:not([open]) .panel-body{display:none}

/* ── panels ── */
.panel{background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.panel-header{display:flex;align-items:center;justify-content:space-between;
  padding:10px 14px;border-bottom:1px solid var(--border);background:var(--surface2)}
.panel-title{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.6px;
  color:var(--muted)}
.panel-body{overflow-x:auto}
.panel-body.pad{padding:12px}

/* ── tables ── */
table{width:100%;border-collapse:collapse;font-size:12px}
th{padding:7px 12px;text-align:left;color:var(--muted);font-weight:500;
  font-size:11px;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:7px 12px;border-bottom:1px solid var(--border);vertical-align:middle;white-space:nowrap}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(108,140,255,.04)}

/* ── status dots ── */
.dot-ok{display:inline-block;width:8px;height:8px;border-radius:50%;
  background:var(--green);box-shadow:0 0 5px var(--green);margin-right:5px}
.dot-warn{display:inline-block;width:8px;height:8px;border-radius:50%;
  background:var(--yellow);box-shadow:0 0 5px var(--yellow);margin-right:5px}
.dot-err{display:inline-block;width:8px;height:8px;border-radius:50%;
  background:var(--red);box-shadow:0 0 5px var(--red);margin-right:5px}

/* ── pill badges ── */
.pill{display:inline-block;padding:2px 7px;border-radius:99px;font-size:10px;font-weight:600}
.pill-ok{background:rgba(74,222,128,.12);color:var(--green)}
.pill-err{background:rgba(248,113,113,.12);color:var(--red)}
.pill-cache{background:rgba(192,132,252,.12);color:var(--purple)}
.pill-warn{background:rgba(250,204,21,.12);color:var(--yellow)}
.pill-grey{background:rgba(136,146,164,.12);color:var(--muted)}

/* ── rating bar ── */
.rating-bar{display:flex;gap:3px;align-items:center}
.rating-pip{width:9px;height:9px;border-radius:2px;background:var(--border)}
.r1 .rating-pip.active{background:var(--green)}
.r2 .rating-pip.active{background:#22d3ee}
.r3 .rating-pip.active{background:var(--accent)}
.r4 .rating-pip.active{background:var(--yellow)}
.r5 .rating-pip.active{background:var(--red)}

/* ── progress bar ── */
.prog-track{background:var(--surface2);border-radius:99px;height:5px;min-width:80px;overflow:hidden}
.prog-fill{height:100%;border-radius:99px;background:var(--accent);transition:width .4s}
.prog-fill.green{background:var(--green)}
.prog-fill.red{background:var(--red)}
.prog-fill.yellow{background:var(--yellow)}

/* ── add-on toggles ── */
.addon-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px;padding:12px}
.addon-card{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:12px}
.addon-card.flag{cursor:pointer;transition:border-color .15s}
.addon-card.flag:hover{border-color:var(--accent)}
.addon-card.busy{opacity:.5;pointer-events:none}
.addon-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:4px}
.addon-name{font-size:12px;font-weight:600}
.addon-desc{font-size:11px;color:var(--muted);line-height:1.4}

/* ── provider scope picker ── */
.scope-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:6px;
  margin:6px 0}
.scope-item{display:flex;align-items:center;gap:6px;font-size:12px;padding:5px 8px;
  border:1px solid var(--border);border-radius:6px;background:var(--surface2);cursor:pointer}
.scope-item input{margin:0}
.scope-item .cnt{color:var(--muted);font-size:10px;margin-left:auto}

/* ── restart banner ── */
#restart-banner{display:none;align-items:center;justify-content:space-between;gap:12px;
  padding:10px 20px;background:rgba(250,204,21,.1);border-bottom:1px solid var(--yellow)}
#restart-banner.show{display:flex}
#restart-banner span{font-size:12px;color:var(--yellow)}
#restart-banner .actions{display:flex;gap:8px}

/* ── config forms ── */
.config-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:14px}
@media(max-width:820px){.config-grid{grid-template-columns:1fr}}
.config-grid.narrow{grid-template-columns:1fr;max-width:440px}
.config-intro{padding:12px 14px 0;color:var(--muted);line-height:1.45}
.config-form{display:flex;flex-direction:column;gap:8px}
.config-form label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}
.config-form select, .config-form input{
  background:var(--surface2);border:1px solid var(--border);border-radius:6px;
  color:var(--text);padding:7px 10px;font-size:12px;font-family:inherit;outline:none}
.config-form select:focus, .config-form input:focus{border-color:var(--accent)}
.config-form .row{display:flex;gap:8px}
.config-form .row > *{flex:1}
.config-msg{font-size:11px;min-height:16px}
.config-msg.ok{color:var(--green)}
.config-msg.err{color:var(--red)}
.config-msg.warn{color:var(--yellow)}
.default-hint{font-size:10px;color:var(--muted)}
.config-form textarea{
  background:var(--surface2);border:1px solid var(--border);border-radius:6px;
  color:var(--text);padding:7px 10px;font-size:12px;font-family:monospace;outline:none;
  min-height:86px;resize:vertical}
.config-form textarea:focus{border-color:var(--accent)}
.instance-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;padding:12px}
.instance-card{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:12px}
.instance-card.warn{border-color:rgba(250,204,21,.45)}
.instance-card.bad{border-color:rgba(248,113,113,.45)}
.instance-actions{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.btn.primary{background:rgba(108,140,255,.16);border-color:var(--accent);color:var(--text);font-weight:600}
.btn.danger:hover{border-color:var(--red);color:var(--red)}
.mode-switch{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.mode-option{display:flex;align-items:flex-start;justify-content:space-between;gap:10px;
  text-align:left;padding:10px;border-radius:8px;border:1px solid var(--border);
  background:var(--surface2);color:var(--text);font-family:inherit;cursor:pointer}
.mode-option.active{border-color:var(--accent);background:rgba(108,140,255,.12)}
.mode-option strong{display:block;font-size:12px;margin-bottom:3px}
.mode-option span{display:block;font-size:11px;color:var(--muted);line-height:1.35}
.mode-dot{width:9px;height:9px;border-radius:50%;margin-top:3px;background:var(--border);flex:0 0 auto}
.mode-option.active .mode-dot{background:var(--accent);box-shadow:0 0 6px var(--accent)}
.instance-form{padding:14px;display:grid;grid-template-columns:minmax(0,1fr) minmax(280px,.85fr);gap:14px}
@media(max-width:900px){.instance-form{grid-template-columns:1fr}.mode-switch{grid-template-columns:1fr}}
.instance-form-col{display:flex;flex-direction:column;gap:10px}
.field-hint{font-size:10.5px;color:var(--muted);line-height:1.35;margin-top:-3px}
.field-line{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.field-line label{margin:0}
.port-map{display:grid;grid-template-columns:1fr auto 1fr;gap:8px;align-items:end}
.port-arrow{color:var(--muted);font-size:15px;padding-bottom:9px}
.instance-advanced{border:1px solid var(--border);border-radius:8px;background:rgba(15,17,23,.2)}
.instance-advanced summary{cursor:pointer;padding:9px 10px;color:var(--muted);font-size:11px;
  text-transform:uppercase;letter-spacing:.4px;list-style:none}
.instance-advanced summary::-webkit-details-marker{display:none}
.instance-advanced .advanced-body{padding:0 10px 10px;display:flex;flex-direction:column;gap:8px}
.instance-empty{display:flex;align-items:center;justify-content:center;min-height:90px;color:var(--muted)}
.instance-empty strong{color:var(--text);font-weight:600}
.hidden-field{display:none!important}

/* ── log table ── */
#log-wrap{max-height:340px;overflow-y:auto}
.log-row-success td:first-child{border-left:2px solid var(--green)}
.log-row-error td:first-child{border-left:2px solid var(--red)}
.log-row-cache_hit td:first-child{border-left:2px solid var(--purple)}

/* ── two-col layout for lower panels ── */
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:820px){.two-col{grid-template-columns:1fr}}

/* ── misc ── */
.mono{font-family:monospace;font-size:11px}
.right{text-align:right}
.muted{color:var(--muted)}
</style>
</head>
<body>

<!-- API key gate -->
<div id="key-gate">
  <div class="gate-box">
    <h2>Hermes Router</h2>
    <p>Enter your proxy API key to view the dashboard.</p>
    <input id="key-input" type="password" placeholder="sk-router-..." autocomplete="off">
    <p id="gate-error" style="color:var(--red);font-size:12px;min-height:16px;margin:8px 0 0"></p>
    <button class="btn" onclick="submitKey()">Open Dashboard</button>
  </div>
</div>

<div class="app-shell">
  <aside class="sidebar">
    <div class="sidebar-brand"><span>Hermes</span> Router</div>
    <nav class="sidebar-nav" id="sidebar-nav">
      <button class="nav-item active" data-page="overview" onclick="showPage('overview')">Overview</button>
      <button class="nav-item" data-page="providers" onclick="showPage('providers')"><span>Providers</span><span class="nav-dot" id="nav-dot-providers"></span></button>
      <button class="nav-item" data-page="instances" onclick="showPage('instances')"><span>Instances</span><span class="nav-dot" id="nav-dot-instances"></span></button>
      <button class="nav-item" data-page="keys" onclick="showPage('keys')">Provider Keys</button>
      <button class="nav-item" data-page="access" onclick="showPage('access')">Access Keys</button>
      <button class="nav-item" data-page="models" onclick="showPage('models')">Models</button>
      <button class="nav-item" data-page="addons" onclick="showPage('addons')">Add-ons</button>
      <button class="nav-item" data-page="logs" onclick="showPage('logs')">Request Log</button>
    </nav>
  </aside>

  <div class="app-main">
    <header>
      <h1><span>Hermes</span> Router &mdash; Dashboard</h1>
      <div class="header-right">
        <div class="badge"><div class="dot" id="hdr-dot"></div><span id="hdr-status">connecting</span></div>
        <span id="last-update"></span>
        <button class="btn" onclick="refresh()">↺ Refresh</button>
      </div>
    </header>

    <div id="restart-banner">
      <span>⚠ Config changed — restart the router to apply it.</span>
      <div class="actions">
        <button class="btn" onclick="dismissBanner()">Later</button>
        <button class="btn" onclick="doRestart()" style="border-color:var(--yellow);color:var(--yellow)">↻ Restart Now</button>
      </div>
    </div>

    <main>

      <!-- ── Overview ─────────────────────────────────────────────────────── -->
      <section class="page active" id="page-overview">
        <section class="overview-grid">
          <div class="hero">
            <div class="hero-top">
              <div>
                <h2 id="plain-title">Checking router...</h2>
                <div class="hero-copy" id="plain-message">Loading status from Hermes Router.</div>
              </div>
              <div class="hero-state warn" id="plain-state">checking</div>
            </div>
            <div class="quick-grid">
              <div class="quick-card">
                <div class="quick-label">API endpoint</div>
                <div class="quick-value mono" id="quick-endpoint">/v1</div>
                <div class="quick-sub">Use this as the OpenAI base URL.</div>
              </div>
              <div class="quick-card">
                <div class="quick-label">Model name</div>
                <div class="quick-value mono" id="quick-model">hermes-router</div>
                <div class="quick-sub">Send this model from your app.</div>
              </div>
              <div class="quick-card">
                <div class="quick-label">Spend</div>
                <div class="quick-value" id="quick-spend">-</div>
                <div class="quick-sub">Estimated since last restart.</div>
              </div>
            </div>
          </div>
          <div class="setup-card">
            <h3>Setup checklist</h3>
            <div class="setup-list">
              <div class="setup-step" id="step-key"><span class="step-dot"></span><strong>Provider key</strong><span id="step-key-text">checking</span></div>
              <div class="setup-step" id="step-health"><span class="step-dot"></span><strong>Provider health</strong><span id="step-health-text">checking</span></div>
              <div class="setup-step" id="step-restart"><span class="step-dot"></span><strong>Restart</strong><span id="step-restart-text">not needed</span></div>
            </div>
            <div class="setup-actions">
              <button class="btn" onclick="showPage('keys')">Add key</button>
              <button class="btn" onclick="doRestart()">Restart</button>
              <button class="btn" onclick="refresh()">Refresh</button>
            </div>
          </div>
        </section>

        <div class="panel">
          <div class="panel-header"><span class="panel-title">Usage Summary</span></div>
          <div class="panel-body pad">
            <div class="stat-row" id="stat-row">
              <div class="stat-card"><div class="label">Providers</div><div class="value" id="s-providers">—</div><div class="sub" id="s-providers-sub"></div></div>
              <div class="stat-card"><div class="label">Uptime</div><div class="value" id="s-uptime">—</div><div class="sub">since last restart</div></div>
              <div class="stat-card"><div class="label">Total Requests</div><div class="value" id="s-requests">—</div><div class="sub" id="s-requests-sub"></div></div>
              <div class="stat-card"><div class="label">Total Tokens</div><div class="value" id="s-tokens">—</div><div class="sub" id="s-cost"></div></div>
              <div class="stat-card"><div class="label">Cache Hit Rate</div><div class="value" id="s-hitrate">—</div><div class="sub" id="s-cache-sub"></div></div>
              <div class="stat-card"><div class="label">Error Rate</div><div class="value" id="s-errrate">—</div><div class="sub" id="s-errrate-sub"></div></div>
            </div>
          </div>
        </div>
      </section>

      <!-- ── Providers ────────────────────────────────────────────────────── -->
      <section class="page" id="page-providers">
        <div class="panel">
          <div class="panel-header"><span class="panel-title">Provider Health</span><span class="muted">attention first</span></div>
          <div class="provider-grid" id="provider-card-grid"></div>
        </div>

        <details class="panel advanced-panel">
          <summary class="panel-header"><span class="panel-title">Advanced Provider Details</span></summary>
          <div class="panel-body">
            <table>
              <thead><tr>
                <th>Provider</th><th>Model</th><th>Rating</th>
                <th class="right">Requests</th><th class="right">Errors</th>
                <th class="right">Err %</th><th class="right">Avg Latency</th>
                <th class="right">Tokens</th><th class="right">Cost (USD)</th>
                <th>Keys</th><th>Breaker</th><th>Status</th>
              </tr></thead>
              <tbody id="provider-tbody"></tbody>
            </table>
          </div>
        </details>
      </section>

      <!-- ── Instances ────────────────────────────────────────────────────── -->
      <section class="page" id="page-instances">
        <div class="panel">
          <div class="panel-header"><span class="panel-title">Instance Manager</span><span class="muted">external or Docker-managed routers</span></div>
          <div class="instance-grid" id="instance-summary-grid"></div>
        </div>

        <div class="panel">
          <div class="panel-header"><span class="panel-title">Add Instance</span></div>
          <div class="instance-form">
            <div class="instance-form-col config-form">
              <label>Mode</label>
              <div class="mode-switch">
                <button type="button" class="mode-option active" id="mode-external" onclick="setInstanceMode('external')">
                  <span><strong>Connect existing</strong><span>Track a router that is already running.</span></span><i class="mode-dot"></i>
                </button>
                <button type="button" class="mode-option" id="mode-docker" onclick="setInstanceMode('docker')">
                  <span><strong>Launch Docker</strong><span>Create a managed router on a host port.</span></span><i class="mode-dot"></i>
                </button>
              </div>
              <select id="inst-mode" class="hidden-field" onchange="onInstanceModeChange()">
                <option value="external">external</option>
                <option value="docker">docker</option>
              </select>

              <label>Name</label>
              <input id="inst-name" type="text" placeholder="agent-a">

              <label>OpenAI base URL</label>
              <input id="inst-base-url" type="text" placeholder="http://localhost:8320/v1">
              <div class="field-hint" id="inst-base-hint">Agents use this as their base URL.</div>

              <div id="inst-port-fields">
                <label>Port mapping</label>
                <div class="port-map">
                  <div>
                    <div class="field-hint">Host</div>
                    <input id="inst-host-port" type="number" min="1" max="65535" placeholder="8320">
                  </div>
                  <div class="port-arrow">to</div>
                  <div>
                    <div class="field-hint">Container</div>
                    <input id="inst-container-port" type="number" min="1" max="65535" value="8319">
                  </div>
                </div>
              </div>
            </div>

            <div class="instance-form-col config-form">
              <label id="inst-key-label">Router access key</label>
              <input id="inst-api-key" type="password" placeholder="sk-router-..." autocomplete="off">
              <div class="field-hint" id="inst-key-hint">Used only to verify authenticated endpoints.</div>

              <details class="instance-advanced" id="inst-docker-options">
                <summary>Docker settings</summary>
                <div class="advanced-body">
                  <label>Image</label>
                  <input id="inst-image" type="text" value="hermes-router:latest">
                  <label>Use existing router keys</label>
                  <div class="field-hint">Selected provider keys are copied into this instance at creation time.</div>
                  <div class="scope-grid" id="inst-copy-provider-keys"></div>
                  <label>Provider env vars</label>
                  <textarea id="inst-env" spellcheck="false" placeholder="GEMINI_API_KEYS=...\nOPENAI_API_KEYS=..."></textarea>
                </div>
              </details>

              <div class="row">
                <button class="btn primary" id="inst-save-btn" onclick="createInstance(false)">Register instance</button>
                <button class="btn" id="inst-start-btn" onclick="createInstance(true)">Create & Start</button>
              </div>
              <div class="config-msg" id="inst-msg"></div>
            </div>
          </div>
        </div>

        <div class="panel" id="new-instance-key-panel" style="display:none">
          <div class="panel-header"><span class="panel-title">Generated Instance Key</span></div>
          <div class="panel-body pad">
            <p class="muted" style="margin-bottom:8px">This Docker instance was assigned a proxy API key. Use it with agents that call this instance.</p>
            <div class="config-form">
              <div class="row">
                <input id="new-instance-key-value" type="text" readonly class="mono" style="flex:1">
                <button class="btn" onclick="copyInstanceKey()" style="flex:0 0 auto">Copy</button>
                <button class="btn" onclick="dismissInstanceKey()" style="flex:0 0 auto">Done</button>
              </div>
            </div>
          </div>
        </div>

        <div class="panel">
          <div class="panel-header"><span class="panel-title">Instances</span></div>
          <div class="panel-body">
            <table>
              <thead><tr>
                <th>Name</th><th>Mode</th><th>Base URL</th><th>Health</th>
                <th>Docker</th><th>Key</th><th>Env</th><th>Actions</th>
              </tr></thead>
              <tbody id="instances-tbody"></tbody>
            </table>
          </div>
        </div>
      </section>

      <!-- ── Provider Keys ────────────────────────────────────────────────── -->
      <section class="page" id="page-keys">
        <div class="panel">
          <div class="panel-header"><span class="panel-title">Provider Key Setup</span></div>
          <div class="page-intro" style="padding:12px 14px 0">Keys the router uses to call upstream providers (Gemini, OpenAI, …) — not the keys your own apps use to call the router (see Access Keys for that).</div>
          <div class="config-grid">
            <div class="config-form">
              <label>Add API key</label>
              <div class="row">
                <select id="cfg-key-provider"></select>
              </div>
              <input id="cfg-key-value" type="password" placeholder="paste provider API key" autocomplete="off">
              <div class="row">
                <button class="btn" onclick="addKey()">Add key</button>
              </div>
              <div class="config-msg" id="cfg-key-msg"></div>
            </div>

            <div class="config-form">
              <label>Key rotation</label>
              <select id="cfg-rotation-value">
                <option value="round-robin">Spread requests across keys</option>
                <option value="sequential">Use one key before the next</option>
              </select>
              <div class="row">
                <button class="btn" onclick="setRotation()">Save rotation mode</button>
              </div>
              <div class="default-hint">Round-robin is best for most users because it spreads load across keys.</div>
              <div class="config-msg" id="cfg-rotation-msg"></div>
            </div>
          </div>
        </div>

        <div class="panel">
          <div class="panel-header"><span class="panel-title">Key & Budget Usage</span></div>
          <div class="panel-body">
            <table>
              <thead><tr>
                <th>Key</th><th class="right">Requests</th>
                <th class="right">Tokens (day)</th><th class="right">Cost (day)</th>
                <th>RPM used</th>
              </tr></thead>
              <tbody id="keys-tbody"></tbody>
            </table>
          </div>
        </div>
      </section>

      <!-- ── Access Keys ──────────────────────────────────────────────────── -->
      <section class="page" id="page-access">
        <div class="panel">
          <div class="panel-header"><span class="panel-title">Create Access Key</span></div>
          <div class="page-intro" style="padding:12px 14px 0">Generate a key for a teammate or another app to call this router with — separate from your own key, with its own optional usage caps.</div>
          <div class="config-grid narrow">
            <div class="config-form">
              <label>Name (optional)</label>
              <input id="ak-name" type="text" placeholder="e.g. Bob, CI pipeline">
              <label style="margin-top:4px">Limits (optional — blank means unlimited)</label>
              <div class="row">
                <input id="ak-rpm" type="number" min="0" placeholder="requests / min">
                <input id="ak-reqday" type="number" min="0" placeholder="requests / day">
              </div>
              <div class="row">
                <input id="ak-tokday" type="number" min="0" placeholder="tokens / day">
                <input id="ak-costday" type="number" min="0" step="0.01" placeholder="cost / day ($)">
              </div>
              <label style="margin-top:4px">Providers this key may use (none checked = all)</label>
              <div class="scope-grid" id="ak-provider-scope"></div>
              <div class="row">
                <button class="btn" onclick="createAccessKey()">Create key</button>
              </div>
              <div class="config-msg" id="ak-create-msg"></div>
            </div>
          </div>
        </div>

        <div class="panel" id="new-key-panel" style="display:none">
          <div class="panel-header"><span class="panel-title">New Key — copy it now</span></div>
          <div class="panel-body pad">
            <p class="muted" style="margin-bottom:8px">This is the only time the full key is shown. It needs a router restart before it can be used.</p>
            <div class="config-form">
              <div class="row">
                <input id="new-key-value" type="text" readonly class="mono" style="flex:1">
                <button class="btn" onclick="copyNewKey()" style="flex:0 0 auto">Copy</button>
                <button class="btn" onclick="dismissNewKey()" style="flex:0 0 auto">Done</button>
              </div>
            </div>
          </div>
        </div>

        <div class="panel">
          <div class="panel-header"><span class="panel-title">Access Keys</span></div>
          <div class="panel-body">
            <table>
              <thead><tr>
                <th>Name</th><th>Key</th><th class="right">RPM</th><th class="right">Req/day</th>
                <th class="right">Tokens/day</th><th class="right">Cost/day</th>
                <th class="right">Used today</th><th>Providers</th><th>Status</th><th></th>
              </tr></thead>
              <tbody id="access-keys-tbody"></tbody>
            </table>
          </div>
        </div>
      </section>

      <!-- ── Models ───────────────────────────────────────────────────────── -->
      <section class="page" id="page-models">
        <div class="panel">
          <div class="panel-header"><span class="panel-title">Provider Model</span></div>
          <div class="page-intro" style="padding:12px 14px 0">Override which model a provider uses — comma-separate several models for per-model failover.</div>
          <div class="config-grid narrow">
            <div class="config-form">
              <div class="row">
                <select id="cfg-model-provider" onchange="onModelProviderChange()"></select>
              </div>
              <input id="cfg-model-value" type="text" placeholder="model or model1,model2,...">
              <div class="default-hint" id="cfg-model-default"></div>
              <div class="row">
                <button class="btn" onclick="setModel()">Save model</button>
                <button class="btn" onclick="resetModel()">Reset</button>
              </div>
              <div class="config-msg" id="cfg-model-msg"></div>
            </div>
          </div>
        </div>

        <div class="panel">
          <div class="panel-header"><span class="panel-title">Model Capabilities</span></div>
          <div class="panel-body">
            <table>
              <thead><tr>
                <th>Provider</th><th>Model</th><th>Rating</th><th>Tools</th><th>Reasoning</th>
              </tr></thead>
              <tbody id="model-caps-tbody"></tbody>
            </table>
          </div>
        </div>
      </section>

      <!-- ── Add-ons ──────────────────────────────────────────────────────── -->
      <section class="page" id="page-addons">
        <div class="panel">
          <div class="panel-header"><span class="panel-title">Feature Add-ons</span></div>
          <div class="addon-grid" id="addon-grid"></div>
        </div>

        <div class="panel">
          <div class="panel-header"><span class="panel-title">Cache</span></div>
          <div class="panel-body">
            <table>
              <tbody id="cache-tbody"></tbody>
            </table>
          </div>
        </div>
      </section>

      <!-- ── Request Log ──────────────────────────────────────────────────── -->
      <section class="page" id="page-logs">
        <div class="panel">
          <div class="panel-header">
            <span class="panel-title">Live Request Log</span>
            <div style="display:flex;gap:8px;align-items:center">
              <select id="log-filter-status" style="background:var(--surface);color:var(--muted);border:1px solid var(--border);border-radius:5px;padding:2px 6px;font-size:11px">
                <option value="">All statuses</option>
                <option value="success">success</option>
                <option value="error">error</option>
                <option value="cache_hit">cache_hit</option>
              </select>
              <select id="log-filter-endpoint" style="background:var(--surface);color:var(--muted);border:1px solid var(--border);border-radius:5px;padding:2px 6px;font-size:11px">
                <option value="">All endpoints</option>
                <option value="chat">chat</option>
                <option value="messages">messages</option>
                <option value="embeddings">embeddings</option>
              </select>
            </div>
          </div>
          <div class="panel-body" id="log-wrap">
            <table>
              <thead><tr>
                <th>Time</th><th>Endpoint</th><th>Provider</th><th>Model</th>
                <th class="right">Latency</th><th class="right">Complexity</th>
                <th class="right">Cascades</th><th class="right">Prompt tok</th>
                <th class="right">Compl tok</th><th>Status</th>
              </tr></thead>
              <tbody id="log-tbody"></tbody>
            </table>
          </div>
        </div>
      </section>

    </main>
  </div>
</div>

<script>
// ── state ──────────────────────────────────────────────────────────────────────
let apiKey = localStorage.getItem('hermes_dash_key') || '';
let statusData = null, usageData = null, logsData = [], accessKeysData = [], instancesData = [];
let editingKeyTail = null;
let autoInstanceBaseUrl = false;
let INTERVAL = 5000;
let timer = null;

// ── sidebar navigation ───────────────────────────────────────────────────────
const PAGES = ['overview', 'providers', 'instances', 'keys', 'access', 'models', 'addons', 'logs'];

function showPage(name) {
  if (!PAGES.includes(name)) name = 'overview';
  document.querySelectorAll('.page').forEach(el => el.classList.toggle('active', el.id === 'page-' + name));
  document.querySelectorAll('.nav-item').forEach(el => el.classList.toggle('active', el.dataset.page === name));
  location.hash = name;
  window.scrollTo({top: 0});
}

// ── key gate ──────────────────────────────────────────────────────────────────
(function init() {
  const initial = (location.hash || '').replace('#', '');
  if (PAGES.includes(initial)) showPage(initial);
  window.addEventListener('hashchange', () => {
    const h = (location.hash || '').replace('#', '');
    if (PAGES.includes(h)) showPage(h);
  });
  onInstanceModeChange();
  document.getElementById('inst-host-port').addEventListener('input', () => updateDockerBaseUrl());
  document.getElementById('inst-base-url').addEventListener('input', () => { autoInstanceBaseUrl = false; });
  if (apiKey) { document.getElementById('key-gate').classList.add('hidden'); start(); }
  document.getElementById('key-input').addEventListener('keydown', e => { if (e.key==='Enter') submitKey(); });
})();

function submitKey() {
  const v = document.getElementById('key-input').value.trim();
  if (!v) return;
  apiKey = v;
  localStorage.setItem('hermes_dash_key', v);
  const errEl = document.getElementById('gate-error');
  if (errEl) errEl.textContent = '';
  document.getElementById('key-gate').classList.add('hidden');
  start();
}

// ── polling ───────────────────────────────────────────────────────────────────
function start() { stop(); refresh(); loadConfigProviders(); timer = setInterval(refresh, INTERVAL); }

async function refresh() {
  try {
    const h = { 'Authorization': 'Bearer ' + apiKey };
    const logStatus  = document.getElementById('log-filter-status').value;
    const logEp      = document.getElementById('log-filter-endpoint').value;
    let logUrl = '/v1/logs?limit=100';
    if (logStatus) logUrl += '&status=' + logStatus;
    if (logEp)     logUrl += '&endpoint=' + logEp;

    const resps = await Promise.all([
      fetch('/v1/status', {headers:h}),
      fetch('/v1/usage',  {headers:h}),
      fetch(logUrl,       {headers:h}),
      fetch('/v1/config/proxy-keys', {headers:h}),
      fetch('/v1/instances', {headers:h}),
    ]);
    // fetch() only rejects on network errors, not on HTTP 4xx/5xx — so a bad key
    // (401) would otherwise parse to an error body and render as all-zeros. Detect
    // it explicitly and send the user back to the key gate instead of faking data.
    if (resps.some(r => r.status === 401)) {
      stop();
      apiKey = '';
      localStorage.removeItem('hermes_dash_key');
      showGate('That key was rejected (401). It must match one of PROXY_API_KEYS.');
      return;
    }
    if (resps.some(r => !r.ok)) { setHeader(false, 'HTTP ' + (resps.find(r=>!r.ok)||{}).status); return; }

    const [s, u, l, ak, inst] = await Promise.all(resps.map(r => r.json()));
    statusData = s; usageData = u; logsData = l.entries || [];
    accessKeysData = ak.keys || [];
    instancesData = inst.instances || [];
    renderAll();
    setHeader(true);
  } catch(e) {
    setHeader(false, 'unreachable');
  }
  document.getElementById('log-filter-status').onchange  = refresh;
  document.getElementById('log-filter-endpoint').onchange = refresh;
}

function stop() { if (timer) { clearInterval(timer); timer = null; } }

function showGate(errMsg) {
  stop();
  const gate = document.getElementById('key-gate');
  gate.classList.remove('hidden');
  const errEl = document.getElementById('gate-error');
  if (errEl) errEl.textContent = errMsg || '';
  const input = document.getElementById('key-input');
  input.value = '';
  input.focus();
}

function setHeader(ok, detail) {
  const dot = document.getElementById('hdr-dot');
  const lbl = document.getElementById('hdr-status');
  dot.className = ok ? 'dot' : 'dot err';
  lbl.textContent = ok ? 'live' : ('error' + (detail ? ' · ' + detail : ''));
  document.getElementById('last-update').textContent =
    'Updated ' + new Date().toLocaleTimeString();
}

// ── helpers ───────────────────────────────────────────────────────────────────
function esc(s){ return String(s==null?'':s).replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function attr(s){ return String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
const fmt = {
  num:  n => n == null ? '—' : Number(n).toLocaleString(),
  tok:  n => { if (n==null||n===0) return '0'; if (n>=1e9) return (n/1e9).toFixed(1)+'B'; if (n>=1e6) return (n/1e6).toFixed(1)+'M'; if (n>=1e3) return (n/1e3).toFixed(1)+'K'; return String(n); },
  pct:  n => n == null ? '—' : n.toFixed(1) + '%',
  ms:   n => n == null ? '—' : (n >= 1000 ? (n/1000).toFixed(1)+'s' : Math.round(n)+'ms'),
  usd:  n => n == null ? '—' : (n < 0.0001 ? '<$0.0001' : '$' + n.toFixed(4)),
  uptime: s => { if (!s) return '—'; const h=Math.floor(s/3600),m=Math.floor((s%3600)/60); return (h?h+'h ':'') + m+'m'; },
  time: ts => { if (!ts) return '—'; try { return new Date(ts).toLocaleTimeString(); } catch { return ts; } },
};

function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html != null) e.innerHTML = html;
  return e;
}

function ratingPips(r) {
  if (!r) return '<span class="muted">—</span>';
  const cls = ['','r1','r2','r3','r4','r5'][r] || 'r5';
  const labels = ['','Outstanding','Best','Good','Fair','Basic'];
  let h = `<div class="rating-bar ${cls}" title="${labels[r]||''}">`;
  for (let i=1;i<=5;i++) h += `<div class="rating-pip ${i<=r?'active':''}"></div>`;
  return h + '</div>';
}

function statusPill(s, breaker) {
  if (breaker) return '<span class="pill pill-err">⨂ tripped</span>';
  if (s && s.total_requests === 0) return '<span class="pill pill-grey">idle</span>';
  const erp = s ? (s.errors / (s.total_requests||1) * 100) : 0;
  if (erp > 20) return '<span class="pill pill-err">degraded</span>';
  if (erp > 5)  return '<span class="pill pill-warn">unstable</span>';
  return '<span class="pill pill-ok">healthy</span>';
}

function keyDots(keys) {
  if (!keys || !keys.length) return '<span class="muted">—</span>';
  return keys.map(k => {
    const cls = k.status === 'cooling' ? 'dot-warn' : 'dot-ok';
    const req = k.requests != null ? `${k.requests} req` : '';
    const title = (k.status === 'cooling' ? `cooling (${k.ready_in}s)` : 'ready') + (req ? ` · ${req}` : '');
    return `<span class="${cls}" title="${title}" style="display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:2px"></span>`;
  }).join('');
}

// ── render all ────────────────────────────────────────────────────────────────
function renderAll() {
  renderPlainOverview();
  renderNavHealth();
  renderInstanceNavHealth();
  renderRotationForm();
  renderStats();
  renderProviderCards();
  renderInstances();
  renderProviders();
  renderLogs();
  renderCache();
  renderAddons();
  renderKeys();
  renderAccessKeys();
  renderModelCaps();
}

function renderNavHealth() {
  const dot = document.getElementById('nav-dot-providers');
  if (!dot || !statusData) return;
  const vals = Object.values(statusData.providers || {});
  const openBreakers = vals.filter(p => p.breaker?.open).length;
  const totalReq = vals.reduce((a,p) => a + (p.stats?.total_requests || 0), 0);
  const totalErr = vals.reduce((a,p) => a + (p.stats?.errors || 0), 0);
  const errRate = totalReq ? totalErr / totalReq * 100 : 0;
  dot.className = 'nav-dot' + (openBreakers ? ' bad' : errRate > 5 ? ' warn' : '');
}

function instanceHealthCounts() {
  const total = instancesData.length;
  const healthy = instancesData.filter(i => i.live?.status === 'healthy').length;
  const unreachable = instancesData.filter(i => i.live?.status === 'unreachable').length;
  const authErrors = instancesData.filter(i => i.live?.status === 'auth_error').length;
  const unknown = instancesData.filter(i => !i.live?.status || i.live.status === 'unknown').length;
  const unhealthy = Math.max(0, total - healthy);
  return {total, healthy, unhealthy, unreachable, authErrors, unknown};
}

function renderInstanceNavHealth() {
  const dot = document.getElementById('nav-dot-instances');
  if (!dot) return;
  const c = instanceHealthCounts();
  dot.className = 'nav-dot' + (c.unreachable || c.authErrors ? ' bad' : c.unhealthy ? ' warn' : '');
}

function renderRotationForm() {
  const sel = document.getElementById('cfg-rotation-value');
  if (sel && statusData?.rotation?.mode) sel.value = statusData.rotation.mode;
}

function renderPlainOverview() {
  if (!statusData || !usageData) return;
  const prov = statusData.providers || {};
  const vals = Object.values(prov);
  const keyCount = vals.reduce((a,p) => a + ((p.keys || []).length), 0);
  const readyKeys = vals.reduce((a,p) => a + ((p.keys || []).filter(k => k.status === 'ready').length), 0);
  const openBreakers = vals.filter(p => p.breaker?.open).length;
  const active = vals.filter(p => (p.stats?.total_requests || 0) > 0).length;
  const totalReq = vals.reduce((a,p) => a + (p.stats?.total_requests || 0), 0);
  const totalErr = vals.reduce((a,p) => a + (p.stats?.errors || 0), 0);
  const errRate = totalReq ? totalErr / totalReq * 100 : 0;

  const state = document.getElementById('plain-state');
  const title = document.getElementById('plain-title');
  const msg = document.getElementById('plain-message');
  state.className = 'hero-state';
  if (!keyCount) {
    state.classList.add('bad');
    state.textContent = 'needs a key';
    title.textContent = 'Add one provider key to start routing';
    msg.textContent = 'Choose a provider below, paste its API key, then restart Hermes Router.';
  } else if (openBreakers || errRate > 25) {
    state.classList.add('warn');
    state.textContent = 'needs attention';
    title.textContent = 'Hermes is running, but some providers are failing';
    msg.textContent = 'Requests can still fall back to healthy providers. Add more keys or check providers with high errors.';
  } else {
    state.classList.add('good');
    state.textContent = 'ready';
    title.textContent = 'Hermes Router is ready';
    msg.textContent = active ? 'Traffic is flowing through your provider pool.' : 'No requests yet. Point your app at the endpoint below.';
  }

  document.getElementById('quick-endpoint').textContent = location.origin + '/v1';
  document.getElementById('quick-model').textContent = 'hermes-router';
  document.getElementById('quick-spend').textContent = fmt.usd(usageData.totals?.cost?.usd);
  setStep('step-key', keyCount > 0, `${readyKeys}/${keyCount} keys ready`);
  setStep('step-health', !openBreakers && errRate <= 25, openBreakers ? `${openBreakers} breaker open` : (errRate ? `${fmt.pct(errRate)} errors` : 'ok'));
}

function setStep(id, done, text) {
  const el = document.getElementById(id);
  el.className = 'setup-step ' + (done ? 'done' : 'warn');
  const label = document.getElementById(id + '-text');
  if (label) label.textContent = text;
}

function renderProviderCards() {
  if (!statusData) return;
  const prov = statusData.providers || {};
  const grid = document.getElementById('provider-card-grid');
  const entries = Object.entries(prov).sort(([an,a],[bn,b]) => {
    const badA = (a.breaker?.open ? 2 : 0) + ((a.stats?.errors || 0) > 0 ? 1 : 0);
    const badB = (b.breaker?.open ? 2 : 0) + ((b.stats?.errors || 0) > 0 ? 1 : 0);
    return badB - badA || an.localeCompare(bn);
  }).slice(0, 8);
  if (!entries.length) {
    grid.innerHTML = '<div class="provider-card bad"><div class="provider-name">No providers configured</div><div class="provider-model">Add an API key below.</div></div>';
    return;
  }
  grid.innerHTML = entries.map(([name,p]) => {
    const req = p.stats?.total_requests || 0;
    const err = p.stats?.errors || 0;
    const erp = req ? err / req * 100 : 0;
    const ready = (p.keys || []).filter(k => k.status === 'ready').length;
    const brk = p.breaker?.open;
    const cls = brk || erp > 25 ? 'bad' : erp > 5 ? 'warn' : '';
    const pill = brk ? '<span class="pill pill-err">paused</span>' :
      erp > 25 ? '<span class="pill pill-err">check</span>' :
      erp > 5 ? '<span class="pill pill-warn">watch</span>' :
      '<span class="pill pill-ok">ready</span>';
    return `<div class="provider-card ${cls}">
      <div class="provider-head"><span class="provider-name">${name}</span>${pill}</div>
      <div class="provider-model" title="${p.model || ''}">${p.model || 'no model'}</div>
      <div class="provider-meta"><span>${ready} ready key${ready===1?'':'s'}</span><span>${fmt.ms(p.stats?.avg_latency_ms)}</span></div>
    </div>`;
  }).join('');
}

// ── instances ────────────────────────────────────────────────────────────────
function renderInstances() {
  const summary = document.getElementById('instance-summary-grid');
  const tbody = document.getElementById('instances-tbody');
  if (!summary || !tbody) return;
  const health = instanceHealthCounts();
  const managed = instancesData.filter(i => i.mode === 'docker').length;
  const running = instancesData.filter(i => i.docker?.running).length;
  const attentionText = health.unreachable
    ? `${health.unreachable} unreachable`
    : health.authErrors
      ? `${health.authErrors} auth issue${health.authErrors===1?'':'s'}`
      : health.unhealthy
        ? `${health.unhealthy} need attention`
        : 'none';
  const attentionSub = health.unreachable
    ? 'health endpoint not reachable'
    : health.authErrors
      ? 'health ok, key rejected'
      : health.unknown
        ? 'waiting for first probe'
        : 'all registered instances reachable';
  const attentionClass = health.unreachable || health.authErrors ? 'bad' : health.unhealthy ? 'warn' : '';
  summary.innerHTML = [
    ['Registered', fmt.num(health.total), 'routers tracked by this dashboard', ''],
    ['Healthy', `${health.healthy}/${health.total || 0}`, 'health endpoint reachable', health.unhealthy ? 'warn' : ''],
    ['Needs attention', attentionText, attentionSub, attentionClass],
    ['Docker managed', fmt.num(managed), `${running} running container${running===1?'':'s'}`, ''],
  ].map(([label,value,sub,cls]) => `<div class="instance-card ${cls}">
    <div class="quick-label">${label}</div><div class="quick-value">${value}</div><div class="quick-sub">${sub}</div>
  </div>`).join('');

  if (!instancesData.length) {
    tbody.innerHTML = '<tr><td colspan="8"><div class="instance-empty"><div><strong>No instances yet</strong><br><span>Connect an existing router or launch a Docker router above.</span></div></div></td></tr>';
    return;
  }
  tbody.innerHTML = instancesData.map(i => {
    const live = i.live || {};
    const docker = i.docker || {};
    const healthCls = live.status === 'healthy' ? 'pill-ok' : live.status === 'auth_error' ? 'pill-warn' : 'pill-err';
    const dockerPill = i.mode !== 'docker'
      ? '<span class="pill pill-grey">external</span>'
      : docker.running
        ? '<span class="pill pill-ok">running</span>'
        : docker.exists
          ? `<span class="pill pill-warn">${esc(docker.status || 'stopped')}</span>`
          : `<span class="pill pill-grey">${esc(docker.status || 'not created')}</span>`;
    const env = i.env || {};
    const actions = i.mode === 'docker'
      ? `<button class="btn" onclick="instanceAction('${i.id}','start')">Start</button>
         <button class="btn" onclick="instanceAction('${i.id}','restart')">Restart</button>
         <button class="btn" onclick="instanceAction('${i.id}','stop')">Stop</button>
         <button class="btn danger" onclick="deleteInstance('${i.id}', true)">Delete</button>`
      : `<button class="btn danger" onclick="deleteInstance('${i.id}', false)">Delete</button>`;
    return `<tr>
      <td><strong>${esc(i.name)}</strong><br><span class="mono muted">${esc(i.id)}</span></td>
      <td>${esc(i.mode)}</td>
      <td class="mono"><a style="color:var(--accent)" href="${attr(i.base_url)}" target="_blank" rel="noreferrer">${esc(i.base_url)}</a></td>
      <td><span class="pill ${healthCls}" title="${attr(live.message || '')}">${esc(live.status || 'unknown')}</span></td>
      <td>${dockerPill}</td>
      <td class="mono muted">${i.api_key?.configured ? '...' + esc(i.api_key.tail) : 'none'}</td>
      <td class="muted" title="${attr((env.keys||[]).join(', '))}">${fmt.num(env.count || 0)} var${(env.count||0)===1?'':'s'}</td>
      <td><div class="instance-actions">${actions}</div></td>
    </tr>`;
  }).join('');
}

function setInstanceMode(mode) {
  document.getElementById('inst-mode').value = mode;
  onInstanceModeChange();
}

function onInstanceModeChange() {
  const mode = document.getElementById('inst-mode').value;
  const base = document.getElementById('inst-base-url');
  const image = document.getElementById('inst-image');
  const env = document.getElementById('inst-env');
  const external = mode === 'external';
  document.getElementById('mode-external').classList.toggle('active', external);
  document.getElementById('mode-docker').classList.toggle('active', !external);
  document.getElementById('inst-port-fields').classList.toggle('hidden-field', external);
  document.getElementById('inst-docker-options').classList.toggle('hidden-field', external);
  document.getElementById('inst-start-btn').classList.toggle('hidden-field', external);
  document.getElementById('inst-save-btn').textContent = external ? 'Register instance' : 'Create instance';
  if (mode === 'docker') {
    image.disabled = false;
    env.disabled = false;
    base.placeholder = 'auto from host port';
    document.getElementById('inst-base-hint').textContent = 'Filled from the host port unless you override it.';
    document.getElementById('inst-key-label').textContent = 'Instance access key';
    document.getElementById('inst-key-hint').textContent = 'Leave blank to generate one for this container.';
    document.getElementById('inst-api-key').placeholder = 'generated if blank';
    updateDockerBaseUrl();
  } else {
    image.disabled = true;
    env.disabled = true;
    base.placeholder = 'http://localhost:8320/v1';
    document.getElementById('inst-base-hint').textContent = 'Agents use this as their base URL.';
    document.getElementById('inst-key-label').textContent = 'Router access key';
    document.getElementById('inst-key-hint').textContent = 'Used to verify authenticated endpoints.';
    document.getElementById('inst-api-key').placeholder = 'sk-router-...';
  }
}

function updateDockerBaseUrl() {
  if (document.getElementById('inst-mode').value !== 'docker') return;
  const port = document.getElementById('inst-host-port').value.trim();
  const base = document.getElementById('inst-base-url');
  if (!port) return;
  if (!base.value.trim() || autoInstanceBaseUrl) {
    base.value = `http://localhost:${port}/v1`;
    autoInstanceBaseUrl = true;
  }
}

function parseInstanceEnv() {
  const raw = document.getElementById('inst-env').value;
  const env = {};
  for (const line of raw.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const idx = trimmed.indexOf('=');
    if (idx < 1) throw new Error('Env lines must be KEY=value.');
    const key = trimmed.slice(0, idx).trim();
    const value = trimmed.slice(idx + 1).trim();
    env[key] = value;
  }
  return env;
}

async function createInstance(start) {
  let env = {};
  try { env = parseInstanceEnv(); }
  catch(e) { setMsg('inst-msg', e.message, false); return; }
  const mode = document.getElementById('inst-mode').value;
  if (mode === 'external') env = {};
  const name = document.getElementById('inst-name').value.trim();
  const baseUrl = document.getElementById('inst-base-url').value.trim();
  const hostPort = document.getElementById('inst-host-port').value.trim();
  if (!name) { setMsg('inst-msg', 'Name this instance first.', false); return; }
  if (mode === 'external' && !baseUrl) { setMsg('inst-msg', 'Enter the router base URL.', false); return; }
  if (mode === 'docker' && !hostPort) { setMsg('inst-msg', 'Choose a host port for the Docker router.', false); return; }
  const body = {
    name,
    mode,
    base_url: baseUrl,
    host_port: hostPort,
    container_port: document.getElementById('inst-container-port').value,
    image: document.getElementById('inst-image').value,
    api_key: document.getElementById('inst-api-key').value,
    env,
    copy_provider_keys: mode === 'docker'
      ? [...document.querySelectorAll('#inst-copy-provider-keys input:checked')].map(el => el.value)
      : [],
    start,
  };
  try {
    const r = await fetch('/v1/instances', {
      method: 'POST',
      headers: {'Authorization':'Bearer '+apiKey, 'Content-Type':'application/json'},
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!r.ok) { setMsg('inst-msg', d.error?.message || 'Failed to save instance.', false); return; }
    ['inst-name','inst-base-url','inst-host-port','inst-api-key','inst-env'].forEach(id => document.getElementById(id).value = '');
    document.querySelectorAll('#inst-copy-provider-keys input:checked').forEach(el => el.checked = false);
    autoInstanceBaseUrl = false;
    if (d.generated_api_key) {
      document.getElementById('new-instance-key-value').value = d.generated_api_key;
      document.getElementById('new-instance-key-panel').style.display = 'block';
    }
    await refresh();
    const saved = instancesData.find(i => i.id === d.instance?.id);
    const savedStatus = saved?.live?.status;
    if (d.action && !d.action.ok) {
      setWarnMsg('inst-msg', 'Saved, but Docker start failed: ' + d.action.message);
    } else if (savedStatus && savedStatus !== 'healthy') {
      setWarnMsg('inst-msg', `Instance saved, but it is ${savedStatus}.`);
    } else {
      setMsg('inst-msg', 'Instance saved.', true);
    }
  } catch(e) { setMsg('inst-msg', 'Network error: ' + e.message, false); }
}

async function instanceAction(id, action) {
  try {
    const r = await fetch(`/v1/instances/${id}/${action}`, {
      method: 'POST',
      headers: {'Authorization':'Bearer '+apiKey},
    });
    const d = await r.json();
    if (!r.ok || !d.ok) { alert(d.message || d.error?.message || 'Instance action failed.'); return; }
    await refresh();
  } catch(e) { alert('Network error: ' + e.message); }
}

async function deleteInstance(id, managed) {
  const msg = managed
    ? 'Delete this instance from the registry and remove its Docker container?'
    : 'Delete this instance from the registry?';
  if (!confirm(msg)) return;
  const qs = managed ? '?remove_container=1' : '';
  try {
    const r = await fetch('/v1/instances/' + id + qs, {
      method: 'DELETE',
      headers: {'Authorization':'Bearer '+apiKey},
    });
    const d = await r.json();
    if (!r.ok) { alert(d.error?.message || 'Failed to delete instance.'); return; }
    await refresh();
  } catch(e) { alert('Network error: ' + e.message); }
}

function copyInstanceKey() {
  const el = document.getElementById('new-instance-key-value');
  el.select();
  navigator.clipboard?.writeText(el.value).catch(() => document.execCommand('copy'));
}

function dismissInstanceKey() {
  document.getElementById('new-instance-key-panel').style.display = 'none';
  document.getElementById('new-instance-key-value').value = '';
}

// ── stat cards ────────────────────────────────────────────────────────────────
function renderStats() {
  if (!statusData || !usageData) return;
  const prov   = statusData.providers || {};
  const nProv  = Object.keys(prov).length;
  const broken = Object.values(prov).filter(p => p.breaker && p.breaker.open).length;
  document.getElementById('s-providers').textContent = nProv;
  document.getElementById('s-providers-sub').textContent =
    broken ? `${broken} breaker(s) tripped` : 'all healthy';

  document.getElementById('s-uptime').textContent = fmt.uptime(usageData.uptime_s);

  const totReq = Object.values(prov).reduce((a,p) => a + (p.stats?.total_requests||0), 0);
  const totErr = Object.values(prov).reduce((a,p) => a + (p.stats?.errors||0), 0);
  document.getElementById('s-requests').textContent = fmt.num(totReq);
  document.getElementById('s-requests-sub').textContent =
    totReq ? fmt.pct(totErr/totReq*100) + ' error rate' : '';

  const tot = usageData.totals || {};
  document.getElementById('s-tokens').textContent = fmt.tok(tot.tokens);
  document.getElementById('s-cost').textContent   = 'est. ' + fmt.usd(tot.cost?.usd);

  const cache = statusData.cache || {};
  document.getElementById('s-hitrate').textContent = fmt.pct((cache.hit_rate||0)*100);
  document.getElementById('s-cache-sub').textContent =
    `${fmt.num(cache.hits)} hits / ${fmt.num(cache.misses)} misses`;

  const errRate = totReq ? (totErr / totReq * 100) : 0;
  const errEl = document.getElementById('s-errrate');
  errEl.textContent = fmt.pct(errRate);
  errEl.style.color = errRate > 10 ? 'var(--red)' : errRate > 3 ? 'var(--yellow)' : 'var(--green)';
  document.getElementById('s-errrate-sub').textContent = fmt.num(totErr) + ' total errors';
}

// ── provider table ────────────────────────────────────────────────────────────
function renderProviders() {
  if (!statusData) return;
  const prov = statusData.providers || {};
  const tbody = document.getElementById('provider-tbody');
  tbody.innerHTML = '';
  Object.entries(prov).forEach(([name, p]) => {
    const s   = p.stats || {};
    const req = s.total_requests || 0;
    const err = s.errors || 0;
    const erp = req ? err / req * 100 : 0;
    const lat = s.avg_latency_ms;
    const brk = p.breaker?.open || false;
    const tr  = document.createElement('tr');
    tr.innerHTML = `
      <td><strong>${name}</strong></td>
      <td class="muted mono" style="max-width:160px;overflow:hidden;text-overflow:ellipsis" title="${p.model||''}">${p.model||'—'}</td>
      <td>${ratingPips(p.rating)}</td>
      <td class="right">${fmt.num(req)}</td>
      <td class="right ${err>0?'':'muted'}">${fmt.num(err)}</td>
      <td class="right" style="color:${erp>10?'var(--red)':erp>3?'var(--yellow)':'var(--muted)'}">${req?fmt.pct(erp):'—'}</td>
      <td class="right">${fmt.ms(lat)}</td>
      <td class="right muted">${fmt.tok(p.tokens)}</td>
      <td class="right muted">${fmt.usd(p.cost_usd)}</td>
      <td>${keyDots(p.keys)}</td>
      <td>${brk?'<span class="pill pill-err">open</span>':'<span class="pill pill-ok">closed</span>'}</td>
      <td>${statusPill(s, brk)}</td>
    `;
    tbody.appendChild(tr);
  });
}

// ── live log ──────────────────────────────────────────────────────────────────
function renderLogs() {
  const tbody = document.getElementById('log-tbody');
  if (!logsData.length) {
    tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:var(--muted);padding:24px">No requests logged yet</td></tr>';
    return;
  }
  tbody.innerHTML = '';
  logsData.forEach(e => {
    const tr = document.createElement('tr');
    tr.className = 'log-row-' + (e.status||'');
    const sp = e.status === 'success' ? 'pill-ok' : e.status === 'error' ? 'pill-err' : 'pill-cache';
    const cascBadge = e.cascades > 0
      ? `<span class="pill pill-warn">${e.cascades}</span>`
      : '<span class="muted">0</span>';
    const cmpx = e.complexity;
    const cmpxColor = !cmpx ? 'var(--muted)' : cmpx<=2?'var(--red)':cmpx>=5?'var(--green)':'var(--yellow)';
    tr.innerHTML = `
      <td class="mono muted">${fmt.time(e.ts)}</td>
      <td>${e.endpoint||'—'}</td>
      <td><strong>${e.provider||'—'}</strong></td>
      <td class="muted mono" style="max-width:140px;overflow:hidden;text-overflow:ellipsis" title="${e.model||''}">${e.model||'—'}</td>
      <td class="right">${fmt.ms(e.latency_ms)}</td>
      <td class="right" style="color:${cmpxColor}">${cmpx||'—'}</td>
      <td class="right">${cascBadge}</td>
      <td class="right muted">${e.prompt_tokens!=null?fmt.num(e.prompt_tokens):'—'}</td>
      <td class="right muted">${e.completion_tokens!=null?fmt.num(e.completion_tokens):'—'}</td>
      <td><span class="pill ${sp}">${e.status||'—'}</span></td>
    `;
    tbody.appendChild(tr);
  });
}

// ── cache panel ───────────────────────────────────────────────────────────────
function renderCache() {
  if (!statusData) return;
  const c = statusData.cache || {};
  const sem = c.semantic || {};
  const rows = [
    ['Enabled',       c.enabled ? '<span class="pill pill-ok">yes</span>' : '<span class="pill pill-grey">no</span>'],
    ['TTL',           c.ttl_s != null ? c.ttl_s + 's' : '—'],
    ['Size',          `${fmt.num(c.size)} / ${fmt.num(c.max_size)}`],
    ['Persistent',    c.persistent ? '<span class="pill pill-ok">yes</span>' : '<span class="pill pill-grey">no</span>'],
    ['Hits',          fmt.num(c.hits)],
    ['Misses',        fmt.num(c.misses)],
    ['Hit rate',      fmt.pct((c.hit_rate||0)*100)],
    ['Semantic cache',sem.enabled ? '<span class="pill pill-ok">yes</span>' : '<span class="pill pill-grey">no</span>'],
    ['Semantic hits', fmt.num(sem.hits)],
    ['Sem. threshold',sem.threshold != null ? sem.threshold : '—'],
  ];
  const tbody = document.getElementById('cache-tbody');
  tbody.innerHTML = rows.map(([k,v]) =>
    `<tr><td class="muted" style="width:50%">${k}</td><td>${v}</td></tr>`
  ).join('');
}

// ── add-ons panel ─────────────────────────────────────────────────────────────
function renderAddons() {
  if (!statusData?.features) return;
  const addons = statusData.features.addons || [];
  const grid = document.getElementById('addon-grid');
  grid.innerHTML = addons.map(a => {
    const clickable = a.kind === 'flag';
    const attrs = clickable ? `onclick="toggleAddon('${a.name}', ${!a.enabled})" title="Click to ${a.enabled?'disable':'enable'}"` : '';
    return `
    <div class="addon-card ${clickable?'flag':''}" id="addon-${a.name}" ${attrs}>
      <div class="addon-top">
        <span class="addon-name">${a.title || a.name}</span>
        <span class="pill ${a.enabled ? 'pill-ok' : 'pill-grey'}">${a.enabled ? 'on' : 'off'}</span>
      </div>
      <div class="addon-desc">${a.desc || ''}</div>
      ${a.env ? `<div class="mono muted" style="margin-top:5px;font-size:10px">${a.env}</div>` : ''}
    </div>
  `;
  }).join('');
}

// ── config: add-ons toggle ───────────────────────────────────────────────────
async function toggleAddon(name, enable) {
  const card = document.getElementById('addon-' + name);
  if (card) card.classList.add('busy');
  try {
    const r = await fetch('/v1/config/features/' + name, {
      method: 'POST',
      headers: {'Authorization':'Bearer '+apiKey, 'Content-Type':'application/json'},
      body: JSON.stringify({enabled: enable}),
    });
    const d = await r.json();
    if (!r.ok) { alert(d.error?.message || 'Failed to toggle ' + name); return; }
    showRestartBanner();
    await refresh();
  } catch(e) {
    alert('Network error: ' + e.message);
  } finally {
    if (card) card.classList.remove('busy');
  }
}

// ── config: providers / add key / model ──────────────────────────────────────
let configProviders = null;

async function loadConfigProviders() {
  try {
    const r = await fetch('/v1/config/providers', {headers:{'Authorization':'Bearer '+apiKey}});
    if (!r.ok) return;
    configProviders = await r.json();
    const keySel = document.getElementById('cfg-key-provider');
    keySel.innerHTML = configProviders.key_settable.map(p => `<option value="${p}">${p}</option>`).join('');
    const modelSel = document.getElementById('cfg-model-provider');
    modelSel.innerHTML = configProviders.model_settable.map(p => `<option value="${p}">${p}</option>`).join('');
    onModelProviderChange();
    renderProviderScopePicker();
    renderInstanceCopyKeyPicker();
    renderAccessKeys();   // re-render now that provider names/counts are known
  } catch(e) { /* dashboard still usable without this */ }
}

// Providers a caller might sensibly scope an access key to — anything with a
// live key count, or already model-settable (covers keyless "local"). Sorted
// with the most-provisioned providers first, since those are the likely picks.
function scopeableProviders() {
  if (!configProviders) return [];
  const counts = configProviders.key_counts || {};
  const names = new Set([...Object.keys(counts), ...configProviders.model_settable]);
  return [...names].sort((a,b) => (counts[b]||0) - (counts[a]||0) || a.localeCompare(b));
}

function renderProviderScopePicker() {
  const grid = document.getElementById('ak-provider-scope');
  if (!grid) return;
  grid.innerHTML = renderScopeCheckboxes([]);
}

function renderInstanceCopyKeyPicker() {
  const grid = document.getElementById('inst-copy-provider-keys');
  if (!grid) return;
  const counts = configProviders?.copyable_provider_keys || {};
  const names = Object.keys(counts).filter(name => counts[name] > 0)
    .sort((a,b) => counts[b] - counts[a] || a.localeCompare(b));
  if (!names.length) {
    grid.innerHTML = '<div class="field-hint">No configured provider keys are available to copy yet.</div>';
    return;
  }
  grid.innerHTML = names.map(name => {
    const n = counts[name] || 0;
    return `<label class="scope-item"><input type="checkbox" value="${attr(name)}"> ${esc(name)}<span class="cnt">${n} key${n===1?'':'s'}</span></label>`;
  }).join('');
}

function onModelProviderChange() {
  if (!configProviders) return;
  const p = document.getElementById('cfg-model-provider').value;
  const def = configProviders.defaults[p] || '';
  document.getElementById('cfg-model-default').textContent = 'default: ' + def;
  const current = statusData?.providers?.[p]?.model || '';
  document.getElementById('cfg-model-value').value = (current && current !== def) ? current : '';
  document.getElementById('cfg-model-value').placeholder = def;
}

function setMsg(id, text, ok) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = 'config-msg ' + (ok ? 'ok' : 'err');
}

function setWarnMsg(id, text) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = 'config-msg warn';
}

async function addKey() {
  const provider = document.getElementById('cfg-key-provider').value;
  const key = document.getElementById('cfg-key-value').value.trim();
  if (!key) { setMsg('cfg-key-msg', 'Enter a key first.', false); return; }
  try {
    const r = await fetch('/v1/config/keys/' + provider, {
      method: 'POST',
      headers: {'Authorization':'Bearer '+apiKey, 'Content-Type':'application/json'},
      body: JSON.stringify({key}),
    });
    const d = await r.json();
    if (!r.ok) { setMsg('cfg-key-msg', d.error?.message || 'Failed.', false); return; }
    if (d.duplicate) { setMsg('cfg-key-msg', 'Already stored — no change.', false); return; }
    document.getElementById('cfg-key-value').value = '';
    setMsg('cfg-key-msg', `Saved — ${provider} now has ${d.total_keys} key(s).`, true);
    showRestartBanner();
  } catch(e) { setMsg('cfg-key-msg', 'Network error: ' + e.message, false); }
}

async function setModel() {
  const provider = document.getElementById('cfg-model-provider').value;
  const model = document.getElementById('cfg-model-value').value.trim();
  if (!model) { setMsg('cfg-model-msg', 'Enter a model first.', false); return; }
  try {
    const r = await fetch('/v1/config/model/' + provider, {
      method: 'POST',
      headers: {'Authorization':'Bearer '+apiKey, 'Content-Type':'application/json'},
      body: JSON.stringify({model}),
    });
    const d = await r.json();
    if (!r.ok) { setMsg('cfg-model-msg', d.error?.message || 'Failed.', false); return; }
    setMsg('cfg-model-msg', `Set ${provider} → ${model}`, true);
    showRestartBanner();
  } catch(e) { setMsg('cfg-model-msg', 'Network error: ' + e.message, false); }
}

async function resetModel() {
  const provider = document.getElementById('cfg-model-provider').value;
  try {
    const r = await fetch('/v1/config/model/' + provider, {
      method: 'DELETE',
      headers: {'Authorization':'Bearer '+apiKey},
    });
    const d = await r.json();
    if (!r.ok) { setMsg('cfg-model-msg', d.error?.message || 'Failed.', false); return; }
    document.getElementById('cfg-model-value').value = '';
    setMsg('cfg-model-msg', `Reset ${provider} to default.`, true);
    showRestartBanner();
  } catch(e) { setMsg('cfg-model-msg', 'Network error: ' + e.message, false); }
}

async function setRotation() {
  const mode = document.getElementById('cfg-rotation-value').value;
  try {
    const r = await fetch('/v1/config/rotation', {
      method: 'POST',
      headers: {'Authorization':'Bearer '+apiKey, 'Content-Type':'application/json'},
      body: JSON.stringify({mode}),
    });
    const d = await r.json();
    if (!r.ok) { setMsg('cfg-rotation-msg', d.error?.message || 'Failed.', false); return; }
    setMsg('cfg-rotation-msg', 'Saved rotation mode.', true);
    showRestartBanner();
  } catch(e) { setMsg('cfg-rotation-msg', 'Network error: ' + e.message, false); }
}

// ── restart ───────────────────────────────────────────────────────────────────
function showRestartBanner() {
  document.getElementById('restart-banner').classList.add('show');
  setStep('step-restart', false, 'needed');
}
function dismissBanner() { document.getElementById('restart-banner').classList.remove('show'); }

async function doRestart() {
  if (!confirm('Restart the router now? It will be unreachable for a few seconds.')) return;
  try {
    await fetch('/v1/config/restart', {method:'POST', headers:{'Authorization':'Bearer '+apiKey}});
  } catch(e) { /* the process may already be going down mid-response — expected */ }
  dismissBanner();
  setStep('step-restart', true, 'restarting');
  setHeader(false, 'restarting…');
  stop();
  // Poll /health until it responds again, then resume normal operation.
  const waitForRestart = setInterval(async () => {
    try {
      const r = await fetch('/health');
      if (r.ok) { clearInterval(waitForRestart); start(); }
    } catch(e) { /* still down — keep polling */ }
  }, 1500);
}

// ── key usage ─────────────────────────────────────────────────────────────────
function renderKeys() {
  if (!usageData) return;
  const keys = usageData.keys || [];
  const limData = statusData?.limits || {};
  const tbody = document.getElementById('keys-tbody');
  if (!keys.length) {
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:18px">No key data</td></tr>';
    return;
  }
  tbody.innerHTML = keys.map(k => {
    const limEntry = (limData.keys || []).find(l => l.key_tail === k.key_tail);
    const lim = limEntry?.limits || {};
    const rpmUsed = k.rpm_current || 0;
    const rpmMax  = lim.rpm || 0;
    const rpmPct  = rpmMax ? Math.min(rpmUsed / rpmMax * 100, 100) : 0;
    const rpmColor = rpmPct > 80 ? 'red' : rpmPct > 50 ? 'yellow' : 'green';
    return `<tr>
      <td class="mono">...${k.key_tail}</td>
      <td class="right">${fmt.num(k.req_total)}</td>
      <td class="right">
        ${fmt.tok(k.tokens_today)}
        ${lim.tokens_day ? `<br><span class="muted" style="font-size:10px">/ ${fmt.tok(lim.tokens_day)}</span>` : ''}
      </td>
      <td class="right">
        ${fmt.usd(k.cost_today)}
        ${lim.cost_day ? `<br><span class="muted" style="font-size:10px">/ ${fmt.usd(lim.cost_day)}</span>` : ''}
      </td>
      <td style="min-width:100px">
        ${rpmMax
          ? `<div class="prog-track"><div class="prog-fill ${rpmColor}" style="width:${rpmPct}%"></div></div>
             <span class="muted" style="font-size:10px">${rpmUsed}/${rpmMax} rpm</span>`
          : '<span class="muted">unlimited</span>'}
      </td>
    </tr>`;
  }).join('');
}

// ── access keys (proxy keys others use to call the router) ───────────────────
function renderAccessKeys() {
  const tbody = document.getElementById('access-keys-tbody');
  if (!tbody) return;
  if (!accessKeysData.length) {
    tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;color:var(--muted);padding:18px">No access keys yet</td></tr>';
    return;
  }
  tbody.innerHTML = accessKeysData.map(k => {
    const tail = k.key_tail;
    const lim = k.limits || {};
    const used = k.usage || {};
    const allowed = k.allowed_providers || [];
    const statusPill = k.pending_restart
      ? '<span class="pill pill-warn">pending restart</span>'
      : '<span class="pill pill-ok">active</span>';

    if (editingKeyTail === tail) {
      return `<tr>
        <td><input id="edit-name-${tail}" type="text" value="${attr(k.name||'')}" style="width:110px;background:var(--surface2);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:4px 6px;font-size:11px"></td>
        <td class="mono muted">...${esc(tail)}</td>
        <td class="right"><input id="edit-rpm-${tail}" type="number" min="0" value="${lim.rpm||''}" placeholder="∞" style="width:55px;text-align:right;background:var(--surface2);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:4px 6px;font-size:11px"></td>
        <td class="right"><input id="edit-reqday-${tail}" type="number" min="0" value="${lim.req_per_day||''}" placeholder="∞" style="width:65px;text-align:right;background:var(--surface2);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:4px 6px;font-size:11px"></td>
        <td class="right"><input id="edit-tokday-${tail}" type="number" min="0" value="${lim.tokens_per_day||''}" placeholder="∞" style="width:75px;text-align:right;background:var(--surface2);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:4px 6px;font-size:11px"></td>
        <td class="right"><input id="edit-costday-${tail}" type="number" min="0" step="0.01" value="${lim.cost_per_day||''}" placeholder="∞" style="width:65px;text-align:right;background:var(--surface2);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:4px 6px;font-size:11px"></td>
        <td class="right muted">${fmt.num(used.req_today)}</td>
        <td><div class="scope-grid" id="edit-scope-${tail}" style="min-width:180px">${renderScopeCheckboxes(allowed)}</div></td>
        <td>${statusPill}</td>
        <td style="white-space:nowrap"><button class="btn" onclick="saveEditAccessKey('${tail}')">Save</button> <button class="btn" onclick="cancelEditAccessKey()">Cancel</button></td>
      </tr>`;
    }
    return `<tr>
      <td>${esc(k.name || '—')}</td>
      <td class="mono muted">...${esc(tail)}</td>
      <td class="right">${lim.rpm || '∞'}</td>
      <td class="right">${lim.req_per_day || '∞'}</td>
      <td class="right">${lim.tokens_per_day ? fmt.tok(lim.tokens_per_day) : '∞'}</td>
      <td class="right">${lim.cost_per_day ? fmt.usd(lim.cost_per_day) : '∞'}</td>
      <td class="right muted">${fmt.num(used.req_today)}</td>
      <td class="muted">${allowed.length ? esc(allowed.join(', ')) : 'all'}</td>
      <td>${statusPill}</td>
      <td style="white-space:nowrap"><button class="btn" onclick="startEditAccessKey('${tail}')">Edit</button> <button class="btn" onclick="revokeAccessKey('${tail}')">Revoke</button></td>
    </tr>`;
  }).join('');
}

function renderScopeCheckboxes(selected) {
  const sel = new Set(selected || []);
  return scopeableProviders().map(name => {
    const n = (configProviders && configProviders.key_counts || {})[name];
    const cnt = n != null ? `<span class="cnt">${n} key${n===1?'':'s'}</span>` : '';
    const checked = sel.has(name) ? ' checked' : '';
    return `<label class="scope-item"><input type="checkbox" value="${attr(name)}"${checked}> ${esc(name)}${cnt}</label>`;
  }).join('');
}

function startEditAccessKey(tail) { editingKeyTail = tail; renderAccessKeys(); }
function cancelEditAccessKey() { editingKeyTail = null; renderAccessKeys(); }

async function saveEditAccessKey(tail) {
  const scopeEl = document.getElementById(`edit-scope-${tail}`);
  const allowedProviders = scopeEl ? [...scopeEl.querySelectorAll('input:checked')].map(el => el.value) : [];
  const body = {
    name: document.getElementById(`edit-name-${tail}`).value,
    rpm: document.getElementById(`edit-rpm-${tail}`).value,
    req_per_day: document.getElementById(`edit-reqday-${tail}`).value,
    tokens_per_day: document.getElementById(`edit-tokday-${tail}`).value,
    cost_per_day: document.getElementById(`edit-costday-${tail}`).value,
    allowed_providers: allowedProviders,
  };
  try {
    const r = await fetch('/v1/config/proxy-keys/' + tail, {
      method: 'POST',
      headers: {'Authorization':'Bearer '+apiKey, 'Content-Type':'application/json'},
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!r.ok) { alert(d.error?.message || 'Failed to save.'); return; }
    editingKeyTail = null;
    showRestartBanner();
    await refresh();
  } catch(e) { alert('Network error: ' + e.message); }
}

async function revokeAccessKey(tail) {
  if (!confirm('Revoke access key ...' + tail + '? Anyone using it will lose access after the next restart.')) return;
  try {
    const r = await fetch('/v1/config/proxy-keys/' + tail, {
      method: 'DELETE',
      headers: {'Authorization':'Bearer '+apiKey},
    });
    const d = await r.json();
    if (!r.ok) { alert(d.error?.message || 'Failed to revoke.'); return; }
    showRestartBanner();
    await refresh();
  } catch(e) { alert('Network error: ' + e.message); }
}

async function createAccessKey() {
  const checked = [...document.querySelectorAll('#ak-provider-scope input:checked')].map(el => el.value);
  const body = {
    name: document.getElementById('ak-name').value,
    rpm: document.getElementById('ak-rpm').value,
    req_per_day: document.getElementById('ak-reqday').value,
    tokens_per_day: document.getElementById('ak-tokday').value,
    cost_per_day: document.getElementById('ak-costday').value,
    allowed_providers: checked,
  };
  try {
    const r = await fetch('/v1/config/proxy-keys', {
      method: 'POST',
      headers: {'Authorization':'Bearer '+apiKey, 'Content-Type':'application/json'},
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (!r.ok) { setMsg('ak-create-msg', d.error?.message || 'Failed to create key.', false); return; }
    ['ak-name','ak-rpm','ak-reqday','ak-tokday','ak-costday'].forEach(id => document.getElementById(id).value = '');
    document.querySelectorAll('#ak-provider-scope input:checked').forEach(el => el.checked = false);
    setMsg('ak-create-msg', 'Key created.', true);
    document.getElementById('new-key-value').value = d.key;
    document.getElementById('new-key-panel').style.display = 'block';
    document.getElementById('new-key-panel').scrollIntoView({behavior:'smooth', block:'nearest'});
    showRestartBanner();
    await refresh();
  } catch(e) { setMsg('ak-create-msg', 'Network error: ' + e.message, false); }
}

function copyNewKey() {
  const el = document.getElementById('new-key-value');
  el.select();
  navigator.clipboard?.writeText(el.value).catch(() => document.execCommand('copy'));
}

function dismissNewKey() {
  document.getElementById('new-key-panel').style.display = 'none';
  document.getElementById('new-key-value').value = '';
}

// ── model capabilities (per-provider, per-model rating/tools/reasoning) ──────
function renderModelCaps() {
  const tbody = document.getElementById('model-caps-tbody');
  if (!tbody || !statusData) return;
  const prov = statusData.providers || {};
  const rows = [];
  Object.entries(prov).forEach(([name, p]) => {
    const caps = p.model_caps;
    if (caps && caps.length) {
      caps.forEach(mc => rows.push({provider: name, model: mc.model, rating: mc.rating,
        tools: mc.supports_tools, reasoning: mc.reasoning}));
    } else if (p.model) {
      rows.push({provider: name, model: p.model, rating: p.rating,
        tools: p.supports_tools, reasoning: p.reasoning});
    }
  });
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:18px">No providers configured</td></tr>';
    return;
  }
  rows.sort((a,b) => a.provider.localeCompare(b.provider) || (a.rating||9) - (b.rating||9));
  tbody.innerHTML = rows.map(r => `<tr>
    <td>${esc(r.provider)}</td>
    <td class="mono muted">${esc(r.model||'—')}</td>
    <td>${ratingPips(r.rating)}</td>
    <td>${r.tools ? '<span class="pill pill-ok">yes</span>' : '<span class="pill pill-grey">no</span>'}</td>
    <td>${r.reasoning ? '<span class="pill pill-ok">yes</span>' : '<span class="pill pill-grey">no</span>'}</td>
  </tr>`).join('');
}
</script>
</body>
</html>
"""


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
    return jsonify({"status": "ok", "providers": [p["name"] for p in PROVIDERS]})


@app.route("/v1/models")
def models():
    err = _auth_check()
    if err:
        return err
    data = [{"id": ROUTER_MODEL, "object": "model", "owned_by": "hermes-router"}]
    # Advertise the fast/conversation profile only when a local model is configured,
    # since that's what it routes short turns to.
    if any(p["name"] == "local" for p in PROVIDERS):
        data.append({"id": f"{ROUTER_MODEL}:fast", "object": "model", "owned_by": "hermes-router"})
    return jsonify({"object": "list", "data": data})


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

    # Cache check (non-streaming only): exact match first (cheap), then optional
    # semantic match. query_emb is reused to store the response so future similar
    # prompts can match it.
    query_emb = None
    if not streaming and not tool_loop:
        cached = cache.get(payload, ns)
        if cached is not None:
            log.info("↩ cache hit")
            _req_ctx.cache_hit = True
            return ("json", cached)
        if SEMANTIC_CACHE and _embed_ordered():
            query_emb = _embed_text(_prompt_text(messages))
            if query_emb:
                hit = cache.semantic_lookup(query_emb, ns)
                if hit is not None:
                    log.info("↩ semantic cache hit")
                    _req_ctx.cache_hit = True
                    return ("json", hit)

    est_tokens = _estimated_tokens(messages)
    ordered    = _ordered_providers(payload, prefer_local, workload_hint)
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
        caller_providers = KEY_PROVIDER_SCOPE.get(_caller_token())
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

        if name in skip_providers:
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

        attempts = pool.key_count(name, model) or 1
        for _ in range(attempts):
            key = pool.get_key(name, model)
            if not key:
                break   # all keys for this (provider, model) are cooling → next candidate

            log.info(f"→ Trying {name}/{model} ...{key[-6:]}")
            _req_ctx.attempts += 1
            t0   = time.time()
            resp = forward(provider, key, payload, streaming, model)
            elapsed = time.time() - t0

            if resp is None:
                stats.record_error(name)
                stats.record_health(name, False)   # network/timeout = provider health failure
                pool.mark_key_down(name, key, retry_after=30)
                continue

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
            stats.record_success(name, elapsed)
            stats.record_health(name, True)        # 2xx = healthy (half-open recovery)
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
                    return ("stream", _with_cleanup(resp, _codex_streaming_generator(resp)), name)
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
                if not tool_loop:
                    cache.set(payload, data, ns, query_emb)
                _session_affinity_set(session_affinity_id, name, model)
                return ("json", data)
            if streaming:
                if session_affinity_id is not None:
                    _session_affinity_set(session_affinity_id, name, model)
                gen = (_anthropic_streaming_generator(resp) if is_anthropic
                       else _streaming_generator(resp))
                wrapped = _streaming_with_usage(_with_cleanup(resp, gen), name, model)
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
                    _strip_response(data)
                _add_provider_tokens(name, data, model)
                if not tool_loop:
                    cache.set(payload, data, ns, query_emb)
                _session_affinity_set(session_affinity_id, name, model)
                return ("json", data)

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
        return Response(stream_with_context(gen), content_type="text/event-stream",
                        headers={"X-Provider": name})
    return jsonify(result[1]), result[2]


@app.route("/v1/messages", methods=["POST"])
def anthropic_messages():
    """Anthropic Messages API endpoint — lets the Anthropic SDK use the router
    plug-and-play. The request is translated to OpenAI format, routed through the
    same multi-provider pipeline as /v1/chat/completions, and translated back."""
    err = _auth_check()
    if err:
        return err

    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict) or "messages" not in body:
        return jsonify(_anthropic_error("request body must be a JSON object with a 'messages' field")), 400

    token  = _caller_token()
    gate   = _admit_request(token)
    if gate:
        # Translate the 429 to Anthropic's error shape for SDK callers.
        return jsonify(_anthropic_error("quota exceeded")), 429

    streaming = bool(body.get("stream", False))
    payload   = _anthropic_request_to_openai(body)
    t_start   = time.time()
    result    = _route_completion(payload, streaming, _cache_ns())
    _record_request_tokens(token, payload, result)

    _log_completion(token, "messages", payload, result, time.time() - t_start)

    if result[0] == "json":
        return jsonify(_openai_response_to_anthropic(result[1])), 200
    if result[0] == "stream":
        _, gen, name = result
        return Response(stream_with_context(_openai_stream_to_anthropic(gen)),
                        content_type="text/event-stream", headers={"X-Provider": name})
    return jsonify(_anthropic_error(result[1].get("error", {}).get("message", "error"))), result[2]


@app.route("/v1/embeddings", methods=["POST"])
def embeddings():
    err = _auth_check()
    if err:
        return err

    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict) or "input" not in payload:
        return jsonify({"error": {"message": "request body must be a JSON object with an 'input' field",
                                  "type": "invalid_request_error"}}), 400

    token  = _caller_token()
    gate   = _admit_request(token)
    if gate:
        return gate

    ordered = _embed_ordered()
    if not ordered:
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
            key = pool.get_key(name, em)
            if not key:
                log.warning(f"All {name} keys cooling — skipping provider")
                break

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
                continue
            if resp.status_code in (400, 401, 403, 404):
                stats.record_error(name)   # request/auth/model-specific, not a health failure
                log.error(f"  {name} embeddings {resp.status_code} — skipping provider: {resp.text[:200]}")
                break
            if resp.status_code >= 500:
                stats.record_error(name); stats.record_health(name, False)
                pool.mark_key_down(name, key, retry_after=15)
                continue
            if not (200 <= resp.status_code < 300):
                stats.record_error(name); stats.record_health(name, False)
                log.warning(f"  {name} embeddings unexpected {resp.status_code} — skipping provider")
                break

            stats.record_success(name, elapsed); stats.record_health(name, True)
            log.info(f"  ✓ {name} embeddings ({elapsed*1000:.0f}ms)")
            data = resp.json()
            key_usage.add_tokens(token, (data.get("usage") or {}).get("total_tokens") or 0)
            _add_provider_tokens(name, data)
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
        {"name": "metrics_auth", "title": "Metrics auth", "kind": "flag",
         "enabled": bool(_int_env("METRICS_REQUIRE_AUTH", 0)), "env": "METRICS_REQUIRE_AUTH",
         "on": "1", "off": "0", "desc": "Require the proxy key on /metrics."},
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
# These back the dashboard's "Add key" / "Model" / add-on toggle forms. Same
# proxy-key auth as every other endpoint — whoever can view /v1/status can also
# change config, matching the existing CLI's trust model (one operator key).
# Every write is a plain, auditable file edit (.env or auth.json), identical to
# what `hr auth add` / `hr model set` / `hr features enable` already produce —
# nothing here is a new mechanism, just an HTTP front-end for the same files.
# Changes take effect after a restart; the dashboard prompts for one via
# POST /v1/config/restart.

@app.route("/v1/config/providers")
def config_providers():
    """List of providers the dashboard can build add-key / set-model forms for,
    plus which ones accept a plain key vs. model-only (codex/local)."""
    err = _auth_check()
    if err:
        return err
    return jsonify({
        "key_settable": KEY_SETTABLE_PROVIDERS,
        "model_settable": list(PROVIDER_MODEL_ENV.keys()),
        "defaults": PROVIDER_MODEL_DEFAULT,
        # Live key count per currently-configured provider, e.g. {"gemini": 6} —
        # informational context for the Access Keys page's provider picker, not
        # an enforced quota split (see monitoring docs on provider scoping).
        "key_counts": {p["name"]: len(p.get("keys", [])) for p in PROVIDERS},
        "copyable_provider_keys": _provider_key_counts_for_instance_copy(),
    })


@app.route("/v1/config/keys/<provider>", methods=["POST"])
def config_add_key(provider):
    """Add one API key for a provider to auth.json. Body: {"key": "..."}"""
    err = _auth_check()
    if err:
        return err
    if provider not in KEY_SETTABLE_PROVIDERS:
        return jsonify({"error": {"message": f"unknown or non-key provider: {provider}",
                                  "type": "invalid_request_error"}}), 400
    body = request.get_json(force=True, silent=True) or {}
    key = (body.get("key") or "").strip()
    if not key:
        return jsonify({"error": {"message": "missing 'key'", "type": "invalid_request_error"}}), 400
    if "\n" in key or "\r" in key:
        return jsonify({"error": {"message": "key must not contain newlines", "type": "invalid_request_error"}}), 400
    added, total = _auth_json_add_key(provider, key)
    return jsonify({"provider": provider, "added": added, "total_keys": total,
                    "duplicate": not added, "restart_required": added})


@app.route("/v1/config/model/<provider>", methods=["POST", "DELETE"])
def config_model(provider):
    """POST {"model": "m1,m2"} to override a provider's model(s); DELETE to reset
    to the built-in default (just removes the .env override line)."""
    err = _auth_check()
    if err:
        return err
    env_var = PROVIDER_MODEL_ENV.get(provider)
    if not env_var:
        return jsonify({"error": {"message": f"unknown provider: {provider}",
                                  "type": "invalid_request_error"}}), 400

    if request.method == "DELETE":
        _env_write_line(env_var, None)
        return jsonify({"provider": provider, "reset": True, "restart_required": True})

    body = request.get_json(force=True, silent=True) or {}
    model = (body.get("model") or "").strip()
    if not model:
        return jsonify({"error": {"message": "missing 'model'", "type": "invalid_request_error"}}), 400
    if any(c in model for c in "\n\r"):
        return jsonify({"error": {"message": "model must not contain newlines", "type": "invalid_request_error"}}), 400
    if not re.fullmatch(r"[A-Za-z0-9._\-:/, ]+", model):
        return jsonify({"error": {"message": "model contains unsupported characters",
                                  "type": "invalid_request_error"}}), 400
    _env_write_line(env_var, model)
    return jsonify({"provider": provider, "model": model, "restart_required": True})


@app.route("/v1/config/features/<name>", methods=["POST"])
def config_feature(name):
    """Toggle a flag-kind add-on on/off. Body: {"enabled": true|false}. Config-kind
    add-ons (per-key budgets, local model) aren't simple flag writes — use their
    own command (`hr limit`, `hr model set local ...`), matching `hr features`."""
    err = _auth_check()
    if err:
        return err
    addon = next((a for a in _features_snapshot()["addons"] if a["name"] == name), None)
    if not addon:
        return jsonify({"error": {"message": f"unknown add-on: {name}", "type": "invalid_request_error"}}), 404
    if addon.get("kind") != "flag":
        return jsonify({"error": {"message": f"'{name}' isn't a simple toggle — manage it with: {addon.get('manage', '(see docs)')}",
                                  "type": "invalid_request_error"}}), 400

    body = request.get_json(force=True, silent=True) or {}
    enabled = bool(body.get("enabled"))
    _env_write_line(addon["env"], addon["on"] if enabled else addon["off"])
    return jsonify({"name": name, "enabled": enabled, "restart_required": True})


@app.route("/v1/config/rotation", methods=["POST"])
def config_rotation():
    """Set key rotation mode from the dashboard. Body: {"mode": "round-robin"|"sequential"}."""
    err = _auth_check()
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
    err = _auth_check()
    if err:
        return err
    _trigger_restart()
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
            if v < 0:
                return {}, f"'{f}' must not be negative"
            out[f] = v
    if "cost_per_day" in body and body["cost_per_day"] not in (None, ""):
        try:
            v = float(body["cost_per_day"])
        except (TypeError, ValueError):
            return {}, "'cost_per_day' must be a number"
        if v < 0:
            return {}, "'cost_per_day' must not be negative"
        out["cost_per_day"] = v
    return out, None


def _parse_allowed_providers(body: dict) -> tuple[dict, str | None]:
    """Validate 'allowed_providers' from a request body. Returns a patch dict
    (empty if the field wasn't sent at all — leaves any existing value untouched
    on an update) and an error message or None. An explicit empty list / null
    means 'unrestricted', matching KEY_PROVIDER_SCOPE's loader semantics."""
    if "allowed_providers" not in body:
        return {}, None
    val = body["allowed_providers"] or []
    if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
        return {}, "'allowed_providers' must be a list of provider names"
    known = set(PROVIDER_MODEL_ENV.keys())
    unknown = [x for x in val if x not in known]
    if unknown:
        return {}, f"unknown provider(s): {', '.join(unknown)}"
    return {"allowed_providers": val}, None


@app.route("/v1/config/proxy-keys")
def config_list_proxy_keys():
    """List every proxy (access) key — the credential CALLERS use to authenticate
    to this router, distinct from the provider keys under /v1/config/keys. Shows
    tail, optional name, limits, and live usage. Reads fresh from .env/auth.json
    (not the process's own stale PROXY_API_KEYS) so a just-created or just-revoked
    key shows immediately, flagged pending until a restart actually applies it."""
    err = _auth_check()
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
            "usage": key_usage.snapshot(k),
            "pending_restart": k not in active_now,
        })
    return jsonify({"keys": out})


@app.route("/v1/config/proxy-keys", methods=["POST"])
def config_create_proxy_key():
    """Mint a new proxy key for a teammate/other app to call the router with.
    Body: {"name": "...", "rpm": N, "req_per_day": N, "tokens_per_day": N,
    "cost_per_day": N, "allowed_providers": ["gemini",...]} — all optional;
    omitted limits fall back to the PROXY_LIMIT_* env defaults (0/unset =
    unlimited); an empty/omitted allowed_providers means unrestricted (can use
    any configured provider). Returns the plaintext key ONCE — like every other
    key in this codebase, only its tail is shown again."""
    err = _auth_check()
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

    live_keys = _read_proxy_api_keys_live()
    new_key = _generate_proxy_key()
    while new_key in live_keys:   # astronomically unlikely; stay correct anyway
        new_key = _generate_proxy_key()
    live_keys.append(new_key)
    _env_write_line("PROXY_API_KEYS", ",".join(live_keys))

    patch = {**limits, **scope}
    if name:
        patch["name"] = name
    if patch:
        _write_proxy_key_meta(new_key, patch)

    return jsonify({"key": new_key, "key_tail": new_key[-6:], "name": name,
                    "limits": limits, "allowed_providers": scope.get("allowed_providers", []),
                    "restart_required": True})


@app.route("/v1/config/proxy-keys/<tail>", methods=["POST"])
def config_update_proxy_key(tail):
    """Update the name/limits of an existing proxy key, found by its last-6-char
    tail. Body: same shape as create. Only fields present in the body change —
    others keep their current value."""
    err = _auth_check()
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

    patch = {**limits, **scope}
    if "name" in body:
        name = str(body.get("name") or "").strip()[:80]
        if "\n" in name or "\r" in name:
            return jsonify({"error": {"message": "name must not contain newlines",
                                      "type": "invalid_request_error"}}), 400
        patch["name"] = name
    if patch:
        _write_proxy_key_meta(key, patch)
    return jsonify({"key_tail": tail, "restart_required": True})


@app.route("/v1/config/proxy-keys/<tail>", methods=["DELETE"])
def config_delete_proxy_key(tail):
    """Revoke a proxy key so it can no longer authenticate to the router.
    Refuses to remove the last remaining key — that would lock everyone out,
    including whoever is using the dashboard right now."""
    err = _auth_check()
    if err:
        return err
    live_keys = _read_proxy_api_keys_live()
    key = _resolve_proxy_key_by_tail(tail, live_keys)
    if not key:
        return jsonify({"error": {"message": f"no proxy key ending in '{tail}'",
                                  "type": "invalid_request_error"}}), 404
    if len(live_keys) <= 1:
        return jsonify({"error": {"message": "can't delete the last proxy key — you'd lock yourself out",
                                  "type": "invalid_request_error"}}), 400
    live_keys.remove(key)
    _env_write_line("PROXY_API_KEYS", ",".join(live_keys))
    _delete_proxy_key_meta(key)
    return jsonify({"key_tail": tail, "revoked": True, "restart_required": True})


@app.route("/v1/instances")
def list_instances():
    """List registered Hermes Router instances and live health/Docker state.
    Secrets are never returned; callers only see key tails and env var names."""
    err = _auth_check()
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
    err = _auth_check()
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
    err = _auth_check()
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
    err = _auth_check()
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
    err = _auth_check()
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
    err = _auth_check()
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
    """Usage analytics: per-provider request/error/token counts, per-key request
    and token totals (key tails only — never full keys), and cache stats."""
    err = _auth_check()
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
    err = _auth_check()
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
    request content or full keys. Set METRICS_REQUIRE_AUTH=1 to require auth."""
    if _int_env("METRICS_REQUIRE_AUTH", 0):
        err = _auth_check()
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
