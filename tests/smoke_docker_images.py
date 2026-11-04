import subprocess, sys, uuid

tag, platform = sys.argv[1:]
name = 'hermes-release-check-' + uuid.uuid4().hex[:10]
volume = name + '-data'
def docker(*args, input=None):
    result = subprocess.run(['docker', *args], input=input, capture_output=True, text=True, encoding='utf-8')
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout

probe = '''
import requests,time,json,hashlib
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import threading
class Upstream(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if body['model']=='custom-smoke': assert self.headers.get('Authorization')=='Bearer replacement-key'
        if body['model']=='local-smoke': assert self.headers.get('Authorization') is None
        payload=json.dumps({'model':body['model'],'choices':[{'message':{'role':'assistant','content':'OK'},'finish_reason':'stop'}],'usage':{'prompt_tokens':1,'completion_tokens':1}}).encode()
        self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
upstream=ThreadingHTTPServer(('127.0.0.1',18320),Upstream)
threading.Thread(target=upstream.serve_forever,daemon=True).start()
base='http://127.0.0.1:8319'
for _ in range(100):
    try:
        if requests.get(base+'/health',timeout=1).status_code==200: break
    except requests.RequestException: pass
    time.sleep(.2)
else: raise RuntimeError('health failed')
assert requests.get(base+'/dashboard').status_code==200
env=dict(x.split('=',1) for x in Path('/app/data/.env').read_text().splitlines() if '=' in x)
admin=env['ADMIN_API_KEY']
assert requests.post(base+'/v1/admin/session',json={'api_key':admin}).status_code==200
record=Path('/app/data/release-smoke.json')
if record.exists():
    saved=json.loads(record.read_text())
    assert saved['admin_hash']==hashlib.sha256(admin.encode()).hexdigest()
    app=saved['app']
    features=requests.get(base+'/v1/status',headers={'Authorization':'Bearer '+admin}).json()['features']['addons']
    state={a['name']:a['enabled'] for a in features}
    assert state['fast_routing'] and state['persistent_cache'] and not state['request_log']
    assert requests.get(base+'/v1/status',headers={'Authorization':'Bearer '+admin}).json()['rotation']['mode']=='sequential'
else:
    r=requests.post(base+'/v1/config/proxy-keys',headers={'Authorization':'Bearer '+admin},json={'name':'release-check'})
    assert r.status_code==200,r.status_code
    app=r.json()['key']
    r=requests.post(base+'/v1/chat/completions',headers={'Authorization':'Bearer '+app},json={'messages':[{'role':'user','content':'hi'}],'max_tokens':8})
    assert r.status_code==503,r.status_code
    started=Path('/proc/1/stat').read_text().split()[21]
    for feature,enabled in [('fast_routing',True),('persistent_cache',True),('request_log',False)]:
        changed=requests.post(base+'/v1/config/features/'+feature,headers={'Authorization':'Bearer '+admin},json={'enabled':enabled})
        assert changed.status_code==200 and changed.json()['restart_required'] is False
    assert Path('/proc/1/stat').read_text().split()[21]==started
    record.write_text(json.dumps({'admin_hash':hashlib.sha256(admin.encode()).hexdigest(),'app':app}))
headers={'Authorization':'Bearer '+app}
assert requests.get(base+'/v1/models',headers=headers).status_code==200
assert requests.get(base+'/v1/status',headers=headers).status_code==401
assert requests.get(base+'/v1/usage',headers=headers).json()['keys'][0]['req_total']>=1
owner={'Authorization':'Bearer '+admin}
started=Path('/proc/1/stat').read_text().split()[21]
changed=requests.post(base+'/v1/config/rotation',headers=owner,json={'mode':'sequential'})
assert changed.status_code==200 and changed.json()['restart_required'] is False
assert requests.get(base+'/v1/status',headers=owner).json()['rotation']['mode']=='sequential'
assert Path('/proc/1/stat').read_text().split()[21]==started
if attempt == 0:
    saved=requests.post(base+'/v1/config/keys/kimi',headers=owner,json={'key':'mock-provider-key','model':'model-a'})
    assert saved.status_code==200 and saved.json()['restart_required'] is False
    expected='model-a'
else:
    expected='model-b'
scoped=requests.post(base+'/v1/config/proxy-keys',headers=owner,json={'name':'kimi-smoke','allowed_providers':['kimi']}).json()['key']
headers={'Authorization':'Bearer '+scoped}
body={'model':'hermes-router','messages':[{'role':'user','content':'test live model'}],'max_tokens':8}
answer=requests.post(base+'/v1/chat/completions',headers=headers,json=body).json()
assert answer['model']==expected,answer
changed=requests.post(base+'/v1/config/model/kimi',headers=owner,json={'model':'model-b'})
assert changed.status_code==200 and changed.json()['restart_required'] is False
assert requests.post(base+'/v1/chat/completions',headers=headers,json=body).json()['model']=='model-b'
assert Path('/proc/1/stat').read_text().split()[21]==started
for provider,model in [('custom_smoke','custom-smoke'),('local','local-smoke')]:
    config={'label':provider,'base_url':'http://127.0.0.1:18320/v1','model':model,'key':'replacement-key' if provider=='custom_smoke' else ''}
    if attempt==0:
        saved=requests.post(base+'/v1/config/custom-providers/'+provider,headers=owner,json=config)
        assert saved.status_code==200 and saved.json()['restart_required'] is False
    else:
        saved=requests.get(base+'/v1/config/providers',headers=owner).json()
        assert saved['configured_models'][provider]==model
    appkey=requests.post(base+'/v1/config/proxy-keys',headers=owner,json={'name':'custom-smoke','allowed_providers':[provider]}).json()['key']
    response=requests.post(base+'/v1/chat/completions',headers={'Authorization':'Bearer '+appkey},json={'model':'hermes-router','messages':[{'role':'user','content':'custom smoke'}],'max_tokens':8})
    assert response.status_code==200 and response.json()['model']==model
assert Path('/proc/1/stat').read_text().split()[21]==started
upstream.shutdown()
print('health/dashboard/auth/live provider/custom/local/model/rotation/settings/persistence/durable usage OK')
'''
try:
    docker('volume','create',volume)
    for attempt in range(2):
        docker('run','-d','--platform',platform,'--name',name,'-e','AUTO_DISCOVER_MODELS=0','-e','KIMI_BASE_URL=http://127.0.0.1:18320/v1','-v',volume+':/app/data',tag)
        print(platform,tag,attempt,docker('exec','-i',name,'python','-',input='attempt='+str(attempt)+'\n'+probe).strip(),flush=True)
        if tag.endswith('-cli'):
            docker('exec',name,'hr','status','--json')
            docker('exec',name,'hr','features','list')
        docker('rm','-f',name)
finally:
    subprocess.run(['docker','rm','-f',name],capture_output=True)
    docker('volume','rm',volume)
