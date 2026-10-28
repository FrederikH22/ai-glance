"""Smoke test for ai_glance.gtk_git_panel. Needs a display; run under xvfb-run:

    XDG_CONFIG_HOME=$(mktemp -d) xvfb-run -a python3 tests/smoke_git_panel.py [screenshot.png]
"""
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if 'XDG_CONFIG_HOME' not in os.environ:
    sys.exit('refusing to run without an isolated XDG_CONFIG_HOME')
if not os.environ.get('DISPLAY'):
    sys.exit('refusing to run without a (virtual) DISPLAY')

CWD = os.path.expanduser('~/ros_ws/src/robot_driver')
os.chdir(CWD)

import gi  # noqa: E402
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib  # noqa: E402

from terminatorlib.terminator import Terminator  # noqa: E402
from terminatorlib.factory import Factory  # noqa: E402
from terminatorlib.paned import Paned  # noqa: E402

failures = []
import tempfile  # noqa: E402
_real_stderr = os.dup(2)
_err_file = tempfile.NamedTemporaryFile('w+')
os.dup2(_err_file.fileno(), 2)


def check(name, ok):
    print('%s %s' % ('PASS' if ok else 'FAIL', name))
    if not ok:
        failures.append(name)


def pump(seconds, until=None):
    end = time.time() + seconds
    while time.time() < end:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if until is not None and until():
            return True
        time.sleep(0.02)
    return until() if until is not None else True


def descendants(widget):
    yield widget
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            yield from descendants(child)


def labels_text(widget):
    return '\n'.join(w.get_text() for w in descendants(widget)
                     if isinstance(w, Gtk.Label))


def count_paned(widget):
    return sum(1 for w in descendants(widget) if isinstance(w, Paned))


def panel_of(term):
    from ai_glance.gtk_git_panel import GitPanel
    return [c for c in term.get_children() if isinstance(c, GitPanel)]


def has_button(term):
    hbox = term.titlebar.get_child()
    first = hbox.get_children()[0]
    return isinstance(first, Gtk.Button) and getattr(first, '_ai_glance_git_button', False)


terminator = Terminator()
terminator.set_origcwd(CWD)
terminator.reconfigure()
factory = Factory()
window = factory.make('Window')
term = factory.make('Terminal')
window.add(term)
window.show_all()
term.spawn_child()
pump(1.0)

try:
    from ai_glance import gtk_git_panel
except ImportError as exc:
    print('FAIL import ai_glance.gtk_git_panel: %s' % exc)
    sys.exit(1)

gtk_git_panel.install()
gtk_git_panel.install()  # idempotent
check('retrofit: existing terminal has git button first', has_button(term))
check('install twice: exactly one button',
      sum(1 for c in term.titlebar.get_child().get_children()
          if getattr(c, '_ai_glance_git_button', False)) == 1)

branch = subprocess.run(['git', '-C', CWD, 'branch', '--show-current'],
                        capture_output=True, text=True).stdout.strip()
paneds_before = count_paned(window)
gtk_git_panel.toggle(term)
panels = panel_of(term)
check('toggle opens one GitPanel', len(panels) == 1)
children = term.get_children()
check('panel directly after titlebar',
      len(panels) == 1 and children.index(panels[0]) == children.index(term.titlebar) + 1)
check('panel shows branch row',
      bool(branch) and pump(5, lambda: bool(panels) and branch in labels_text(panels[0])))
check('no omp on this pane: no ◆ marker anywhere', '◆' not in labels_text(panels[0]))
labels_before = [w for w in descendants(panels[0]) if isinstance(w, Gtk.Label)]
panels[0].refresh()
pump(5, lambda: not panels[0]._in_flight)
labels_after = [w for w in descendants(panels[0]) if isinstance(w, Gtk.Label)]
check('unchanged refresh keeps the same label widgets',
      bool(labels_before) and labels_before == labels_after)
check('vte still descendant of term', term.vte in list(descendants(term)))
check('no new Paned', count_paned(window) == paneds_before)
if len(sys.argv) > 1 and shutil.which('import'):
    pump(0.5)
    subprocess.run(['import', '-window', 'root', sys.argv[1]])
    print('screenshot ->', sys.argv[1])

gtk_git_panel.toggle(term)
check('second toggle removes panel', not panel_of(term))
check('closed panel: _closed and timer removed',
      panels[0]._closed and panels[0]._timer is None)

# stale result discard: hold the worker, close the panel, release
import threading  # noqa: E402
from ai_glance import gitinfo  # noqa: E402
gate = threading.Event()
started = threading.Event()
real_build = gitinfo.build_model


def slow_build(*a, **k):
    started.set()
    gate.wait(10)
    return real_build(*a, **k)


gitinfo.build_model = slow_build
gtk_git_panel.toggle(term)
stale = panel_of(term)[0]
renders = []
stale._render = lambda *a, **k: renders.append(a)
check('stale: worker started and in flight', started.wait(5) and stale._in_flight)
gtk_git_panel.toggle(term)  # close while in flight
gate.set()
pump(2, lambda: not stale._in_flight)
gitinfo.build_model = real_build
check('stale: result dropped (no render after close)', not renders and not stale._in_flight)

term.key_split_vert()
pump(1.0)
terms = list(terminator.terminals)
check('split: two terminals', len(terms) == 2)
check('split: every terminal has button', all(has_button(t) for t in terms))

other = [t for t in terms if t is not term][0]
other.spawn_child() if other.pid is None else None
pump(0.5)
gtk_git_panel.toggle(other)
pump(0.1)
other_panel = panel_of(other)[0]
other.close()  # refresh in flight
pump(5)
check('close with refresh in flight: panel shut down',
      other_panel._closed and other_panel._timer is None)

survivor = [t for t in terminator.terminals if t is not other]
victim = survivor[0] if survivor else term
victim.config['exit_action'] = 'hold'  # keep the dead pane (default closes it)
victim.reconfigure()
if victim.pid:
    os.kill(victim.pid, signal.SIGKILL)
    pump(0.5)
gtk_git_panel.toggle(victim)
pump(2)
vpanels = panel_of(victim)
vtext = labels_text(vpanels[0]) if vpanels else ''
check('killed child: panel rendered n/a or real content',
      len(vpanels) == 1 and 'Loading' not in vtext and
      ('n/a' in vtext or 'repo' in vtext or 'Not a git repository' in vtext))
pump(4)

os.dup2(_real_stderr, 2)
_err = Path(_err_file.name).read_text()
for line in _err.splitlines():
    if not (line.startswith('ConfigBase::load') or 'etc/xdg/terminator' in line or not line.strip()):
        print('stderr:', line)
check('stderr clean (no Gtk-CRITICAL/WARNING/Traceback)',
      not any(k in _err for k in ('Gtk-CRITICAL', 'Gtk-WARNING', 'Traceback', 'GLib-')))
print('FAILURES: %s' % failures if failures else 'ALL PASS')
sys.stdout.flush()
os._exit(1 if failures else 0)
