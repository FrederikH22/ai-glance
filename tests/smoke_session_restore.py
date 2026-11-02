"""Smoke test: session restore across Terminator processes. Needs a display:

    XDG_CONFIG_HOME=$(mktemp -d) XDG_STATE_HOME=$(mktemp -d) xvfb-run -a python3 tests/smoke_session_restore.py

Run without arguments this is the orchestrator: it installs the plugin into the temp
config, then starts fresh python subprocesses (one per "Terminator process") that
bootstrap like /usr/bin/terminator (default layout, then the plugin loads lazily):

  A  builds two windows (splits, tabs, profiles, folders, fake agent panes) and is kill -9'd
  B  restores from A's snapshot, checks it, and keeps the session lock
  C  starts while B holds the lock: no restore, no save
  G  restores, closes every window: the snapshot keeps its windows
  D  AI_GLANCE_NO_RESTORE=1: no restore, never saves
  E  corrupt snapshot: normal window, never saves, file untouched

Agents are fake scripts (PATH is replaced); SHELL is a bash without rc files. Never touches
the real ~/.config, ~/.local/state or a running Terminator.
"""
import atexit
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROLE = sys.argv[1] if len(sys.argv) > 1 else None
XDG_CONFIG = os.environ.get('XDG_CONFIG_HOME')
XDG_STATE = os.environ.get('XDG_STATE_HOME')
if not os.environ.get('DISPLAY') or not XDG_CONFIG or not XDG_STATE:
    sys.exit('run under xvfb-run with isolated XDG_CONFIG_HOME and XDG_STATE_HOME')
for _p in (XDG_CONFIG, XDG_STATE):
    if Path(_p).resolve() in ((Path.home() / '.config').resolve(), (Path.home() / '.local/state').resolve()):
        sys.exit('refusing to use the real config/state dir')

SNAPSHOT = Path(XDG_STATE) / 'ai-glance' / 'session.json'
ERR_PATTERN = re.compile(r'Gtk-(CRITICAL|WARNING)|GLib-.*(CRITICAL|WARNING)|Traceback|Error')
OMP_SESSION = 'omp-session.jsonl'
CLAUDE_SESSION = 'c1a0de00-0000-4000-8000-000000000001'


def layout_tree(layout, name):
    """Nested description of one layout object (flat key names differ between runs)."""
    node = layout[name]
    kids = sorted((k for k, v in layout.items() if v.get('parent') == name),
                  key=lambda k: int(layout[k].get('order', 0)))
    sub = [layout_tree(layout, k) for k in kids]
    kind = node['type']
    if kind == 'Terminal':
        return ('T', str(node.get('uuid')), node.get('directory'), node.get('profile'))
    if kind in ('VPaned', 'HPaned'):
        return ('P', kind, float(node.get('ratio', 0.5)), sub)
    if kind == 'Window':
        return ('W', [int(x) for x in node.get('size', (0, 0))], sub)
    return ('N', sub)


def uuids_of(tree):
    if tree[0] == 'T':
        return [tree[1]]
    return [u for child in tree[-1] for u in uuids_of(child)]


def layout_signature(layout):
    """{sorted uuids of a window: tree} for every window of a flat layout."""
    out = {}
    for name, node in layout.items():
        if node.get('type') == 'Window':
            tree = layout_tree(layout, name)
            out[tuple(sorted(uuids_of(tree)))] = tree
    return out


def tree_diff(a, b, path='root'):
    """First difference between two trees (ratios within 0.05, window size within 4px), or ''."""
    if a[0] != b[0]:
        return f'{path}: {a[0]} != {b[0]}'
    if a[0] == 'T':
        return '' if a == b else f'{path}: {a} != {b}'
    if a[0] == 'P' and (a[1] != b[1] or abs(a[2] - b[2]) > 0.05):
        return f'{path}: paned {a[1:3]} != {b[1:3]}'
    if a[0] == 'W' and any(abs(x - y) > 4 for x, y in zip(a[1], b[1])):
        return f'{path}: size {a[1]} != {b[1]}'
    if len(a[-1]) != len(b[-1]):
        return f'{path}: {len(a[-1])} children != {len(b[-1])}'
    for i, (x, y) in enumerate(zip(a[-1], b[-1])):
        diff = tree_diff(x, y, f'{path}/{i}')
        if diff:
            return diff
    return ''


def signature_diff(a, b):
    if set(a) != set(b):
        return f'windows differ: {sorted(map(len, a))} vs {sorted(map(len, b))}'
    for key in a:
        diff = tree_diff(a[key], b[key])
        if diff:
            return diff
    return ''


# ============================== orchestrator ==============================
if ROLE is None:
    work = Path(tempfile.mkdtemp(prefix='ai-glance-restore-'))
    atexit.register(lambda: subprocess.run(['rm', '-rf', str(work)]))
    for name in ('proj_alpha', 'proj_beta', 'proj_gamma', 'proj_delta', 'proj_epsilon', 'omp_cwd'):
        (work / name).mkdir()
    (work / OMP_SESSION).write_text('{}\n')
    fakebin = work / 'bin'
    fakebin.mkdir()
    for agent in ('omp', 'claude'):
        script = fakebin / agent
        script.write_text(f'#!/bin/sh\nprintf \'%s\\n%s\\n\' "$*" "$(pwd)" > "{work}/{agent}.log"\n')
        script.chmod(0o755)
    shell = work / 'fakesh'
    shell.write_text('#!/bin/bash\nexec /bin/bash --norc --noprofile "$@"\n')
    shell.chmod(0o755)

    conf = Path(XDG_CONFIG) / 'terminator'
    conf.mkdir(parents=True, exist_ok=True)
    (conf / 'config').write_text('[profiles]\n  [[default]]\n  [[alt]]\n    background_color = "#102030"\n')
    subprocess.run(['bash', str(ROOT / 'install.sh')], check=True, stdout=subprocess.DEVNULL)

    env = dict(os.environ, AI_GLANCE_SMOKE_TMP=str(work), SHELL=str(shell), HISTFILE='/dev/null',
               PATH=f'{fakebin}:/usr/bin:/bin')
    env.pop('AI_GLANCE_NO_RESTORE', None)
    FAILURES = []

    def check(name, ok, detail=''):
        print(('PASS' if ok else 'FAIL') + ': ' + name + (f' ({detail})' if detail and not ok else ''))
        if not ok:
            FAILURES.append(name)

    class Role:
        def __init__(self, role, extra_env=None):
            self.role = role
            self.lines = []
            self.ready = threading.Event()
            self.proc = subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve()), role],
                                         env=dict(env, **(extra_env or {})), stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True)
            self.thread = threading.Thread(target=self._read, daemon=True)
            self.thread.start()

        def _read(self):
            for line in self.proc.stdout:
                line = line.rstrip('\n')
                self.lines.append(line)
                print(f'  [{self.role}] {line}')
                if line == 'READY':
                    self.ready.set()

        def wait_ready(self, timeout=90):
            return self.ready.wait(timeout) and self.proc.poll() is None

        def kill9(self):
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(10)
            self.thread.join(5)

        def finish(self, timeout=120):
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)
                self.lines.append('FAIL: role timed out')
            self.thread.join(5)

        def verdict(self):
            check(f'process {self.role} ran and reported no failure',
                  any(l == 'DONE' or l == 'READY' for l in self.lines)
                  and not any(l.startswith('FAIL') for l in self.lines),
                  '; '.join(l for l in self.lines if l.startswith('FAIL'))[:300] or 'no DONE')

    a = Role('A')
    ok = a.wait_ready()
    check('A: built the layout and saved it', ok)
    a.kill9()  # a freeze: no clean shutdown
    a.verdict()
    saved = SNAPSHOT.read_bytes() if SNAPSHOT.exists() else b''
    check('A: snapshot survived kill -9', bool(saved))

    b = Role('B')
    check('B: restored and holds the lock', b.wait_ready())
    c = Role('C')
    c.finish()
    c.verdict()
    b.kill9()
    b.verdict()

    g = Role('G')
    g.finish()
    g.verdict()

    d = Role('D', {'AI_GLANCE_NO_RESTORE': '1'})
    d.finish()
    d.verdict()

    half = SNAPSHOT.read_bytes() if SNAPSHOT.exists() else b'{"version": 1, "layout": {"win'
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_bytes(half[:len(half) // 2])
    e = Role('E')
    e.finish()
    e.verdict()

    print('FAILED: ' + ', '.join(FAILURES) if FAILURES else 'ALL PASS')
    sys.stdout.flush()
    os._exit(1 if FAILURES else 0)

# ================================ roles ================================
WORK = Path(os.environ['AI_GLANCE_SMOKE_TMP'])
DIRS = {n: str(WORK / n) for n in ('proj_alpha', 'proj_beta', 'proj_gamma', 'proj_delta',
                                   'proj_epsilon', 'omp_cwd')}
OMP_PATH = str(WORK / OMP_SESSION)

_errfile = tempfile.TemporaryFile(mode='w+b')
os.dup2(_errfile.fileno(), 2)

import gi  # noqa: E402
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk  # noqa: E402

# NOTE: ROOT is deliberately not on sys.path: ai_glance must come from the plugin dir.
from terminatorlib.terminator import Terminator  # noqa: E402

Gtk.main_quit = lambda *a: None
FAILURES = []


def check(name, ok, detail=''):
    print(('PASS' if ok else 'FAIL') + ': ' + name + (f' ({detail})' if detail and not ok else ''))
    if not ok:
        FAILURES.append(name)


def pump(seconds=0.2, until=None):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if until is not None and until():
            return True
        time.sleep(0.01)
    return until() if until is not None else True


def boot():
    """Bootstrap like /usr/bin/terminator: default layout, then (lazily loaded) plugins."""
    terminator = Terminator()
    terminator.set_origcwd(os.getcwd())
    terminator.reconfigure()
    terminator.create_layout('default')
    terminator.layout_done()
    pump(0.5)
    return terminator


def restore_module():
    mod = sys.modules.get('ai_glance.gtk_session_restore')
    if mod is None:
        print('FAIL: gtk_session_restore was not loaded by the plugin')
        print('FAILED: plugin did not load it')
        os._exit(1)
    return mod


def snapshot_json():
    try:
        return json.loads(SNAPSHOT.read_text())
    except (OSError, ValueError):
        return None


def stat_ns():
    try:
        return SNAPSHOT.stat().st_mtime_ns
    except OSError:
        return None


def stderr_ok():
    _errfile.seek(0)
    text = _errfile.read().decode(errors='replace')
    bad = [l for l in text.splitlines() if ERR_PATTERN.search(l) and 'xdg/terminator/config' not in l]
    check('stderr pristine', not bad, '\n'.join(bad[:10]))


def finish():
    stderr_ok()
    print('FAILED: ' + ', '.join(FAILURES) if FAILURES else 'DONE')
    sys.stdout.flush()
    os._exit(1 if FAILURES else 0)


def focus(term):
    term.vte.grab_focus()
    term.on_vte_focus_in(term.vte, None)
    pump(0.3)


def set_term_title(term, text):
    term.vte.feed(f'\x1b]0;{text}\x07'.encode())
    pump(0.3)


def cd(term, path):
    term.vte.feed_child(f'cd {path}\n'.encode())
    return pump(8.0, lambda: term.get_cwd() == str(path))


def descendants(widget):
    yield widget
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            yield from descendants(child)


def role_a():
    terminator = boot()
    mod = restore_module()
    from ai_glance import session_store
    records = {}
    session_store.agent_record = lambda pid, *a, **k: records.get(pid)
    check('A: no snapshot -> state no-snapshot', pump(10, lambda: mod.state() is not None) and mod.state() == 'no-snapshot',
          mod.state())
    check('A: saving enabled after the decision', mod.saving())

    win1 = terminator.windows[0]
    t1 = terminator.terminals[0]
    win1.resize(1000, 700)
    t1.emit('split-vert', None)
    pump(0.8)
    t2 = [t for t in terminator.terminals if t is not t1][0]
    t2.emit('split-horiz', None)
    pump(0.8)
    t3 = [t for t in terminator.terminals if t not in (t1, t2)][0]
    win2, t4 = terminator.new_window()
    pump(0.8)
    win2.resize(700, 500)
    t4.emit('tab-new', False, t4)
    pump(1.0)
    t5 = [t for t in terminator.terminals if t not in (t1, t2, t3, t4)][0]
    paneds = [w for w in descendants(win1) if type(w).__name__ in ('VPaned', 'HPaned')]
    outer = paneds[0]
    outer.set_position(int(outer.get_allocated_height() * 0.3) if type(outer).__name__ == 'VPaned'
                       else int(outer.get_allocated_width() * 0.3))
    win1.title.force_title('Frozen A')  # must not reach the snapshot
    pump(0.5)

    plan = [(t1, 'proj_alpha'), (t2, 'proj_beta'), (t3, 'proj_gamma'), (t4, 'proj_delta'), (t5, 'proj_epsilon')]
    check('A: all five panes have a shell', pump(8, lambda: all(t.pid for t, _ in plan)))
    for term, folder in plan:
        check(f'A: cd {folder}', cd(term, DIRS[folder]), term.get_cwd())
    t3.force_set_profile(None, 'alt')
    t5.force_set_profile(None, 'alt')
    pump(0.5)
    records[t2.pid] = {'agent': 'omp', 'session': OMP_PATH, 'cwd': DIRS['omp_cwd']}
    records[t3.pid] = {'agent': 'Claude Code', 'session': CLAUDE_SESSION, 'cwd': DIRS['proj_gamma']}

    def saved_ok():
        snap = snapshot_json()
        if snap is None:
            return False
        terms = [i for i in snap['layout'].values() if i.get('type') == 'Terminal']
        dirs = {i.get('directory') for i in terms}
        return (len(terms) == 5 and len(snap['agents']) == 2
                and dirs == {DIRS['proj_alpha'], DIRS['omp_cwd'], DIRS['proj_gamma'],
                             DIRS['proj_delta'], DIRS['proj_epsilon']}
                and sum(1 for i in terms if i.get('profile') == 'alt') == 2)
    check('A: a save captured the whole layout', pump(30, saved_ok))
    snap = snapshot_json() or {'layout': {}, 'agents': {}}
    layout = snap['layout']
    windows = [i for i in layout.values() if i.get('type') == 'Window']
    check('A: two windows in the snapshot', len(windows) == 2, len(windows))
    check('A: no window title in the snapshot', not any('title' in w for w in windows))
    check('A: a notebook (tabs) is in the snapshot', any(i.get('type') == 'Notebook' for i in layout.values()))
    uuids = {i['uuid'] for i in layout.values() if i.get('type') == 'Terminal'}
    check('A: agents keyed by terminal uuid', set(snap['agents']) == {str(t2.uuid), str(t3.uuid)}
          and set(snap['agents']) <= uuids, snap['agents'])
    check('A: agent records as detected', snap['agents'].get(str(t2.uuid)) == records[t2.pid]
          and snap['agents'].get(str(t3.uuid)) == records[t3.pid])
    check('A: agent pane saved in the agent cwd, not the shell cwd',
          next(i for i in layout.values() if i.get('uuid') == str(t2.uuid))['directory'] == DIRS['omp_cwd'])
    (WORK / 'expected.json').write_text(json.dumps(layout))
    (WORK / 'expected-uuids.json').write_text(json.dumps(
        {'omp': str(t2.uuid), 'claude': str(t3.uuid), 'plain': str(t1.uuid), 'win1': str(t1.uuid)}))
    stderr_ok()
    print('READY')
    while True:
        pump(0.5)


def role_b(close_all=False):
    before = stat_ns()
    terminator = Terminator()
    terminator.set_origcwd(os.getcwd())
    terminator.reconfigure()
    terminator.create_layout('default')
    terminator.layout_done()
    startup_window = terminator.windows[0]
    startup_term = terminator.terminals[0]
    mod = restore_module()
    from ai_glance import session_store
    check('restore decision made', pump(20, lambda: mod.state() is not None), mod.state())
    check(f'state is restored ({mod.state()})', mod.state() == 'restored')
    check('saving enabled after the restore', mod.saving())
    expected = json.loads((WORK / 'expected.json').read_text())
    ids = json.loads((WORK / 'expected-uuids.json').read_text())
    check('the startup window is gone', startup_window not in terminator.windows
          and startup_term not in terminator.terminals)
    check('two windows, five panes', len(terminator.windows) == 2 and len(terminator.terminals) == 5,
          (len(terminator.windows), len(terminator.terminals)))
    by_uuid = {str(t.uuid): t for t in terminator.terminals}
    check('terminal uuids preserved', set(by_uuid) == {i['uuid'] for i in expected.values()
                                                       if i.get('type') == 'Terminal'})
    pump(3.0)  # let the split ratios settle
    diff = signature_diff(layout_signature(expected),
                          layout_signature(json.loads(json.dumps(
                              terminator.describe_layout(save_cwd=True), default=str))))
    check('structure, ratios, folders, profiles match the snapshot', diff == '', diff)

    omp_term, claude_term, plain_term = by_uuid[ids['omp']], by_uuid[ids['claude']], by_uuid[ids['plain']]
    check('omp pane starts the resume command',
          omp_term.layout_command == session_store.resume_command(
              {'agent': 'omp', 'session': OMP_PATH, 'cwd': DIRS['omp_cwd']}), omp_term.layout_command)
    check('claude pane starts the resume command',
          claude_term.layout_command == session_store.resume_command(
              {'agent': 'Claude Code', 'session': CLAUDE_SESSION, 'cwd': DIRS['proj_gamma']}),
          claude_term.layout_command)
    check('plain pane runs no command', not plain_term.layout_command)
    check('no pane runs --restore/--continue', not any(
        w in (t.layout_command or '') for t in terminator.terminals for w in ('--restore', '--continue')))

    def agent_log(name):
        path = WORK / f'{name}.log'
        return path.read_text().splitlines() if path.exists() else []
    check('fake omp was started with --resume <session> in the agent cwd',
          pump(15, lambda: agent_log('omp') == [f'--resume {OMP_PATH}', DIRS['omp_cwd']]), agent_log('omp'))
    check('fake claude was started with --resume <id> in its folder',
          pump(15, lambda: agent_log('claude') == [f'--resume {CLAUDE_SESSION}', DIRS['proj_gamma']]),
          agent_log('claude'))
    check('after the agent exits the pane is a shell in the folder',
          pump(10, lambda: omp_term.get_cwd() == DIRS['omp_cwd'] and claude_term.get_cwd() == DIRS['proj_gamma']),
          (omp_term.get_cwd(), claude_term.get_cwd()))

    # the window title still follows the focused pane (not frozen by a restored title)
    win = plain_term.get_toplevel()
    others = [t for t in terminator.terminals if t is not plain_term and t.get_toplevel() is win]
    check('the restored window has several panes', len(others) >= 1)
    other = others[0]
    check('restored window title is not forced', not win.title.forced)
    focus(plain_term)
    set_term_title(plain_term, 'First pane')
    check(f'title follows the focused pane ({win.get_title()!r})', win.get_title() == 'First pane')
    focus(other)
    set_term_title(other, 'Second pane')
    check(f'title follows the newly focused pane ({win.get_title()!r})', win.get_title() == 'Second pane')
    set_term_title(plain_term, 'First pane renamed')
    check('unfocused pane does not take the title', win.get_title() == 'Second pane')

    check('the restored process saves too (snapshot rewritten)', pump(25, lambda: stat_ns() != before))
    if close_all:
        data = SNAPSHOT.read_bytes()
        for w in list(terminator.windows):
            w.emit('destroy')
        pump(13.0)
        check('closing every window does not overwrite the snapshot', SNAPSHOT.read_bytes() == data)
        snap = snapshot_json()
        check('the snapshot still has both windows',
              snap is not None and session_store.window_count(snap['layout']) == 2)
        finish()
    stderr_ok()
    print('READY')
    while True:
        pump(0.5)


def role_idle(expected_state, name):
    """C / D / E: a plain startup window must stay, nothing is saved."""
    terminator = boot()
    mod = restore_module()
    data, mtime = (SNAPSHOT.read_bytes() if SNAPSHOT.exists() else None), stat_ns()
    check(f'{name}: state is {expected_state}',
          pump(20, lambda: mod.state() is not None) and mod.state() == expected_state, mod.state())
    check(f'{name}: the startup window stays alone',
          len(terminator.windows) == 1 and len(terminator.terminals) == 1)
    check(f'{name}: saving is off', not mod.saving())
    pump(13.0)  # longer than the save interval
    check(f'{name}: snapshot bytes unchanged after 13 s',
          (SNAPSHOT.read_bytes() if SNAPSHOT.exists() else None) == data and stat_ns() == mtime)
    finish()


def run_role():
    if ROLE == 'A':
        role_a()
    elif ROLE == 'B':
        role_b()
    elif ROLE == 'G':
        role_b(close_all=True)
    elif ROLE == 'C':
        role_idle('not-owner', 'C')
    elif ROLE == 'D':
        role_idle('skipped', 'D')
    elif ROLE == 'E':
        role_idle('failed', 'E')
    else:
        sys.exit(f'unknown role {ROLE}')


try:
    run_role()
except Exception:  # stderr is captured for the pristine check: report on stdout
    import traceback
    print('FAIL: role crashed: ' + traceback.format_exc().replace('\n', ' | '))
    sys.stdout.flush()
    os._exit(1)
