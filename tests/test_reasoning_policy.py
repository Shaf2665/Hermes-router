import pytest
import reasoning_policy as rp


def provider(name, protocol=None):
    return {"name": name, "protocol": protocol or "openai"}


def settings(name, model, level, payload=None, output=32768, protocol=None):
    control = rp.request_control(payload or {}, level)
    policy = rp.resolve(rp.validate_config({}), control, provider(name, protocol), model)
    return rp.native_settings(provider(name, protocol), model, policy, payload or {}, output)


def test_precedence_and_explicit_auto():
    config = rp.validate_config({"default": "high", "providers": {"openai": "medium"},
                                 "models": {"openai::o3": "low"}})
    assert rp.resolve(config, {}, provider("openai"), "o3")["level"] == "low"
    assert rp.resolve(config, rp.request_control({}, "auto"), provider("openai"), "o3")["level"] == "auto"
    with pytest.raises(rp.PolicyError):
        rp.request_control({"reasoning_effort": "low"}, "high")


@pytest.mark.parametrize("value", [None, [], {"default": True}, {"extra": "high"},
                                  {"models": {"bad": "high"}}])
def test_invalid_config(value):
    with pytest.raises(ValueError):
        rp.validate_config(value)


def test_provider_mappings_and_unknown_gateways():
    assert settings("openai", "o3", "high")["fields"] == {"reasoning_effort": "high"}
    assert settings("codex", "gpt-5", "low", protocol="codex")["fields"] == {"reasoning": {"effort": "low"}}
    assert settings("gemini", "gemini-2.5-flash", "off")["fields"] == {"reasoning_effort": "none"}
    for name, model, level in [("gemini", "gemini-2.5-pro", "off"),
                               ("openrouter", "openai/o3", "high"),
                               ("openai", "unknown", "high")]:
        with pytest.raises(rp.PolicyError, match="not supported"):
            settings(name, model, level)


def test_manual_budget_and_continuity():
    result = settings("anthropic", "claude-sonnet-4-5", "low", protocol="anthropic")
    assert result["fields"] == {"thinking": {"type": "enabled", "budget_tokens": 1024}}
    with pytest.raises(rp.PolicyError) as exc:
        settings("anthropic", "claude-sonnet-4-5", "low", output=1024, protocol="anthropic")
    assert exc.value.code == "reasoning_budget_exceeded"
    with pytest.raises(rp.PolicyError) as exc:
        settings("anthropic", "claude-sonnet-4-5", "low", {"tools": [{"type": "function"}]}, protocol="anthropic")
    assert exc.value.code == "reasoning_continuity_unsupported"


def test_legacy_auto_and_private_input_boundary():
    history = {"messages": [{"role": "assistant", "reasoning_content": "private"}]}
    assert settings("kimi", "kimi-k2.5", "auto", history)["strict"] is False
    with pytest.raises(rp.PolicyError) as exc:
        settings("openai", "o3", "low", history)
    assert exc.value.code == "reasoning_continuity_unsupported"


def test_native_constraints_are_not_dropped_or_reinterpreted():
    payload = {"thinking": {"type": "enabled", "budget_tokens": 2048}}
    control = rp.request_control(payload, None)
    policy = rp.resolve(rp.validate_config({}), control, provider("anthropic", "anthropic"), "claude-sonnet-4-5")
    assert rp.native_settings(provider("anthropic", "anthropic"), "claude-sonnet-4-5", policy, payload, 4096)["fields"] == payload
    with pytest.raises(rp.PolicyError):
        rp.native_settings(provider("openai"), "o3", policy, payload, 4096)
    for payload in [{"reasoning": []}, {"reasoning": {"summary": "auto"}},
                    {"thinking": {"type": "enabled", "budget_tokens": True}},
                    {"reasoning_effort": "garbage"}]:
        with pytest.raises(rp.PolicyError):
            rp.request_control(payload, None)


@pytest.mark.parametrize("field", ["reasoning_effort", "output_config"])
def test_native_enabled_and_off_conflict(field):
    payload = {"thinking": {"type": "enabled", "budget_tokens": 2048},
               field: "none" if field == "reasoning_effort" else {"effort": "none"}}
    with pytest.raises(rp.PolicyError):
        rp.request_control(payload, None)


@pytest.mark.parametrize("name,model,level,protocol,fields", [
    ("anthropic", "claude-sonnet-4-6", "high", "anthropic", {"thinking": {"type": "adaptive"}, "output_config": {"effort": "high"}}),
    ("anthropic", "claude-haiku-4-5-20251001", "off", "anthropic", {"thinking": {"type": "disabled"}}),
    ("deepseek", "deepseek-flash", "high", "openai", {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}),
    ("kimi", "kimi-k3.1-pro", "low", "openai", {"reasoning_effort": "low"}),
    ("kimi", "kimi-k2.5", "off", "openai", {"thinking": {"type": "disabled"}}),
    ("gemini", "gemini-2.5-pro", "high", "openai", {"reasoning_effort": "high"}),
])
def test_remaining_verified_adapters(name, model, level, protocol, fields):
    assert settings(name, model, level, protocol=protocol)["fields"] == fields
