# ai-glance

A Terminator 2.1.3 plugin for working with AI coding agents (omp) across git worktrees.

- **Git button** (⎇) at the far left of every pane's title bar. Click it to open a panel
  inside that pane (no split) showing branch, ahead/behind vs `origin/main`, dirty flag and
  all worktrees of the repo. In a directory that contains several repos (e.g. `~/ros_ws`) it
  lists every repo under `src/` with its worktrees. Outside a repo: "Not a git repository".
  Worktrees that an omp session running in that pane is working in are marked ◆.
- **Usage bar** at the bottom of every window with the latest omp usage figures
  ("n/a" when unavailable).

## Install / uninstall

    bash install.sh              # symlinks into ~/.config/terminator/plugins, enables AiGlance
    bash install.sh --uninstall  # removes the symlinks and the AiGlance entry

Both back up the Terminator config to `~/.config/terminator/config.bak-ai-glance` first.
**Restart Terminator after installing or uninstalling**; open windows are not changed
until then. Terminator's title bars must be on (the
`show_titlebar` option, on by default), otherwise there is no place for the git button.

## Risk: monkeypatching

Terminator has no plugin API for title-bar widgets or window chrome, so the plugin patches
`terminatorlib` classes at runtime (Titlebar, Window). Nothing under `terminatorlib` is
modified on disk, patches are applied once and are idempotent, and failures are logged
rather than raised, but a Terminator update that changes those internals can silently
disable the button or bar. It was written against Terminator 2.1.3. Disabling the plugin
and restarting returns Terminator to stock behaviour.

## Data sources

- `git` CLI (run in worker threads, never on the GTK main thread).
- `~/.omp/agent/terminal-sessions` and the omp session logs, to find which worktrees an
  omp process on a pane's tty is using.
- `~/.omp/agent/agent.db`, opened **read-only**, for the usage figures.

Nothing is sent over the network.

## Tests

    python3 -m pytest tests -q
    XDG_CONFIG_HOME=$(mktemp -d) xvfb-run -a python3 tests/smoke_git_panel.py
    XDG_CONFIG_HOME=$(mktemp -d) xvfb-run -a python3 tests/smoke_usage_bar.py
    XDG_CONFIG_HOME=$(mktemp -d) xvfb-run -a python3 tests/smoke_plugin.py   # needs an installed config copy, see file
