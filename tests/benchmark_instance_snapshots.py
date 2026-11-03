"""Synthetic instance-list comparison; no network, Docker, or operator config.

Run from any directory: python -B D:/Hermes-router/tests/benchmark_instance_snapshots.py
"""
import json
from pathlib import Path
import platform
import runpy
import statistics
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
runpy.run_path(str(ROOT / 'tests' / 'conftest.py'))
import router

assert not router.PROVIDERS
AUTH = {'Authorization': 'Bearer sk-test-admin'}
COUNTS = [1, 5, 10, 50]
SAMPLES = 7
counts = {'health': 0, 'docker': 0, 'active': 0, 'peak': 0}
lock = threading.Lock()


def fake_probe(entry):
    with lock:
        counts['health'] += 1
        counts['active'] += 1
        counts['peak'] = max(counts['peak'], counts['active'])
    time.sleep(.01)
    with lock:
        counts['active'] -= 1
    return {'status': 'healthy', 'health_ok': True}


def fake_docker(entry):
    counts['docker'] += 1
    time.sleep(.01)
    return {'status': 'running', 'running': True}


def request_samples(client, n):
    values = []
    for _ in range(n):
        start = time.perf_counter()
        response = client.get('/v1/instances', headers=AUTH)
        values.append(round((time.perf_counter() - start) * 1000, 3))
        assert response.status_code == 200
        assert len(response.json['instances']) == size
    return values


router._probe_instance = fake_probe
router._docker_state = fake_docker
client = router.app.test_client()
original_snapshot = router._instance_snapshot
report = {'python': platform.python_version(), 'platform': platform.platform(),
          'samples_per_case': SAMPLES, 'probe_delay_ms': 10, 'max_workers': 4,
          'baseline': 'Emulated previous serial live serializer in current authenticated Flask endpoint',
          'results': []}
for size in COUNTS:
    entries = [{'id': f'{size}-{i}', 'mode': 'docker', 'base_url': 'http://unused/v1'} for i in range(size)]
    with router._INSTANCE_LOCK:
        router._write_instances_doc({'instances': entries})
    router._instance_snapshot = lambda entry: {'live': fake_probe(entry), 'docker': fake_docker(entry)}
    serial = request_samples(client, SAMPLES)
    router._instance_snapshot = original_snapshot
    counts.update({'health': 0, 'docker': 0, 'peak': 0})
    cold = request_samples(client, SAMPLES)
    deadline = time.monotonic() + 5
    while True:
        response = client.get('/v1/instances', headers=AUTH)
        if all(e['freshness']['state'] == 'fresh' for e in response.json['instances']):
            break
        assert time.monotonic() < deadline
        time.sleep(.005)
    warm = request_samples(client, SAMPLES)
    assert counts['health'] == counts['docker'] == size
    assert counts['peak'] <= 4
    report['results'].append({'instances': size, 'serial_ms': serial, 'cold_snapshot_ms': cold,
                              'warm_snapshot_ms': warm, 'serial_median_ms': statistics.median(serial),
                              'cold_median_ms': statistics.median(cold), 'warm_median_ms': statistics.median(warm),
                              'background_checks': counts['health'], 'peak_parallel_checks': counts['peak']})
print(json.dumps(report, indent=2))
