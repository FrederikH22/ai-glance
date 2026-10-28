import json
import re
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
    for acc_id, email, cause, *extra in accounts:
        provider, ctype, org = (extra + [None] * 3)[:3] if extra else (None, None, None)
        data = {'accountId': acc_id, 'email': email, 'access': 'SECRET-TOKEN',
                'refresh': 'SECRET-REFRESH', 'orgSecret': 'SECRET-ORG'}
        if org:
            data['orgName'] = org
        conn.execute(
            "INSERT INTO auth_credentials (provider, credential_type, data, disabled_cause)"
            " VALUES (?,?,?,?)",
            (provider or 'anthropic', ctype or 'oauth', json.dumps(data), cause))
    for acc_id, label, rec, frac, status, resets, *prov in rows:
        conn.execute(
            "INSERT INTO usage_history (recorded_at, provider, account_id, label, used_fraction,"
            " status, resets_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ms(rec), prov[0] if prov else 'anthropic', acc_id, label, frac, status,
             resets and ms(resets)))
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
    assert usage.short_label('Codex 5 Hour') == '5h'
    assert usage.short_label('Grok 7 Day') == '7d'


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


def _plain(markup):
    return re.sub(r'<[^>]+>', '', markup)


def _sev_window(frac, status='ok'):
    return usage.UsageWindow('Claude 7 Day', '7d', frac, status, None, NOW)


def test_severity_thresholds_and_colours():
    cases = [(0.59, 'ok', 'normal', '#8ae234'), (0.60, 'ok', 'warning', '#fce94f'),
             (0.79, 'ok', 'warning', '#fce94f'), (0.80, 'ok', 'error', '#ef2929'),
             (0.10, 'exhausted', 'error', '#ef2929')]
    for frac, status, expected, colour in cases:
        w = _sev_window(frac, status)
        assert usage.severity(w, NOW) == expected, (frac, status)
        snap = usage.UsageSnapshot((usage.AccountUsage('a@x.io', (w,)),), NOW, None)
        markup, _, worst = usage.format_bar(snap, NOW)
        assert f'foreground="{colour}"' in markup, (frac, status)
        assert worst == expected


def test_provider_and_kind_in_markup_and_tooltip(tmp_path):
    db = tmp_path / 'agent.db'
    make_db(db, [('acc-f', 'Claude 5 Hour', NOW, 0.09, 'ok', None)],
            accounts=[('acc-f', 'frederik@example.com', None, 'anthropic', 'oauth', 'Acme GmbH')])
    markup, tip, _ = usage.format_bar(usage.read_usage(db), NOW)
    assert _plain(markup).startswith('Anthropic subscription \u00b7 frederik@  5h 9%')
    assert tip.splitlines()[0] == \
        'Anthropic subscription \u00b7 frederik@example.com \u00b7 Acme GmbH'
    for secret in ('SECRET-TOKEN', 'SECRET-REFRESH', 'SECRET-ORG'):
        assert secret not in markup and secret not in tip


def test_other_providers_and_mcp_ignored(tmp_path):
    db = tmp_path / 'agent.db'
    t = NOW - timedelta(minutes=1)
    make_db(db, [('acc-f', 'Claude 5 Hour', t, 0.1, 'ok', None),
                 ('acc-o', 'Codex 5 Hour', t, 0.2, 'ok', None, 'openai'),
                 ('acc-m', 'Claude 5 Hour', t, 0.3, 'ok', None, 'mcp_oauth:srv'),
                 ('acc-n', 'Claude 5 Hour', t, 0.3, 'ok', None, 'xai')],
            accounts=[('acc-f', 'frederik@example.com', None),
                      ('acc-o', 'oli@example.com', None, 'openai', 'api_key'),
                      ('acc-m', 'mcp@example.com', None, 'mcp_oauth:srv', 'oauth'),
                      ('acc-x', 'norows@example.com', None, 'google', 'oauth')])
    snap = usage.read_usage(db)
    assert [(a.provider, a.kind) for a in snap.accounts] == [
        ('anthropic', 'oauth'), ('openai', 'api_key')]
    markup, tip, _ = usage.format_bar(snap, NOW)
    assert 'OpenAI API key \u00b7 oli@  5h 20%' in _plain(markup)
    assert 'mcp' not in markup and 'norows' not in markup and 'xAI' not in markup
    assert 'OpenAI API key \u00b7 oli@example.com' in tip


def test_provider_and_kind_names():
    assert usage.provider_name('openai-codex') == 'OpenAI'
    assert usage.provider_name('xai') == 'xAI'
    assert usage.provider_name('gemini') == 'Google'
    assert usage.provider_name('foo') == 'Foo'
    assert usage.kind_name('apikey') == 'API key'
    assert usage.kind_name('weird') == 'weird'


def test_reset_in_past(tmp_path):
    db = tmp_path / 'agent.db'
    make_db(db, [('acc-f', 'Claude 5 Hour', NOW, 1.0, 'exhausted', NOW - timedelta(hours=1))])
    markup, _, _ = usage.format_bar(usage.read_usage(db), NOW)
    assert 'reset' in markup and 'resets' not in markup


def test_reset_three_days_out_shows_weekday_and_date(tmp_path):
    db = tmp_path / 'agent.db'
    reset = NOW + timedelta(days=3)
    make_db(db, [('acc-f', 'Claude 7 Day', NOW, 0.8, 'ok', reset),
                 ('acc-f', 'Claude 5 Hour', NOW, 0.1, 'ok', NOW + timedelta(hours=1))])
    markup, tip, _ = usage.format_bar(usage.read_usage(db), NOW)
    local = reset.astimezone()
    assert f'resets {local.strftime("%a %H:%M")}' in markup
    assert tip.splitlines()[0] == 'Anthropic subscription \u00b7 frederik@example.com'
    assert f'resets {local.strftime("%a %Y-%m-%d %H:%M")}' in tip
    # below the warning threshold the bar stays terse but the tooltip is exact
    assert '5h 10% resets' not in markup
    assert f'5h 10% \u2014 resets {(NOW + timedelta(hours=1)).astimezone().strftime("%a %Y-%m-%d %H:%M")}' in tip


def test_reset_today_has_no_weekday_in_bar(tmp_path):
    db = tmp_path / 'agent.db'
    now = datetime(2026, 10, 28, 8, 0, tzinfo=timezone.utc).astimezone().replace(hour=8, minute=0)
    reset = now.replace(hour=9, minute=30)
    make_db(db, [('acc-f', 'Claude 5 Hour', now, 1.0, 'exhausted', reset)])
    markup, _, _ = usage.format_bar(usage.read_usage(db), now)
    assert 'resets 09:30' in markup



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


def test_bar_segments_model(tmp_path):
    db = tmp_path / 'agent.db'
    t = NOW - timedelta(minutes=4)
    make_db(db, [('acc-f', 'Claude 5 Hour', t, 0.85, 'ok', NOW + timedelta(hours=2)),
                 ('acc-f', 'Claude 7 Day', t, 0.30, 'ok', NOW + timedelta(days=2)),
                 ('acc-o', 'Codex 5 Hour', t, 0.65, 'ok', None, 'openai'),
                 ('acc-x', 'Grok 5 Hour', t, 0.10, 'ok', None, 'xai')],
            accounts=[('acc-f', 'frederik@example.com', None),
                      ('acc-o', 'oli@example.com', None, 'openai', 'api_key'),
                      ('acc-x', 'x@example.com', None, 'xai', 'oauth', 'XOrg')])
    model = usage.bar_segments(usage.read_usage(db), NOW)
    assert model.error is None and model.worst == 'error'
    assert model.updated == 'updated 4 min ago'
    names = [(a.provider_name, a.kind, a.who) for a in model.accounts]
    assert names == [('Anthropic', 'subscription', 'frederik@'), ('OpenAI', 'API key', 'oli@'),
                     ('xAI', 'subscription', 'x@')]
    anth, oai, xai = model.accounts
    assert (anth.badge_bg, anth.badge_fg) == ('#d97757', '#ffffff')
    assert oai.badge_bg == '#10a37f'
    assert (xai.badge_bg, xai.badge_fg) == ('#333333', '#ffffff')
    w5, w7 = anth.windows
    assert (w5.short, w5.percent, w5.severity, w5.color) == ('5h', 85, 'error', '#ef2929')
    assert w5.fraction == 0.85 and w5.reset and w5.reset.startswith('resets')
    assert (w7.severity, w7.color, w7.reset) == ('normal', '#8ae234', None)
    assert oai.windows[0].severity == 'warning' and oai.windows[0].color == '#fce94f'
    assert 'Anthropic subscription' in model.tooltip and 'XOrg' in anth.tooltip + xai.tooltip
    assert 'SECRET' not in repr(model)


def test_bar_segments_fraction_clamped_and_error():
    w = usage.UsageWindow('Claude 5 Hour', '5h', 1.4, 'exhausted', None, NOW)
    snap = usage.UsageSnapshot((usage.AccountUsage('a@x.io', (w,)),), NOW, None)
    assert usage.bar_segments(snap, NOW).accounts[0].windows[0].fraction == 1.0
    bad = usage.bar_segments(usage.UsageSnapshot((), None, 'agent.db not found'), NOW)
    assert bad.error == 'agent.db not found' and bad.accounts == () and bad.tooltip == bad.error


def test_short_label_strips_known_provider_words():
    assert usage.short_label('Claude Sonnet 7 Day') == 'Sonnet 7d'
    assert usage.short_label('Codex 5 Hour') == '5h'
    assert usage.short_label('GPT 7 Day') == '7d'
    assert usage.short_label('Foo 5 Hour') == '5h'  # generic fallback


def test_severity_uses_displayed_percent():
    for frac, sev, pct in [(0.595, 'warning', '60%'), (0.5949, 'normal', '59%'),
                           (0.795, 'error', '80%'), (0.7949, 'warning', '79%')]:
        w = _sev_window(frac)
        assert usage.severity(w, NOW) == sev, frac
        snap = usage.UsageSnapshot((usage.AccountUsage('a@x.io', (w,)),), NOW, None)
        markup, _, _ = usage.format_bar(snap, NOW)
        assert pct in _plain(markup)
        assert usage.bar_segments(snap, NOW).accounts[0].windows[0].severity == sev


def omp_payload(frac=0.37, fetched=NOW, email='frederik@example.com', account='acc-f'):
    return {
        'generatedAt': ms(fetched),
        'reports': [{
            'provider': 'anthropic', 'fetchedAt': ms(fetched),
            'limits': [
                {'label': 'Claude 5 Hour', 'status': 'ok',
                 'window': {'resetsAt': ms(fetched + timedelta(hours=2))},
                 'amount': {'usedFraction': frac}},
                {'label': 'Claude 7 Day', 'status': 'ok',
                 'window': {'resetsAt': ms(fetched + timedelta(days=5))},
                 'amount': {'usedFraction': 0.23}},
            ],
            'metadata': {'accountId': account, 'email': email, 'orgName': 'Acme GmbH'},
        }],
    }


def fake_omp(tmp_path, payload=None, exit_code=0, stdout=None):
    """A command that prints ``payload`` as JSON and counts its runs."""
    counter = tmp_path / 'runs'
    out = stdout if stdout is not None else json.dumps(payload)
    script = tmp_path / 'omp.py'
    script.write_text(
        'import pathlib, sys\n'
        f'p = pathlib.Path({str(counter)!r}); p.write_text(str(int(p.read_text() or 0) + 1)'
        ' if p.exists() else "1")\n'
        f'sys.stdout.write({out!r})\n'
        f'sys.exit({exit_code})\n')
    return ('python3', str(script)), counter


def test_parse_omp_usage_uses_live_values_and_credential_kind():
    snap = usage.parse_omp_usage(omp_payload(), {('anthropic', 'acc-f'): 'api_key'})
    assert snap.error is None and snap.updated_at == NOW
    (acc,) = snap.accounts
    assert (acc.email, acc.provider, acc.kind, acc.org) == (
        'frederik@example.com', 'anthropic', 'api_key', 'Acme GmbH')
    assert [(w.short, w.used_fraction) for w in acc.windows] == [('5h', 0.37), ('7d', 0.23)]
    assert acc.windows[0].resets_at == NOW + timedelta(hours=2)


def test_parse_omp_usage_without_limits_is_an_error():
    assert usage.parse_omp_usage({'reports': []}, {}).error


def test_live_usage_preferred_over_history(tmp_path):
    db = tmp_path / 'agent.db'
    make_db(db, [('acc-f', 'Claude 5 Hour', NOW - timedelta(minutes=50), 0.10, 'ok', None)])
    cmd, _ = fake_omp(tmp_path, omp_payload(frac=0.42))
    snap = usage.read_live_usage(db, cmd)
    assert snap.accounts[0].windows[0].used_fraction == 0.42
    assert snap.accounts[0].kind == 'oauth'  # from auth_credentials


def test_live_usage_falls_back_to_history(tmp_path):
    db = tmp_path / 'agent.db'
    make_db(db, [('acc-f', 'Claude 5 Hour', NOW, 0.10, 'ok', None)])
    for cmd, _ in (fake_omp(tmp_path, exit_code=1, stdout='boom'),
                   fake_omp(tmp_path, stdout='not json'),
                   (('/nonexistent/omp',), None)):
        snap = usage.read_live_usage(db, cmd)
        assert snap.error is None and snap.accounts[0].windows[0].used_fraction == 0.10
    assert usage.read_live_usage(db, None).accounts[0].windows[0].used_fraction == 0.10


def test_current_usage_shares_one_run_per_interval(tmp_path, monkeypatch):
    db = tmp_path / 'agent.db'
    make_db(db, [])
    cmd, counter = fake_omp(tmp_path, omp_payload())
    clock = [1000.0]
    monkeypatch.setattr(usage.time, 'monotonic', lambda: clock[0])
    usage._live_cache.clear()
    first = usage.read_current_usage(db, cmd)
    assert usage.read_current_usage(db, cmd) is first
    assert counter.read_text() == '1'
    clock[0] += usage.LIVE_MIN_INTERVAL_S
    usage.read_current_usage(db, cmd)
    assert counter.read_text() == '2'


def test_bar_says_just_now_for_fresh_data():
    snap = usage.parse_omp_usage(omp_payload(fetched=NOW), {})
    assert usage.bar_segments(snap, NOW + timedelta(seconds=20)).updated == 'updated just now'
