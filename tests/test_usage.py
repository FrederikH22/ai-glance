import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from ai_glance import usage

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


def ms(dt):
    return int(dt.timestamp() * 1000)


def make_db(path, rows, accounts=None, wal=True):
    conn = sqlite3.connect(path)
    if wal:
        conn.execute('PRAGMA journal_mode=WAL')
    conn.execute(
        'CREATE TABLE usage_history (id INTEGER PRIMARY KEY, recorded_at INTEGER, provider TEXT,'
        ' account_key TEXT, email TEXT, account_id TEXT, limit_id TEXT, label TEXT,'
        ' window_label TEXT, used_fraction REAL, status TEXT, resets_at INTEGER)')
    conn.execute(
        'CREATE TABLE auth_credentials (id INTEGER PRIMARY KEY, provider TEXT, credential_type TEXT,'
        ' data TEXT, disabled_cause TEXT, identity_key TEXT, created_at INTEGER, updated_at INTEGER)')
    if accounts is None:
        accounts = [('acc-f', 'frederik@example.com', None)]
    for acc_id, email, cause in accounts:
        data = json.dumps({'accountId': acc_id, 'email': email, 'access': 'SECRET-TOKEN'})
        conn.execute(
            "INSERT INTO auth_credentials (provider, credential_type, data, disabled_cause)"
            " VALUES ('anthropic','oauth',?,?)", (data, cause))
    for acc_id, label, rec, frac, status, resets in rows:
        conn.execute(
            "INSERT INTO usage_history (recorded_at, provider, account_id, label, used_fraction,"
            " status, resets_at) VALUES (?, 'anthropic', ?, ?, ?, ?, ?)",
            (ms(rec), acc_id, label, frac, status, resets and ms(resets)))
    conn.commit()
    if wal:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    conn.close()


def test_only_enabled_accounts(tmp_path):
    db = tmp_path / 'agent.db'
    t = NOW - timedelta(minutes=3)
    make_db(db, [('acc-f', 'Claude 5 Hour', t, 0.1, 'ok', None),
                 ('acc-a', 'Claude 5 Hour', t, 0.2, 'ok', None)],
            accounts=[('acc-f', 'frederik@example.com', None),
                      ('acc-a', 'colleague@example.com', 'deleted by user')])
    snap = usage.read_usage(db)
    assert snap.error is None
    assert [a.email for a in snap.accounts] == ['frederik@example.com']


def test_latest_row_per_label(tmp_path):
    db = tmp_path / 'agent.db'
    old, new = NOW - timedelta(minutes=30), NOW - timedelta(minutes=2)
    make_db(db, [('acc-f', 'Claude 5 Hour', old, 0.1, 'ok', None),
                 ('acc-f', 'Claude 5 Hour', new, 0.6, 'ok', None),
                 ('acc-f', 'Claude 7 Day', old, 0.3, 'ok', None)])
    snap = usage.read_usage(db)
    ws = {w.label: w for w in snap.accounts[0].windows}
    assert ws['Claude 5 Hour'].used_fraction == 0.6
    assert snap.updated_at == new
    assert [w.short for w in snap.accounts[0].windows] == ['5h', '7d']


def test_secret_never_surfaces(tmp_path):
    db = tmp_path / 'agent.db'
    make_db(db, [('acc-f', 'Claude 5 Hour', NOW, 0.1, 'ok', None)])
    snap = usage.read_usage(db)
    markup, tip, _ = usage.format_bar(snap, NOW)
    assert 'SECRET-TOKEN' not in repr(snap)
    assert 'SECRET-TOKEN' not in markup
    assert 'SECRET-TOKEN' not in tip


def test_short_labels():
    assert usage.short_label('Claude 5 Hour') == '5h'
    assert usage.short_label('Claude 7 Day') == '7d'
    assert usage.short_label('Claude 7 Day (Fable)') == '7d Fable'


def test_format_bar_exhausted(tmp_path):
    db = tmp_path / 'agent.db'
    make_db(db, [('acc-f', 'Claude 5 Hour', NOW, 1.0, 'exhausted', NOW + timedelta(hours=2)),
                 ('acc-f', 'Claude 7 Day', NOW, 0.17, 'ok', None)])
    snap = usage.read_usage(db)
    markup, tip, sev = usage.format_bar(snap, NOW)
    assert '5h 100%' in markup and 'resets' in markup
    assert sev == 'error'
    assert 'frederik@' in markup and 'example.com' not in markup
    assert '7d 17%' in markup
    assert 'updated 0 min ago' in markup
    w7 = snap.accounts[0].windows[1]
    assert usage.severity(w7, NOW) == 'normal'


def test_warning_threshold():
    w = usage.UsageWindow('Claude 7 Day', '7d', 0.8, 'ok', None, NOW)
    assert usage.severity(w, NOW) == 'warning'


def test_reset_in_past(tmp_path):
    db = tmp_path / 'agent.db'
    make_db(db, [('acc-f', 'Claude 5 Hour', NOW, 1.0, 'exhausted', NOW - timedelta(hours=1))])
    markup, _, _ = usage.format_bar(usage.read_usage(db), NOW)
    assert 'reset' in markup and 'resets' not in markup


def test_email_escaped():
    w = usage.UsageWindow('Claude 5 Hour', '5h', 0.1, 'ok', None, NOW)
    snap = usage.UsageSnapshot((usage.AccountUsage('a<b&c@x.io', (w,)),), NOW, None)
    markup, _, _ = usage.format_bar(snap, NOW)
    assert 'a&lt;b&amp;c@' in markup


def test_missing_db(tmp_path):
    snap = usage.read_usage(tmp_path / 'nope.db')
    assert snap.error
    assert snap.accounts == ()
    markup, tip, _ = usage.format_bar(snap, NOW)
    assert markup == 'Claude usage: n/a'
    assert tip == snap.error


def test_locked_db(tmp_path):
    db = tmp_path / 'agent.db'
    make_db(db, [('acc-f', 'Claude 5 Hour', NOW, 0.1, 'ok', None)])
    holder = sqlite3.connect(db, isolation_level=None)
    try:
        holder.execute('PRAGMA locking_mode=EXCLUSIVE')
        holder.execute('BEGIN EXCLUSIVE')
        holder.execute("INSERT INTO usage_history (recorded_at, provider) VALUES (1, 'x')")
        start = time.monotonic()
        snap = usage.read_usage(db)
        assert time.monotonic() - start < 2
        assert snap.error is not None or snap.accounts
    finally:
        holder.close()


def test_no_side_files_created(tmp_path):
    db = tmp_path / 'agent.db'
    make_db(db, [('acc-f', 'Claude 5 Hour', NOW, 0.1, 'ok', None)])
    before = sorted(p.name for p in tmp_path.iterdir())
    assert 'agent.db-wal' not in before and 'agent.db-shm' not in before
    snap = usage.read_usage(db)
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert snap.accounts and snap.error is None
