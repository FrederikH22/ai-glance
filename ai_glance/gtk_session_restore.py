"""Reopen the windows, tabs, splits and agent sessions of the last run, and keep a
snapshot of the current ones on disk while Terminator runs.

Startup (a ``GLib.idle_add`` scheduled at plugin load, i.e. after Terminator's own
``create_layout('default')``): whether this is a plain start is decided at plugin load,
before the startup pane clears -e/-x/--working-directory; a worker thread then takes the
session lock (plain starts only) and reads the snapshot; the main thread then rebuilds the layout with ``Terminator.create_layout`` from
a private in-memory layout and closes the untouched startup window.

Saving (only after that decision, only in the lock holder): every ``SAVE_SECONDS`` the
main thread reads the layout, the pane pids and folder URIs, a worker thread resolves the
folders, detects the agents and writes the snapshot atomically. A save is skipped while a
pane is zoomed (the layout would list only that pane).
"""
import json
import os
import threading
import time

from terminatorlib.util import dbg

from . import session_store

SAVE_SECONDS = 10
LAYOUT_NAME = '__ai_glance_restore__'
_PATCH_ATTR = '_ai_glance_session_restore_installed'

_RESTORED, _NO_SNAPSHOT, _SKIPPED, _FAILED, _NOT_OWNER = (
    'restored', 'no-snapshot', 'skipped', 'failed', 'not-owner')

_installed = False
_state = None            # one of the strings above once the startup decision is made
_saving = False          # True while the periodic save timer runs
_lock = session_store.SessionLock()
_restored_agents = {}    # uuid -> agent record that was restored
_restored_at = 0.0
_in_flight = False
_plain = False           # decided in install(), before any child is started
_last_bytes = None


def state():
    """Startup decision: 'restored' | 'no-snapshot' | 'skipped' | 'failed' | 'not-owner',
    or None while it is still pending."""
    return _state


def saving():
    """True while the periodic snapshot timer runs (only after a successful decision)."""
    return _saving


def install():
    """Schedule the startup decision once per process (idempotent)."""
    global _installed, _plain
    if _installed:
        return
    _installed = True
    _plain = _plain_start()
    from gi.repository import GLib
    GLib.idle_add(_startup)


def _decide(value):
    global _state
    _state = value
    dbg(f'ai-glance session restore: {value}')
    return False


def _plain_start():
    """No option asked for something specific, and AI_GLANCE_NO_RESTORE is unset. Must run
    before the first pane starts: it clears -e/-x/--working-directory when it spawns."""
    if os.environ.get('AI_GLANCE_NO_RESTORE'):
        return False
    from terminatorlib import optionparse
    opts = getattr(optionparse, 'options', None)
    if opts is None:
        return True
    if getattr(opts, 'layout', None) not in (None, '', 'default'):
        return False
    for name in ('execute', 'command', 'working_directory', 'profile', 'config', 'configjson',
                 'hidden', 'forcedtitle'):
        if getattr(opts, name, None):
            return False
    return True


def _startup():
    try:
        threading.Thread(target=_prepare, daemon=True, name='ai-glance-restore').start()
    except Exception as exc:  # noqa: BLE001 - never raise into Terminator
        dbg(f'ai-glance session restore: cannot start worker: {exc!r}')
        _decide(_FAILED)
    return False


def _prepare():
    """Worker thread: lock, snapshot read, resume commands. Hands the result to the main thread."""
    from gi.repository import GLib
    result = (_FAILED, None)
    try:
        if not _plain:
            result = (_SKIPPED, None)
        elif not _lock.acquire():
            result = (_NOT_OWNER, None)
        else:
            path = session_store.snapshot_path()
            snap = session_store.read_snapshot(path)
            if snap is None:
                result = (_FAILED if path.exists() else _NO_SNAPSHOT, None)
            elif session_store.window_count(snap['layout']) == 0:
                result = (_NO_SNAPSHOT, None)
            else:
                layout = session_store.apply_agents(
                    session_store.clean_layout(snap['layout']), snap['agents'])
                restored = {u: r for u, r in snap['agents'].items()
                            if isinstance(r, dict) and session_store.resume_command(r) is not None}
                result = ('restore', (layout, restored))
    except Exception as exc:  # noqa: BLE001
        dbg(f'ai-glance session restore: prepare failed: {exc!r}')
        result = (_FAILED, None)
    GLib.idle_add(_apply, *result)


def _apply(outcome, payload):
    """Main thread: rebuild the layout (or settle on why not) and start the save timer."""
    try:
        _apply_inner(outcome, payload)
    except Exception as exc:  # noqa: BLE001
        dbg(f'ai-glance session restore: apply failed: {exc!r}')
        _decide(_FAILED)
    return False


def _apply_inner(outcome, payload):
    from terminatorlib.terminator import Terminator
    if outcome in (_NOT_OWNER, _FAILED, _SKIPPED):
        _decide(outcome)
        return
    terminator = Terminator()
    if len(terminator.windows) != 1 or len(terminator.terminals) != 1:
        _decide(_SKIPPED)
        return
    if outcome == _NO_SNAPSHOT:
        _decide(_NO_SNAPSHOT)
        _start_saving()
        return
    _restore(terminator, *payload)


def _restore(terminator, layout, restored):
    global _restored_agents, _restored_at
    from terminatorlib.config import Config
    config = Config()
    startup = list(terminator.windows)
    previous_name = terminator.layoutname
    config.layout_set_config(LAYOUT_NAME, layout)
    try:
        terminator.create_layout(LAYOUT_NAME)
        terminator.layout_done()
    except Exception as exc:  # noqa: BLE001
        dbg(f'ai-glance session restore: create_layout failed: {exc!r}')
        for window in [w for w in terminator.windows if w not in startup]:
            window.emit('destroy')
        # create_layout sets these and only layout_done clears them; without the reset the
        # startup window could not spawn shells (spawn_child returns while doing_layout).
        terminator.doing_layout = False
        terminator.prelayout_windows = None
        _decide(_FAILED)
        return
    finally:
        config.del_layout(LAYOUT_NAME)
        terminator.layoutname = previous_name
    for window in startup:
        window.emit('destroy')
    _restored_agents = restored
    _restored_at = time.monotonic()
    _decide(_RESTORED)
    _start_saving()


def _start_saving():
    global _saving
    from gi.repository import GLib
    _saving = True
    GLib.timeout_add_seconds(SAVE_SECONDS, _tick)


def _tick():
    """Main thread: read widget state and hand it to a worker (at most one in flight)."""
    global _in_flight
    if _in_flight:
        return True
    try:
        from terminatorlib.terminator import Terminator
        terminator = Terminator()
        if any(w.is_zoomed() for w in terminator.windows):
            dbg('ai-glance session restore: save skipped, a pane is zoomed')
            return True
        layout = terminator.describe_layout(save_cwd=False)
        panes = [(str(t.uuid), t.pid, t.vte.get_current_directory_uri())
                 for t in terminator.terminals]
        _in_flight = True
        threading.Thread(target=_save, args=(layout, panes), daemon=True,
                         name='ai-glance-save').start()
    except Exception as exc:  # noqa: BLE001
        _in_flight = False
        dbg(f'ai-glance session restore: tick failed: {exc!r}')
    return True


def _folder(uri, pid):
    """Folder of a pane from its OSC 7 URI, else /proc/<pid>/cwd; None when neither works."""
    if uri:
        try:
            from gi.repository import GLib
            return GLib.filename_from_uri(uri)[0]
        except Exception:  # noqa: BLE001
            pass
    if pid:
        try:
            return os.readlink(f'/proc/{pid}/cwd')
        except OSError:
            pass
    return None


def _save(layout, panes):
    """Worker thread: folders, agent detection, then the atomic write."""
    global _in_flight, _last_bytes
    try:
        layout = json.loads(json.dumps(layout, default=str))  # UUID objects -> str
        detected = {}
        folders = {}
        for uuid, pid, uri in panes:
            folder = _folder(uri, pid)
            if folder:
                folders[uuid] = folder
            record = session_store.agent_record(pid)
            if record is not None:
                detected[uuid] = record
        agents = session_store.merge_agents(detected, _restored_agents, _restored_at,
                                            time.monotonic(), {u for u, _, _ in panes})
        for item in layout.values():
            if isinstance(item, dict) and item.get('type') == 'Terminal':
                record = agents.get(item.get('uuid'))
                if record is not None:
                    item['directory'] = record['cwd']
                elif item.get('uuid') in folders:
                    item['directory'] = folders[item.get('uuid')]
        written = session_store.write_snapshot(session_store.snapshot_path(), layout, agents,
                                               _last_bytes)
        if written is not None:
            _last_bytes = written
    except Exception as exc:  # noqa: BLE001 - OSError (disk full, ...) included
        dbg(f'ai-glance session restore: save failed: {exc!r}')
    finally:
        _in_flight = False
