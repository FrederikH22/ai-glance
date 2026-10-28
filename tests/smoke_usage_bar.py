"""Smoke test for the per-window usage bar. Needs a display:

    XDG_CONFIG_HOME=$(mktemp -d) xvfb-run -a python3 tests/smoke_usage_bar.py
"""
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

if not os.environ.get('DISPLAY') or not os.environ.get('XDG_CONFIG_HOME'):
    sys.exit('run under xvfb-run with an isolated XDG_CONFIG_HOME')
import atexit  # noqa: E402

# Capture stderr (GTK criticals, tracebacks) to a file for the final check.
_errfile = tempfile.TemporaryFile(mode='w+b')
os.dup2(_errfile.fileno(), 2)
_report_stderr = True


def _dump_stderr():
    if _report_stderr:
        _errfile.seek(0)
        os.write(1, _errfile.read())


atexit.register(_dump_stderr)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gi  # noqa: E402
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk  # noqa: E402

from terminatorlib.factory import Factory  # noqa: E402
from terminatorlib.terminator import Terminator  # noqa: E402

from ai_glance import gtk_usage_bar, usage  # noqa: E402

FAILURES = []
QUIT_CALLS = []
# Terminator quits the main loop when its last window closes; we run no Gtk.main().
_real_main_quit = Gtk.main_quit
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


def bar_of(window):
    return window.__dict__.get('_ai_glance_usage')


def expected_markup():
    """Accounts the bar must show (the 'updated N min ago' text changes with time)."""
    return usage.bar_segments(usage.read_current_usage(usage.DEFAULT_DB, usage.OMP_COMMAND),
                              datetime.now(timezone.utc)).accounts


def bar_text_matches(window, expected):
    bar = bar_of(window)
    return bar is not None and bar.model is not None and bar.model.accounts == expected


terminator = Terminator()
terminator.set_origcwd(os.getcwd())
terminator.reconfigure()
maker = Factory()

window, term = terminator.new_window()
pump(0.5)
child_before = Gtk.Bin.get_child(window)
check('pre-install: Gtk child is the terminal', child_before is term)

gtk_usage_bar.install()
gtk_usage_bar.install()  # idempotent
check('install twice patches once', len(terminator.windows) == 1 and
      type(window).add.__name__ == 'add' and
      getattr(type(window).add, '__wrapped__', None) is not None and
      getattr(type(window).add.__wrapped__, '__wrapped__', None) is None)
pump(0.3)

bar = bar_of(window)
check('window rewrapped', bar is not None)
check('Gtk.Bin.get_child is the wrapper', Gtk.Bin.get_child(window) is bar.wrapper)
check('window.get_child() is the terminal', window.get_child() is term)
check('window.get_children()[0] is the terminal', window.get_children()[0] is term)
check('terminal.get_parent() is the window', term.get_parent() is window)
check('Factory isinstance Window still holds', maker.isinstance(window, 'Window'))
check('bar box is a wrapper child', bar.box.get_parent() is bar.wrapper)

expected = expected_markup()
check('bar model matches bar_segments(read_usage())',
      pump(2.0, lambda: bar_text_matches(window, expected)),
      f'{bar.model!r} != {expected!r}')
check('real DB: at least one account group with a level bar',
      bool(expected) and any(isinstance(w, gtk_usage_bar._LevelBar) for w in descendants(bar.box)))
check(f'bar is compact (<= 28 px): {bar.box.get_allocated_height()}', 0 < bar.box.get_allocated_height() <= 28)
badge = [w for w in descendants(bar.box) if isinstance(w, Gtk.Label)
         and w.get_text() == expected[0].provider_name]
check('provider badge shows the provider name', len(badge) == 1)


def fills_slot(win, b, tol=1):
    child, slot = win.get_child(), b.slot
    ca, sa = child.get_allocation(), slot.get_allocation()
    return (abs(ca.width - sa.width) <= tol and abs(ca.height - sa.height) <= tol
            and sa.width > 1000)


# root child must fill the slot when the window grows (RGBA window: gaps are see-through)
window.resize(1600, 1000)
check('resize: root child fills slot', pump(1.0, lambda: fills_slot(window, bar)),
      f'child={window.get_child().get_allocation().width}x{window.get_child().get_allocation().height}'
      f' slot={bar.slot.get_allocation().width}x{bar.slot.get_allocation().height}')
window.fullscreen()
pump(1.0)
check('fullscreen: root child fills slot',
      (lambda c, s: abs(c.width - s.width) <= 1 and abs(c.height - s.height) <= 1)(
          window.get_child().get_allocation(), bar.slot.get_allocation()))
window.unfullscreen()
window.resize(1600, 1000)
pump(0.5)

# opaque black bar background across the full window width
bg = bar.box.get_style_context().get_background_color(Gtk.StateFlags.NORMAL)
check('bar box is visible and labelled', bar.box.get_visible() and bar.label.get_visible()
      and bar.box.get_style_context().has_class('ai-glance-usage-bar')
      and bar.content.get_parent() is bar.box)
check('bar box background is opaque #000000', (bg.red, bg.green, bg.blue, bg.alpha) == (0, 0, 0, 1),
      str(bg))
check('bar box spans window width', abs(bar.box.get_allocation().width - bar.wrapper.get_allocation().width) <= 1
      and bar.box.get_allocation().width >= 1500, str(bar.box.get_allocation().width))
check('root child fills slot after unfullscreen', fills_slot(window, bar))

# split vert / horiz
term.emit('split-vert', None)
pump(0.5)
check('split vert: 2 terminals', len(window.get_terminals()) == 2)
check('split vert: window child is a container', maker.isinstance(window.get_child(), 'Container'))
term2 = [t for t in window.get_terminals() if t is not term][0]
term2.emit('split-horiz', None)
pump(0.5)
terms = window.get_terminals()
check('split horiz: 3 terminals', len(terms) == 3)
check('bar still present after splits', Gtk.Bin.get_child(window) is bar.wrapper
      and bar.box.get_parent() is bar.wrapper)

# zoom / unzoom
old_child = window.get_child()
window.zoom(term2)
pump(0.3)
check('zoomed: child is the zoomed terminal', window.get_child() is term2 and window.is_zoomed())
check('zoomed: bar present', Gtk.Bin.get_child(window) is bar.wrapper)
check('zoomed: terminal fills slot', pump(0.5, lambda: fills_slot(window, bar)))
window.unzoom()
pump(0.3)
check('unzoomed: layout restored', window.get_child() is old_child and not window.is_zoomed())
check('unzoomed: root child fills slot', pump(0.5, lambda: fills_slot(window, bar)))
check('unzoomed: 3 terminals', len(window.get_terminals()) == 3)

# tabs
known = set(window.get_terminals())
window.tab_new(term)
pump(0.5)
check('tab_new: Notebook is the window child', window.is_child_notebook()
      and maker.isinstance(window.get_child(), 'Notebook'))
check('tab_new: bar present', Gtk.Bin.get_child(window) is bar.wrapper
      and bar.box.get_parent() is bar.wrapper)
check('tab_new: notebook fills slot', pump(0.5, lambda: fills_slot(window, bar)))
check('tab_new: 4 terminals', len(window.get_terminals()) == 4)
newtab_term = [t for t in window.get_terminals() if t not in known][0]
newtab_term.close()
pump(0.5)
check('close tab: 3 terminals remain', len(window.get_terminals()) == 3)
check('close tab: bar present', Gtk.Bin.get_child(window) is bar.wrapper)

# close everything -> window destroyed (hoover)
destroyed = []
window.connect('destroy', lambda *_: destroyed.append(True))
for t in list(window.get_terminals()):
    t.close()
    pump(0.3)
pump(2.0, lambda: bool(destroyed))
check('closing all terminals destroys the window', bool(destroyed))
check('bar marked dead and sources removed', bar.dead and not bar.sources)

# missing DB -> n/a (existing-window path)
window2, _term2 = terminator.new_window()  # created after install: lazy wrap
pump(0.5)
bar2 = bar_of(window2)
check('window created after install gets a bar', bar2 is not None
      and window2.get_child() is _term2)
orig_db, orig_cmd = usage.DEFAULT_DB, usage.OMP_COMMAND
try:
    usage.DEFAULT_DB = Path('/nonexistent/ai-glance/agent.db')
    usage.OMP_COMMAND = None  # no live omp either
    bar2.in_flight = False
    bar2._tick()
    check('missing DB shows "Claude usage: n/a"',
          pump(2.0, lambda: bar2.label.get_label() == 'Claude usage: n/a'),
          repr(bar2.label.get_label()))
    check('n/a gets error class', bar2.label.get_style_context().has_class('error'))
finally:
    usage.DEFAULT_DB, usage.OMP_COMMAND = orig_db, orig_cmd

# destroy while a read is in flight
orig_read = usage.read_current_usage
try:
    def slow_read(path, command):
        time.sleep(0.6)
        return orig_read(path, command)
    usage.read_current_usage = slow_read
    window3, _term3 = terminator.new_window()
    pump(0.2)
    bar3 = bar_of(window3)
    pump(0.1)
    check('read in flight', bar3 is not None and bar3.in_flight)
    window3.emit('destroy')
    pump(1.2)
    check('destroy with read in flight: bar dead, no apply', bar3.dead and bar3.snapshot is None)
finally:
    usage.read_current_usage = orig_read

# screenshots for the report (AI_GLANCE_SHOTS=<dir>)
shots = os.environ.get('AI_GLANCE_SHOTS')
if shots:
    window2.resize(1280, 780)
    bar2.in_flight = False
    bar2._tick()
    window2.get_terminals()[0].emit('split-vert', None)
    pump(1.5, lambda: bar_text_matches(window2, expected))
    window2.show_all()
    pump(1.0)
    os.system(f'import -window root {shots}/user-fullscreen.png')
    os.system(f'import -window root -crop 1280x32+0+748 +repage {shots}/user-bar.png')

for w in list(terminator.windows):
    w.emit('destroy')
pump(0.3)

Gtk.main_quit = _real_main_quit
_errfile.seek(0)
stderr = _errfile.read().decode(errors='replace')
bad = [l for l in stderr.splitlines()
       if re.search(r'Gtk-(CRITICAL|WARNING)|GLib-.*(CRITICAL|WARNING)|Traceback|Error', l)]
check('stderr pristine', not bad, '\n'.join(bad[:10]))
_report_stderr = bool(bad)

print('FAILED: ' + ', '.join(FAILURES) if FAILURES else 'ALL PASS')
sys.exit(1 if FAILURES else 0)
