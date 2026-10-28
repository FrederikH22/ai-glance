"""Animated gradient header and frame for the active Terminator pane.

``Titlebar.update`` is wrapped: when the titlebar is in the "active terminal"
branch (``other`` is its own terminal) and no broadcast is on, it is registered
here. Registered titlebars paint a horizontal two-hue gradient (hues 120 degrees
apart, base hue rotating once every ``PERIOD`` seconds) in a ``draw`` handler
that runs before the titlebar's children, and their ``Terminal`` paints a 3 px
frame with the same gradient in a ``draw`` handler that runs after its children
(inside its own allocation, so it never touches neighbouring panes). One module
level 100 ms timer queues redraws; it only exists while a registered titlebar's
window has toplevel focus (a focused pane paused by broadcast keeps a cheap
check running and resumes when broadcast ends). Broadcasting (all, or group with the pane in a group) keeps stock colours. The colour is a pure function of ``time.monotonic()``
so painting needs no per-widget state. Any other ``update`` state unregisters
the titlebar and the stock colours (set by the original ``update``) apply.
"""
import colorsys
import time

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from terminatorlib.util import dbg  # noqa: E402

PERIOD = 12.0
SPREAD = 120.0
SATURATION = 0.70
VALUE = 0.62
FRAME_WIDTH = 3
TICK_MS = 100
_PATCH_ATTR = '_ai_glance_color_patched'
_ACTIVE = '_ai_glance_color_active'
_HOOKED = '_ai_glance_color_hooked'

_registered = set()
_waiting = set()  # focused panes whose animation is paused by broadcast
_timer = None


def hue_rgb(degrees):
    return colorsys.hsv_to_rgb((degrees % 360.0) / 360.0, SATURATION, VALUE)


def base_hue(now=None):
    now = time.monotonic() if now is None else now
    return (now % PERIOD) / PERIOD * 360.0


def stops(now=None):
    """(offset, (r, g, b)) gradient stops left to right, 0..1 floats."""
    h = base_hue(now)
    return [(0.0, hue_rgb(h)), (0.5, hue_rgb(h + SPREAD / 2)), (1.0, hue_rgb(h + SPREAD))]


def _gradient(width, now=None):
    import cairo
    pattern = cairo.LinearGradient(0, 0, max(width, 1), 0)
    for offset, (r, g, b) in stops(now):
        pattern.add_color_stop_rgb(offset, r, g, b)
    return pattern


def is_active(titlebar):
    return bool(titlebar.__dict__.get(_ACTIVE))


def _paint_header(widget, cr, titlebar):
    """Paint the titlebar-wide gradient into ``widget`` (the titlebar or a child with
    its own GdkWindow: the viewport, the editable label, the group box)."""
    try:
        if is_active(titlebar):
            width = titlebar.get_allocated_width()
            ox, oy = widget.translate_coordinates(titlebar, 0, 0) if widget is not titlebar \
                else (0, 0)
            alloc = widget.get_allocation()
            cr.save()
            cr.translate(-ox, -oy)
            cr.set_source(_gradient(width))
            cr.rectangle(ox, oy, alloc.width, alloc.height)
            cr.fill()
            cr.restore()
    except Exception as exc:  # never raise into GTK
        dbg(f'ai-glance titlebar colour: header draw failed: {exc!r}')
    return False


def _paint_frame(terminal, cr):
    try:
        titlebar = getattr(terminal, 'titlebar', None)
        if titlebar is not None and is_active(titlebar):
            alloc = terminal.get_allocation()
            half = FRAME_WIDTH / 2.0
            cr.set_source(_gradient(alloc.width))
            cr.set_line_width(FRAME_WIDTH)
            cr.rectangle(half, half, alloc.width - FRAME_WIDTH, alloc.height - FRAME_WIDTH)
            cr.stroke()
    except Exception as exc:
        dbg(f'ai-glance titlebar colour: frame draw failed: {exc!r}')
    return False


def _transparent():
    return Gdk.RGBA(0, 0, 0, 0)


def _on_destroy(titlebar):
    _waiting.discard(titlebar)
    _unregister(titlebar, restore=False)


def _hook(titlebar):
    if titlebar.__dict__.get(_HOOKED):
        return
    titlebar.__dict__[_HOOKED] = True
    # Children with their own GdkWindow would otherwise show the stock colour.
    painters = {titlebar, titlebar.ebox, titlebar.label}
    parent = titlebar.label.get_parent()
    if parent is not None and parent is not titlebar:
        painters.add(parent)  # the Gtk.Viewport around the title label
    for widget in painters:
        widget.connect('draw', _paint_header, titlebar)
    titlebar.connect('destroy', _on_destroy)
    terminal = titlebar.terminal
    if terminal is not None:
        terminal.connect_after('draw', _paint_frame)


def _broadcasting(titlebar):
    """True when keystrokes really go to other panes (stock cue must stay visible).

    Terminator's default ``broadcast_default`` is 'group', which only broadcasts
    for a pane that is in a group, so 'group' alone must not disable the animation.
    """
    terminator = titlebar.terminator
    kinds = terminator.groupsend_type
    if terminator.groupsend == kinds['all']:
        return True
    return terminator.groupsend == kinds['group'] and bool(titlebar.terminal.group)


def _register(titlebar):
    _hook(titlebar)
    titlebar.__dict__[_ACTIVE] = True
    # Every original update repaints the stock transmit colours; the gradient is
    # painted by our draw handlers, so make the stock backgrounds clear again.
    titlebar.override_background_color(Gtk.StateFlags.NORMAL, _transparent())
    titlebar.ebox.override_background_color(Gtk.StateFlags.NORMAL, _transparent())
    _registered.add(titlebar)
    _queue(titlebar)
    _ensure_timer()


def _unregister(titlebar, restore=True):
    _registered.discard(titlebar)
    if titlebar.__dict__.pop(_ACTIVE, False):
        titlebar.queue_draw()
        terminal = titlebar.terminal
        if terminal is not None:
            terminal.queue_draw()
        if restore:
            try:
                titlebar.update(terminal)
            except Exception as exc:
                dbg(f'ai-glance titlebar colour: restore failed: {exc!r}')


def _queue(titlebar):
    """Invalidate only what the gradient covers: the header and the four frame strips
    (all in the Terminal's coordinates); the VTE interior is not repainted."""
    terminal = titlebar.terminal
    if terminal is None:
        titlebar.queue_draw()
        return
    width, height = terminal.get_allocated_width(), terminal.get_allocated_height()
    pos = titlebar.translate_coordinates(terminal, 0, 0)
    rects = [(0, 0, width, FRAME_WIDTH), (0, height - FRAME_WIDTH, width, FRAME_WIDTH),
             (0, 0, FRAME_WIDTH, height), (width - FRAME_WIDTH, 0, FRAME_WIDTH, height)]
    if pos is not None:
        rects.append((pos[0], pos[1], titlebar.get_allocated_width(),
                      titlebar.get_allocated_height()))
    for rect in rects:
        terminal.queue_draw_area(*rect)


def _window_active(titlebar):
    window = titlebar.get_toplevel()
    return isinstance(window, Gtk.Window) and window.is_active()


def _pause(titlebar):
    """Broadcast started while this pane stays focused: stock colours, resume later."""
    _unregister(titlebar, restore=False)
    _waiting.add(titlebar)
    _ensure_timer()


def _resume_waiting():
    for titlebar in list(_waiting):
        try:
            if titlebar.get_parent() is None or not _window_active(titlebar):
                _waiting.discard(titlebar)
            elif not _broadcasting(titlebar):
                _waiting.discard(titlebar)
                titlebar.update(titlebar.terminal)  # registers again via the hook
        except Exception as exc:
            _waiting.discard(titlebar)
            dbg(f'ai-glance titlebar colour: resume failed: {exc!r}')


def _tick():
    global _timer
    try:
        for titlebar in list(_registered):
            if titlebar.get_parent() is None:
                _unregister(titlebar, restore=False)
            elif _broadcasting(titlebar):
                titlebar.update(titlebar.terminal)  # stock colours; the hook pauses it
        _resume_waiting()
        live = [t for t in _registered if _window_active(t)]
        for titlebar in live:
            _queue(titlebar)
        if live or _waiting:
            return True
    except Exception as exc:
        dbg(f'ai-glance titlebar colour: tick failed: {exc!r}')
    _timer = None
    return False


def _ensure_timer():
    global _timer
    if _timer is None:
        _timer = GLib.timeout_add(TICK_MS, _tick)


def timer_running():
    return _timer is not None


def _after_update(titlebar, other):
    if not other or isinstance(other, str):
        if other:  # 'window-focus-out' and friends: stock inactive colours
            _waiting.discard(titlebar)
            _unregister(titlebar, restore=False)
        return
    if other is titlebar.terminal:
        if _broadcasting(titlebar):
            _pause(titlebar)
        else:
            _waiting.discard(titlebar)
            _register(titlebar)
    else:
        _waiting.discard(titlebar)
        _unregister(titlebar, restore=False)


def install():
    """Wrap ``Titlebar.update`` (idempotent) and pick up the focused pane."""
    from terminatorlib.titlebar import Titlebar
    from terminatorlib.terminator import Terminator
    if not getattr(Titlebar.update, _PATCH_ATTR, False):
        original = Titlebar.update

        def update(self, other=None):
            result = original(self, other)
            try:
                _after_update(self, other)
            except Exception as exc:
                dbg(f'ai-glance titlebar colour: update hook failed: {exc!r}')
            return result

        setattr(update, _PATCH_ATTR, True)
        Titlebar.update = update
    try:
        for window in list(Terminator().windows):
            if not window.is_active():
                continue
            focused = window.get_focussed_terminal()
            if focused is not None:
                focused.titlebar.update(focused)
    except Exception as exc:
        dbg(f'ai-glance titlebar colour: retrofit failed: {exc!r}')
