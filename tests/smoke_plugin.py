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
HW = HOME / 'ros_ws/src/robot_driver'
WS = HOME / 'ros_ws'
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
from gi.repository import Gtk  # noqa: E402

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
    return isinstance(first, Gtk.Button) and getattr(first, '_ai_glance_git_button', False)


def button_of(term):
    return term.titlebar.get_child().get_children()[0]


def panels(term):
    return [c for c in term.get_children() if isinstance(c, GitPanel)]


def bar_ok(win):
    bar = win.__dict__.get('_ai_glance_usage')
    return (bar is not None and Gtk.Bin.get_child(win) is bar.wrapper
            and bar.wrapper.get_children()[-1] is bar.label)


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
expected = usage.format_bar(usage.read_usage(usage.DEFAULT_DB), __import__('datetime').datetime.now(
    __import__('datetime').timezone.utc))[0]
norm = lambda m: re.sub(r'updated \d+ min', 'updated N min', m)  # noqa: E731
check('7: bar text matches usage_history',
      pump(3.0, lambda: norm(bar.label.get_label()) == norm(expected)),
      f'{bar.label.get_label()!r} vs {expected!r}')

# 2/3: click on robot_driver --------------------------------------------------
model = gitinfo.build_model(HW)
wt_paths = [wt.path for repo in model.repos for wt in repo.worktrees]
paneds_before = sum(1 for w in descendants(window) if isinstance(w, Paned))
button_of(term).clicked()
check('2: click opens one panel inside the pane', len(panels(term)) == 1)
panel = panels(term)[0]
branch = model.repos[0].worktrees[0].branch
check('3: panel lists branch, base and every worktree',
      pump(8.0, lambda: branch in text_of(panel) and all(
          (str(p).replace(str(HOME), '~')) in text_of(panel) or p.name in text_of(panel)
          for p in wt_paths)) and 'origin/main' in text_of(panel), text_of(panel)[:400])
check('3: worktree count matches model', len(wt_paths) >= 10, len(wt_paths))
check('2: no new split, vte still in the pane',
      sum(1 for w in descendants(window) if isinstance(w, Paned)) == paneds_before
      and term.vte in list(descendants(term)))
check('1/2: no omp here -> no highlight', '◆' not in text_of(panel))
shot('accept-panel-robot_driver.png')

# 5(b): highlight --------------------------------------------------------------
target = wt_paths[1]
wt_branches = [wt.branch for repo in model.repos for wt in repo.worktrees]
labels_before = [w.get_text() for w in descendants(panel) if isinstance(w, Gtk.Label)]
real_fn = gtk_git_panel.ompsessions.pane_worktrees_in_use
gtk_git_panel.ompsessions.pane_worktrees_in_use = lambda pid, paths, now: {target}
panel._last_key = None
panel.refresh()
pump(8.0, lambda: '◆' in text_of(panel) and not panel._in_flight)
labels_after = [w.get_text() for w in descendants(panel) if isinstance(w, Gtk.Label)]
diff = [i for i, (a, b) in enumerate(zip(labels_before, labels_after)) if a != b]
check('5: exactly one row changed, only by the ◆ marker',
      len(labels_before) == len(labels_after) and len(diff) == 1
      and labels_after[diff[0]].replace('◆ ', '', 1) == labels_before[diff[0]], diff)
check('5: that row is the in-use worktree (2nd worktree row, its branch)',
      len(diff) == 1 and wt_branches[1] in labels_after[diff[0]]
      and wt_branches[0] in labels_after[diff[0] - 1])
shot('accept-highlight.png')
gtk_git_panel.ompsessions.pane_worktrees_in_use = real_fn
panel._last_key = None
panel.refresh()
pump(8.0, lambda: '◆' not in text_of(panel) and not panel._in_flight)
check('5: highlight gone again without omp', '◆' not in text_of(panel))

button_of(term).clicked()
check('2: second click removes the panel', not panels(term))
check('2: panel closed cleanly', panel._closed and panel._timer is None)

# 4: workspace view -------------------------------------------------------------
check('4: cd ~/ros_ws', cd(term, WS), term.get_cwd())
button_of(term).clicked()
wsmodel = gitinfo.build_model(WS)
repos = [r.name for r in wsmodel.repos]
panel = panels(term)[0]
check('4: workspace view lists every repo under src/ with worktree rows',
      pump(10.0, lambda: 'workspace' in text_of(panel) and all(n in text_of(panel) for n in repos)),
      text_of(panel)[:400])
check('4: repos found', len(repos) >= 3, repos)
shot('accept-workspace-ros_ws.png')
button_of(term).clicked()

# 6: not a git repo -------------------------------------------------------------
check('6: cd /tmp', cd(term, '/tmp'), term.get_cwd())
button_of(term).clicked()
panel = panels(term)[0]
check('6: "Not a git repository"', pump(8.0, lambda: 'Not a git repository' in text_of(panel)), text_of(panel))
button_of(term).clicked()
cd(term, HW)

# 7: split / zoom / tab / close ----------------------------------------------------
term.emit('split-vert', None)
pump(1.0)
term2 = [t for t in terminator.terminals if t is not term][0]
term2.emit('split-horiz', None)
pump(1.0)
check('7: split -> 3 panes, all with buttons', len(terminator.terminals) == 3 and all_buttons())
check('7: bar survives split', bar_ok(window))
button_of(term2).clicked()
pump(1.0)
shot('accept-split-bar.png')
button_of(term2).clicked()
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
