import time

from usage_store import KeyUsage


def test_aggregate_snapshot_preserves_days_provenance_and_key_isolation(tmp_path):
    ledger = KeyUsage(tmp_path/'usage.db')
    now = time.time()
    today = time.strftime('%Y-%m-%d', time.gmtime(now))
    yesterday = time.strftime('%Y-%m-%d', time.gmtime(now-86400))
    rows = [
        ('pending', today, now, 10, .1, 0, 'reserved', 0),
        ('reported', today, now-120, 3, .03, 1, 'reported', 0),
        ('estimated', yesterday, now-86400, 4, .04, 1, 'estimated', 0),
        ('unknown', today, now-1, 5, 0, 1, 'unknown_price', 0),
        ('cached', today, now-1, 6, 0, 1, 'reported', 1),
    ]
    with ledger._connect() as db:
        db.executemany('INSERT INTO usage(id,key_hash,day,created,requests,tokens,cost,settled,source,cached) VALUES(?,?,?,?,1,?,?,?,?,?)',
                       [(rid, ledger._key('key'), day, created, tokens, cost, settled, source, cached)
                        for rid, day, created, tokens, cost, settled, source, cached in rows])
    ledger.reserve('other-key', {}, 1000, 100)
    assert ledger.snapshot('key') == {
        'req_today': 4, 'tokens_today': 24, 'cost_today': .13,
        'req_total': 5, 'tokens_total': 28, 'cost_total': .17,
        'rpm_window': 3, 'pending_reservations': 1, 'estimated_requests': 1,
        'unknown_price_requests': 1, 'cost_status': 'unknown', 'cached_requests': 1,
    }
    assert ledger.snapshot('missing') == {
        'req_today': 0, 'tokens_today': 0, 'cost_today': 0,
        'req_total': 0, 'tokens_total': 0, 'cost_total': 0,
        'rpm_window': 0, 'pending_reservations': 0, 'estimated_requests': 0,
        'unknown_price_requests': 0, 'cost_status': 'estimated', 'cached_requests': 0,
    }


def test_rolling_admission_query_uses_key_timestamp_index(tmp_path):
    ledger = KeyUsage(tmp_path/'usage.db')
    with ledger._connect() as db:
        plan = db.execute('EXPLAIN QUERY PLAN SELECT COALESCE(SUM(requests),0) FROM usage WHERE key_hash=? AND created>?',
                          (ledger._key('key'), time.time()-60)).fetchall()
    assert any('usage_key_created' in row[3] for row in plan)
