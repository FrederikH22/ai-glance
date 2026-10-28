"""Terminator plugin entry point for ai-glance.

Terminator imports this file from ~/.config/terminator/plugins and instantiates
every class listed in AVAILABLE that is enabled in the config. The helper
package `ai_glance/` is symlinked next to this file by install.sh.
"""
import terminatorlib.plugin as plugin
from terminatorlib.util import err

AVAILABLE = ['AiGlance']


class AiGlance(plugin.Plugin):
    """Git/worktree panel button on every pane, a usage bar on every window, animated active-pane colours."""

    capabilities = ['ai_glance']

    def __init__(self):
        plugin.Plugin.__init__(self)
        try:
            from ai_glance import gtk_git_panel
            gtk_git_panel.install()
        except Exception as exc:
            err('ai-glance: git panel install failed: %r' % (exc,))
        try:
            from ai_glance import gtk_usage_bar
            gtk_usage_bar.install()
        except Exception as exc:
            err('ai-glance: usage bar install failed: %r' % (exc,))

        try:
            from ai_glance import gtk_titlebar_color
            gtk_titlebar_color.install()
        except Exception as exc:
            err('ai-glance: titlebar colour install failed: %r' % (exc,))
