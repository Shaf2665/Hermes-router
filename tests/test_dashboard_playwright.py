"""Real-browser dashboard checks; all inference stays on a local mock provider."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
AUTH_KEY = "sk-test"
ADMIN_KEY = "sk-test-admin"
HOSTILE = '\"><img src=x onerror="window.hermesXss=true">'


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def stream_release():
    release = threading.Event()
    release.started = threading.Event()
    yield release
    release.set()


@pytest.fixture
def router_server(tmp_path, stream_release):
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self._send({"data": [{"id": "test-local"}]})

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            if data.get('stream'):
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                chunk = {'id':'test-stream', 'model':data['model'], 'choices':[{'index':0,'delta':{'content':'OK'},'finish_reason':None}]}
                self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
                self.wfile.flush()
                if data.get('messages', [{}])[-1].get('content') == '__hold_stream__':
                    stream_release.started.set()
                    stream_release.wait(10)
                self.wfile.write(b'data: [DONE]\n\n')
                return
            self._send({"id": "test-completion", "model": data.get('model', 'test-local'), "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 8, "completion_tokens": 1, "total_tokens": 9}})

        def _send(self, data):
            payload = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{_free_port()}"
    env = os.environ.copy()
    for name in list(env):
        if any(part in name for part in ("API_KEY", "_TOKEN", "LOCAL_BASE_URL", "LOCAL_MODEL")):
            env.pop(name)
    env.update({
        "HOST": "127.0.0.1", "PORT": base_url.rsplit(":", 1)[1],
        "PROXY_API_KEYS": AUTH_KEY, "ADMIN_API_KEY": ADMIN_KEY,
        "HR_ENV_FILE": str(tmp_path / "router.env"), "HR_USAGE_DB": str(tmp_path / "usage.db"),
        "ROUTER_AUTH_FILE": str(tmp_path / "auth.json"),
        "ROUTER_STATE_FILE": str(tmp_path / "router_state.json"),
        "HERMES_INSTANCES_FILE": str(tmp_path / "instances.json"),
        "CACHE_DB_PATH": str(tmp_path / "cache.db"), "CACHE_PERSIST": "0",
        "AUTO_DISCOVER_MODELS": "0", "REQUEST_LOG_SIZE": "50", "LOG_LEVEL": "WARNING",
        "WORKER_THREADS": "4", "LOCAL_BASE_URL": f"http://127.0.0.1:{upstream.server_port}/v1",
        "LOCAL_MODEL": "test-local",
    })
    log = (tmp_path / "server.log").open("w", encoding="utf-8")
    env["PYTHONPATH"] = str(ROOT)
    server_code = "import router; from waitress import serve; router._probe_provider=lambda p,k,**kw:(True,1,p['model'],'ok'); router._resolve_caps=lambda *a:{'rating':1,'supports_tools':True,'tools_confirmed':True,'reasoning':False}; serve(router.app,host=router.HOST,port=router.PORT,threads=4)"
    proc = subprocess.Popen([sys.executable, '-c', server_code], cwd=tmp_path, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError((tmp_path / "server.log").read_text())
            try:
                with urllib.request.urlopen(base_url + "/health", timeout=1):
                    break
            except (OSError, urllib.error.URLError):
                time.sleep(.1)
        else:
            raise RuntimeError("test router failed to start")
        yield base_url
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        log.close()
        upstream.shutdown()
        upstream.server_close()


@pytest.fixture
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        yield page
        browser.close()
        assert not errors, errors


def login(page, base_url):
    page.goto(base_url + "/dashboard", wait_until="domcontentloaded")
    page.locator("#key-input").fill(ADMIN_KEY)
    page.get_by_role("button", name="Open Dashboard").click()
    expect(page.locator("#key-gate")).to_be_hidden()
    expect(page.locator("#plain-title")).not_to_contain_text("Checking")


def test_live_settings_and_connect_layout(router_server, page):
    login(page, router_server)
    expect(page.locator('.sidebar-brand img')).to_have_attribute('alt', 'Hermes Router logo')
    page.get_by_role('button', name='Settings', exact=True).click()
    toggle = page.get_by_role('switch', name='Fast routing', exact=True)
    expect(toggle).to_have_attribute('aria-checked', 'false')
    toggle.click()
    expect(toggle).to_have_attribute('aria-checked', 'true')
    expect(page.locator('#settings-save-msg')).to_contain_text('applied')
    expect(page.locator('#restart-banner')).not_to_be_visible()
    page.reload()
    expect(toggle).to_have_attribute('aria-checked', 'true')
    toggle.click()
    expect(toggle).to_have_attribute('aria-checked', 'false')
    page.route('**/v1/config/features/fast_routing', lambda route: route.fulfill(status=500, json={'error': {'message': 'Storage unavailable'}}))
    toggle.click()
    expect(page.locator('#settings-save-msg')).to_contain_text('Storage unavailable')
    expect(toggle).to_have_attribute('aria-checked', 'false')
    page.locator('#theme').select_option('dark')
    for width in (1012, 918, 390):
        page.set_viewport_size({'width': width, 'height': 800})
        page.get_by_role('button', name='Connect', exact=True).click()
        field = page.locator('#connect-name').bounding_box()
        button = page.locator('#connect-create').bounding_box()
        assert button['x'] >= field['x'] + field['width'] + 8 or button['y'] >= field['y'] + field['height'] + 8
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path=str(ROOT / '.pytest_cache' / f'live-connect-{width}.png'), full_page=True)
        page.get_by_role('button', name='Settings', exact=True).click()
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
        page.screenshot(path=str(ROOT / '.pytest_cache' / f'live-settings-{width}.png'), full_page=True)


def test_provider_presets_key_link_live_models_and_multiple_selection(router_server, page):
    login(page, router_server)
    page.locator('[data-page="providers"]').click()
    page.locator('#cfg-key-provider').select_option('kilo_gateway')
    expect(page.locator('#cfg-provider-url')).to_have_value('https://ai-gateway.kilo.ai/api/v1')
    expect(page.locator('#cfg-provider-key-link')).to_have_attribute('href', 'https://app.kilo.ai/')
    page.locator('#cfg-key-value').fill('mock-provider-key')
    page.get_by_role('button', name='Save provider', exact=True).click()
    expect(page.locator('#cfg-key-msg')).to_contain_text('Select at least one model')
    page.route('**/v1/config/providers/kilo_gateway/models', lambda route: route.fulfill(
        json={'models': ['model-a:free', 'model-b:free'], 'notice': 'Check prices.'}))
    page.get_by_role('button', name='Refresh model list', exact=True).click()
    expect(page.locator('#cfg-provider-model-status')).to_contain_text('2 models')
    for checkbox in page.locator('#cfg-provider-models input').all():
        checkbox.check()
    page.get_by_role('button', name='Save provider', exact=True).click()
    expect(page.locator('#cfg-key-msg')).to_contain_text('2 model(s)')
    expect(page.locator('#cfg-key-value')).to_have_value('')
    result = page.request.get(router_server + '/v1/config/providers').json()
    assert result['configured_models']['kilo_gateway'] == 'model-a:free,model-b:free'
    assert 'mock-provider-key' not in page.evaluate('JSON.stringify(localStorage)')
    page.locator('#cfg-key-value').fill('must-not-follow-provider-switch')
    page.locator('#cfg-key-provider').select_option('groq')
    expect(page.locator('#cfg-key-value')).to_have_value('')
    expect(page.locator('#cfg-provider-key-link')).to_have_attribute('href', 'https://console.groq.com/keys')
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')


def test_provider_catalog_failure_keeps_suggestions(router_server, page):
    login(page, router_server)
    page.locator('[data-page="providers"]').click()
    page.locator('#cfg-key-provider').select_option('kilo_gateway')
    page.locator('#cfg-key-value').fill('mock-provider-key')
    page.route('**/v1/config/providers/kilo_gateway/models', lambda route: route.fulfill(
        status=502, json={'error': {'message': 'Provider cannot list models'}}))
    page.get_by_role('button', name='Refresh model list', exact=True).click()
    expect(page.locator('#cfg-provider-model-status')).to_contain_text('Provider cannot list models')
    expect(page.locator('#cfg-provider-models')).to_contain_text('kilo-auto/free')


def test_session_login_resume_logout_and_no_persistent_secret(router_server, page):
    page.goto(router_server + "/dashboard", wait_until="domcontentloaded")
    expect(page.get_by_text("Enter your administrator credential")).to_be_visible()
    page.locator("#key-input").fill(AUTH_KEY)
    page.get_by_role("button", name="Open Dashboard").click()
    expect(page.locator("#gate-error")).to_contain_text("retry")
    expect(page.locator("#key-input")).to_have_value("")
    page.locator("#key-input").fill(ADMIN_KEY)
    page.get_by_role("button", name="Open Dashboard").click()
    expect(page.locator("#key-gate")).to_be_hidden()
    assert page.evaluate("JSON.stringify(localStorage)") == "{}"
    assert ADMIN_KEY not in page.content()
    cookies = page.context.cookies()
    assert cookies and cookies[0]["httpOnly"]
    page.reload()
    expect(page.locator("#key-gate")).to_be_hidden()
    page.get_by_role("button", name="Sign out").click()
    expect(page.locator("#key-gate")).to_be_visible()
    page.reload()
    expect(page.locator("#key-gate")).to_be_visible()


def test_connect_real_request_uses_new_app_key_and_live_access(router_server, page):
    login(page, router_server)
    page.get_by_role("button", name="Connect", exact=True).click()
    page.get_by_role("button", name="Create application key", exact=True).click()
    expect(page.locator("#connect-key-panel")).to_be_visible()
    page.screenshot(path=str(ROOT / ".pytest_cache" / "dashboard-connect.png"), full_page=True)
    app_key = page.locator("#connect-key").input_value()
    assert app_key.startswith("sk-router-") and app_key != ADMIN_KEY
    with page.expect_request("**/v1/chat/completions") as sent:
        page.get_by_role("button", name="Send test request").click()
    assert sent.value.headers["authorization"] == "Bearer " + app_key
    assert "cookie" not in sent.value.headers
    expect(page.locator("#connect-test-msg")).to_contain_text("Request succeeded", timeout=15000)
    expect(page.locator("#connect-test-msg")).to_contain_text("test-local")
    expect(page.locator("#connect-test-msg")).to_contain_text("OK")
    assert app_key not in page.locator("#client-snippet").inner_text()
    denied = page.request.get(router_server + "/v1/status", headers={"Authorization": "Bearer " + app_key})
    assert denied.status in (401, 403)
    page.get_by_role("button", name="Settings", exact=True).click()
    page.locator("#settings-access > summary").click()
    expect(page.locator("#access-keys-tbody")).to_contain_text(app_key[-6:])
    expect(page.locator("#restart-banner")).not_to_be_visible()
    page.screenshot(path=str(ROOT / ".pytest_cache" / "dashboard-settings.png"), full_page=True)


def test_untrusted_metadata_partial_failure_and_primary_navigation(router_server, page):
    def status(route):
        data = route.fetch().json()
        data["providers"][HOSTILE] = {"model": HOSTILE, "keys": [{"status": "ready"}], "stats": {}}
        data.setdefault("features", {})["addons"] = [{"name": HOSTILE, "title": HOSTILE, "desc": HOSTILE, "env": HOSTILE, "kind": "flag", "enabled": False}]
        route.fulfill(json=data)
    page.route("**/v1/logs?*", lambda route: route.fulfill(json={"entries": [{"ts": "2026-11-02", "provider": HOSTILE, "model": HOSTILE, "endpoint": HOSTILE, "status": HOSTILE}]}))
    page.route("**/v1/status", status)
    page.route("**/v1/usage", lambda route: route.fulfill(status=503, json={"error": "usage unavailable"}))
    login(page, router_server)
    expect(page.locator("#panel-errors")).to_contain_text("Usage")
    expect(page.locator("#plain-title")).to_contain_text("ready")
    for label, name in [("Providers", "providers"), ("Connect", "connect"), ("Activity", "logs"), ("Settings", "settings"), ("Home", "overview")]:
        page.get_by_role("button", name=label, exact=True).click()
        expect(page.locator("#page-" + name)).to_be_visible()
    page.get_by_role("button", name="Providers", exact=True).click()
    expect(page.locator("#provider-card-grid")).to_contain_text(HOSTILE)
    assert page.locator("#provider-card-grid img").count() == 0
    assert page.locator("#provider-tbody img").count() == 0
    page.get_by_role("button", name="Activity", exact=True).click()
    expect(page.locator("#log-tbody")).to_contain_text(HOSTILE)
    assert page.locator("#log-tbody img").count() == 0
    page.get_by_role("button", name="Settings", exact=True).click()
    expect(page.locator("#addon-grid")).to_contain_text(HOSTILE)
    assert page.locator("#addon-grid img").count() == 0
    assert page.evaluate("window.hermesXss || false") is False
    page.set_viewport_size({"width": 360, "height": 800})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    expect(page.get_by_role("button", name="Settings", exact=True)).to_be_in_viewport()
    page.get_by_role("button", name="Home", exact=True).click()
    page.screenshot(path=str(ROOT / ".pytest_cache" / "dashboard-mobile.png"), full_page=True)


def test_visible_page_polling_deduplicates_and_pauses(router_server, page):
    page.add_init_script("""
      const originalFetch=window.fetch; window.hermesFetchConcurrency={};
      window.fetch=async (...args)=>{
        const path=String(args[0]).split('?')[0], counts=window.hermesFetchConcurrency;
        if(!['/v1/status','/v1/logs'].includes(path))return originalFetch(...args);
        const stat=counts[path] ||= {active:0,max:0};stat.active++;stat.max=Math.max(stat.max,stat.active);
        try{await new Promise(resolve=>setTimeout(resolve,150));return await originalFetch(...args);}
        finally{stat.active--;}
      };
    """)
    calls = []
    page.on("request", lambda request: calls.append(request.url))
    login(page, router_server)
    assert not any("/v1/instances" in url or "/v1/logs" in url or "/v1/config/proxy-keys" in url for url in calls)
    page.get_by_role("button", name="Activity", exact=True).click()
    expect(page.locator("#log-tbody")).to_contain_text("No requests")
    page.wait_for_timeout(100)
    calls.clear()
    page.get_by_role("button", name="Refresh", exact=True).click(click_count=3)
    page.wait_for_timeout(200)
    assert all("/v1/logs" in url for url in calls)
    assert page.evaluate("Object.values(window.hermesFetchConcurrency).every(stat=>stat.max===1)")
    page.evaluate("Object.defineProperty(document, 'hidden', {configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'))")
    calls.clear()
    page.wait_for_timeout(5300)
    assert calls == []
    page.evaluate("Object.defineProperty(document, 'hidden', {configurable:true,get:()=>false});document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_timeout(300)
    assert any("/v1/logs" in url for url in calls)


def test_live_provider_save_and_pending_rotation_restart_are_truthful(router_server, page):
    login(page, router_server)
    page.get_by_role("button", name="Providers", exact=True).click()
    page.locator("#cfg-key-provider").select_option("openai")
    page.locator("#cfg-key-value").fill("mock-provider-key-no-network")
    page.locator("#cfg-provider-models input").first.check()
    page.get_by_role("button", name="Save provider", exact=True).click()
    expect(page.locator("#cfg-key-msg")).to_contain_text("Saved")
    expect(page.locator("#cfg-key-value")).to_have_value("")
    expect(page.locator("#restart-banner")).not_to_be_visible()
    page.locator('.rotation-settings > summary').click()
    page.get_by_role('button', name='Save rotation mode', exact=True).click()
    page.get_by_role("button", name="Home", exact=True).click()
    expect(page.locator("#plain-state")).to_contain_text("restart required")
    page.route("**/v1/config/restart", lambda route: route.fulfill(status=409, json={"error": {"message": "Restart unsupported in this runtime"}}))
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Restart Now").click()
    expect(page.locator("#panel-errors")).to_contain_text("Restart unsupported")
    expect(page.locator("#panel-errors")).to_contain_text("start.ps1")
    expect(page.locator("#restart-banner")).to_be_visible()
    page.get_by_role("button", name="Later", exact=True).click()
    expect(page.locator("#plain-state")).to_contain_text("restart required")


def test_external_instance_snapshot_and_application_access_actions(router_server, page):
    login(page, router_server)
    page.get_by_role("button", name="Settings", exact=True).click()
    page.locator("#settings-instances > summary").click()
    expect(page.locator("#instances-tbody")).to_contain_text("No instances yet")
    page.locator("#mode-docker").click()
    page.locator("#inst-host-port").fill("8320")
    expect(page.locator("#inst-base-url")).to_have_value("http://localhost:8320/v1")
    page.locator("#mode-external").click()
    expect(page.locator("#inst-port-fields")).not_to_be_visible()
    page.locator("#inst-name").fill("local dashboard instance")
    page.locator("#inst-base-url").fill(router_server + "/v1")
    page.locator("#inst-api-key").fill(AUTH_KEY)
    page.get_by_role("button", name="Register instance", exact=True).click()
    expect(page.locator("#instances-tbody")).to_contain_text("local dashboard instance")
    expect(page.locator("#instances-tbody")).to_contain_text("healthy", timeout=12000)
    expect(page.locator("#instances-tbody")).to_contain_text("fresh")
    assert "[object Object]" not in page.locator("#instances-tbody").inner_text()
    page.locator("#settings-access > summary").click()
    page.locator("#ak-name").fill(HOSTILE)
    page.locator("#ak-rpm").fill("5")
    page.get_by_role("button", name="Create key", exact=True).click()
    expect(page.locator("#new-key-panel")).to_be_visible()
    key = page.locator("#new-key-value").input_value()
    expect(page.locator("#access-keys-tbody")).to_contain_text(HOSTILE)
    assert page.locator("#access-keys-tbody img").count() == 0
    row = page.locator("#access-keys-tbody tr").filter(has_text=key[-6:])
    row.get_by_role("button", name="Edit", exact=True).click()
    row.locator('input[aria-label="Application name"]').fill("Renamed application")
    row.get_by_role("button", name="Save", exact=True).click()
    expect(page.locator("#access-keys-tbody")).to_contain_text("Renamed application")
    page.once("dialog", lambda dialog: dialog.accept())
    row.get_by_role("button", name="Revoke", exact=True).click()
    expect(page.locator("#access-keys-tbody")).not_to_contain_text(key[-6:])
    denied = page.request.get(router_server + "/v1/models", headers={"Authorization": "Bearer " + key})
    assert denied.status == 401
    expect(page.locator("#restart-banner")).not_to_be_visible()


def test_process_alive_does_not_claim_provider_ready(router_server, page):
    def unavailable(route):
        data = route.fetch().json()
        data["readiness"] = {"state": "unavailable", "configured_providers": 1, "available_providers": 0}
        route.fulfill(json=data)
    page.route("**/v1/status", unavailable)
    login(page, router_server)
    expect(page.locator("#plain-state")).to_contain_text("not ready")
    expect(page.locator("#plain-title")).to_contain_text("No provider is ready")
    expect(page.locator("#plain-message")).to_contain_text("Check provider credentials")


def test_workspace_layout_theme_and_navigation(router_server, page):
    login(page, router_server)
    page.get_by_role('button', name='Settings', exact=True).click()
    page.locator('#theme').select_option('dark')
    page.reload()
    expect(page.locator('#key-gate')).to_be_hidden()
    expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
    expect(page.locator('#theme')).to_have_value('dark')
    assert page.evaluate('JSON.parse(JSON.stringify(localStorage)).hermes_theme') == 'dark'
    for theme in ['light', 'dark']:
        page.get_by_role('button', name='Settings', exact=True).click()
        page.locator('#theme').select_option(theme)
        for width in [360, 768, 1440]:
            page.set_viewport_size({'width': width, 'height': 900})
            for name, label in [('overview','Home'),('providers','Providers'),('connect','Connect'),('logs','Activity'),('settings','Settings')]:
                nav = page.locator('[data-page="'+name+'"]')
                nav.click()
                expect(nav).to_have_attribute('aria-current', 'page')
                expect(page.locator('#current-page-title')).to_have_text(label)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (theme,width,name)
    assert page.evaluate('new Set([...document.querySelectorAll("[id]")].map(e=>e.id)).size === document.querySelectorAll("[id]").length')


def test_rotation_control_still_saves_in_advanced_section(router_server, page):
    login(page, router_server)
    page.get_by_role('button', name='Providers', exact=True).click()
    page.locator('.rotation-settings > summary').click()
    page.locator('#cfg-rotation-value').select_option('sequential')
    page.get_by_role('button', name='Save rotation mode', exact=True).click()
    expect(page.locator('#cfg-rotation-msg')).to_contain_text('Saved rotation mode')
    expect(page.locator('#restart-banner')).to_be_visible()


def test_model_change_keeps_active_stream_and_new_requests_use_new_model(router_server, stream_release):
    import requests
    headers = {'Authorization': 'Bearer ' + AUTH_KEY}
    owner = {'Authorization': 'Bearer ' + ADMIN_KEY}
    body = {'model':'hermes-router', 'messages':[{'role':'user','content':'repeat this'}], 'max_tokens':8}
    initial = requests.post(router_server+'/v1/chat/completions', headers=headers, json=body).json()
    assert initial['model'] == 'test-local'
    from concurrent.futures import ThreadPoolExecutor
    def consume():
        with requests.post(router_server+'/v1/chat/completions', headers=headers,
                json={**body, 'messages':[{'role':'user','content':'__hold_stream__'}], 'stream':True}, stream=True, timeout=15) as response:
            return list(response.iter_lines(chunk_size=1))
    with ThreadPoolExecutor(max_workers=1) as executor:
        active = executor.submit(consume)
        try:
            assert stream_release.started.wait(3)
            assert not active.done()
            uptime = requests.get(router_server+'/v1/usage',headers=owner).json()['uptime_s']
            changed = requests.post(router_server+'/v1/config/model/local', headers=owner,json={'model':'changed-local'})
            assert changed.status_code == 200 and changed.json()['restart_required'] is False
            assert not active.done()
        finally:
            stream_release.set()
        lines = active.result(timeout=5)
        assert b'test-local' in lines[0]
        assert b'data: [DONE]' in lines
    after = requests.post(router_server+'/v1/chat/completions', headers=headers, json=body).json()
    assert after['model'] == 'changed-local'
    assert requests.get(router_server+'/v1/usage',headers=owner).json()['uptime_s'] >= uptime
