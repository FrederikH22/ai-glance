"""Smoke test for omp-state header colours on unfocused panes. Needs a display:

    XDG_CONFIG_HOME=$(mktemp -d) [AI_GLANCE_SHOTS=<dir>] xvfb-run -a python3 tests/smoke_omp_state.py
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

from ai_glance import gtk_omp_state as st, gtk_titlebar_color as tc, ompsessions  # noqa: E402

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
    top = widget.get_toplevel()
    x, y = widget.translate_coordinates(top, dx, dy)
    buf = Gdk.pixbuf_get_from_window(top.get_window(), x, y, 1, 1)
    return tuple(buf.get_pixels()[:3])


def header(term):
    # Right end of the title bar: plain background (no group icon, no text).
    return pixel(term.titlebar, term.titlebar.get_allocated_width() - 4, 3)


def close(a, b, tol=24):
    return all(abs(p - q) <= tol for p, q in zip(a, b))


def rgb255(rgb):
    return tuple(round(c * 255) for c in rgb)


def focus(term):
    term.vte.grab_focus()
    term.emit('focus-in')
    pump(0.4)


# Fake omp states per shell pid: no real omp runs inside Xvfb.
STATES = {}
ompsessions.pane_omp_state = lambda pid, *a, **k: STATES.get(pid)


def poll():
    st.poll_now()
    pump(0.6)


terminator = Terminator()
terminator.set_origcwd(os.getcwd())
terminator.reconfigure()
window, term_a = terminator.new_window()
window.resize(1200, 600)
pump(0.5)
tc.install()
st.install()
st.install()  # idempotent
term_a.emit('split-vert', None)
pump(0.5)
term_b = [t for t in terminator.terminals if t is not term_a][0]
term_b.emit('split-vert', None)
pump(0.5)
term_c = [t for t in terminator.terminals if t not in (term_a, term_b)][0]
focus(term_a)

stock = Gdk.RGBA()
stock.parse(term_b.config['title_inactive_bg_color'])
STOCK = rgb255((stock.red, stock.green, stock.blue))
check(f'before any state: unfocused header is stock {STOCK} (got {header(term_b)})',
      close(header(term_b), STOCK, 6))

STATES.update({term_a.pid: ompsessions.NEEDS_INPUT, term_b.pid: ompsessions.WORKING,
               term_c.pid: ompsessions.NEEDS_INPUT})
poll()
GREEN = rgb255(st.WORKING_RGB)
check(f'working pane header is pastel green {GREEN} (got {header(term_b)})',
      close(header(term_b), GREEN, 8))
check('focused pane keeps the gradient, not a state colour',
      tc.is_active(term_a.titlebar) and close(header(term_a), rgb255(tc.stops()[-1][1]), 30),
      f'{header(term_a)} vs {rgb255(tc.stops()[-1][1])}')
check('pulse timer runs for an unfocused pane needing input', st.pulse_running())
samples = []
for _ in range(8):
    pump(0.2)
    samples.append(header(term_c))
spread = max(max(s[i] for s in samples) - min(s[i] for s in samples) for i in range(3))
check(f'input pane pulses (channel spread {spread} over 1.6 s)', spread >= 40)
check('input pane is orange (red channel high, blue low)',
      all(s[0] >= 230 and s[2] <= 170 for s in samples), str(samples[:3]))
if SHOTS:
    subprocess.run(['import', '-window', 'root', str(Path(SHOTS) / 'omp-state.png')], check=False)

# Focus moves: the state colour follows the pane, the gradient the focus.
focus(term_b)
check('focused working pane shows the gradient', tc.is_active(term_b.titlebar)
      and not close(header(term_b), GREEN, 8))
check(f'previously focused pane (needs input) now pulses orange (got {header(term_a)})',
      header(term_a)[0] >= 230 and header(term_a)[2] <= 170)
focus(term_a)
check(f'unfocused working pane is green again after a focus change (got {header(term_b)})',
      close(header(term_b), GREEN, 8))

# State ends: stock colours come back and the pulse stops.
STATES.clear()
poll()
pump(0.3)
check(f'no omp: unfocused headers back to stock (got {header(term_b)}, {header(term_c)})',
      close(header(term_b), STOCK, 6) and close(header(term_c), STOCK, 6))
check('pulse timer stopped', not st.pulse_running())

for w in list(terminator.windows):
    w.emit('destroy')
pump(0.5)

_errfile.seek(0)
stderr = _errfile.read().decode(errors='replace')
bad = [l for l in stderr.splitlines()
       if re.search(r'Gtk-(CRITICAL|WARNING)|GLib-.*(CRITICAL|WARNING)|Gdk-(CRITICAL|WARNING)|Traceback|Error', l)]
check('stderr pristine', not bad, '\n'.join(bad[:10]))
_report_stderr = bool(bad)
print('FAILED: ' + ', '.join(FAILURES) if FAILURES else 'ALL PASS')
sys.stdout.flush()
os._exit(1 if FAILURES else 0)
