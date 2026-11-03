"""SQLite admission ledger. Unfinished reservations remain charged across restarts."""
import hashlib
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class KeyUsage:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS usage (
                id TEXT PRIMARY KEY, key_hash TEXT NOT NULL, day TEXT NOT NULL,
                created REAL NOT NULL, requests INTEGER NOT NULL,
                tokens INTEGER NOT NULL, cost REAL NOT NULL,
                settled INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'reserved',
                cached INTEGER NOT NULL DEFAULT 0)""")
            db.execute("CREATE INDEX IF NOT EXISTS usage_key_day ON usage(key_hash, day)")
            db.execute("CREATE INDEX IF NOT EXISTS usage_key_created ON usage(key_hash, created)")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _key(key):
        return hashlib.sha256(key.encode()).hexdigest()

    def reserve(self, key, limits, tokens=0, cost=0):
        now = time.time()
        day = time.strftime('%Y-%m-%d', time.gmtime(now))
        midnight = max(1, 86400 - int(now % 86400))
        kh = self._key(key)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            req, tok, usd = db.execute(
                'SELECT COALESCE(SUM(requests),0),COALESCE(SUM(tokens),0),COALESCE(SUM(cost),0) FROM usage WHERE key_hash=? AND day=?',
                (kh, day)).fetchone()
            rpm = db.execute('SELECT COALESCE(SUM(requests),0) FROM usage WHERE key_hash=? AND created>?', (kh, now-60)).fetchone()[0]
            checks = [('rpm', rpm+1, 60), ('req_per_day', req+1, midnight),
                      ('tokens_per_day', tok+tokens, midnight), ('cost_per_day', usd+cost, midnight)]
            for field, value, retry in checks:
                limit = limits.get(field, 0)
                if limit and value > limit:
                    return None, retry, field
            rid = uuid.uuid4().hex
            db.execute('INSERT INTO usage(id,key_hash,day,created,requests,tokens,cost) VALUES(?,?,?,?,1,?,?)',
                       (rid, kh, day, now, tokens, cost))
            return rid, 0, ''

    def settle(self, rid, tokens=None, cost=None, *, failed=False, cached=False, source='reported'):
        if not rid:
            return
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT tokens,cost FROM usage WHERE id=? AND settled=0', (rid,)).fetchone()
            if row is None:
                return
            tok = 0 if failed else row[0] if tokens is None else max(0, int(tokens))
            usd = 0 if failed or cached else row[1] if cost is None else max(0.0, float(cost))
            db.execute('UPDATE usage SET tokens=?,cost=?,settled=1,source=?,cached=? WHERE id=? AND settled=0',
                       (tok, usd, 'failed' if failed else source, int(cached), rid))

    def snapshot(self, key):
        day = time.strftime('%Y-%m-%d', time.gmtime())
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            result = dict(db.execute('''SELECT
                COALESCE(SUM(CASE WHEN day=:day THEN requests ELSE 0 END),0) AS req_today,
                COALESCE(SUM(CASE WHEN day=:day THEN tokens ELSE 0 END),0) AS tokens_today,
                COALESCE(SUM(CASE WHEN day=:day THEN cost ELSE 0 END),0) AS cost_today,
                COALESCE(SUM(requests),0) AS req_total,
                COALESCE(SUM(tokens),0) AS tokens_total,
                COALESCE(SUM(cost),0) AS cost_total,
                COALESCE(SUM(CASE WHEN created>:cutoff THEN requests ELSE 0 END),0) AS rpm_window,
                COALESCE(SUM(settled=0),0) AS pending_reservations,
                COALESCE(SUM(source='estimated'),0) AS estimated_requests,
                COALESCE(SUM(source='unknown_price'),0) AS unknown_price_requests,
                COALESCE(SUM(cached),0) AS cached_requests
                FROM usage WHERE key_hash=:key''',
                {'day': day, 'cutoff': time.time()-60, 'key': self._key(key)}).fetchone())
        result['cost_today'] = round(result['cost_today'], 8)
        result['cost_total'] = round(result['cost_total'], 8)
        result['cost_status'] = 'unknown' if result['unknown_price_requests'] else 'estimated'
        return result
