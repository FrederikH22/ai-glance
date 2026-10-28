"""End-to-end smoke for the installed plugin, loaded by Terminator's own PluginRegistry.

    XDG_CONFIG_HOME=$(mktemp -d) AI_GLANCE_SHOTS=<dir> xvfb-run -a python3 tests/smoke_plugin.py

The isolated XDG_CONFIG_HOME receives a copy of the user's Terminator config (if any),
then install.sh enables the plugin there. Bootstraps Terminator in-process like
/usr/bin/terminator. Never touches the real ~/.config or a running Terminator.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
XDG = os.environ.get('XDG_CONFIG_HOME')
if not XDG or not os.environ.get('DISPLAY'):
    sys.exit('run under xvfb-run with an isolated XDG_CONFIG_HOME')
if Path(XDG).resolve() == (Path.home() / '.config').resolve():
    sys.exit('refusing to use the real config dir')

HOME = Path.home()
# A ROS workspace with git repos under src/ (some with linked worktrees) and one repo in it.
WS = Path(os.path.expanduser(os.environ.get('AI_GLANCE_SMOKE_WS', '~/ros_ws')))
HW = Path(os.path.expanduser(os.environ.get('AI_GLANCE_SMOKE_REPO', str(WS / 'src/robot_driver'))))
if not HW.is_dir():
    sys.exit(f'set AI_GLANCE_SMOKE_WS / AI_GLANCE_SMOKE_REPO: {HW} does not exist')
SHOTS = os.environ.get('AI_GLANCE_SHOTS')

# --- isolated config with the plugin installed -----------------------------
conf = Path(XDG) / 'terminator'
conf.mkdir(parents=True, exist_ok=True)
real_cfg = HOME / '.config/terminator/config'
if not (conf / 'config').exists() and real_cfg.exists():
    shutil.copy(real_cfg, conf / 'config')
subprocess.run(['bash', str(ROOT / 'install.sh')], check=True, stdout=subprocess.DEVNULL)

_errfile = tempfile.TemporaryFile(mode='w+b')
os.dup2(_errfile.fileno(), 2)

import gi  # noqa: E402
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk, Gtk  # noqa: E402

# NOTE: ROOT is deliberately not on sys.path: ai_glance must come from the plugin dir.
from terminatorlib.factory import Factory  # noqa: E402
from terminatorlib.paned import Paned  # noqa: E402
from terminatorlib.plugin import PluginRegistry  # noqa: E402
from terminatorlib.terminator import Terminator  # noqa: E402

FAILURES = []
QUIT_CALLS = []
Gtk.main_quit = lambda *a: QUIT_CALLS.append(a)


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


def descendants(widget):
    yield widget
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            yield from descendants(child)


def text_of(widget):
    return '\n'.join(w.get_text() for w in descendants(widget) if isinstance(w, Gtk.Label))


def omp_rows(panel):
    return [w for w in descendants(panel) if w.get_style_context().has_class('gg-omp-row')]


def label_texts(widget):
    return [w.get_text() for w in descendants(widget) if isinstance(w, Gtk.Label)]


def shot(name):
    if SHOTS:
        pump(0.5)
        subprocess.run(['import', '-window', 'root', str(Path(SHOTS) / name)], check=False)
        print('screenshot ->', Path(SHOTS) / name)


# --- bootstrap like /usr/bin/terminator ---------------------------------------
os.chdir(HW)
terminator = Terminator()
terminator.set_origcwd(str(HW))
terminator.reconfigure()
terminator.create_layout('default')
terminator.layout_done()
pump(2.0)

window = terminator.windows[0]
window.resize(1100, 700)
registry = PluginRegistry()
check('AiGlance loaded by the PluginRegistry', registry.is_enabled('AiGlance'))
mod = sys.modules.get('ai_glance')
check('ai_glance imported from the plugin dir (symlink), not the checkout',
      mod is not None and str(conf / 'plugins') in (mod.__file__ or ''), getattr(mod, '__file__', None))
if mod is None or not registry.is_enabled('AiGlance'):
    print('FAILED: plugin did not load')
    _errfile.seek(0)
    sys.stdout.write(_errfile.read().decode(errors='replace'))
    sys.exit(1)

from ai_glance import gitinfo, gtk_git_panel  # noqa: E402
from ai_glance.gtk_git_panel import GitPanel  # noqa: E402
from ai_glance import usage  # noqa: E402


def button_ok(term):
    hbox = term.titlebar.get_child()
    first = hbox.get_children()[0]
    return (isinstance(first, Gtk.EventBox) and not isinstance(first, Gtk.Button)
            and getattr(first, '_ai_glance_git_button', False))


def button_of(term):
    return term.titlebar.get_child().get_children()[0]


def click_git(term):
    ev = Gdk.Event.new(Gdk.EventType.BUTTON_PRESS)
    ev.button = 1
    button_of(term).emit('button-press-event', ev)

def panels(term):
    return [c for c in term.get_children() if isinstance(c, GitPanel)]


def bar_ok(win):
    bar = win.__dict__.get('_ai_glance_usage')
    return (bar is not None and Gtk.Bin.get_child(win) is bar.wrapper
            and bar.wrapper.get_children()[-1] is bar.box)


def all_buttons():
    return all(button_ok(t) for t in terminator.terminals)


def cd(term, path):
    term.vte.feed_child(f'cd {path}\n'.encode())
    return pump(5.0, lambda: term.get_cwd() == str(path))


# 1: button on every pane, index 0 ----------------------------------------------
term = terminator.terminals[0]
check('1: first pane cwd is robot_driver', term.get_cwd() == str(HW), term.get_cwd())
check('1: every pane has the git button at hbox index 0', all_buttons())
check('7: usage bar at window bottom', bar_ok(window))
bar = window.__dict__['_ai_glance_usage']
expected = usage.bar_segments(usage.read_current_usage(usage.DEFAULT_DB, usage.OMP_COMMAND),
                              __import__('datetime').datetime.now(__import__('datetime').timezone.utc)).accounts
check('7: bar model matches live usage',
      pump(3.0, lambda: bar.model is not None and bar.model.accounts == expected),
      f'{bar.model!r} vs {expected!r}')

# 2/3: click on robot_driver --------------------------------------------------
model = gitinfo.build_model(HW)
wt_paths = [wt.path for repo in model.repos for wt in repo.worktrees]
paneds_before = sum(1 for w in descendants(window) if isinstance(w, Paned))
click_git(term)
check('2: click opens one panel inside the pane', len(panels(term)) == 1)
panel = panels(term)[0]
branch = model.repos[0].worktrees[0].branch
check('3: panel lists the current branch and the base, no worktree list',
      pump(8.0, lambda: branch in label_texts(panel) and 'origin/main' in text_of(panel)), text_of(panel)[:400])
check('8: no omp -> no worktree rows and no omp badge',
      not omp_rows(panel) and 'omp' not in label_texts(panel) and '◆' not in text_of(panel))
_cur = model.repos[0].current.path if model.repos[0].current else wt_paths[0]
_short = lambda p: str(p).replace(str(HOME), '~')  # noqa: E731
check('8: no label shows the path of any non-current worktree (no omp)',
      not any(_short(p) in label_texts(panel) or str(p) in label_texts(panel)
              for p in wt_paths if p != _cur))
check('3: several worktrees exist (live repo state varies)', len(wt_paths) >= 3, len(wt_paths))
check('2: no new split, vte still in the pane',
      sum(1 for w in descendants(window) if isinstance(w, Paned)) == paneds_before
      and term.vte in list(descendants(term)))
shot('accept-panel-robot_driver.png')

# 8: omp live in the pane -> exactly the in-use worktrees ---------------------------
current_path = model.repos[0].current.path if model.repos[0].current else wt_paths[0]
targets = [p for p in wt_paths if p != current_path][:2]
real_fn = gtk_git_panel.ompsessions.pane_worktrees_in_use
gtk_git_panel.ompsessions.pane_worktrees_in_use = lambda pid, paths, now, **kw: set(targets)
panel._last_key = None
panel.refresh()
pump(8.0, lambda: len(omp_rows(panel)) == 2 and not panel._in_flight)
rows = omp_rows(panel)
row_paths = {[l for l in label_texts(r)][0] for r in rows}
short = lambda p: str(p).replace(str(HOME), '~')  # noqa: E731
check('8: exactly the two in-use worktrees get rows (path, branch pill)',
      len(rows) == 2 and row_paths == {short(p) for p in targets}, (row_paths, targets))
check('8: omp badge in the header', 'omp' in label_texts(panel))
check('8: no non-target worktree path is listed',
      not any(_short(p) in label_texts(panel) for p in wt_paths if p not in targets))
shot('accept-highlight.png')
if SHOTS:
    pump(0.5)
    subprocess.run(['import', '-window', 'root', str(Path(SHOTS) / 'user-git-panel-omp.png')], check=False)
gtk_git_panel.ompsessions.pane_worktrees_in_use = real_fn
panel._last_key = None
panel.refresh()
pump(8.0, lambda: not omp_rows(panel) and not panel._in_flight)
check('8: rows gone again without omp', not omp_rows(panel) and 'omp' not in label_texts(panel))
if SHOTS:
    pump(0.5)
    subprocess.run(['import', '-window', 'root', str(Path(SHOTS) / 'user-git-panel.png')], check=False)

click_git(term)
check('2: second click removes the panel', not panels(term))
check('2: panel closed cleanly', panel._closed and panel._timer is None)

# 4: workspace view -------------------------------------------------------------
check('4: cd ~/ros_ws', cd(term, WS), term.get_cwd())
click_git(term)
wsmodel = gitinfo.build_model(WS)
repos = [r.name for r in wsmodel.repos]
panel = panels(term)[0]
check('4: workspace view lists every repo under src/ (one row each)',
      pump(10.0, lambda: 'GIT' in text_of(panel) and all(n in label_texts(panel) for n in repos)),
      text_of(panel)[:400])
check('4: repos found', len(repos) >= 3, repos)
shot('accept-workspace-ros_ws.png')
click_git(term)

# 4b: a worktree workspace: repo rows must show that workspace's branches ------------
WSW = Path(os.path.expanduser(os.environ.get('AI_GLANCE_SMOKE_WORKTREE_WS', str(WS / '.worktrees/v2-ws'))))
if WSW.is_dir():
    check('4b: cd v2-ws', cd(term, WSW), term.get_cwd())
    click_git(term)
    wmodel = gitinfo.build_model(WSW)
    panel = panels(term)[0]
    row_ok = lambda: all(  # noqa: E731
        r.name in label_texts(panel) and r.current.branch in label_texts(panel)
        for r in wmodel.repos if r.current is not None and r.current.branch)
    check('4b: repo rows show the worktree-workspace branches',
          bool(wmodel.repos) and pump(10.0, lambda: 'GIT' in text_of(panel) and row_ok()),
          [(r.name, r.current and r.current.branch) for r in wmodel.repos])
    check('4b: current is not the main checkout for at least one repo',
          any(r.current is not None and r.current is not r.worktrees[0] for r in wmodel.repos))
    shot('accept-worktree-ws.png')
    click_git(term)

# 6: not a git repo -------------------------------------------------------------
check('6: cd /tmp', cd(term, '/tmp'), term.get_cwd())
click_git(term)
panel = panels(term)[0]
check('6: "Not a git repository"', pump(8.0, lambda: 'Not a git repository' in text_of(panel)), text_of(panel))
click_git(term)
cd(term, HW)

# 7: split / zoom / tab / close ----------------------------------------------------
term.emit('split-vert', None)
pump(1.0)
term2 = [t for t in terminator.terminals if t is not term][0]
term2.emit('split-horiz', None)
pump(1.0)
check('7: split -> 3 panes, all with buttons', len(terminator.terminals) == 3 and all_buttons())
check('7: bar survives split', bar_ok(window))
click_git(term2)
pump(1.0)
shot('accept-split-bar.png')
click_git(term2)
before = window.get_child()
window.zoom(term2)
pump(0.5)
check('7: bar + button survive zoom', bar_ok(window) and all_buttons())
window.unzoom()
pump(0.5)
check('7: bar + button survive unzoom', bar_ok(window) and all_buttons() and window.get_child() is before)
known = set(terminator.terminals)
window.tab_new(term)
pump(1.5)
newterm = [t for t in terminator.terminals if t not in known]
check('7: new tab pane has the button, bar survives',
      len(newterm) == 1 and button_ok(newterm[0]) and bar_ok(window) and all_buttons())
newterm[0].close()
pump(0.8)
check('7: closing the tab keeps bar and buttons', bar_ok(window) and all_buttons()
      and len(terminator.terminals) == 3)
term2.close()
pump(0.8)
check('7: closing a pane keeps bar and buttons', bar_ok(window) and all_buttons()
      and len(terminator.terminals) == 2)

destroyed = []
window.connect('destroy', lambda *_: destroyed.append(True))
for t in list(terminator.terminals):
    t.close()
    pump(0.5)
check('7: closing the last pane destroys the window', pump(3.0, lambda: bool(destroyed)))
check('7: no windows left, bar dead', not terminator.windows and bar.dead)

# stderr ---------------------------------------------------------------------------
pump(0.5)
_errfile.seek(0)
stderr = _errfile.read().decode(errors='replace')
bad = [l for l in stderr.splitlines()
       if re.search(r'Gtk-(CRITICAL|WARNING)|GLib-.*(CRITICAL|WARNING)|Gdk-(CRITICAL|WARNING)|Traceback|Error', l)]
check('stderr clean', not bad, '\n'.join(bad[:10]))
if stderr.strip():
    print('--- stderr ---\n' + stderr)
print('FAILED: ' + ', '.join(FAILURES) if FAILURES else 'ALL PASS')
sys.stdout.flush()
os._exit(1 if FAILURES else 0)
