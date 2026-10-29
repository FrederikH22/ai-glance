"""Colour the headers of unfocused panes by the state of the coding agent (omp or
Claude Code) running in them.

- agent working (turn running, tool in flight, subagents/background agents running):
  pastel green
- agent waiting for the user (turn over, ``ask``/permission prompt open): pulsing orange
- no live agent in the pane: stock colours, untouched

The focused pane keeps ``gtk_titlebar_color``'s gradient; this module never paints
an active titlebar. States come from ``ompsessions.pane_agent_state``, polled every
``POLL_SECONDS`` in one worker thread for all panes and applied on the GTK main
thread. Painting uses ``draw`` handlers that run before the titlebar's own
background; while a state colour shows, the stock background is made transparent
(and is restored by re-running the stock ``Titlebar.update`` when the state ends).
"""
import math
import threading
import time

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from terminatorlib.util import dbg  # noqa: E402

from . import gtk_titlebar_color, ompsessions  # noqa: E402

POLL_SECONDS = 2
PULSE_MS = 80
PULSE_PERIOD = 1.0
WORKING_RGB = (0.66, 0.88, 0.64)      # pastel green  #a8e0a3
INPUT_DIM_RGB = (1.00, 0.80, 0.55)    # light orange  #ffcc8c
INPUT_BRIGHT_RGB = (1.00, 0.55, 0.10)  # strong orange #ff8c1a

_STATE = '_ai_glance_omp_state'
_HOOKED = '_ai_glance_omp_state_hooked'
_PATCH_ATTR = '_ai_glance_omp_state_patched'

_stateful = set()      # titlebars that currently show a state colour
_pulse_timer = None
_poll_timer = None
_poll_in_flight = False


def state_rgb(state, now=None):
    """Header colour for ``state`` at ``now`` (monotonic seconds); None = stock."""
    if state == ompsessions.WORKING:
        return WORKING_RGB
    if state == ompsessions.NEEDS_INPUT:
        now = time.monotonic() if now is None else now
        t = 0.5 - 0.5 * math.cos(2 * math.pi * (now % PULSE_PERIOD) / PULSE_PERIOD)
        return tuple(d + (b - d) * t for d, b in zip(INPUT_DIM_RGB, INPUT_BRIGHT_RGB))
    return None


def state_of(titlebar):
    return titlebar.__dict__.get(_STATE)


def _shows_state(titlebar):
    return state_of(titlebar) is not None and not gtk_titlebar_color.is_active(titlebar)


def _paint(widget, cr, titlebar):
    try:
        if _shows_state(titlebar):
            rgb = state_rgb(state_of(titlebar))
            alloc = widget.get_allocation()
            cr.set_source_rgb(*rgb)
            cr.rectangle(0, 0, alloc.width, alloc.height)
            cr.fill()
    except Exception as exc:  # never raise into GTK
        dbg(f'ai-glance omp state: draw failed: {exc!r}')
    return False


def _hook(titlebar):
    if titlebar.__dict__.get(_HOOKED):
        return
    titlebar.__dict__[_HOOKED] = True
    painters = {titlebar, titlebar.ebox, titlebar.label}
    parent = titlebar.label.get_parent()
    if parent is not None and parent is not titlebar:
        painters.add(parent)  # the Gtk.Viewport around the title label
    for widget in painters:
        widget.connect('draw', _paint, titlebar)
    titlebar.connect('destroy', lambda tb: _stateful.discard(tb))


def _clear_stock_background(titlebar):
    clear = Gdk.RGBA(0, 0, 0, 0)
    titlebar.override_background_color(Gtk.StateFlags.NORMAL, clear)
    titlebar.ebox.override_background_color(Gtk.StateFlags.NORMAL, clear)
    titlebar.queue_draw()


def _restore_stock(titlebar):
    """Re-run the stock update so Terminator repaints its own colours."""
    window = titlebar.get_toplevel()
    other = None
    if isinstance(window, Gtk.Window) and window.is_active() \
            and hasattr(window, 'get_focussed_terminal'):
        other = window.get_focussed_terminal()
    titlebar.update(other or 'window-focus-out')
    titlebar.queue_draw()


def set_state(titlebar, state):
    """Apply ``state`` (WORKING, NEEDS_INPUT or None) to one titlebar (GTK thread)."""
    if state_of(titlebar) == state:
        return
    _hook(titlebar)
    titlebar.__dict__[_STATE] = state
    if state is None:
        _stateful.discard(titlebar)
        _restore_stock(titlebar)
    else:
        _stateful.add(titlebar)
        if not gtk_titlebar_color.is_active(titlebar):
            _clear_stock_background(titlebar)
        _ensure_pulse()


def _pulse():
    global _pulse_timer
    try:
        pulsing = [t for t in list(_stateful)
                   if t.get_parent() is not None and state_of(t) == ompsessions.NEEDS_INPUT
                   and not gtk_titlebar_color.is_active(t)]
        for titlebar in pulsing:
            titlebar.queue_draw()
        if pulsing:
            return True
    except Exception as exc:
        dbg(f'ai-glance omp state: pulse failed: {exc!r}')
    _pulse_timer = None
    return False


def _ensure_pulse():
    global _pulse_timer
    if _pulse_timer is None and any(state_of(t) == ompsessions.NEEDS_INPUT for t in _stateful):
        _pulse_timer = GLib.timeout_add(PULSE_MS, _pulse)


def pulse_running():
    return _pulse_timer is not None


def _poll():
    """GTK thread: snapshot (titlebar, shell pid) pairs and read states in a worker."""
    global _poll_in_flight
    try:
        if _poll_in_flight:
            return True
        from terminatorlib.terminator import Terminator
        panes = [(t.titlebar, t.pid) for t in list(Terminator().terminals)
                 if getattr(t, 'titlebar', None) is not None]
        if not panes and not _stateful:
            return True
        _poll_in_flight = True
        threading.Thread(target=_read_states, args=(panes,), daemon=True,
                         name='ai-glance-omp-state').start()
    except Exception as exc:
        _poll_in_flight = False
        dbg(f'ai-glance omp state: poll failed: {exc!r}')
    return True


def _read_states(panes):
    results = [(titlebar, ompsessions.pane_agent_state(pid)) for titlebar, pid in panes]
    GLib.idle_add(_apply_states, results)


def _apply_states(results):
    global _poll_in_flight
    _poll_in_flight = False
    seen = set()
    for titlebar, state in results:
        try:
            if titlebar.get_parent() is None:
                continue
            seen.add(titlebar)
            set_state(titlebar, state)
        except Exception as exc:
            dbg(f'ai-glance omp state: apply failed: {exc!r}')
    for titlebar in list(_stateful - seen):  # pane gone or no longer listed
        _stateful.discard(titlebar)
    return False


def poll_now():
    """Run one poll immediately (used by install and the smoke test)."""
    _poll()


def install():
    """Wrap ``Titlebar.update`` (idempotent) and start the state poller."""
    global _poll_timer
    from terminatorlib.titlebar import Titlebar
    if not getattr(Titlebar.update, _PATCH_ATTR, False):
        original = Titlebar.update

        def update(self, other=None):
            result = original(self, other)
            try:
                # The stock update just repainted its background; keep ours visible.
                if _shows_state(self):
                    _clear_stock_background(self)
                    _ensure_pulse()
            except Exception as exc:
                dbg(f'ai-glance omp state: update hook failed: {exc!r}')
            return result

        setattr(update, _PATCH_ATTR, True)
        Titlebar.update = update
    if _poll_timer is None:
        _poll_timer = GLib.timeout_add_seconds(POLL_SECONDS, _poll)
        GLib.idle_add(lambda: (_poll(), False)[1])
