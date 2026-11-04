"""Bounded catalog reads and conservative, metadata-only free chat discovery.

Catalog requests never send prompts or follow redirects with provider credentials.
Unknown prices are not zero prices. Account-dependent free tiers need an explicit
operator confirmation and a documented model allowlist.
"""
import json
import re
import time
from decimal import Decimal, InvalidOperation

MAX_CATALOG_BYTES = 4_000_000
MAX_CATALOG_MODELS = 5000
FREE_TIER_CHAT_MODELS = {
    # https://console.groq.com/docs/rate-limits (Free Plan, checked 2026-11-04)
    "groq": {"openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"},
}
_NON_CHAT = re.compile(
    r"(?:whisper|orpheus|(?:^|[-_/:])(?:tts|stt|embed(?:ding)?s?|rerank(?:er)?|"
    r"moderation|classifier|classification|prompt-guard|content-safety|safeguard|"
    r"llama-guard)(?:$|[-_/:]))", re.I)
_CHAT_FAMILY = re.compile(
    r"(?:^|[/_-])(?:gpt|qwen|llama|gemini|claude|mistral|mixtral|deepseek|"
    r"gemma|nemotron|llm|glm|minimax|kimi|command|jamba|ling|mimo)(?:[0-9._:/-]|$)", re.I)


class CatalogError(ValueError):
    """Safe error text: never includes provider bodies, URLs, or credentials."""


def valid_model_id(value):
    return (isinstance(value, str) and len(value) <= 200
            and re.fullmatch(r"[A-Za-z0-9@+._:/-]+", value) is not None)


def non_chat_id(model):
    return bool(_NON_CHAT.search(model))


def chat_compatible(item):
    model = item.get("id", "")
    if not valid_model_id(model) or non_chat_id(model) or item.get("active") is False:
        return False
    task = item.get("task") or item.get("pipeline_tag")
    if task and task not in ("text-generation", "conversational", "chat", "chat-completion"):
        return False
    endpoints = item.get("supported_endpoints")
    if isinstance(endpoints, list):
        if not any(isinstance(e, str) and e.rstrip("/").endswith(("chat/completions", "messages"))
                   for e in endpoints):
            return False
    architecture = item.get("architecture")
    if isinstance(architecture, dict):
        inputs, outputs = architecture.get("input_modalities"), architecture.get("output_modalities")
        if isinstance(inputs, list) and isinstance(outputs, list):
            return "text" in inputs and "text" in outputs
        modality = architecture.get("modality", "")
        if isinstance(modality, str) and "->" in modality:
            inputs, outputs = modality.split("->", 1)
            return "text" in inputs.split("+") and "text" in outputs.split("+")
    return bool(task or isinstance(endpoints, list) or _CHAT_FAMILY.search(model))


def zero_priced(item):
    prices = item.get("pricing")
    if not isinstance(prices, dict) or not {"prompt", "completion"} <= prices.keys():
        return False
    # Ancillary request/image/reasoning charges also disqualify a free import.
    try:
        return all(not isinstance(v, bool) and Decimal(str(v)).is_finite()
                   and Decimal(str(v)) == 0 for v in prices.values())
    except (InvalidOperation, ValueError, TypeError):
        return False


def free_chat_models(provider, items, free_tier_confirmed=False):
    selected = {}
    for item in items:
        if not isinstance(item, dict) or not chat_compatible(item):
            continue
        model = item["id"]
        free = zero_priced(item)
        # Only documented provider conventions count; a random 'free' substring
        # is never enough. Conflicting or malformed prices veto a suffix.
        if "pricing" not in item:
            free = provider == "openrouter" and model.endswith(":free")
        if free_tier_confirmed and model in FREE_TIER_CHAT_MODELS.get(provider, set()):
            free = True
        if free:
            selected[model] = item
    return selected


def fetch_catalog(session, provider, key):
    headers = {"Authorization": "Bearer " + key, **provider.get("headers", {})}
    started = time.monotonic()
    try:
        with session.get(provider["base_url"].rstrip("/") + "/models", headers=headers,
                         timeout=(3, 8), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise CatalogError("Provider could not list models. Check its key and availability.")
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > MAX_CATALOG_BYTES or time.monotonic() - started > 15:
                    raise CatalogError("Provider catalog exceeds the discovery safety limit.")
                chunks.append(chunk)
            payload = json.loads(b"".join(chunks))
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list) or len(items) > MAX_CATALOG_MODELS:
            raise CatalogError("Provider returned an unsupported or oversized model catalog.")
        # Reject incomplete pages rather than claiming a partial list is complete.
        if payload.get("has_more") or payload.get("next_page") or payload.get("next_cursor"):
            raise CatalogError("Provider catalog is paginated; automatic import is not supported yet.")
        normalized = []
        for item in items:
            if isinstance(item, dict):
                item = dict(item)
                if provider["name"] == "gemini" and isinstance(item.get("id"), str):
                    item["id"] = item["id"].removeprefix("models/")
                normalized.append(item)
        return normalized
    except CatalogError:
        raise
    except Exception:
        raise CatalogError("Could not read the provider catalog. Existing models are unchanged.") from None
