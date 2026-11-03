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
else:
    r=requests.post(base+'/v1/config/proxy-keys',headers={'Authorization':'Bearer '+admin},json={'name':'release-check'})
    assert r.status_code==200,r.status_code
    app=r.json()['key']
    r=requests.post(base+'/v1/chat/completions',headers={'Authorization':'Bearer '+app},json={'messages':[{'role':'user','content':'hi'}],'max_tokens':8})
    assert r.status_code==503,r.status_code
    record.write_text(json.dumps({'admin_hash':hashlib.sha256(admin.encode()).hexdigest(),'app':app}))
headers={'Authorization':'Bearer '+app}
assert requests.get(base+'/v1/models',headers=headers).status_code==200
assert requests.get(base+'/v1/status',headers=headers).status_code==401
assert requests.get(base+'/v1/usage',headers=headers).json()['keys'][0]['req_total']==1
print('health/dashboard/auth/live key/durable usage OK')
'''
try:
    docker('volume','create',volume)
    for attempt in range(2):
        docker('run','-d','--platform',platform,'--name',name,'-e','AUTO_DISCOVER_MODELS=0','-v',volume+':/app/data',tag)
        print(platform,tag,attempt,docker('exec','-i',name,'python','-',input=probe).strip(),flush=True)
        if tag.endswith('-cli'):
            docker('exec',name,'hr','status','--json')
            docker('exec',name,'hr','features','list')
        docker('rm','-f',name)
finally:
    subprocess.run(['docker','rm','-f',name],capture_output=True)
    docker('volume','rm',volume)
