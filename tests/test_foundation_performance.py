import json
import threading
import time

import pytest
import requests
import router


class Reply:
    def __init__(self, status=200, chunks=(), data=None):
        self.status_code = status
        self.headers = {}
        self.text = ''
        self.chunks = chunks
        self.data = data or {'choices': [{'message': {'content': 'ok'}}]}
        self.closed = 0

    def close(self):
        self.closed += 1

    def json(self):
        return self.data

    def iter_content(self, **_):
        yield from self.chunks

    def iter_lines(self):
        for chunk in self.chunks:
            yield from chunk.splitlines()


class Pool:
    def key_count(self, *_): return 1
    def get_key(self, *_): return 'fake-key'
    def mark_key_down(self, *_, **__): pass
    def mark_rate_limited(self, *_, **__): pass


@pytest.fixture
def routed(monkeypatch):
    candidates = [{'provider': {'name': f'fake{i}', 'model': 'm', 'protocol': '', 'base_url': 'http://unused'}, 'model': 'm'} for i in range(2)]
    monkeypatch.setattr(router, '_ordered_providers', lambda *_: candidates)
    monkeypatch.setattr(router, 'pool', Pool())
    monkeypatch.setattr(router, 'cache', router.ResponseCache(ttl=0))
    monkeypatch.setattr(router, 'stats', router.ProviderStats())
    monkeypatch.setattr(router, 'SEMANTIC_CACHE', False)
    return candidates


@pytest.mark.parametrize('status', [429, 401, 400, 413, 500, 302])
def test_rejected_response_closes_before_failover(monkeypatch, routed, status):
    rejected, accepted = Reply(status), Reply()
    def forward(provider, *_):
        if provider['name'] == 'fake0': return rejected
        assert rejected.closed == 1
        return accepted
    monkeypatch.setattr(router, 'forward', forward)
    assert router._route_completion({'messages': []}, False)[0] == 'json'
    assert rejected.closed == accepted.closed == 1


def test_stream_close_before_iteration_releases_response():
    reply = Reply(chunks=[b'data: [DONE]\n\n'])
    gen = router._with_cleanup(reply, router._streaming_generator(reply))
    gen.close()
    gen.close()
    assert reply.closed == 1


def test_stream_cancellation_closes_and_settles_once(monkeypatch):
    reply = Reply(chunks=[b'data: {"choices":[{"delta":{"content":"yes"}}]}\n\n', b'data: [DONE]\n\n'])
    settlements = []
    monkeypatch.setattr(router, '_settle_request_usage', lambda *args, **kwargs: settlements.append((args, kwargs)), raising=False)
    with router.app.test_request_context():
        from flask import g
        g.usage_reservation = 'reserved'
        gen = router._streaming_with_usage(router._with_cleanup(reply, router._streaming_generator(reply)), 'fake', 'm')
    next(gen)
    gen.close()
    gen.close()
    assert reply.closed == 1
    assert len(settlements) == 1
    assert settlements[0][0][0] == 'reserved'
    assert settlements[0][1]['failed'] is False


def test_cumulative_deadline_stops_additional_attempts(monkeypatch, routed):
    monkeypatch.setattr(router, 'REQUEST_TIMEOUT', .01, raising=False)
    calls = []
    def slow(*_):
        calls.append(1)
        time.sleep(.02)
        return Reply(503)
    monkeypatch.setattr(router, 'forward', slow)
    result = router._route_completion({'messages': []}, False)
    assert result[2] == 504
    assert len(calls) == 1


def test_codex_aggregation_error_closes_response(monkeypatch, routed):
    routed[0]['provider']['protocol'] = 'codex'
    class Broken(Reply):
        def iter_lines(self):
            raise requests.ConnectionError('broken stream')
            yield
    bad, good = Broken(), Reply()
    monkeypatch.setattr(router, 'forward', lambda p, *_: bad if p['name'] == 'fake0' else good)
    assert router._route_completion({'messages': []}, False)[0] == 'json'
    assert bad.closed == good.closed == 1


def test_semantic_cache_requires_compatible_request_and_embedding_identity(tmp_path):
    path = str(tmp_path / 'cache.db')
    cache = router.ResponseCache(persist=True, db_path=path)
    payload = {'model': 'a', 'messages': [{'role': 'system', 'content': 'private'}, {'role': 'user', 'content': 'hello'}], 'temperature': 0}
    emb = {'identity': 'provider/model-a', 'vector': [1., 0.]}
    cache.set(payload, {'answer': 'safe'}, 'caller', emb)
    compatible = {**payload, 'messages': [payload['messages'][0], {'role': 'user', 'content': 'hi'}]}
    assert cache.semantic_lookup(emb, 'caller', compatible) == {'answer': 'safe'}
    for changed in ({**compatible, 'model': 'b'}, {**compatible, 'temperature': 1}, {**compatible, 'tools': []}, {**compatible, 'messages': [{'role': 'system', 'content': 'public'}, {'role': 'user', 'content': 'hi'}]}):
        assert cache.semantic_lookup(emb, 'caller', changed) is None
    assert cache.semantic_lookup({**emb, 'identity': 'provider/model-b'}, 'caller', compatible) is None
    restarted = router.ResponseCache(persist=True, db_path=path)
    assert restarted.semantic_lookup(emb, 'caller', compatible) == {'answer': 'safe'}
    assert restarted.get(payload, 'caller') == {'answer': 'safe'}


def test_legacy_embeddings_are_exact_only(tmp_path):
    path = str(tmp_path / 'legacy.db')
    cache = router.ResponseCache(persist=True, db_path=path)
    cache.set({'messages': []}, {'legacy': True}, 'caller', [1., 0.])
    cache = router.ResponseCache(persist=True, db_path=path)
    assert cache.get({'messages': []}, 'caller') == {'legacy': True}
    assert cache.semantic_lookup({'identity': 'a', 'vector': [1., 0.]}, 'caller', {'messages': []}) is None


def test_stats_summary_does_not_deadlock():
    stats = router.ProviderStats()
    stats.record_success('fake', .01)
    result = []
    thread = threading.Thread(target=lambda: result.append(stats.all_summaries()), daemon=True)
    thread.start()
    thread.join(.5)
    assert not thread.is_alive()
    assert result[0]['fake']['total_requests'] == 1


@pytest.mark.parametrize('protocol', ['', 'anthropic', 'codex'])
def test_stream_protocols_report_usage_and_close(monkeypatch, routed, protocol):
    routed[:] = routed[:1]
    routed[0]['provider']['protocol'] = protocol
    usage = {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}
    if protocol == 'anthropic':
        events = [
            {'type': 'message_start', 'message': {'usage': {'input_tokens': 3}}},
            {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'ok'}},
            {'type': 'message_delta', 'usage': {'output_tokens': 2}},
            {'type': 'message_stop'},
        ]
    elif protocol == 'codex':
        events = [
            {'type': 'response.output_text.delta', 'delta': 'ok'},
            {'type': 'response.completed', 'response': {'usage': {'input_tokens': 3, 'output_tokens': 2, 'total_tokens': 5}}},
        ]
    else:
        events = [{'choices': [{'delta': {'content': 'ok'}}]}, {'choices': [], 'usage': usage}]
    raw = b''.join(('data: ' + json.dumps(event) + '\n\n').encode() for event in events)
    reply = Reply(chunks=[raw[:17], raw[17:]] if protocol != 'codex' else [raw])
    monkeypatch.setattr(router, 'forward', lambda *_: reply)
    settlements, provider_usage = [], []
    monkeypatch.setattr(router, '_settle_request_usage', lambda *args, **kwargs: settlements.append((args, kwargs)))
    monkeypatch.setattr(router, '_add_provider_tokens', lambda *args: provider_usage.append(args))
    with router.app.test_request_context():
        from flask import g
        g.usage_reservation = 'reservation'
        result = router._route_completion({'messages': []}, True)
    assert router.stats.summary('fake0')['total_requests'] == 0
    list(result[1])
    result[1].close()
    assert reply.closed == 1
    assert len(settlements) == len(provider_usage) == 1
    assert settlements[0][0] == ('reservation', usage, 'm')
    assert settlements[0][1]['failed'] is False
    assert router.stats.summary('fake0')['total_requests'] == 1


def test_stream_iteration_error_closes_and_releases_unemitted_reservation(monkeypatch):
    class Broken(Reply):
        def iter_content(self, **_):
            raise requests.ConnectionError('disconnected')
            yield
    reply = Broken()
    settlements = []
    monkeypatch.setattr(router, '_settle_request_usage', lambda *args, **kwargs: settlements.append((args, kwargs)))
    with router.app.test_request_context():
        from flask import g
        g.usage_reservation = 'reservation'
        router._req_ctx.deadline = time.monotonic() + 1
        stream = router._streaming_with_usage(router._with_cleanup(reply, router._streaming_generator(reply)), 'fake', 'm')
    with pytest.raises(requests.ConnectionError):
        next(stream)
    assert reply.closed == 1
    assert len(settlements) == 1
    assert settlements[0][1]['failed'] is True


def test_embedding_fallback_identity_and_cleanup(monkeypatch):
    providers = [{'name': name, 'embed_model': 'same-size', 'base_url': f'http://{name}'} for name in ('first', 'second')]
    monkeypatch.setattr(router, '_embed_ordered', lambda: providers)
    monkeypatch.setattr(router, 'pool', Pool())
    first, second = Reply(503), Reply(data={'data': [{'embedding': [1., 0.]}]})
    monkeypatch.setattr(router, 'forward_embeddings', lambda p, *_: first if p['name'] == 'first' else second)
    router._req_ctx.deadline = time.monotonic() + 1
    embedding = router._embed_text('text')
    assert embedding['vector'] == [1., 0.]
    assert first.closed == second.closed == 1
    monkeypatch.setattr(router, 'forward_embeddings', lambda *_: Reply(data={'data': [{'embedding': [1., 0.]}]}))
    assert router._embed_text('text')['identity'] != embedding['identity']


def test_budgeted_requests_skip_internal_embedding_work(monkeypatch, routed):
    monkeypatch.setattr(router, 'SEMANTIC_CACHE', True)
    monkeypatch.setattr(router, '_embed_ordered', lambda: (_ for _ in ()).throw(AssertionError('must skip embeddings')))
    monkeypatch.setattr(router, 'forward', lambda *_: Reply())
    with router.app.test_request_context():
        from flask import g
        g.caller_limits = {'tokens_per_day': 50}
        assert router._route_completion({'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 1}, False)[0] == 'json'


def test_embedding_provider_scope_is_enforced(monkeypatch):
    providers = [{'name': name, 'embed_model': 'embed'} for name in ('allowed', 'forbidden')]
    monkeypatch.setattr(router, 'PROVIDERS', providers)
    with router.app.test_request_context():
        from flask import g
        g.caller_scope = {'allowed'}
        assert [p['name'] for p in router._embed_ordered()] == ['allowed']


def test_health_and_inference_serve_while_capability_probe_is_blocked(monkeypatch, routed):
    entered, release = threading.Event(), threading.Event()
    def blocked(*_):
        entered.set()
        assert release.wait(2)
    monkeypatch.setattr(router, '_initialize_ratings', blocked)
    monkeypatch.setattr(router, '_INITIALIZATION_STATE', {'state': 'initializing'}, raising=False)
    monkeypatch.setattr(router, '_provider_state', {})
    monkeypatch.setattr(router, '_model_state', {})
    monkeypatch.setattr(router, 'PROVIDERS', [routed[0]['provider']])
    monkeypatch.setattr(router, 'forward', lambda *_: Reply())
    thread = threading.Thread(target=lambda: router._initialize_providers(router.PROVIDERS, router.pool), daemon=True)
    thread.start()
    try:
        assert entered.wait(.2)
        started = time.monotonic()
        health = router.app.test_client().get('/health')
        assert health.status_code == 200
        assert health.json['initializing'] is True
        assert health.json['ready'] is False
        assert time.monotonic() - started < .2
        assert router._route_completion({'messages': []}, False)[0] == 'json'
        with router.app.test_request_context(headers={'X-Hermes-Tool-Loop': 'true'}):
            result = router._route_completion({'messages': [], 'tools': [{'type': 'function', 'function': {'name': 'read'}}]}, False)
            assert result[2] == 503
    finally:
        release.set()
        thread.join(1)
    assert not thread.is_alive()
    assert router.app.test_client().get('/health').json['initializing'] is False


def test_initialization_failure_remains_visible_without_secrets(monkeypatch):
    monkeypatch.setattr(router, '_INITIALIZATION_STATE', {'state': 'initializing'}, raising=False)
    def broken(*_):
        raise ValueError('sensitive provider credential')
    monkeypatch.setattr(router, '_initialize_ratings', broken)
    router._initialize_providers([], None)
    health = router.app.test_client().get('/health')
    assert health.json['readiness']['state'] == 'error'
    assert health.json['ready'] is False
    assert 'sensitive provider credential' not in health.text
