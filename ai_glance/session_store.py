"""Session snapshot store: the layout of all Terminator windows plus the agent
(omp / Claude Code) session of each pane, kept on disk so a restart can rebuild them.

GTK-free and thread-safe: called from worker threads.
"""
import copy
import fcntl
import json
import os
import shlex
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .ompsessions import CLAUDE, CLAUDE_DIR, OMP, TERMINAL_SESSIONS, pane_agent

VERSION = 1
CARRY_FORWARD_S = 120

_EXECUTABLE = {OMP: 'omp', CLAUDE: 'claude'}


def state_dir() -> Path:
    base = os.environ.get('XDG_STATE_HOME')
    root = Path(base) if base else Path.home() / '.local' / 'state'
    return root / 'ai-glance'


def snapshot_path() -> Path:
    return state_dir() / 'session.json'


def lock_path() -> Path:
    return state_dir() / 'session.lock'


class SessionLock:
    """Non-blocking exclusive flock held (fd kept open) until ``release`` or process exit."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else lock_path()
        self._fd: Optional[int] = None

    def acquire(self) -> bool:
        if self._fd is not None:
            return True
        fd = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if fd is not None:
                os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)  # closing drops the flock
            self._fd = None


def agent_record(shell_pid: Optional[int], proc: Path = Path('/proc'),
                 sessions_dir: Path = TERMINAL_SESSIONS,
                 claude_dir: Path = CLAUDE_DIR) -> Optional[dict]:
    """{'agent', 'session', 'cwd'} of the agent in this pane, or None. Never raises."""
    try:
        hit = pane_agent(shell_pid, proc, sessions_dir, claude_dir)
        if hit is None:
            return None
        name, log, fallback_cwd, pid = hit
        if name == OMP:
            session = str(log)
        else:
            session = json.loads((claude_dir / 'sessions' / f'{pid}.json').read_text())['sessionId']
        try:
            cwd = os.readlink(proc / str(pid) / 'cwd')
        except OSError:
            cwd = fallback_cwd
        if not isinstance(session, str) or not session or not cwd:
            return None
        return {'agent': name, 'session': session, 'cwd': cwd}
    except Exception:  # noqa: BLE001 - contract: never raises
        return None


def clean_layout(layout: dict) -> dict:
    """Deep copy that create_layout can rebuild from:

    - no window ``title`` (create_layout would freeze it as force_title);
    - a window's ``last_active_term`` only if it names a Terminal of the layout (a stale one
      makes Terminator raise while focusing it);
    - ``maximised`` / ``fullscreen`` / ``last_active_window`` as 'True'/'False' strings
      (describe_layout stores booleans, Terminator compares with the string 'True')."""
    out = copy.deepcopy(layout)
    uuids = {str(item.get('uuid')) for item in out.values()
             if isinstance(item, dict) and item.get('type') == 'Terminal'}
    for item in out.values():
        if not isinstance(item, dict) or item.get('type') != 'Window':
            continue
        item.pop('title', None)
        if 'last_active_term' in item and str(item['last_active_term']) not in uuids:
            del item['last_active_term']
        for key in ('maximised', 'fullscreen', 'last_active_window'):
            if key in item:
                value = item[key]
                item[key] = 'True' if value is True or str(value) == 'True' else 'False'
    return out


def window_count(layout: dict) -> int:
    return sum(1 for item in layout.values()
               if isinstance(item, dict) and item.get('type') == 'Window')


def resume_command(record: dict, exists: Callable[[str], bool] = os.path.exists) -> Optional[str]:
    """Shell command (run by Terminator as ``$SHELL -c``) that reopens the agent session
    and leaves an interactive shell after it; None when it cannot be resumed."""
    try:
        exe = _EXECUTABLE.get(record['agent'])
        session = record['session']
    except (TypeError, KeyError):
        return None
    if exe is None or not isinstance(session, str) or not session:
        return None
    if record['agent'] == OMP and not exists(session):
        return None
    inner = f'{exe} --resume {shlex.quote(session)}; exec "$SHELL" -i'
    return f'exec "$SHELL" -ic {shlex.quote(inner)}'


def apply_agents(layout: dict, agents: dict, exists: Callable[[str], bool] = os.path.exists) -> dict:
    """Copy of ``layout`` where agent panes (matched by uuid) get the resume command and
    the agent's cwd as directory; directories that no longer exist become $HOME."""
    out = copy.deepcopy(layout)
    home = os.path.expanduser('~')
    for item in out.values():
        if not isinstance(item, dict) or item.get('type') != 'Terminal':
            continue
        record = agents.get(item.get('uuid'))
        if isinstance(record, dict):
            command = resume_command(record, exists)
            if command is not None:
                item['command'] = command
            if isinstance(record.get('cwd'), str) and record['cwd']:
                item['directory'] = record['cwd']
        directory = item.get('directory')
        if directory and not exists(directory):
            item['directory'] = home
    return out


def read_snapshot(path: Path) -> Optional[dict]:
    """The snapshot dict, or None when missing, unreadable, corrupt or of another version."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if (not isinstance(data, dict) or data.get('version') != VERSION
            or not isinstance(data.get('layout'), dict)
            or not isinstance(data.get('agents'), dict)):
        return None
    return data


def _content(data: bytes) -> Optional[dict]:
    try:
        obj = json.loads(data)
    except ValueError:
        return None
    if isinstance(obj, dict):
        obj.pop('saved_at', None)
    return obj


def write_snapshot(path: Path, layout: dict, agents: dict, previous: Optional[bytes]) -> Optional[bytes]:
    """Atomically write the snapshot; returns the written bytes, or None when skipped
    (no windows, or same content as ``previous``, the bytes of the last write)."""
    layout = clean_layout(layout)
    if window_count(layout) == 0:
        return None
    body = {'version': VERSION, 'layout': layout, 'agents': agents}
    if previous is not None and _content(previous) == json.loads(json.dumps(body)):
        return None
    data = json.dumps({'version': VERSION,
                       'saved_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                       'layout': layout, 'agents': agents},
                      indent=1, sort_keys=True).encode()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return data


def merge_agents(detected: dict, restored: dict, restored_at: float, now: float,
                 live_uuids: set) -> dict:
    """Detected records win; for CARRY_FORWARD_S after a restore, restored records of live
    panes whose agent is not (yet) detected are kept."""
    out = dict(detected)
    if now - restored_at < CARRY_FORWARD_S:
        for uuid, record in restored.items():
            if uuid in live_uuids and uuid not in out:
                out[uuid] = record
    return out
