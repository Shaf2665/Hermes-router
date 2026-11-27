"""Provider-aware controls, without transport or credential access.

Verified interfaces and deliberate unsupported cases: documentation/reasoning.md.
Observing reasoning tokens does not prove a model accepts an effort control.
"""
import hashlib
import json
import re
from urllib.parse import urlparse

LEVELS = ("auto", "off", "low", "medium", "high")
NATIVE_FIELDS = ("reasoning_effort", "reasoning", "thinking", "think", "output_config")
VERSION = 1
MANUAL_BUDGETS = {"low": 1024, "medium": 4096, "high": 8192}


class PolicyError(ValueError):
    def __init__(self, message, code="reasoning_not_supported"):
        super().__init__(message)
        self.code = code


def validate_config(value):
    if not isinstance(value, dict) or set(value) - {"default", "providers", "models"}:
        raise ValueError("Expected default, providers and/or models")
    clean = {"default": value.get("default", "auto"), "providers": {}, "models": {}}
    if clean["default"] not in LEVELS:
        raise ValueError("Invalid reasoning default")
    for field in ("providers", "models"):
        entries = value.get(field, {})
        if not isinstance(entries, dict) or len(entries) > 1000:
            raise ValueError(f"{field} must be an object with at most 1000 entries")
        for key, level in entries.items():
            if (not isinstance(key, str) or not 0 < len(key) <= 512
                    or any(ord(c) < 32 for c in key) or level not in LEVELS
                    or (field == "models" and ("::" not in key or not all(key.split("::", 1))))):
                raise ValueError("Invalid reasoning override")
            clean[field][key] = level
    return clean


def revision(config, control=None):
    return hashlib.sha256(json.dumps([VERSION, config, control], sort_keys=True).encode()).hexdigest()


def request_control(payload, header):
    """Keep native-only constraints typed; never guess an effort from a budget."""
    def invalid(message):
        raise PolicyError(message, "invalid_reasoning_control")
    if header is not None and header not in LEVELS:
        invalid("X-Hermes-Reasoning must be auto, off, low, medium or high")
    control, efforts = {}, []
    if "think" in payload:
        invalid("Use X-Hermes-Reasoning instead of think")
    if "reasoning_effort" in payload:
        efforts.append(payload["reasoning_effort"])
    if "reasoning" in payload:
        value = payload["reasoning"]
        # Codex CLI always sends summary. Hermes never returns summaries, so it is
        # accepted as a display hint and dropped by native_fields().
        if not isinstance(value, dict) or not value or set(value) - {"effort", "summary"}:
            invalid("reasoning supports only effort and summary")
        if "effort" in value:
            efforts.append(value["effort"])
    if "output_config" in payload:
        value = payload["output_config"]
        if not isinstance(value, dict) or set(value) != {"effort"}:
            invalid("output_config supports only effort")
        efforts.append(value["effort"])
        control["transport"] = "anthropic"
    if efforts:
        if any(not isinstance(e, str) or e not in (*LEVELS, "none", "minimal", "xhigh", "max") for e in efforts):
            invalid("Invalid native reasoning effort")
        efforts = ["off" if e == "none" else e for e in efforts]
        if len(set(efforts)) != 1:
            invalid("Conflicting native reasoning efforts")
        control.update(level=efforts[0], native=True)
    if "thinking" in payload:
        value = payload["thinking"]
        if not isinstance(value, dict) or set(value) - {"type", "budget_tokens"}:
            invalid("Invalid thinking configuration")
        kind = value.get("type")
        if kind not in ("enabled", "disabled", "adaptive"):
            invalid("thinking.type must be enabled, disabled or adaptive")
        if "budget_tokens" in value:
            budget = value["budget_tokens"]
            if kind != "enabled" or type(budget) is not int or not 1024 <= budget <= 16_000_000:
                invalid("thinking budget_tokens must be an integer >=1024 for enabled thinking")
            control["transport"] = "anthropic"
        if kind == "adaptive":
            control["transport"] = "anthropic"
        if kind == "disabled" and control.get("level", "off") != "off":
            invalid("Disabled thinking conflicts with reasoning effort")
        if kind != "disabled" and control.get("level") == "off":
            invalid("Enabled thinking conflicts with disabled reasoning effort")
        control.update(thinking=dict(value), native=True)
        control.setdefault("level", "off" if kind == "disabled" else "native")
    if header is not None:
        if control and (control.get("level") != header or "thinking" in control):
            invalid("Conflicting Hermes and native reasoning controls")
        control["level"] = header
    # Nested native controls cannot bypass the shared policy by going through extra_body.
    if any(word in json.dumps(payload.get("extra_body", {})).lower() for word in ("thinking", "reasoning")):
        invalid("Use the Hermes reasoning header or top-level supported native controls")
    return control


def native_fields(body):
    """Validated native controls to carry into a chat payload, without reasoning.summary."""
    fields = {field: body[field] for field in NATIVE_FIELDS if field in body}
    if isinstance(fields.get("reasoning"), dict):
        fields["reasoning"] = {k: v for k, v in fields["reasoning"].items() if k != "summary"}
        if not fields["reasoning"]:
            del fields["reasoning"]
    return fields


def resolve(config, control, provider, model):
    level = control.get("level", config["models"].get(provider["name"] + "::" + model,
                config["providers"].get(provider["name"], config["default"])))
    return {**control, "level": level, "strict": level != "auto" or bool(control.get("native"))}


def has_private_state(value):
    if isinstance(value, list):
        return any(has_private_state(v) for v in value)
    if not isinstance(value, dict):
        return False
    if (set(value) & {"reasoning_content", "reasoning", "thinking", "signature", "thought_signature", "encrypted_content"}
            or value.get("type") in ("thinking", "redacted_thinking", "reasoning", "think")):
        return True
    return any(has_private_state(v) for v in value.values())


def public_output(value):
    """Remove provider-private fields, including nested content/signature blocks."""
    if isinstance(value, list):
        return [public_output(v) for v in value if not (isinstance(v, dict) and
                v.get("type") in ("thinking", "redacted_thinking", "reasoning", "think"))]
    if isinstance(value, dict):
        return {k: public_output(v) for k, v in value.items() if k not in {
            "reasoning_content", "reasoning", "thinking", "think", "signature", "thought_signature", "encrypted_content"}}
    return value


def capabilities(provider, model):
    name, protocol = provider["name"], provider.get("protocol", "openai")
    modes, adapter, continuity = [], None, False
    # Explicit endpoint contracts: custom gateways cannot inherit support by name.
    hosts = {"openai": "api.openai.com", "gemini": "generativelanguage.googleapis.com",
             "groq": "api.groq.com", "deepseek": "api.deepseek.com", "kimi": "api.moonshot.ai"}
    host = urlparse(provider.get("base_url", "")).hostname
    trusted = not provider.get("custom") and (not host or host == hosts.get(name))
    if (name == "openai" and trusted) or protocol == "codex":
        if re.fullmatch(r"(?:o3(?:-mini)?|o4-mini|gpt-5(?:-mini|-nano)?)(?:-\d{4}-\d{2}-\d{2})?", model):
            modes = ["low", "medium", "high"]
        elif model == "gpt-5.1":
            modes = ["off", "low", "medium", "high"]
        elif model in {"gpt-5.2", "gpt-5.4"}:
            modes = ["off", "low", "medium", "high", "xhigh"]
        elif model == "gpt-5.5":
            modes = ["low", "medium", "high", "xhigh"]
        adapter = "responses" if protocol == "codex" else "effort"
    elif name == "groq" and trusted and model in {"openai/gpt-oss-120b", "openai/gpt-oss-20b"}:
        adapter, modes = "effort", ["low", "medium", "high"]
    elif name == "gemini" and trusted:
        if model in {"gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-pro"}:
            adapter, modes = "gemini", ["low", "medium", "high"]
            if model != "gemini-2.5-pro":
                modes.insert(0, "off")
        # Gemini tool signatures need a separately verified continuation path.
        continuity = True
    elif name == "anthropic" and protocol == "anthropic" and not provider.get("custom"):
        if re.fullmatch(r"claude-(?:sonnet-4-5|haiku-4-5|sonnet-4)(?:-\d{8})?", model):
            adapter, modes = "anthropic_manual", ["off", "low", "medium", "high"]
        elif model in {"claude-sonnet-4-6", "claude-opus-4-6"}:
            adapter, modes = "anthropic_adaptive", ["off", "low", "medium", "high"]
        continuity = True
    elif name == "deepseek" and trusted and model in {"deepseek-flash", "deepseek-v4-pro"}:
        adapter, modes, continuity = "deepseek", ["off", "low", "high", "max"], True
    elif name == "kimi" and trusted:
        if model in {"kimi-k3.1-pro", "kimi-k3.1-pro-highspeed", "kimi-k3"}:
            adapter, modes = "effort", ["low", "high", "max"]
        elif model == "kimi-k2.5":
            adapter, modes = "toggle", ["off"]
        continuity = True
    return {"modes": ["auto", *modes], "adapter": adapter, "private_tool_state": continuity}


def native_settings(provider, model, policy, payload, output):
    caps = capabilities(provider, model)
    level, strict = policy["level"], policy["strict"]
    result = {"requested": level, "effective": "provider_default", "fields": {}, "strict": strict}
    if not strict:
        if provider.get("protocol") == "codex":
            result["effective"] = "medium"
        return result
    if has_private_state(payload.get("messages", [])) or has_private_state(payload.get("input", [])):
        raise PolicyError("Private reasoning history is unsupported with explicit controls; use portable history", "reasoning_continuity_unsupported")
    adapter = caps["adapter"]
    thinking = policy.get("thinking")
    if policy.get("transport") == "anthropic" and not str(adapter).startswith("anthropic_"):
        raise PolicyError("Native Anthropic reasoning is not supported by this candidate")
    if level == "native":
        valid_native = ((adapter == "anthropic_manual" and thinking.get("type") == "enabled" and "budget_tokens" in thinking)
                        or (adapter == "anthropic_adaptive" and thinking.get("type") == "adaptive")
                        or (adapter in {"deepseek", "toggle"} and thinking == {"type": "enabled"}))
        if not valid_native:
            raise PolicyError("Native thinking mode is not supported by this candidate")
    elif level not in caps["modes"] or level == "auto":
        raise PolicyError(f"Reasoning {level} is not supported by this candidate")
    if payload.get("tools") and caps["private_tool_state"] and level != "off":
        raise PolicyError("Thinking tool continuity is not supported with explicit controls; use legacy Auto or another model", "reasoning_continuity_unsupported")
    fields = {}
    if adapter in {"effort", "gemini", "responses"}:
        effort = "none" if level == "off" else level
        fields = {"reasoning": {"effort": effort}} if adapter == "responses" else {"reasoning_effort": effort}
        if thinking:
            raise PolicyError("Native thinking configuration is not supported by this candidate")
    elif adapter in {"anthropic_manual", "anthropic_adaptive"}:
        if adapter == "anthropic_manual" and policy.get("transport") == "anthropic" and policy.get("native") and level != "native" and not thinking:
            raise PolicyError("Native output effort is not supported by this manual-thinking model")
        if level == "off":
            fields = {"thinking": {"type": "disabled"}}
        elif adapter == "anthropic_manual":
            if thinking and level != "native":
                raise PolicyError("This model supports a thinking budget, not an additional native effort")
            if thinking and (thinking["type"] != "enabled" or "budget_tokens" not in thinking):
                raise PolicyError("Manual thinking requires budget_tokens")
            budget = thinking["budget_tokens"] if thinking else MANUAL_BUDGETS[level]
            if output is None or budget >= output:
                raise PolicyError("Thinking budget must be smaller than the total output ceiling", "reasoning_budget_exceeded")
            fields = {"thinking": {"type": "enabled", "budget_tokens": budget}}
        else:
            if thinking and thinking["type"] != "adaptive":
                raise PolicyError("This model requires adaptive thinking")
            fields = {"thinking": {"type": "adaptive"}}
            if level != "native":
                fields["output_config"] = {"effort": level}
        if level != "off" and ("top_p" in payload or payload.get("temperature", 1) != 1):
            raise PolicyError("Thinking is incompatible with these sampling settings")
    elif adapter in {"deepseek", "toggle"}:
        fields = {"thinking": {"type": "disabled" if level == "off" else "enabled"}}
        if level not in {"off", "native"}:
            fields["reasoning_effort"] = level
    else:
        raise PolicyError("Reasoning is not supported by this candidate")
    if adapter == "gemini" and level != "off":
        budget = {"low": 1024, "medium": 8192, "high": 24576}[level]
        if output is None or budget >= output:
            raise PolicyError("Gemini thinking budget needs a larger total output ceiling", "reasoning_budget_exceeded")
    result.update(fields=fields, effective=fields)
    return result
