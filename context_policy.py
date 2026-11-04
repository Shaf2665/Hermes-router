"""Validated context metadata shared by routing and client discovery.

Never infer capacity from a model name or from the owner's minimum selection.
Unknown capacities stay unknown until discovery or an owner supplies limits.
"""
import hashlib
import json

MAX_TOKENS = 16_000_000
OUTPUT_RESERVE = 8192


def token_count(value, *, allow_zero=False):
    if type(value) is not int or not (0 if allow_zero else 1) <= value <= MAX_TOKENS:
        raise ValueError("token limits must be integers between %s and %s" %
                         (0 if allow_zero else 1, MAX_TOKENS))
    return value


def validate_limits(value):
    if not isinstance(value, dict) or set(value) - {"context_window", "max_output_tokens"}:
        raise ValueError("model limits need context_window and max_output_tokens")
    context = token_count(value.get("context_window"))
    output = token_count(value.get("max_output_tokens"))
    if output >= context:
        raise ValueError("max_output_tokens must be smaller than context_window")
    return {"context_window": context, "max_output_tokens": output}


def validate_config(value):
    if not isinstance(value, dict) or set(value) - {"context_window", "model_limits"}:
        raise ValueError("expected context_window and/or model_limits")
    floor = token_count(value.get("context_window", 0), allow_zero=True)
    models = value.get("model_limits", {})
    if not isinstance(models, dict) or len(models) > 1000:
        raise ValueError("model_limits must be an object with at most 1000 entries")
    clean = {}
    for key, limits in models.items():
        if (not isinstance(key, str) or "::" not in key or len(key) > 512
                or any(ord(c) < 32 for c in key)):
            raise ValueError("model limit keys must be provider::model")
        clean[key] = validate_limits(limits)
    return {"context_window": floor, "model_limits": clean}


def discovered_limits(item):
    """Read explicit catalog fields (e.g. OpenRouter); never guess a default."""
    if not isinstance(item, dict):
        return None
    top = item.get("top_provider")
    top = top if isinstance(top, dict) else {}
    try:
        return validate_limits({
            "context_window": item.get("context_window", item.get("context_length", top.get("context_length"))),
            "max_output_tokens": item.get("max_output_tokens", top.get("max_completion_tokens")),
        })
    except ValueError:
        return None


def candidate_budget(limits, floor, provider):
    if limits is None or limits["context_window"] < floor:
        return None
    # A selected minimum is also the client budget target. Larger actual models
    # remain eligible, but a dropdown change is reflected in VS Code's budget.
    context = floor or limits["context_window"]
    output = min(OUTPUT_RESERVE, limits["max_output_tokens"], context - 1)
    if provider.get("max_output_tokens", 0) > 0:
        output = min(output, provider["max_output_tokens"])
    input_tokens = context - output
    if provider.get("skip_if_tokens_over", 0) > 0:
        input_tokens = min(input_tokens, provider["skip_if_tokens_over"])
    return {"max_input_tokens": input_tokens, "max_output_tokens": output}


def client_config(model_id, config, candidates):
    """Only caller-visible model budgets leave the server; no keys or admin data."""
    model = None
    if candidates:
        model = {
            "id": model_id,
            "max_input_tokens": min(c["budget"]["max_input_tokens"] for c in candidates),
            "max_output_tokens": min(c["budget"]["max_output_tokens"] for c in candidates),
            "capabilities": {"tool_calling": True, "image_input": False},
        }
        model["context_window"] = model["max_input_tokens"] + model["max_output_tokens"]
    result = {"schema_version": 1, "context_window": config["context_window"], "model": model}
    # Include the eligible pool so a scope/model change invalidates an in-flight
    # preflight even when the displayed token limits happen to be unchanged.
    identity = {"config": result, "pool": sorted(
        (c["provider"]["name"], c["model"], c["budget"]["max_input_tokens"], c["budget"]["max_output_tokens"])
        for c in candidates)}
    result["revision"] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    if model is None:
        result["reason"] = "No enabled, eligible model has confirmed context/output limits. Check Providers and Settings > Context length."
    return result
