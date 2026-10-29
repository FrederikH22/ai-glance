"""Map a terminal pane to its omp session: worktrees it recently touched and
whether it is working or waiting for the user.

GTK-free and thread-safe: called from worker threads.
"""
import json
import os
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

TERMINAL_SESSIONS = Path.home() / '.omp/agent/terminal-sessions'

_HEAD_BYTES = 64 * 1024

_CACHE_MAX = 256  # session logs + subagent logs of every agent pane
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


def _parse_ts(value) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _line_paths(obj: dict, cwd: Optional[str]) -> list:
    """Raw path strings referenced by one JSONL record (omp or Claude Code)."""
    out = []
    if isinstance(obj.get('cwd'), str):  # omp session record; every Claude Code record
        out.append(obj['cwd'])
    message = obj.get('message')
    if isinstance(message, dict):
        content = message.get('content')
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                args = item.get('arguments')  # omp tool call
                if isinstance(args, dict):
                    out += [args.get('path'), args.get('cwd')]
                tool_input = item.get('input')  # Claude Code tool_use
                if isinstance(tool_input, dict):
                    out += [tool_input.get('file_path'), tool_input.get('path'),
                            tool_input.get('notebook_path')]
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
        if isinstance(obj.get('cwd'), str):
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


def _subagent_logs(log: Path) -> list:
    """Subagent logs next to a session log: omp ``<stem>/*.jsonl``, Claude Code
    ``<stem>/subagents/*.jsonl``."""
    base = log.with_suffix('')
    try:
        return sorted(list(base.glob('*.jsonl')) + list(base.glob('subagents/*.jsonl')))
    except OSError:
        return []


def working_paths(log: Path, now: datetime, window: timedelta = timedelta(minutes=15),
                  tail_bytes: int = 512 * 1024, base_cwd: Optional[str] = None,
                  errors: Optional[list] = None) -> set:
    """Paths the session was working with: those referenced in the ``window`` before
    its newest path reference (at or before ``now``), across the session log and all
    its subagent logs. Anchoring on the session's own activity (not on ``now``) keeps
    the result while the session sits idle waiting for input or was just resumed.

    ``base_cwd`` resolves relative paths when a log has no session record; it is only
    a fallback (the log's own records win, subagents inherit the parent's cwd)."""
    try:
        parent_cwd = _head_cwd(log) or base_cwd
    except OSError:
        parent_cwd = base_cwd
    entries = list(_entries(log, tail_bytes, base_cwd, errors))
    for sub in _subagent_logs(log):
        entries += _entries(sub, tail_bytes, parent_cwd, errors)
    stamps = [when for when, _p in entries if when <= now]
    if not stamps:
        return set()
    cutoff = max(stamps) - window
    return {p for when, p in entries if cutoff <= when <= now}


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


CLAUDE_DIR = Path.home() / '.claude'
OMP, CLAUDE = 'omp', 'Claude Code'


def _agent_pid_on_tty(tty: str, proc: Path) -> Optional[tuple]:
    """(agent name, pid) of the coding agent whose stdin is ``tty``; omp wins."""
    found = {}
    try:
        entries = list(proc.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if os.readlink(entry / 'fd' / '0') != tty:
                continue
            argv = [a for a in (entry / 'cmdline').read_bytes().split(b'\0') if a]
        except OSError:
            continue
        names = {os.path.basename(a) for a in argv}
        if b'omp' in names:
            found.setdefault(OMP, int(entry.name))
        elif argv and os.path.basename(argv[0]) == b'claude':
            found.setdefault(CLAUDE, int(entry.name))
    for name in (OMP, CLAUDE):
        if name in found:
            return name, found[name]
    return None


def claude_session_log(pid: int, claude_dir: Path = CLAUDE_DIR) -> Optional[Path]:
    """Session log of the Claude Code process ``pid``: ``sessions/<pid>.json`` names
    the session id, ``projects/*/<id>.jsonl`` holds it (newest if several)."""
    try:
        sid = json.loads((claude_dir / 'sessions' / f'{pid}.json').read_text()).get('sessionId')
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(sid, str) or not sid or '/' in sid:
        return None
    logs = []
    for f in (claude_dir / 'projects').glob(f'*/{sid}.jsonl'):
        try:
            logs.append((f.stat().st_mtime, f))
        except OSError:
            continue
    return max(logs)[1] if logs else None


def pane_agent(shell_pid: Optional[int], proc: Path = Path('/proc'),
               sessions_dir: Path = TERMINAL_SESSIONS,
               claude_dir: Path = CLAUDE_DIR) -> Optional[tuple]:
    """(agent name, session log, fallback cwd) of the agent running in this pane,
    or None. Never raises."""
    try:
        tty = pane_tty(shell_pid, proc)
        hit = _agent_pid_on_tty(tty, proc) if tty else None
        if hit is None:
            return None
        name, pid = hit
        if name == OMP:
            log, cwd = session_log_for_tty(tty, sessions_dir), session_cwd_for_tty(tty, sessions_dir)
        else:
            log, cwd = claude_session_log(pid, claude_dir), None
        return (name, log, cwd) if log is not None else None
    except Exception:  # noqa: BLE001 - contract: never raises
        return None


def pane_agent_paths(shell_pid: Optional[int], now: datetime,
                     proc: Path = Path('/proc'),
                     sessions_dir: Path = TERMINAL_SESSIONS,
                     errors: Optional[list] = None,
                     claude_dir: Path = CLAUDE_DIR) -> tuple:
    """(agent name or None, paths it is working with) for the agent (omp or Claude Code)
    on this pane; see ``working_paths``. Never raises.

    Exceptions are appended to ``errors`` (if given) so the caller can log them.
    """
    try:
        agent = pane_agent(shell_pid, proc, sessions_dir, claude_dir)
        if agent is None:
            return None, set()
        name, log, cwd = agent
        return name, working_paths(log, now, base_cwd=cwd, errors=errors)
    except Exception as exc:
        if errors is not None:
            errors.append(exc)
        return None, set()


# --- session state -------------------------------------------------------------

WORKING = 'working'  # agent turn running, a tool in flight, or subagents still writing
NEEDS_INPUT = 'input'  # turn ended (or an `ask` is open): omp waits for the user
STATE_TAIL_BYTES = 64 * 1024
SUBAGENT_ACTIVE_S = 20
_SKIP_TYPES = {'title', 'title_change', 'credential_pin', 'model_change',
               'thinking_level_change', 'model_usage'}
_SKIP_CUSTOM = {'todo_hud_state', 'system-prompt-digest', 'user_todo_edit'}


def _last_event(log: Path) -> Optional[dict]:
    """Newest log line that says something about the turn (bookkeeping skipped)."""
    with open(log, 'rb') as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - STATE_TAIL_BYTES))
        lines = f.read().split(b'\n')
    if size > STATE_TAIL_BYTES:
        lines = lines[1:]  # first line may be cut
    for raw in reversed(lines):
        try:
            event = json.loads(raw)
        except ValueError:
            continue  # blank or half-written line
        if not isinstance(event, dict) or event.get('type') in _SKIP_TYPES:
            continue
        if event.get('type') == 'custom' and event.get('customType') in _SKIP_CUSTOM:
            continue
        return event
    return None


def _subagents_active(log: Path, now: float) -> bool:
    try:
        return any(now - p.stat().st_mtime < SUBAGENT_ACTIVE_S
                   for p in log.with_suffix('').glob('*.jsonl'))
    except OSError:
        return False


def session_state(log: Path, now: Optional[float] = None) -> Optional[str]:
    """WORKING, NEEDS_INPUT, or None (no events / session exited)."""
    now = time.time() if now is None else now
    event = _last_event(log)
    if event is None:
        return None
    kind = event.get('type')
    if kind == 'custom':
        custom = event.get('customType')
        if custom == 'session_exit':
            return None
        if custom == 'tool_execution_start':
            tool = (event.get('data') or {}).get('toolName')
            return NEEDS_INPUT if tool == 'ask' else WORKING
    if kind == 'message':
        msg = event.get('message') or {}
        if msg.get('role') == 'assistant':
            if msg.get('stopReason') == 'toolUse':
                calls = [c.get('name') for c in msg.get('content') or []
                         if isinstance(c, dict) and c.get('type') == 'toolCall']
                return NEEDS_INPUT if 'ask' in calls else WORKING
            # Turn over (stop/error/aborted): background subagents may still run.
            return WORKING if _subagents_active(log, now) else NEEDS_INPUT
    return WORKING  # user message, tool result, delivered job result: a turn is running


_CLAUDE_STATES = {'busy': WORKING, 'waiting': NEEDS_INPUT, 'idle': NEEDS_INPUT}


def claude_state(pid: int, claude_dir: Path = CLAUDE_DIR) -> Optional[str]:
    """State of the Claude Code process ``pid`` from ``sessions/<pid>.json``: ``busy``
    (a turn or background agents running) is WORKING; ``waiting`` (permission prompt
    and the like) and ``idle`` (turn over) are NEEDS_INPUT."""
    try:
        status = json.loads((claude_dir / 'sessions' / f'{pid}.json').read_text()).get('status')
    except (OSError, ValueError, AttributeError):
        return None
    return _CLAUDE_STATES.get(status)


def pane_agent_state(shell_pid: Optional[int], proc: Path = Path('/proc'),
                     sessions_dir: Path = TERMINAL_SESSIONS,
                     now: Optional[float] = None,
                     claude_dir: Path = CLAUDE_DIR) -> Optional[str]:
    """State of the agent (omp or Claude Code) running in this pane, or None (no live
    agent). Never raises."""
    try:
        tty = pane_tty(shell_pid, proc)
        hit = _agent_pid_on_tty(tty, proc) if tty else None
        if hit is None:
            return None
        name, pid = hit
        if name == CLAUDE:
            return claude_state(pid, claude_dir)
        log = session_log_for_tty(tty, sessions_dir)
        if log is None or not log.exists():
            return None
        return session_state(log, now)
    except Exception:  # noqa: BLE001 - contract: never raises
        return None
