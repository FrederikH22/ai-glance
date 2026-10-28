"""Titlebar git button and in-pane git/worktree panel for Terminator.

All git/SQLite/proc work runs in daemon worker threads; GTK is only touched on
the main thread (results arrive via GLib.idle_add).
"""
import threading
from datetime import datetime, timezone
from pathlib import Path

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import GLib, Gtk, Gdk, Pango  # noqa: E402

from terminatorlib.util import dbg  # noqa: E402

from . import gitinfo, ompsessions  # noqa: E402

REFRESH_SECONDS = 3
HEIGHT_FRACTION = 0.4
MIN_MAX_HEIGHT = 80
ACCENT = '#f5a623'
GREY = '#888888'
_PANEL_ATTR = '_ai_glance_git_panel'
_BUTTON_ATTR = '_ai_glance_git_button'
_PATCH_ATTR = '_ai_glance_git_patched'


def _esc(text):
    return GLib.markup_escape_text(str(text))


def _short_path(path):
    p = str(path)
    home = str(Path.home())
    if p == home:
        return '~'
    if p.startswith(home + '/'):
        return '~/' + p[len(home) + 1:]
    return p


def _worktree_markup(wt, in_use):
    """Pango markup for one worktree row."""
    if wt.branch:
        branch = wt.branch
    else:
        branch = 'detached @%s' % (wt.head or '')[:7]
    parts = []
    marker = '◆ ' if in_use else ''
    parts.append(_esc(marker) + _esc(_short_path(wt.path)))
    parts.append('<i>%s</i>' % _esc(branch))
    if wt.prunable:
        parts.append('(prunable)')
    elif wt.error:
        parts.append('?')
    else:
        if wt.ahead is not None and wt.behind is not None:
            parts.append('↑%d ↓%d' % (wt.ahead, wt.behind))
        if wt.dirty:
            parts.append('●')
    if wt.locked:
        parts.append('🔒')
    text = '  '.join(parts)
    if wt.prunable:
        return '<span foreground="%s">%s</span>' % (GREY, text)
    if in_use:
        return '<span foreground="%s" weight="bold">%s</span>' % (ACCENT, text)
    return text


def _repo_markup(repo, show_base):
    main = repo.current or (repo.worktrees[0] if repo.worktrees else None)
    text = '<b>%s</b>' % _esc(repo.name)
    if repo.error or main is None:
        return text + '  ?'
    if main.branch:
        text += '  <i>%s</i>' % _esc(main.branch)
    else:
        text += '  <i>%s</i>' % _esc('detached @%s' % (main.head or '')[:7])
    if not main.prunable and not main.error:
        if main.ahead is not None and main.behind is not None:
            text += '  ↑%d ↓%d' % (main.ahead, main.behind)
        if main.dirty:
            text += '  ●'
    elif main.error:
        text += '  ?'
    if show_base and repo.base:
        text += '  <span foreground="%s">vs %s</span>' % (GREY, _esc(repo.base))
    return text


_logged_errors: set = set()


def _log_once(exc):
    name = type(exc).__name__
    if name not in _logged_errors:
        _logged_errors.add(name)
        dbg('ai_glance git panel: omp session parse failed: %r' % (exc,))


def _compute(cwd, pid):
    """Worker-thread computation. Returns (model, in_use) or None."""
    try:
        model = gitinfo.build_model(Path(cwd))
        paths = [wt.path for repo in model.repos for wt in repo.worktrees]
        errors = []
        in_use = ompsessions.pane_worktrees_in_use(
            pid, paths, datetime.now(timezone.utc), errors=errors)
        for exc in errors:
            _log_once(exc)
        return model, set(in_use)
    except Exception as exc:  # never raise into Terminator
        dbg('ai_glance git panel: refresh failed: %r' % (exc,))
        return None


class GitPanel(Gtk.Box):
    """Per-terminal panel; owns a refresh timer and a generation counter."""

    def __init__(self, terminal):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL)
        self._term = terminal
        self._gen = 0
        self._closed = False
        self._in_flight = False
        self._timer = None

        self._content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._content.set_border_width(4)
        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._scroll.set_propagate_natural_height(True)
        self._scroll.add(self._content)
        self.pack_start(self._scroll, True, True, 0)
        self.pack_end(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
                      False, False, 0)
        self._provider = None
        self._last_key = None
        self._close_id = None
        self._apply_colors()
        self._set_text('Loading…')
        self.connect('destroy', self._on_destroy)

    def _apply_colors(self):
        """Match the terminal's colours so text stays readable on any theme."""
        try:
            cfg = self._term.config
            bg, fg = Gdk.RGBA(), Gdk.RGBA()
            if not (bg.parse(cfg['background_color']) and fg.parse(cfg['foreground_color'])):
                return
            css = ('box, scrolledwindow, viewport, label { background-color: %s; color: %s; }'
                   % (bg.to_string(), fg.to_string()))
            provider = Gtk.CssProvider()
            provider.load_from_data(css.encode())
            self.get_style_context().add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            for w in (self._scroll, self._content):
                w.get_style_context().add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self._provider = provider
        except Exception as exc:
            dbg('ai_glance git panel: colours skipped: %r' % (exc,))

    # lifecycle ---------------------------------------------------------
    def start(self):
        self._close_id = self._term.connect('close-term', self._on_term_close)
        self._timer = GLib.timeout_add_seconds(REFRESH_SECONDS, self._on_timer)
        self.refresh()

    def _on_term_close(self, _term):
        self.shutdown()

    def shutdown(self):
        self._closed = True
        self._gen += 1
        if self._close_id is not None:
            try:
                self._term.disconnect(self._close_id)
            except Exception:
                pass
            self._close_id = None
        if self._timer is not None:
            GLib.source_remove(self._timer)
            self._timer = None

    def _on_destroy(self, _widget):
        self.shutdown()

    def _on_timer(self):
        if self._closed:
            self._timer = None
            return False
        self.refresh()
        return True

    # refresh -----------------------------------------------------------
    def refresh(self):
        if self._closed or self._in_flight:
            return
        try:
            cwd = self._term.get_cwd()
            pid = self._term.pid
        except Exception as exc:
            dbg('ai_glance git panel: cwd unavailable: %r' % (exc,))
            cwd, pid = None, None
        if not cwd:
            self._render(None, set())
            return
        self._in_flight = True
        gen = self._gen
        threading.Thread(target=self._work, args=(gen, cwd, pid),
                         daemon=True).start()

    def _work(self, gen, cwd, pid):
        result = _compute(cwd, pid)
        GLib.idle_add(self._apply, gen, result)

    def _apply(self, gen, result):
        self._in_flight = False
        if self._closed or gen != self._gen:
            return False
        try:
            if result is None:
                self._render(None, set())
            else:
                self._render(*result)
        except Exception as exc:
            dbg('ai_glance git panel: render failed: %r' % (exc,))
        return False

    # rendering ---------------------------------------------------------
    def _set_text(self, text):
        for child in self._content.get_children():
            self._content.remove(child)
        label = Gtk.Label(label=text, xalign=0)
        self._style(label)
        self._content.pack_start(label, False, False, 0)
        self._content.show_all()

    def _style(self, label):
        if self._provider is not None:
            label.get_style_context().add_provider(
                self._provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _add_markup(self, markup, indent=0):
        label = Gtk.Label(xalign=0)
        label.set_markup(markup)
        label.set_margin_start(indent)
        label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self._style(label)
        self._content.pack_start(label, False, False, 0)

    def _render(self, model, in_use):
        self._update_max_height()
        key = (model, frozenset(in_use))
        if key == self._last_key:
            return
        if model is None:
            self._set_text('Git status: n/a')
            self._last_key = key
            return
        if model.kind == 'none':
            self._set_text('Not a git repository: %s' % _short_path(model.path))
            self._last_key = key
            return
        for child in self._content.get_children():
            self._content.remove(child)
        bases = {r.base for r in model.repos}
        real_bases = {b for b in bases if b}
        if len(real_bases) == 1 and None not in bases:
            base_text = 'vs %s' % next(iter(real_bases))
            show_base = False
        elif not real_bases:
            base_text = 'no base ref'
            show_base = False
        else:
            base_text = 'per-repo base'
            show_base = True
        kind = 'workspace' if model.kind == 'workspace' else 'repo'
        self._add_markup('<span foreground="%s">%s %s — %s</span>' % (
            GREY, _esc(kind), _esc(_short_path(model.path)), _esc(base_text)))
        if not model.repos:
            self._add_markup('no git repositories found')
        for repo in model.repos:
            self._add_markup(_repo_markup(repo, show_base))
            for wt in repo.worktrees:
                self._add_markup(
                    _worktree_markup(wt, wt.path in in_use),
                    indent=16)
        self._last_key = key
        self._content.show_all()

    def _update_max_height(self):
        height = self._term.get_allocated_height()
        cap = max(MIN_MAX_HEIGHT, int(height * HEIGHT_FRACTION))
        self._scroll.set_max_content_height(cap)


# --- toggling ------------------------------------------------------------

def toggle(terminal):
    """Open or close the git panel in this Terminal. Never raises."""
    try:
        panel = getattr(terminal, _PANEL_ATTR, None)
        if panel is not None:
            setattr(terminal, _PANEL_ATTR, None)
            panel.shutdown()
            terminal.remove(panel)
            panel.destroy()
            return
        panel = GitPanel(terminal)
        setattr(terminal, _PANEL_ATTR, panel)
        terminal.pack_start(panel, False, True, 0)
        children = terminal.get_children()
        idx = children.index(terminal.titlebar)
        if terminal.config['title_at_bottom']:
            terminal.reorder_child(panel, idx)
        else:
            terminal.reorder_child(panel, idx + 1)
        panel.show_all()
        panel.start()
    except Exception as exc:
        dbg('ai_glance git panel: toggle failed: %r' % (exc,))


# --- titlebar button -----------------------------------------------------

def _sync_button_color(titlebar):
    """Give the glyph the same foreground Titlebar.update gave the group label."""
    try:
        glyph = getattr(titlebar, _BUTTON_ATTR, None)
        if glyph is None:
            return
        rgba = titlebar.grouplabel.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        glyph.override_color(Gtk.StateFlags.NORMAL, rgba)
    except Exception as exc:
        dbg('ai_glance git panel: button colour failed: %r' % (exc,))


def _add_button(titlebar):
    try:
        hbox = titlebar.get_child()
        if hbox is None or getattr(titlebar, _BUTTON_ATTR, None) is not None:
            return
        # Same construction as the group icon (EventBox + label): a Gtk.Button
        # would add theme padding and roughly double the title bar height.
        glyph = Gtk.Label(label='⎇')
        glyph.set_margin_start(4)
        glyph.set_margin_end(4)
        ebox = Gtk.EventBox()
        ebox.set_visible_window(False)
        ebox.add(glyph)
        ebox.set_tooltip_text('Git status')
        ebox.connect('button-press-event', lambda _w, ev: _on_button_press(titlebar, ev))
        setattr(titlebar, _BUTTON_ATTR, glyph)
        setattr(ebox, _BUTTON_ATTR, True)
        hbox.pack_start(ebox, False, False, 0)
        hbox.reorder_child(ebox, 0)
        ebox.show_all()
        _sync_button_color(titlebar)
    except Exception as exc:
        dbg('ai_glance git panel: button failed: %r' % (exc,))


def _on_button_press(titlebar, event):
    if event.button == 1:
        toggle(titlebar.terminal)
        return True
    return False


def install():
    """Patch Titlebar (idempotent) and retrofit existing terminals."""
    from terminatorlib.titlebar import Titlebar
    from terminatorlib.terminator import Terminator
    if not getattr(Titlebar.update, _PATCH_ATTR, False):
        original_update = Titlebar.update

        def patched_update(self, *args, **kwargs):
            result = original_update(self, *args, **kwargs)
            _sync_button_color(self)
            return result

        setattr(patched_update, _PATCH_ATTR, True)
        Titlebar.update = patched_update
    if not getattr(Titlebar.__init__, _PATCH_ATTR, False):
        original = Titlebar.__init__

        def patched(self, *args, **kwargs):
            original(self, *args, **kwargs)
            _add_button(self)

        setattr(patched, _PATCH_ATTR, True)
        Titlebar.__init__ = patched
    try:
        for term in list(Terminator().terminals):
            if getattr(term, 'titlebar', None) is not None:
                _add_button(term.titlebar)
    except Exception as exc:
        dbg('ai_glance git panel: retrofit failed: %r' % (exc,))
