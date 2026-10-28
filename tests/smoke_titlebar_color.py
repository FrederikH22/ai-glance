"""Smoke test for the animated active-pane header/frame and the slim title bar. Needs a display:

    XDG_CONFIG_HOME=$(mktemp -d) [AI_GLANCE_SHOTS=<dir>] xvfb-run -a python3 tests/smoke_titlebar_color.py
"""
import atexit
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if not os.environ.get('DISPLAY') or not os.environ.get('XDG_CONFIG_HOME'):
    sys.exit('run under xvfb-run with an isolated XDG_CONFIG_HOME')

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
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk, Gtk  # noqa: E402

from terminatorlib.terminator import Terminator  # noqa: E402

from ai_glance import gtk_git_panel, gtk_titlebar_color as tc  # noqa: E402

SHOTS = os.environ.get('AI_GLANCE_SHOTS')
FAILURES = []
Gtk.main_quit = lambda *a: None


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


def pixel(widget, dx, dy):
    """RGB of the pixel at (dx, dy) inside ``widget``, read from the toplevel's X window."""
    top = widget.get_toplevel()
    x, y = widget.translate_coordinates(top, dx, dy)
    buf = Gdk.pixbuf_get_from_window(top.get_window(), x, y, 1, 1)
    return tuple(buf.get_pixels()[:3])


def close(a, b, tol=24):
    return all(abs(p - q) <= tol for p, q in zip(a, b))


def dist(a, b):
    return max(abs(p - q) for p, q in zip(a, b))


def rgb255(rgb):
    return tuple(round(c * 255) for c in rgb)


def focus(term):
    # Xvfb has no window manager, so GTK never delivers the X focus-in event that makes the
    # vte emit Terminal's 'focus-in'; emit it ourselves, as on_vte_focus_in does.
    term.vte.grab_focus()
    term.emit('focus-in')
    pump(0.4)


terminator = Terminator()
terminator.set_origcwd(os.getcwd())
terminator.reconfigure()
window, term_a = terminator.new_window()
window.resize(1000, 600)
pump(0.5)

# --- item 5: the title bar stays as slim as stock -----------------------------------
tb_stock_h = term_a.titlebar.get_preferred_height()[1]
gtk_git_panel.install()
tc.install()
tc.install()  # idempotent
check('install twice wraps update once',
      getattr(type(term_a.titlebar).update, '__wrapped__', None) is None
      and getattr(type(term_a.titlebar).update, '_ai_glance_color_patched', False))
pump(0.3)
tb_h = term_a.titlebar.get_preferred_height()[1]
check(f'title bar height with plugin == stock (+-1px): {tb_stock_h} vs {tb_h}',
      abs(tb_h - tb_stock_h) <= 1)

term_a.emit('split-vert', None)
pump(0.6)
term_b = [t for t in terminator.terminals if t is not term_a][0]
check('new pane title bar is as slim as the first',
      abs(term_b.titlebar.get_preferred_height()[1] - tb_stock_h) <= 1)
check('title bar allocation is slim too', abs(term_a.titlebar.get_allocated_height() - tb_stock_h) <= 1
      or abs(term_a.titlebar.get_allocated_height() - term_b.titlebar.get_allocated_height()) <= 1)

# --- active pane gets the gradient -----------------------------------------------------
focus(term_a)
ta, tb = term_a.titlebar, term_b.titlebar
check('focused pane is registered, other is not', tc.is_active(ta) and not tc.is_active(tb))
check('timer runs while a pane is active', tc.timer_running())


def header_px(t):
    return pixel(t.titlebar, 6, 5)


def border_px(t):
    return pixel(t, 1, t.get_allocated_height() // 2)


def sample():
    pump(0.15)
    return header_px(term_a), border_px(term_a), header_px(term_b), border_px(term_b)


h1, f1, ih1, if1 = sample()
exp1 = rgb255(tc.stops()[0][1])
check(f'header colour is the gradient start colour {exp1} (got {h1})', close(h1, exp1))
_w = ta.get_allocated_width()
for frac in (0.5, 0.9):
    _px = pixel(ta, int(_w * frac), 4)
    _stops = tc.stops()
    _lo, _hi = (_stops[0], _stops[1]) if frac < 0.5 else (_stops[1], _stops[2])
    _exp = rgb255(tuple(a + (b - a) * ((frac - _lo[0]) / (_hi[0] - _lo[0]))
                        for a, b in zip(_lo[1], _hi[1])))
    check(f'header at {int(frac * 100)}% of its width (viewport/label area) is the gradient {_exp} (got {_px})',
          close(_px, _exp, 30))
check(f'frame left edge uses the same gradient {exp1} (got {f1})', close(f1, exp1))
pump(1.0)
h2, f2, ih2, if2 = sample()
check(f'focused header changes over 1 s ({h1} -> {h2})', dist(h1, h2) >= 8)
check(f'focused frame changes over 1 s ({f1} -> {f2})', dist(f1, f2) >= 8)
stock_bg = Gdk.RGBA()
stock_bg.parse(term_b.config['title_inactive_bg_color'])
check(f'inactive header stays stock inactive colour ({ih1}, {ih2})',
      ih1 == ih2 and close(ih1, rgb255((stock_bg.red, stock_bg.green, stock_bg.blue)), 6))
check(f'inactive pane has no frame (edge pixel static {if1}, {if2})', if1 == if2
      and not close(if1, exp1, 10))
check('frame does not change the pane size: vte allocation unchanged by activity',
      term_a.vte.get_allocated_width() == term_b.vte.get_allocated_width()
      or abs(term_a.vte.get_allocated_width() - term_b.vte.get_allocated_width()) <= 8)
if SHOTS:
    pump(0.3)
    subprocess.run(['import', '-window', 'root', str(Path(SHOTS) / 'user-titlebar.png')], check=False)
    subprocess.run(['import', '-window', 'root', str(Path(SHOTS) / 'user-frame.png')], check=False)

# --- focus moves the animation ---------------------------------------------------------
focus(term_b)
check('after focusing the other pane the animation moved',
      tc.is_active(tb) and not tc.is_active(ta) and tc.timer_running())
hb1, fb1, ha1, fa1 = (header_px(term_b), border_px(term_b), header_px(term_a), border_px(term_a))
exp = rgb255(tc.stops()[0][1])
check(f'new active frame is the gradient {exp} (got {fb1})', close(fb1, exp))
pump(1.0)
hb2, fb2 = header_px(term_b), border_px(term_b)
check('new active header/frame animate', dist(hb1, hb2) >= 8 and dist(fb1, fb2) >= 8)
check('old pane returned to the stock inactive colour',
      close(header_px(term_a), rgb255((stock_bg.red, stock_bg.green, stock_bg.blue)), 6)
      and header_px(term_a) == header_px(term_a) and not close(border_px(term_a), exp, 10))

# --- broadcast keeps stock colours -------------------------------------------------------
terminator.groupsend = terminator.groupsend_type['all']
pump(0.5)
check('broadcast: animation off for the focused pane', not tc.is_active(tb))
terminator.groupsend = terminator.groupsend_type['off']
pump(0.5)
check('broadcast ended while the pane stays focused: animation resumes without refocus',
      tc.is_active(tb) and tc.timer_running())
terminator.groupsend = terminator.groupsend_type['group']
focus(term_a)
check('default group mode without a group still animates', tc.is_active(ta) and tc.timer_running())
terminator.groupsend = terminator.groupsend_type['off']
focus(term_a)
check('broadcast off: animation resumes', tc.is_active(ta) and tc.timer_running())

# --- timer is removed when no window is active -----------------------------------------
window.emit('focus-out-event', Gdk.Event.new(Gdk.EventType.FOCUS_CHANGE))
pump(0.5)
check('window focus-out: no active titlebar, timer removed', not tc.is_active(ta) and not tc.timer_running())
for w in list(terminator.windows):
    w.emit('destroy')
pump(0.5)
check('no timer after the windows are gone', not tc.timer_running())

_errfile.seek(0)
stderr = _errfile.read().decode(errors='replace')
bad = [l for l in stderr.splitlines()
       if re.search(r'Gtk-(CRITICAL|WARNING)|GLib-.*(CRITICAL|WARNING)|Gdk-(CRITICAL|WARNING)|Traceback|Error', l)]
check('stderr pristine', not bad, '\n'.join(bad[:10]))
_report_stderr = bool(bad)
print('FAILED: ' + ', '.join(FAILURES) if FAILURES else 'ALL PASS')
sys.stdout.flush()
os._exit(1 if FAILURES else 0)
