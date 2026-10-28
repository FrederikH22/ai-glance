"""Per-window Claude usage bar for Terminator.

Each Terminator ``Window`` gets its real GTK child replaced by a wrapper box
``[slot, separator, bar]`` (the bar is an EventBox with an opaque black
background; Terminator windows use an RGBA visual, so unpainted areas would be
see-through). The slot packs its child with expand+fill so it always receives
the whole allocation. The Terminator child (Terminal/Paned/Notebook) lives
in ``slot``; patched ``Window.add/remove/get_child`` keep every Terminator
caller seeing it as the window's only child. ``get_children`` needs no patch:
the original is ``[self.get_child()]``.

Thread rules: ``usage.read_current_usage`` (runs ``omp usage --json``, shared
by all windows, falls back to agent.db) runs in a daemon thread; everything
else (GTK, ``bar_segments``) runs on the GTK main thread via ``GLib.idle_add``.
"""

import contextlib
import functools
import threading
from datetime import datetime, timezone

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import GLib, Gtk  # noqa: E402

from terminatorlib.util import dbg  # noqa: E402

from . import usage  # noqa: E402

REFRESH_SECONDS = 60
RERENDER_SECONDS = 60
_ATTR = '_ai_glance_usage'
_PATCHED = '_ai_glance_usage_patched'
_OWNER = '_ai_glance_slot_owner'
BAR_BACKGROUND = '#000000'
BAR_FOREGROUND = '#d3d7cf'  # neutral text, readable on black
BAR_MUTED = '#8b949e'
TRACK_COLOR = '#30363d'
LEVEL_WIDTH, LEVEL_HEIGHT = 64, 8
BAR_FONT = 'font-size: 110%; font-weight: bold;'


def _rgb(hex_color):
    return tuple(int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))


class _LevelBar(Gtk.DrawingArea):
    """Small rounded horizontal level bar, filled to ``fraction`` in ``color``."""

    def __init__(self, fraction, color):
        Gtk.DrawingArea.__init__(self)
        self.fraction, self.color = fraction, color
        self.set_size_request(LEVEL_WIDTH, LEVEL_HEIGHT)
        self.set_valign(Gtk.Align.CENTER)
        self.connect('draw', self._draw)

    def _draw(self, _widget, cr):
        try:
            import math
            w, h = self.get_allocated_width(), self.get_allocated_height()
            r = h / 2.0

            def pill(width):
                cr.new_sub_path()
                cr.arc(r, r, r, math.pi / 2, 3 * math.pi / 2)
                cr.arc(width - r, r, r, -math.pi / 2, math.pi / 2)
                cr.close_path()

            cr.set_source_rgb(*_rgb(TRACK_COLOR))
            pill(w)
            cr.fill()
            fill = max(self.fraction * w, h if self.fraction > 0 else 0)
            if fill > 0:
                cr.set_source_rgb(*_rgb(self.color))
                pill(min(fill, w))
                cr.fill()
        except Exception as exc:  # never raise into GTK
            dbg(f'ai-glance usage bar: level draw failed: {exc!r}')
        return False


def _style(widget, css):
    provider = Gtk.CssProvider()
    provider.load_from_data(css.encode())
    widget.get_style_context().add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


class _Bar:
    """Wrapper box, slot and label of one window plus its refresh state."""

    def __init__(self, window):
        self.window = window
        self.slot = Gtk.Box()
        self.slot.__dict__[_OWNER] = window
        self.label = Gtk.Label()
        self.box = Gtk.EventBox()
        self.content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.model = None
        self.wrapper = Gtk.VBox()
        self.dead = False
        self.generation = 0
        self.in_flight = False
        self.snapshot = None
        self.sources = []

        self.label.set_xalign(0)
        self.content.set_margin_start(6)
        self.content.set_margin_end(6)
        self.label.set_ellipsize(3)  # Pango.EllipsizeMode.END
        self.label.set_text('Claude usage: \u2026')
        self.label.get_style_context().add_class('ai-glance-usage')
        self.content.set_margin_top(2)
        self.content.set_margin_bottom(2)
        self.content.pack_start(self.label, True, True, 0)
        self.box.add(self.content)
        # Providers are attached to these two widgets only, never to the screen.
        self.box.get_style_context().add_class('ai-glance-usage-bar')
        _style(self.box, f'* {{ background-color: {BAR_BACKGROUND}; }}')
        _style(self.label, f'* {{ color: {BAR_MUTED}; {BAR_FONT} }}')
        self.wrapper.pack_start(self.slot, True, True, 0)
        self.wrapper.pack_start(Gtk.Separator(), False, False, 0)
        self.wrapper.pack_start(self.box, False, False, 0)
        self.slot.show()
        self.wrapper.get_children()[1].show()
        self.label.show()
        self.content.show()
        self.box.show()
        self.wrapper.show()
        self.wrapper.connect('destroy', self._on_destroy)

    def add_child(self, widget):
        """Put ``widget`` in the slot so it gets the slot's whole allocation."""
        self.slot.pack_start(widget, True, True, 0)

    def start(self):
        self.sources.append(GLib.timeout_add_seconds(REFRESH_SECONDS, self._tick))
        self.sources.append(GLib.timeout_add_seconds(RERENDER_SECONDS, self._rerender))
        self._tick()

    def _on_destroy(self, _widget):
        self.dead = True
        for source in self.sources:
            GLib.source_remove(source)
        self.sources = []

    def _tick(self):
        if self.dead:
            return False
        if not self.in_flight:
            self.in_flight = True
            self.generation += 1
            threading.Thread(target=self._read, args=(self.generation,),
                             daemon=True, name='ai-glance-usage').start()
        return True

    def _read(self, generation):
        try:
            snap = usage.read_current_usage(usage.DEFAULT_DB, usage.OMP_COMMAND)
        except Exception as exc:  # read_usage should never raise; be safe
            snap = usage.UsageSnapshot((), None, f'{type(exc).__name__}: {exc}')
        GLib.idle_add(self._apply, generation, snap)

    def _apply(self, generation, snap):
        try:
            self.in_flight = False
            if self.dead or generation != self.generation:
                return False
            self.snapshot = snap
            self._render()
        except Exception as exc:  # never raise into Terminator
            dbg(f'ai-glance usage bar: apply failed: {exc!r}')
        return False

    def _rerender(self):
        if self.dead:
            return False
        try:
            self._render()
        except Exception as exc:
            dbg(f'ai-glance usage bar: render failed: {exc!r}')
        return True

    def _render(self):
        if self.snapshot is None:
            return
        now = datetime.now(timezone.utc)
        model = usage.bar_segments(self.snapshot, now)
        self.model = model
        self.box.set_tooltip_text(model.tooltip)
        for child in self.content.get_children():
            self.content.remove(child)
        ctx = self.label.get_style_context()
        for cls in ('warning', 'error'):
            if cls == model.worst:
                ctx.add_class(cls)
            else:
                ctx.remove_class(cls)
        if model.error is not None:
            self.label.set_text('Claude usage: n/a')
            self.label.set_tooltip_text(model.tooltip)
            self.content.pack_start(self.label, True, True, 0)
        else:
            for i, acc in enumerate(model.accounts):
                if i:
                    sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
                    sep.set_margin_top(2)
                    sep.set_margin_bottom(2)
                    self.content.pack_start(sep, False, False, 0)
                self.content.pack_start(self._account_group(acc), False, False, 0)
            filler = Gtk.Box()
            self.content.pack_start(filler, True, True, 0)
            if model.updated:
                self.content.pack_end(self._text(model.updated, BAR_MUTED), False, False, 0)
        self.content.show_all()

    @staticmethod
    def _text(text, color):
        label = Gtk.Label(label=text)
        label.set_valign(Gtk.Align.CENTER)
        _style(label, f'* {{ color: {color}; {BAR_FONT} }}')
        return label

    def _account_group(self, acc):
        group = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        group.set_tooltip_text(acc.tooltip)
        badge = Gtk.Label(label=acc.provider_name)
        badge.set_valign(Gtk.Align.CENTER)
        _style(badge, '* { background-color: %s; color: %s; border-radius: 10px; padding: 0 8px;'
                      ' font-weight: bold; font-size: 105%%; }' % (acc.badge_bg, acc.badge_fg))
        group.pack_start(badge, False, False, 0)
        muted = acc.kind + (f' \u00b7 {acc.who}' if acc.who else '')
        group.pack_start(self._text(muted, BAR_MUTED), False, False, 0)
        for w in acc.windows:
            cell = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            cell.pack_start(self._text(w.short, BAR_FOREGROUND), False, False, 0)
            cell.pack_start(_LevelBar(w.fraction, w.color), False, False, 0)
            cell.pack_start(self._text(f'{w.percent}%', w.color), False, False, 0)
            if w.reset:
                cell.pack_start(self._text(w.reset, BAR_MUTED), False, False, 0)
            group.pack_start(cell, False, False, 4)
        return group


def _bar_of(window):
    return window.__dict__.get(_ATTR)


@contextlib.contextmanager
def _redirect_gtk_window(window, bar):
    """Temporarily make Gtk.Window.add/remove target the slot for ``window``."""
    orig_add, orig_remove = Gtk.Window.add, Gtk.Window.remove
    had_add = 'add' in Gtk.Window.__dict__
    had_remove = 'remove' in Gtk.Window.__dict__

    def add(win, widget):
        if win is window and widget is not bar.wrapper:
            return bar.add_child(widget)
        return orig_add(win, widget)

    def remove(win, widget):
        if win is window and widget is not bar.wrapper:
            return bar.slot.remove(widget)
        return orig_remove(win, widget)

    Gtk.Window.add, Gtk.Window.remove = add, remove
    try:
        yield
    finally:
        for name, orig, had in (('add', orig_add, had_add),
                                ('remove', orig_remove, had_remove)):
            if had:
                setattr(Gtk.Window, name, orig)
            else:
                delattr(Gtk.Window, name)


def _wrap(window, child=None):
    """Attach a bar to ``window``; ``child`` is its current Gtk child, if any.

    On any failure the child is put back directly and the window gets no bar.
    """
    bar = _Bar(window)
    try:
        if child is not None:
            Gtk.Window.remove(window, child)
            bar.add_child(child)
        Gtk.Window.add(window, bar.wrapper)
    except Exception as exc:
        dbg(f'ai-glance usage bar: cannot wrap window: {exc!r}')
        with contextlib.suppress(Exception):
            if child is not None and child in bar.slot.get_children():
                bar.slot.remove(child)
            if child is not None and not Gtk.Bin.get_child(window):
                Gtk.Window.add(window, child)
        return None
    window.__dict__[_ATTR] = bar
    bar.start()
    return bar


def _rewrap_existing(window):
    focused = None
    with contextlib.suppress(Exception):
        focused = window.get_focussed_terminal()
    child = Gtk.Bin.get_child(window)
    bar = _wrap(window, child)
    if bar is None:
        return
    if focused is not None:
        with contextlib.suppress(Exception):
            focused.vte.grab_focus()


def _patch_window_class(window_cls):
    orig_add, orig_remove, orig_get_child = (
        window_cls.add, window_cls.remove, window_cls.get_child)

    @functools.wraps(orig_add)
    def add(self, widget, *args, **kwargs):
        bar = _bar_of(self)
        if bar is None and not getattr(self, 'isDestroyed', False) \
                and Gtk.Bin.get_child(self) is None:
            bar = _wrap(self)  # window created after install()
        if bar is None:
            return orig_add(self, widget, *args, **kwargs)
        with _redirect_gtk_window(self, bar):
            return orig_add(self, widget, *args, **kwargs)

    @functools.wraps(orig_remove)
    def remove(self, widget):
        bar = _bar_of(self)
        if bar is None:
            return orig_remove(self, widget)
        with _redirect_gtk_window(self, bar):
            return orig_remove(self, widget)

    @functools.wraps(orig_get_child)
    def get_child(self):
        bar = _bar_of(self)
        if bar is None:
            return orig_get_child(self)
        children = bar.slot.get_children()
        return children[0] if children else None

    window_cls.add, window_cls.remove, window_cls.get_child = add, remove, get_child


def _patch_get_parent():
    """Report the Window (not the slot) as parent of the slot's child.

    Terminator walks ``get_parent()`` from Terminal/Paned/Notebook and expects
    the Window at the top. Only Python callers are affected, not GTK itself.
    """
    orig = Gtk.Widget.get_parent

    @functools.wraps(orig)
    def get_parent(self):
        parent = orig(self)
        if parent is not None:
            owner = parent.__dict__.get(_OWNER)
            if owner is not None:
                return owner
        return parent

    Gtk.Widget.get_parent = get_parent


def install():
    """Patch terminatorlib Window and rewrap existing windows. Idempotent."""
    try:
        from terminatorlib import window as window_mod
        from terminatorlib.terminator import Terminator

        window_cls = window_mod.Window
        if window_cls.__dict__.get(_PATCHED):
            return
        _patch_window_class(window_cls)
        _patch_get_parent()
        window_cls._ai_glance_usage_patched = True
        for window in list(Terminator().windows):
            if _bar_of(window) is None:
                try:
                    _rewrap_existing(window)
                except Exception as exc:
                    dbg(f'ai-glance usage bar: rewrap failed: {exc!r}')
        dbg('ai-glance usage bar installed')
    except Exception as exc:
        dbg(f'ai-glance usage bar: install failed: {exc!r}')
