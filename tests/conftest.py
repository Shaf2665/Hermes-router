"""Test bootstrap that keeps router imports isolated from local operator config."""
import os
import sys
import tempfile
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_TMP = Path(tempfile.mkdtemp(prefix="hermes-router-tests-"))

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# router.py loads .env from the current directory at import time. Run imports from
# an empty temp directory so local developer secrets/config never affect tests.
os.chdir(_TMP)

for name in [
    "GEMINI_API_KEY", "GEMINI_API_KEYS",
    "OPENROUTER_API_KEY", "OPENROUTER_API_KEYS",
    "SAMBANOVA_API_KEY", "SAMBANOVA_API_KEYS",
    "GITHUB_MODELS_TOKEN", "GITHUB_MODELS_TOKENS",
    "CEREBRAS_API_KEY", "CEREBRAS_API_KEYS",
    "GROQ_API_KEY", "GROQ_API_KEYS",
    "MISTRAL_API_KEY", "MISTRAL_API_KEYS",
    "COHERE_API_KEY", "COHERE_API_KEYS",
    "GLM_API_KEY", "GLM_API_KEYS",
    "NAGA_API_KEY", "NAGA_API_KEYS",
    "NVIDIA_API_KEY", "NVIDIA_API_KEYS",
    "HUGGINGFACE_API_KEY", "HUGGINGFACE_API_KEYS",
    "KIMI_API_KEY", "KIMI_API_KEYS",
    "OPENCODE_API_KEY", "OPENCODE_API_KEYS",
    "OPENCODE_GO_API_KEY", "OPENCODE_GO_API_KEYS",
    "OPENAI_API_KEY", "OPENAI_API_KEYS",
    "ANTHROPIC_API_KEY", "ANTHROPIC_API_KEYS",
    "LOCAL_BASE_URL", "LOCAL_MODEL",
]:
    os.environ.pop(name, None)

os.environ.update({
    "PROXY_API_KEYS": "sk-test",
    "ROUTER_AUTH_FILE": str(_TMP / "auth.json"),
    "ROUTER_STATE_FILE": str(_TMP / "router_state.json"),
    "CACHE_DB_PATH": str(_TMP / "cache.db"),
    "CACHE_PERSIST": "0",
    "AUTO_DISCOVER_MODELS": "0",
    "REQUEST_LOG_SIZE": "50",
    "LOG_LEVEL": "WARNING",
})
