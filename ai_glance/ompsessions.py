"""Map a terminal pane to the worktrees its omp session recently touched.

GTK-free and thread-safe: called from worker threads.
"""
import json
import os
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

TERMINAL_SESSIONS = Path.home() / '.omp/agent/terminal-sessions'

_HEAD_BYTES = 64 * 1024

_CACHE_MAX = 64
_cache: OrderedDict = OrderedDict()
_cache_lock = threading.Lock()


def pane_tty(shell_pid: Optional[int], proc: Path = Path('/proc')) -> Optional[str]:
    if shell_pid is None:
        return None
    try:
        return os.readlink(proc / str(shell_pid) / 'fd' / '0')
    except OSError:
        return None


def session_log_for_tty(tty: str, sessions_dir: Path = TERMINAL_SESSIONS) -> Optional[Path]:
    try:
        lines = (sessions_dir / ('pts-' + Path(tty).name)).read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    if len(lines) < 2 or not lines[1].strip():
        return None
    return Path(lines[1].strip())


def session_cwd_for_tty(tty: str, sessions_dir: Path = TERMINAL_SESSIONS) -> Optional[str]:
    """Line 1 of the pts-N file: the omp session's working directory."""
    try:
        lines = (sessions_dir / ('pts-' + Path(tty).name)).read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    return lines[0].strip() or None if lines else None


def omp_running_on_tty(tty: str, proc: Path = Path('/proc')) -> bool:
    try:
        entries = list(proc.iterdir())
    except OSError:
        return False
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if os.readlink(entry / 'fd' / '0') != tty:
                continue
            argv = (entry / 'cmdline').read_bytes().split(b'\0')
        except OSError:
            continue
        if any(os.path.basename(a) == b'omp' for a in argv if a):
            return True
    return False


def _parse_ts(value) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _line_paths(obj: dict, cwd: Optional[str]) -> list:
    """Raw path strings referenced by one JSONL record."""
    out = []
    if obj.get('type') == 'session':
        out.append(obj.get('cwd'))
    message = obj.get('message')
    if isinstance(message, dict):
        content = message.get('content')
        if isinstance(content, list):
            for item in content:
                args = item.get('arguments') if isinstance(item, dict) else None
                if isinstance(args, dict):
                    out += [args.get('path'), args.get('cwd')]
        details = message.get('details')
        if isinstance(details, dict):
            out += [details.get('cwd'), details.get('path')]
    paths = []
    for p in out:
        if not isinstance(p, str):
            continue
        for part in p.split(';'):  # omp grep/glob take ';'-separated lists
            part = part.strip()
            if not part:
                continue
            path = Path(part)
            if not path.is_absolute():
                if not cwd:
                    continue
                path = Path(cwd) / path
            paths.append(path)
    return paths


def _head_cwd(log: Path) -> Optional[str]:
    """cwd of the first ``session`` record within the first _HEAD_BYTES of the log."""
    read = 0
    with open(log, 'rb') as f:
        while read < _HEAD_BYTES:
            raw = f.readline(_HEAD_BYTES - read)
            if not raw:
                break
            read += len(raw)
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get('type') == 'session':
                cwd = obj.get('cwd')
                return cwd if isinstance(cwd, str) and cwd else None
    return None


def _parse_log(log: Path, size: int, tail_bytes: int, base_cwd: Optional[str] = None) -> list:
    offset = max(0, size - tail_bytes)
    cwd = base_cwd
    if offset > 0:
        # the session record sits near the top and the tail cut may have dropped it
        cwd = _head_cwd(log) or base_cwd
    with open(log, 'rb') as f:
        if offset > 0:
            f.seek(offset)
            f.readline()  # drop the partial first line
        data = f.read(tail_bytes)
    entries = []
    for raw in data.splitlines():
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get('type') == 'session' and isinstance(obj.get('cwd'), str):
            cwd = obj['cwd']
        when = _parse_ts(obj.get('timestamp'))
        if when is None:
            continue
        entries.extend((when, p) for p in _line_paths(obj, cwd))
    return entries


def _entries(log: Path, tail_bytes: int, base_cwd: Optional[str],
             errors: Optional[list]) -> list:
    try:
        st = log.stat()
    except OSError:
        return []
    key = (str(log), st.st_size, st.st_mtime_ns, tail_bytes, base_cwd)
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit
    try:
        entries = _parse_log(log, st.st_size, tail_bytes, base_cwd)
    except Exception as exc:
        if errors is not None:
            errors.append(exc)
        return []
    with _cache_lock:
        for k in [k for k in _cache if k[0] == key[0]]:
            del _cache[k]
        _cache[key] = entries
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return entries


def recent_paths(log: Path, now: datetime, window: timedelta = timedelta(minutes=15),
                 tail_bytes: int = 512 * 1024, base_cwd: Optional[str] = None,
                 errors: Optional[list] = None) -> set:
    """Paths referenced in the last ``window`` by the session log and its subagents.

    ``base_cwd`` resolves relative paths when a log has no session record; it
    is only a fallback (the log's own session record wins, subagents inherit
    the parent's cwd).
    """
    cutoff = now - window
    try:
        parent_cwd = _head_cwd(log) or base_cwd
    except OSError:
        parent_cwd = base_cwd
    logs = [(log, base_cwd)]
    try:
        for sub in sorted(log.with_suffix('').glob('*.jsonl')):
            try:
                mtime = datetime.fromtimestamp(sub.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if mtime >= cutoff:
                logs.append((sub, parent_cwd))
    except OSError:
        pass
    return {p for lg, base in logs for when, p in _entries(lg, tail_bytes, base, errors)
            if when >= cutoff}


def worktrees_in_use(paths: Iterable[Path], worktrees: Iterable[Path]) -> set:
    roots = []
    for w in worktrees:
        try:
            roots.append(Path(w).resolve())
        except (OSError, RuntimeError):
            continue
    used = set()
    for p in paths:
        try:
            rp = Path(p).resolve()
        except (OSError, RuntimeError):
            continue
        best = None
        for r in roots:
            if (rp == r or r in rp.parents) and (best is None or len(r.parts) > len(best.parts)):
                best = r
        if best is not None:
            used.add(best)
    return used


def pane_worktrees_in_use(shell_pid: Optional[int], worktrees: Iterable[Path], now: datetime,
                          proc: Path = Path('/proc'),
                          sessions_dir: Path = TERMINAL_SESSIONS,
                          errors: Optional[list] = None) -> set:
    """Worktrees touched recently by the omp session on this pane. Never raises.

    Exceptions are appended to ``errors`` (if given) so the caller can log them.
    """
    try:
        tty = pane_tty(shell_pid, proc)
        if tty is None or not omp_running_on_tty(tty, proc):
            return set()
        log = session_log_for_tty(tty, sessions_dir)
        if log is None:
            return set()
        paths = recent_paths(log, now, base_cwd=session_cwd_for_tty(tty, sessions_dir),
                             errors=errors)
        return worktrees_in_use(paths, worktrees)
    except Exception as exc:
        if errors is not None:
            errors.append(exc)
        return set()
