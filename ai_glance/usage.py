"""Read AI-provider usage from omp's agent.db (read-only, GTK-free, thread-safe)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Literal

DEFAULT_DB = Path.home() / '.omp/agent/agent.db'

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


def short_label(label: str) -> str:
    s = label.strip()
    first, _, rest = s.partition(' ')
    if first.isalpha() and rest[:1].isdigit():  # leading provider word: Claude, Codex, Grok
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


def _query(db_path: Path, mode: str) -> list[tuple]:
    uri = f'file:{db_path}?{mode}'
    conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        conn.execute(f'PRAGMA busy_timeout={BUSY_TIMEOUT_MS}')
        return conn.execute(_QUERY).fetchall()
    finally:
        conn.close()


def read_usage(db_path: Path = DEFAULT_DB) -> UsageSnapshot:
    """Latest usage per enabled non-MCP account and window. Never raises."""
    try:
        db_path = Path(db_path)
        if not db_path.exists():
            return UsageSnapshot((), None, f'agent.db not found: {db_path}')
        # A WAL database opened with mode=ro creates -wal/-shm side files. With
        # no -wal present nobody is writing, so read it immutably instead.
        wal = db_path.with_name(db_path.name + '-wal')
        try:
            if not wal.exists():
                raise sqlite3.OperationalError('no WAL; read immutably')
            rows = _query(db_path, 'mode=ro')
        except sqlite3.Error:
            rows = _query(db_path, 'immutable=1')
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


def severity(w: UsageWindow, now: datetime) -> Severity:
    if w.status != 'ok' or w.used_fraction >= ERROR_AT:
        return 'error'
    if w.used_fraction >= WARNING_AT:
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
    text = f'{w.short} {round(w.used_fraction * 100)}%'
    if severity(w, now) == 'error':
        text += ' \u26d4'
    reset = _reset_text(w, now)
    if reset:
        text += ' ' + reset
    return text


def _window_detail(w: UsageWindow, now: datetime) -> str:
    """Tooltip line: usage plus the exact local reset time, whatever the severity."""
    text = f'{w.short} {round(w.used_fraction * 100)}%'
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
                w.short, min(1.0, max(0.0, w.used_fraction)), round(w.used_fraction * 100),
                sev, _COLORS[sev], _reset_text(w, now)))
        bg, fg = badge_colors(acc.provider)
        accounts.append(BarAccount(
            provider_name(acc.provider), bg, fg, kind_name(acc.kind), _local_part(acc.email),
            _account_tooltip(acc, now), tuple(windows)))
    updated = None
    if snap.updated_at is not None:
        mins = max(0, int((now - snap.updated_at).total_seconds() // 60))
        updated = f'updated {mins} min ago'
    tooltip = format_bar(snap, now)[1]
    return BarModel(tuple(accounts), updated, tooltip, worst, None)
