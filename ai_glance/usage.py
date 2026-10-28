"""Read AI-provider usage via omp (``omp usage --json``, falling back to omp's
agent.db history). GTK-free and thread-safe; agent.db is only ever read."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Literal

DEFAULT_DB = Path.home() / '.omp/agent/agent.db'
# Live usage: omp fetches the provider limits itself. None disables it (history only).
OMP_COMMAND: tuple[str, ...] | None = ('omp', 'usage', '--json', '--no-extensions')
LIVE_TIMEOUT_S = 20
LIVE_MIN_INTERVAL_S = 50  # all bars share one fetch per interval

WARNING_AT = 0.60
ERROR_AT = 0.80
BUSY_TIMEOUT_MS = 500

Severity = Literal['normal', 'warning', 'error']
_COLORS = {'normal': '#8ae234', 'warning': '#fce94f', 'error': '#ef2929'}
_RANK = {'normal': 0, 'warning': 1, 'error': 2}

_QUERY = """
SELECT c.provider, c.kind, c.email, c.org,
       u.label, u.used_fraction, u.status, u.resets_at, u.recorded_at
FROM (
    SELECT provider, credential_type AS kind,
           json_extract(data, '$.accountId') AS account_id,
           json_extract(data, '$.email') AS email,
           json_extract(data, '$.orgName') AS org
    FROM auth_credentials
    WHERE disabled_cause IS NULL AND provider NOT LIKE 'mcp_oauth:%'
) AS c
JOIN usage_history AS u ON u.provider = c.provider AND u.account_id = c.account_id
WHERE u.recorded_at = (
      SELECT MAX(u2.recorded_at) FROM usage_history AS u2
      WHERE u2.provider = u.provider AND u2.account_id = u.account_id
        AND u2.label = u.label
  )
GROUP BY c.provider, c.account_id, u.label
"""

_KIND_QUERY = """
SELECT provider, credential_type, json_extract(data, '$.accountId')
FROM auth_credentials
WHERE disabled_cause IS NULL AND provider NOT LIKE 'mcp_oauth:%'
"""

_PROVIDER_NAMES = {
    'anthropic': 'Anthropic', 'openai': 'OpenAI', 'openai-codex': 'OpenAI',
    'xai': 'xAI', 'cursor': 'Cursor', 'google': 'Google', 'gemini': 'Google',
}
_KIND_NAMES = {'oauth': 'subscription', 'api_key': 'API key', 'apikey': 'API key'}


def provider_name(provider: str) -> str:
    return _PROVIDER_NAMES.get(provider, provider.capitalize())


def kind_name(kind: str) -> str:
    return _KIND_NAMES.get(kind, kind)


@dataclass(frozen=True)
class UsageWindow:
    label: str
    short: str
    used_fraction: float
    status: str
    resets_at: datetime | None
    recorded_at: datetime


@dataclass(frozen=True)
class AccountUsage:
    email: str
    windows: tuple[UsageWindow, ...]
    provider: str = 'anthropic'
    kind: str = 'oauth'
    org: str | None = None

    @property
    def title(self) -> str:
        return f'{provider_name(self.provider)} {kind_name(self.kind)}'


@dataclass(frozen=True)
class UsageSnapshot:
    accounts: tuple[AccountUsage, ...]
    updated_at: datetime | None
    error: str | None


_PROVIDER_WORDS = {'claude', 'codex', 'grok', 'gpt', 'gemini', 'cursor', 'openai', 'anthropic', 'xai'}


def short_label(label: str) -> str:
    s = label.strip()
    first, _, rest = s.partition(' ')
    if first.lower() in _PROVIDER_WORDS and rest:
        s = rest
    elif first.isalpha() and rest[:1].isdigit():  # unknown provider word before a window size
        s = rest
    s = s.replace(' Hour', 'h').replace(' Day', 'd')
    parts = s.split(' ', 1)
    if len(parts) == 2 and parts[0][:-1].isdigit():
        rest = parts[1]
        if rest.startswith('(') and rest.endswith(')'):
            rest = rest[1:-1]
        return f'{parts[0]} {rest}'
    return s.replace('5 h', '5h').replace('7 d', '7d')


def _ms(value: int | float | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def _sort_key(w: UsageWindow) -> tuple[int, str]:
    if w.short == '5h':
        return (0, '')
    if w.short == '7d':
        return (1, '')
    return (2, w.label)


def _query(db_path: Path, mode: str, sql: str = _QUERY) -> list[tuple]:
    uri = f'file:{db_path}?{mode}'
    conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        conn.execute(f'PRAGMA busy_timeout={BUSY_TIMEOUT_MS}')
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _read_rows(db_path: Path, sql: str) -> list[tuple]:
    # A WAL database opened with mode=ro creates -wal/-shm side files. With
    # no -wal present nobody is writing, so read it immutably instead.
    wal = db_path.with_name(db_path.name + '-wal')
    try:
        if not wal.exists():
            raise sqlite3.OperationalError('no WAL; read immutably')
        return _query(db_path, 'mode=ro', sql)
    except sqlite3.Error:
        return _query(db_path, 'immutable=1', sql)


def read_usage(db_path: Path = DEFAULT_DB) -> UsageSnapshot:
    """Latest usage per enabled non-MCP account and window. Never raises."""
    try:
        db_path = Path(db_path)
        if not db_path.exists():
            return UsageSnapshot((), None, f'agent.db not found: {db_path}')
        rows = _read_rows(db_path, _QUERY)
        by_account: dict[tuple[str, str, str], tuple[str | None, list[UsageWindow]]] = {}
        for provider, kind, email, org, label, frac, status, resets, rec in rows:
            recorded = _ms(rec)
            if recorded is None or label is None:
                continue
            key = (provider, email or '', kind or '')
            by_account.setdefault(key, (org, []))[1].append(UsageWindow(
                label=label,
                short=short_label(label),
                used_fraction=float(frac or 0.0),
                status=status or 'ok',
                resets_at=_ms(resets),
                recorded_at=recorded,
            ))
        accounts = tuple(
            AccountUsage(e, tuple(sorted(ws, key=_sort_key)), p, k, org)
            for (p, e, k), (org, ws) in sorted(
                by_account.items(), key=lambda kv: (provider_name(kv[0][0]), kv[0][1], kv[0][2]))
        )
        times = [w.recorded_at for a in accounts for w in a.windows]
        return UsageSnapshot(accounts, max(times) if times else None, None)
    except Exception as exc:  # noqa: BLE001 - contract: never raises
        return UsageSnapshot((), None, f'{type(exc).__name__}: {exc}')


def parse_omp_usage(payload: dict, kinds: dict[tuple[str, str], str]) -> UsageSnapshot:
    """Turn ``omp usage --json`` output into a snapshot. ``kinds`` maps
    (provider, accountId) to the credential type; unknown accounts count as
    subscriptions (omp only reports usage limits for those)."""
    by_account: dict[tuple[str, str, str], tuple[str | None, list[UsageWindow]]] = {}
    for report in payload.get('reports') or ():
        provider = report.get('provider') or ''
        meta = report.get('metadata') or {}
        fetched = _ms(report.get('fetchedAt')) or _ms(payload.get('generatedAt'))
        if not provider or fetched is None:
            continue
        email = meta.get('email') or ''
        kind = kinds.get((provider, meta.get('accountId') or ''), 'oauth')
        windows = by_account.setdefault((provider, email, kind), (meta.get('orgName'), []))[1]
        for limit in report.get('limits') or ():
            label = limit.get('label')
            frac = (limit.get('amount') or {}).get('usedFraction')
            if not label or frac is None:
                continue
            windows.append(UsageWindow(
                label=label,
                short=short_label(label),
                used_fraction=float(frac),
                status=limit.get('status') or 'ok',
                resets_at=_ms((limit.get('window') or {}).get('resetsAt')),
                recorded_at=fetched,
            ))
    accounts = tuple(
        AccountUsage(e, tuple(sorted(ws, key=_sort_key)), p, k, org)
        for (p, e, k), (org, ws) in sorted(
            by_account.items(), key=lambda kv: (provider_name(kv[0][0]), kv[0][1], kv[0][2]))
        if ws
    )
    times = [w.recorded_at for a in accounts for w in a.windows]
    if not accounts:
        return UsageSnapshot((), None, 'omp usage reported no limits')
    return UsageSnapshot(accounts, max(times), None)


def _resolve_command(command: tuple[str, ...]) -> tuple[list[str], dict[str, str]]:
    """Find omp even when Terminator was started without ~/.bun/bin on PATH."""
    env = dict(os.environ)
    bun_bin = str(Path.home() / '.bun/bin')
    env['PATH'] = env.get('PATH', '') + os.pathsep + bun_bin
    exe = shutil.which(command[0], path=env['PATH']) or command[0]
    return [exe, *command[1:]], env


def _read_kinds(db_path: Path) -> dict[tuple[str, str], str]:
    try:
        if not db_path.exists():
            return {}
        return {(p, a): k for p, k, a in _read_rows(db_path, _KIND_QUERY) if p and a and k}
    except Exception:  # noqa: BLE001 - kinds are cosmetic
        return {}


def read_live_usage(db_path: Path = DEFAULT_DB,
                    command: tuple[str, ...] | None = OMP_COMMAND) -> UsageSnapshot:
    """Live limits from ``omp usage --json``; on any failure the latest
    agent.db history (``read_usage``). Never raises."""
    if not command:
        return read_usage(db_path)
    try:
        argv, env = _resolve_command(command)
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=LIVE_TIMEOUT_S,
                              env=env, stdin=subprocess.DEVNULL, check=True)
        snap = parse_omp_usage(json.loads(proc.stdout), _read_kinds(Path(db_path)))
        if snap.error is None:
            return snap
    except Exception:  # noqa: BLE001 - fall back to the recorded history
        pass
    return read_usage(db_path)


_live_lock = threading.Lock()
_live_cache: dict[tuple, tuple[float, UsageSnapshot]] = {}


def read_current_usage(db_path: Path = DEFAULT_DB,
                       command: tuple[str, ...] | None = OMP_COMMAND) -> UsageSnapshot:
    """``read_live_usage`` shared by every caller: at most one omp run per
    ``LIVE_MIN_INTERVAL_S`` for the same (db, command). Never raises."""
    key = (str(db_path), command)
    with _live_lock:
        hit = _live_cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < LIVE_MIN_INTERVAL_S:
            return hit[1]
        snap = read_live_usage(db_path, command)
        _live_cache.clear()
        _live_cache[key] = (time.monotonic(), snap)
        return snap


def percent(w: UsageWindow) -> int:
    """The percentage shown for ``w``; severity is decided on this same number."""
    return round(w.used_fraction * 100)


def severity(w: UsageWindow, now: datetime) -> Severity:
    pct = percent(w)
    if w.status != 'ok' or pct >= round(ERROR_AT * 100):
        return 'error'
    if pct >= round(WARNING_AT * 100):
        return 'warning'
    return 'normal'


def _reset_text(w: UsageWindow, now: datetime) -> str | None:
    """Terse reset hint, only for warning/error windows with a known reset time."""
    if severity(w, now) == 'normal' or w.resets_at is None:
        return None
    if w.resets_at <= now:
        return 'reset'
    local = w.resets_at.astimezone()
    fmt = '%H:%M' if local.date() == now.astimezone().date() else '%a %H:%M'
    return f'resets {local.strftime(fmt)}'


def _window_text(w: UsageWindow, now: datetime) -> str:
    text = f'{w.short} {percent(w)}%'
    if severity(w, now) == 'error':
        text += ' \u26d4'
    reset = _reset_text(w, now)
    if reset:
        text += ' ' + reset
    return text


def _window_detail(w: UsageWindow, now: datetime) -> str:
    """Tooltip line: usage plus the exact local reset time, whatever the severity."""
    text = f'{w.short} {percent(w)}%'
    if severity(w, now) == 'error':
        text += ' \u26d4'
    if w.resets_at is None:
        return text + ' \u2014 no reset time'
    when = w.resets_at.astimezone().strftime('%a %Y-%m-%d %H:%M')
    return text + (f' \u2014 reset {when}' if w.resets_at <= now else f' \u2014 resets {when}')


def _local_part(email: str) -> str:
    return email.split('@', 1)[0] + '@' if email else ''


def _account_tooltip(acc: AccountUsage, now: datetime) -> str:
    head = acc.title + (f' \u00b7 {acc.email}' if acc.email else '')
    if acc.org:
        head += f' \u00b7 {acc.org}'
    return head + '\n' + '\n'.join('  ' + _window_detail(w, now) for w in acc.windows)


def format_bar(snap: UsageSnapshot, now: datetime) -> tuple[str, str, Severity]:
    """Return (Pango markup, plain tooltip, worst severity)."""
    if snap.error is not None or not snap.accounts:
        msg = snap.error or 'no enabled accounts with usage data'
        return 'Claude usage: n/a', msg, 'error'
    worst: Severity = 'normal'
    markup_accounts: list[str] = []
    plain_accounts: list[str] = []
    for acc in snap.accounts:
        mparts: list[str] = []
        for w in acc.windows:
            sev = severity(w, now)
            if _RANK[sev] > _RANK[worst]:
                worst = sev
            text = _window_text(w, now)
            mparts.append(f'<span foreground="{_COLORS[sev]}">{escape(text)}</span>')
        who = _local_part(acc.email)
        head = acc.title + (f' \u00b7 {who}' if who else '')
        markup_accounts.append(f'{escape(head)}  ' + ' \u00b7 '.join(mparts))
        plain_accounts.append(_account_tooltip(acc, now))
    suffix = ''
    if snap.updated_at is not None:
        mins = max(0, int((now - snap.updated_at).total_seconds() // 60))
        suffix = f'  (updated {mins} min ago)'
    sep = '  \u2502  '
    markup = sep.join(markup_accounts) + escape(suffix)
    tooltip = '\n'.join(plain_accounts) + (('\n' + suffix.strip()) if suffix else '')
    return markup, tooltip, worst


# --- graphical bar model -------------------------------------------------------

# provider id -> (badge background, badge foreground)
_BADGE_COLORS = {
    'Anthropic': ('#d97757', '#ffffff'),
    'OpenAI': ('#10a37f', '#ffffff'),
    'xAI': ('#333333', '#ffffff'),
}
_BADGE_DEFAULT = ('#555555', '#ffffff')


def badge_colors(provider: str) -> tuple[str, str]:
    return _BADGE_COLORS.get(provider_name(provider), _BADGE_DEFAULT)


def severity_color(sev: Severity) -> str:
    return _COLORS[sev]


@dataclass(frozen=True)
class BarWindow:
    short: str
    fraction: float  # clamped to 0..1, for the level bar
    percent: int
    severity: Severity
    color: str
    reset: str | None


@dataclass(frozen=True)
class BarAccount:
    provider_name: str
    badge_bg: str
    badge_fg: str
    kind: str
    who: str
    tooltip: str
    windows: tuple[BarWindow, ...]


@dataclass(frozen=True)
class BarModel:
    """Everything the graphical bar draws; ``error`` set means show it as n/a."""
    accounts: tuple[BarAccount, ...]
    updated: str | None
    tooltip: str
    worst: Severity
    error: str | None


def bar_segments(snap: UsageSnapshot, now: datetime) -> BarModel:
    if snap.error is not None or not snap.accounts:
        msg = snap.error or 'no enabled accounts with usage data'
        return BarModel((), None, msg, 'error', msg)
    worst: Severity = 'normal'
    accounts = []
    for acc in snap.accounts:
        windows = []
        for w in acc.windows:
            sev = severity(w, now)
            if _RANK[sev] > _RANK[worst]:
                worst = sev
            windows.append(BarWindow(
                w.short, min(1.0, max(0.0, w.used_fraction)), percent(w),
                sev, _COLORS[sev], _reset_text(w, now)))
        bg, fg = badge_colors(acc.provider)
        accounts.append(BarAccount(
            provider_name(acc.provider), bg, fg, kind_name(acc.kind), _local_part(acc.email),
            _account_tooltip(acc, now), tuple(windows)))
    updated = None
    if snap.updated_at is not None:
        mins = max(0, int((now - snap.updated_at).total_seconds() // 60))
        updated = 'updated just now' if mins == 0 else f'updated {mins} min ago'
    tooltip = format_bar(snap, now)[1]
    return BarModel(tuple(accounts), updated, tooltip, worst, None)
