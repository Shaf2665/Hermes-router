"""Playwright coverage for the built-in dashboard against a real local router."""
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
AUTH_KEY = "sk-test"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(base_url: str, proc: subprocess.Popen, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"router exited early with code {proc.returncode}")
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=1) as resp:
                if resp.status == 200:
                    return
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
        time.sleep(0.2)
    raise RuntimeError(f"router did not become healthy: {last_error}")


@pytest.fixture
def router_server(tmp_path):
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update({
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "PROXY_API_KEYS": AUTH_KEY,
        "ROUTER_AUTH_FILE": str(tmp_path / "auth.json"),
        "ROUTER_STATE_FILE": str(tmp_path / "router_state.json"),
        "HERMES_INSTANCES_FILE": str(tmp_path / "instances.json"),
        "CACHE_DB_PATH": str(tmp_path / "cache.db"),
        "CACHE_PERSIST": "0",
        "AUTO_DISCOVER_MODELS": "0",
        "REQUEST_LOG_SIZE": "50",
        "LOG_LEVEL": "WARNING",
        "WORKER_THREADS": "2",
    })
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "router.py")],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_health(base_url, proc)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        yield page
        browser.close()


def test_dashboard_login_navigation_and_safe_actions(router_server, page):
    page.goto(f"{router_server}/dashboard", wait_until="domcontentloaded")

    expect(page.locator("#key-gate")).to_be_visible()
    expect(page.get_by_text("Enter your proxy API key")).to_be_visible()

    page.locator("#key-input").fill("bad-key")
    page.get_by_role("button", name="Open Dashboard").click()
    expect(page.locator("#gate-error")).to_contain_text("rejected")

    page.locator("#key-input").fill(AUTH_KEY)
    page.get_by_role("button", name="Open Dashboard").click()
    expect(page.locator("#key-gate")).to_have_class("hidden")
    assert "active" in page.locator("#page-overview").get_attribute("class")
    expect(page.get_by_text("needs a key")).to_be_visible()
    expect(page.locator("#provider-card-grid")).to_contain_text("No providers configured")

    for label, page_id in [
        ("Providers", "providers"),
        ("Instances", "instances"),
        ("Provider Keys", "keys"),
        ("Access Keys", "access"),
        ("Models", "models"),
        ("Add-ons", "addons"),
        ("Request Log", "logs"),
        ("Overview", "overview"),
    ]:
        page.get_by_role("button", name=label).click()
        assert "active" in page.locator(f"#page-{page_id}").get_attribute("class")

    page.get_by_role("button", name="Instances").click()
    expect(page.locator("#instances-tbody")).to_contain_text("No instances registered yet")
    page.locator("#inst-name").fill("local dashboard")
    page.locator("#inst-base-url").fill(f"{router_server}/v1")
    page.locator("#inst-api-key").fill(AUTH_KEY)
    page.get_by_role("button", name="Save instance").click()
    expect(page.locator("#instances-tbody")).to_contain_text("local dashboard")
    expect(page.locator("#instances-tbody")).to_contain_text("healthy")
    expect(page.locator("#instances-tbody")).to_contain_text("...k-test")

    page.get_by_role("button", name="Access Keys").click()
    expect(page.locator("#access-keys-tbody")).to_contain_text("...k-test")

    page.locator("#ak-name").fill("ci dashboard key")
    page.locator("#ak-rpm").fill("5")
    page.get_by_role("button", name="Create key").click()
    expect(page.locator("#new-key-panel")).to_be_visible()
    assert page.locator("#new-key-value").input_value().startswith("sk-router-")
    expect(page.locator("#restart-banner")).to_be_visible()

    page.get_by_role("button", name="Request Log").click()
    page.locator("#log-filter-status").select_option("error")
    page.locator("#log-filter-endpoint").select_option("chat")
    expect(page.locator("#log-tbody")).to_be_visible()
