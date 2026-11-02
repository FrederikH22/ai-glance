"""Smoke test: the window title follows the focused pane only. Needs a display:

    XDG_CONFIG_HOME=$(mktemp -d) xvfb-run -a python3 tests/smoke_window_title.py
"""
import atexit
import os
import re
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
from gi.repository import Gtk  # noqa: E402

from terminatorlib.terminator import Terminator  # noqa: E402

from ai_glance import gtk_window_title  # noqa: E402

FAILURES = []
Gtk.main_quit = lambda *a: None


def check(name, ok, detail=''):
    print(('PASS' if ok else 'FAIL') + ': ' + name + (f' ({detail})' if detail and not ok else ''))
    if not ok:
        FAILURES.append(name)


def pump(seconds=0.2):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(0.01)


def set_term_title(term, text):
    term.vte.feed(f'\x1b]0;{text}\x07'.encode())
    pump(0.3)


def focus(term):
    # Xvfb has no window manager: deliver the focus-in the vte would get from X.
    term.vte.grab_focus()
    term.on_vte_focus_in(term.vte, None)
    pump(0.3)


terminator = Terminator()
terminator.set_origcwd(os.getcwd())
terminator.reconfigure()
window, term_a = terminator.new_window()
pump(0.5)
gtk_window_title.install()
gtk_window_title.install()  # idempotent
term_a.emit('split-vert', None)
pump(0.5)
term_b = [t for t in terminator.terminals if t is not term_a][0]

focus(term_a)
set_term_title(term_a, 'A: focused')
check(f'focused pane sets the window title ({window.get_title()!r})', window.get_title() == 'A: focused')

for frame in '\u280b\u2819\u2839\u2838':  # an omp spinner in the unfocused pane
    set_term_title(term_b, f'\u03c0 {frame} B: working')
    check(f'unfocused pane does not take the window title ({window.get_title()!r})',
          window.get_title() == 'A: focused')

set_term_title(term_a, 'A: renamed')
check('focused pane title changes still show', window.get_title() == 'A: renamed')

focus(term_b)
check(f'focusing the other pane shows its title ({window.get_title()!r})',
      window.get_title().endswith('B: working'))
set_term_title(term_a, 'A: in background')
check('the now unfocused pane no longer changes it', window.get_title().endswith('B: working'))

for w in list(terminator.windows):
    w.emit('destroy')
pump(0.3)

_errfile.seek(0)
stderr = _errfile.read().decode(errors='replace')
bad = [l for l in stderr.splitlines()
       if re.search(r'Gtk-(CRITICAL|WARNING)|GLib-.*(CRITICAL|WARNING)|Traceback|Error', l)
       and 'xdg/terminator/config' not in l]
check('stderr pristine', not bad, '\n'.join(bad[:10]))
_report_stderr = bool(bad)
print('FAILED: ' + ', '.join(FAILURES) if FAILURES else 'ALL PASS')
sys.stdout.flush()
os._exit(1 if FAILURES else 0)
