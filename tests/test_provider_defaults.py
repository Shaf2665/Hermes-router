"""Keep provider defaults current and consistent across the API and CLI."""
from pathlib import Path

import router


ROOT = Path(__file__).resolve().parents[1]

CURRENT_FREE_DEFAULTS = {
    "groq": "openai/gpt-oss-120b",
    "mistral": "mistral-medium-3-5",
    "zai": "glm-4.7-flash",
    "opencode": (
        "deepseek-v4-flash-free,nemotron-3-ultra-free,"
        "mimo-v2.5-free,north-mini-code-free"
    ),
}


def test_current_free_provider_defaults():
    for provider, model in CURRENT_FREE_DEFAULTS.items():
        assert router.PROVIDER_MODEL_DEFAULT[provider] == model


def test_cli_defaults_match_router_defaults():
    script = (ROOT / "scripts" / "model.sh").read_text()

    for provider, model in router.PROVIDER_MODEL_DEFAULT.items():
        assert f'{provider})' in script
        assert f'echo "{model}"' in script
