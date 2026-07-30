"""Regression: fully excluded providers must not route with an empty model id."""
import router


def test_smart_ordered_skips_provider_with_empty_models():
    empty = {"name": "mistral", "model": "", "models": []}
    good = {"name": "groq", "model": "llama-3.1-8b-instant", "models": ["llama-3.1-8b-instant"]}

    ordered = router._get_smart_ordered([empty, good], complexity=3)
    models = [c["model"] for c in ordered]
    names = [c["provider"]["name"] for c in ordered]

    assert "" not in models
    assert "mistral" not in names
    assert names == ["groq"]
    assert models == ["llama-3.1-8b-instant"]


def test_credential_pool_skips_empty_model_bucket():
    providers = [{
        "name": "mistral",
        "model": "",
        "models": [],
        "keys": ["sk-test"],
    }]
    pool = router.CredentialPool(providers)
    assert list(pool.pools["mistral"].keys()) == []
