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


PANEL_CLASS = 'ai-glance-git'

_CSS = """
.ai-glance-git, .ai-glance-git scrolledwindow, .ai-glance-git viewport,
.ai-glance-git .gg-content { background-color: #1e1f24; }
.ai-glance-git .gg-content { padding: 8px; }
.ai-glance-git label { color: #e6edf3; }
.ai-glance-git .gg-muted { color: #8b949e; font-size: 80%; }
.ai-glance-git .gg-header { letter-spacing: 1px; }
.ai-glance-git .gg-glyph { color: #8b949e; }
.ai-glance-git .gg-name { font-weight: bold; }
.ai-glance-git .gg-repo { border-top: 1px solid #30363d; padding: 6px 0 2px 0; }
.ai-glance-git .gg-repo.gg-first { border-top-width: 0; padding-top: 2px; }
.ai-glance-git .gg-omp-row { border-left: 3px solid #8957e5; margin: 4px 0 0 14px; padding-left: 8px; }
.ai-glance-git .gg-pill { background-color: #2d333b; border-radius: 9px; padding: 2px 8px;
    font-family: monospace; font-size: 90%; }
.ai-glance-git .gg-pill-detached { color: #d29922; }
.ai-glance-git .gg-pill-muted { color: #8b949e; font-family: inherit; }
.ai-glance-git .gg-badge { border-radius: 9px; padding: 1px 7px; font-size: 85%; font-weight: bold; }
.ai-glance-git .gg-up { background-color: #2ea043; color: #ffffff; }
.ai-glance-git .gg-down { background-color: #d29922; color: #1e1f24; }
.ai-glance-git .gg-down-bad { background-color: #da3633; color: #ffffff; }
.ai-glance-git .gg-omp-badge { background-color: #8957e5; color: #ffffff; border-radius: 9px;
    padding: 1px 7px; font-size: 80%; font-weight: bold; }
.ai-glance-git .gg-dirty { color: #d29922; }
.ai-glance-git .gg-error { background-color: #da3633; color: #ffffff; }
"""


def _pill(text, *classes, tooltip=None):
    label = Gtk.Label(label=text)
    label.set_valign(Gtk.Align.CENTER)
    ctx = label.get_style_context()
    for cls in classes:
        ctx.add_class(cls)
    if tooltip:
        label.set_tooltip_text(tooltip)
    return label


def _state_widgets(wt):
    """Branch pill and ahead/behind/dirty/error badges for one worktree state."""
    out = []
    if wt.branch:
        out.append(_pill(wt.branch, 'gg-pill'))
    else:
        out.append(_pill('detached @%s' % (wt.head or '')[:7], 'gg-pill', 'gg-pill-detached'))
    if wt.error:
        out.append(_pill('?', 'gg-badge', 'gg-error', tooltip=str(wt.error)))
    elif wt.prunable:
        out.append(_pill('prunable', 'gg-pill', 'gg-pill-muted'))
    elif wt.ahead is not None and wt.behind is not None:
        if wt.ahead:
            out.append(_pill('\u2191%d' % wt.ahead, 'gg-badge', 'gg-up'))
        if wt.behind:
            out.append(_pill('\u2193%d' % wt.behind, 'gg-badge',
                             'gg-down-bad' if wt.behind > 10 else 'gg-down'))
        if not wt.ahead and not wt.behind:
            out.append(_pill('in sync', 'gg-pill', 'gg-pill-muted'))
    if not wt.error and not wt.prunable and wt.dirty:
        out.append(_pill('\u25cf', 'gg-dirty', tooltip='uncommitted changes'))
    if wt.locked:
        out.append(_pill('\U0001f512', 'gg-muted', tooltip='locked'))
    return out


_logged_errors: set = set()


def _log_once(exc):
    name = type(exc).__name__
    if name not in _logged_errors:
        _logged_errors.add(name)
        dbg('ai_glance git panel: omp session parse failed: %r' % (exc,))


def _compute(cwd, pid):
    """Worker-thread computation. Returns (model, in_use, agent name, external repos)
    or None. ``external`` are repos the pane's agent works in outside ``model``."""
    try:
        model = gitinfo.build_model(Path(cwd))
        covered = [wt.path for repo in model.repos for wt in repo.worktrees]
        errors = []
        agent, agent_paths = ompsessions.pane_agent_paths(
            pid, datetime.now(timezone.utc), errors=errors)
        for exc in errors:
            _log_once(exc)
        in_use = ompsessions.worktrees_in_use(agent_paths, covered)
        external = gitinfo.external_repos(agent_paths, covered) if agent_paths else ()
        in_use |= ompsessions.worktrees_in_use(
            agent_paths, [wt.path for repo in external for wt in repo.worktrees])
        return model, set(in_use), agent or ompsessions.OMP, external
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
        self._content.get_style_context().add_class('gg-content')
        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._scroll.set_propagate_natural_height(True)
        self._scroll.add(self._content)
        self.pack_start(self._scroll, True, True, 0)
        self._last_key = None
        self._close_id = None
        self._provider = None
        self._apply_css()
        self._set_text('Loading\u2026')
        self.connect('destroy', self._on_destroy)

    def _apply_css(self):
        """Dark card styling, scoped to this panel by its unique class (never global)."""
        try:
            self.get_style_context().add_class(PANEL_CLASS)
            provider = Gtk.CssProvider()
            provider.load_from_data(_CSS.encode())
            self.get_style_context().add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self._provider = provider
            self._style_tree(self._scroll)
        except Exception as exc:
            dbg('ai_glance git panel: css skipped: %r' % (exc,))

    def _style_tree(self, widget):
        """GTK does not cascade a widget's provider to its children: attach it to each
        widget of the subtree (selectors are all scoped under ``.ai-glance-git``)."""
        if self._provider is None:
            return
        widget.get_style_context().add_provider(
            self._provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        if isinstance(widget, Gtk.Container):
            for child in widget.get_children():
                self._style_tree(child)

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
    def _clear(self):
        for child in self._content.get_children():
            self._content.remove(child)

    def _set_text(self, text):
        self._clear()
        label = Gtk.Label(label=text, xalign=0)
        self._content.pack_start(label, False, False, 0)
        self._style_tree(self._content)
        self._content.show_all()

    def _header(self, base_text, agent, tooltip):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        label = Gtk.Label(label=base_text, xalign=0)
        label.get_style_context().add_class('gg-muted')
        label.get_style_context().add_class('gg-header')
        label.set_tooltip_text(tooltip)
        box.pack_start(label, False, False, 0)
        if agent:
            box.pack_start(_pill(agent, 'gg-omp-badge', tooltip='%s is running in this pane' % agent),
                           False, False, 0)
        return box

    def _repo_card(self, repo, in_use, first, show_base, agent):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        ctx = card.get_style_context()
        ctx.add_class('gg-repo')
        if first:
            ctx.add_class('gg-first')
        current, extra, current_in_use = gitinfo.repo_rows(repo, in_use)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        glyph = _pill('\u2387', 'gg-glyph')
        name = _pill(repo.name, 'gg-name')
        name.set_xalign(0)
        row.pack_start(glyph, False, False, 0)
        row.pack_start(name, False, False, 0)
        if repo.error or current is None:
            row.pack_start(_pill('?', 'gg-badge', 'gg-error', tooltip=str(repo.error or 'no checkout')),
                           False, False, 0)
        else:
            if current_in_use:
                row.pack_start(_pill(agent, 'gg-omp-badge', tooltip='%s works in this checkout' % agent),
                               False, False, 0)
            for w in _state_widgets(current):
                row.pack_start(w, False, False, 0)
        if show_base and repo.base:
            row.pack_start(_pill('vs %s' % repo.base, 'gg-muted'), False, False, 0)
        row.set_tooltip_text(_short_path(current.path if current is not None else repo.root))
        card.pack_start(row, False, False, 0)
        for wt in extra:
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            line.get_style_context().add_class('gg-omp-row')
            path = Gtk.Label(label=_short_path(wt.path), xalign=0)
            path.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            path.get_style_context().add_class('gg-muted')
            line.pack_start(path, False, False, 0)
            for w in _state_widgets(wt):
                line.pack_start(w, False, False, 0)
            line.set_tooltip_text(str(wt.path))
            card.pack_start(line, False, False, 0)
        return card

    def _render(self, model, in_use, agent=ompsessions.OMP, external=()):
        self._update_max_height()
        key = (model, frozenset(in_use), agent, tuple(external))
        if key == self._last_key:
            return
        if model is None:
            self._set_text('Git status: n/a')
            self._last_key = key
            return
        if model.kind == 'none' and not external:
            self._set_text('Not a git repository: %s' % _short_path(model.path))
            self._last_key = key
            return
        self._clear()
        bases = {r.base for r in (model.repos or external)}
        real_bases = {b for b in bases if b}
        if len(real_bases) == 1 and None not in bases:
            header_base = next(iter(real_bases))
            base_text = 'GIT \u00b7 vs %s' % header_base
            show_base = False
        elif not real_bases:
            header_base = None
            base_text = 'GIT \u00b7 no base ref'
            show_base = False
        else:
            header_base = None
            base_text = 'GIT \u00b7 per-repo base'
            show_base = True
        kind = {'workspace': 'workspace', 'repo': 'repo'}.get(model.kind, 'folder')
        self._content.pack_start(
            self._header(base_text, agent if in_use else None,
                         '%s %s' % (kind, _short_path(model.path))),
            False, False, 0)
        if model.kind == 'none':
            self._content.pack_start(
                self._muted('Not a git repository: %s' % _short_path(model.path)), False, False, 0)
        elif not model.repos:
            self._content.pack_start(Gtk.Label(label='no git repositories found', xalign=0),
                                     False, False, 0)
        for i, repo in enumerate(model.repos):
            self._content.pack_start(self._repo_card(repo, in_use, i == 0, show_base, agent),
                                     False, False, 0)
        if external:
            section = self._muted('%s also works in' % agent)
            section.get_style_context().add_class('gg-header')
            section.set_margin_top(6)
            self._content.pack_start(section, False, False, 0)
            for repo in external:
                self._content.pack_start(
                    self._repo_card(repo, in_use, False, repo.base != header_base, agent),
                    False, False, 0)
        self._last_key = key
        self._style_tree(self._content)
        self._content.show_all()

    @staticmethod
    def _muted(text):
        label = Gtk.Label(label=text, xalign=0)
        label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        label.get_style_context().add_class('gg-muted')
        return label

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
