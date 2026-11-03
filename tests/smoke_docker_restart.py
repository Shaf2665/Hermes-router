"""Run against a built image: no external network, no real provider credentials."""
import json
import subprocess
import sys
import time
import uuid

image = sys.argv[1]
name = 'hermes-restart-check-' + uuid.uuid4().hex[:10]
volume = name + '-data'


def docker(*args, input=None):
    result = subprocess.run(['docker', *args], input=input, capture_output=True,
                            text=True, encoding='utf-8', check=True)
    return result.stdout


probe = '''
import requests,time,json
from pathlib import Path
base='http://127.0.0.1:8319'
for _ in range(100):
    try:
        if requests.get(base+'/health',timeout=1).status_code==200: break
    except requests.RequestException: pass
    time.sleep(.2)
else: raise RuntimeError('health failed')
env=dict(x.split('=',1) for x in Path('/app/data/.env').read_text().splitlines() if '=' in x)
admin={'Authorization':'Bearer '+env['ADMIN_API_KEY']}
assert requests.post(base+'/v1/config/restart').status_code==401
assert requests.post(base+'/v1/config/restart',headers={'Authorization':'Bearer '+env['PROXY_API_KEYS']}).status_code==401
'''

try:
    docker('volume', 'create', volume)
    docker('run', '-d', '--name', name, '--restart', 'unless-stopped', '--network', 'none',
           '-e', 'HR_CONTAINER_RESTART=1', '-e', 'AUTO_DISCOVER_MODELS=0',
           '-v', volume+':/app/data', image)
    docker('exec', '-i', name, 'python', '-', input=probe + '''
assert requests.post(base+'/v1/config/keys/openai',headers=admin,json={'key':'not-a-real-key'}).status_code==200
assert requests.post(base+'/v1/config/model/openai',headers=admin,json={'model':'hermes-restart-test-model'}).status_code==200
''')
    # Docker activates the restart policy after the container has run for ten seconds.
    time.sleep(10)
    docker('exec', '-i', name, 'python', '-', input=probe + '''
assert requests.post(base+'/v1/config/restart',headers=admin).status_code==200
''')
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        state = json.loads(docker('inspect', name))[0]
        if state['RestartCount'] >= 1 and state['State']['Running']:
            break
        time.sleep(.5)
    else:
        raise AssertionError('Docker did not restart the container')
    docker('exec', '-i', name, 'python', '-', input=probe + '''
status=requests.get(base+'/v1/status',headers=admin).json()
assert status['providers']['openai']['model']=='hermes-restart-test-model'
assert requests.post(base+'/v1/admin/session',json={'api_key':env['ADMIN_API_KEY']}).status_code==200
''')
    print('PASS Docker restart: access control, response delivery, automatic restart, saved provider/model loaded, administrator login.')
finally:
    subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
    subprocess.run(['docker', 'volume', 'rm', volume], capture_output=True)
