"""The window title follows the focused pane only.

Stock Terminator copies every pane's title into the window title, so several omp
sessions whose titles animate a spinner (``π ⠋ …`` every 80 ms) take turns
overwriting it and the window title flickers between sessions. Here
``WindowTitle.set_title`` ignores title changes from panes that are not the
window's focused pane, and a pane that gains focus pushes its title once.
"""
from terminatorlib.util import dbg

_PATCH_ATTR = '_ai_glance_title_patched'


def _is_terminal(widget):
    return widget is not None and hasattr(widget, 'vte') and hasattr(widget, 'get_window_title')


def _focused_elsewhere(widget):
    """True when another pane of the same window holds the focus."""
    if widget.vte.is_focus():
        return False
    window = widget.get_toplevel()
    finder = getattr(window, 'get_focussed_terminal', None)
    focused = finder() if finder else None
    return focused is not None and focused is not widget


def install():
    """Patch WindowTitle.set_title and Terminal.on_vte_focus_in (idempotent)."""
    from terminatorlib.terminal import Terminal
    from terminatorlib.window import WindowTitle
    if getattr(WindowTitle.set_title, _PATCH_ATTR, False):
        return
    orig_set_title = WindowTitle.set_title
    orig_focus_in = Terminal.on_vte_focus_in

    def set_title(self, widget, text):
        try:
            if _is_terminal(widget) and _focused_elsewhere(widget):
                return None
        except Exception as exc:  # never raise into Terminator; fall back to stock
            dbg(f'ai-glance window title: focus check failed: {exc!r}')
        return orig_set_title(self, widget, text)

    def on_vte_focus_in(self, widget, event):
        result = orig_focus_in(self, widget, event)
        try:
            window = self.get_toplevel()
            title = getattr(window, 'title', None)
            if title is not None and hasattr(title, 'set_title'):
                title.set_title(self, self.get_window_title())
        except Exception as exc:
            dbg(f'ai-glance window title: focus update failed: {exc!r}')
        return result

    setattr(set_title, _PATCH_ATTR, True)
    WindowTitle.set_title = set_title
    Terminal.on_vte_focus_in = on_vte_focus_in
