# ai-glance: Terminator plugin with per-pane git panel + Claude usage bar

Date: 2026-10-28 · Owner: Frederik · Target: Terminator 2.1.3 (Ubuntu package, GTK 3.24, Python 3)

## Why

Frederik works in Terminator with several omp agents running in parallel across a
ROS 2 workspace (`~/ros_ws`) that has many git worktrees (`ros_ws/.worktrees/*`,
`~/.omp/wt/*`). Today he has to run `git` by hand in each pane to see which branch
a pane is on, how it relates to `main`, and which worktree an agent is touching.
He also can't see how much of his Claude limits are used without asking omp.
He wants both at a glance, inside Terminator, without changing terminals.

Success: one click on a pane shows the git state of where that pane is, including
which worktrees the omp running in that pane is using; the Claude usage of every
logged-in account is always visible at the bottom of each Terminator window.

## Goals

1. **Git button per pane.** Every terminal pane gets a git icon at the far left
   (top-left corner) of its title bar.
2. **Toggle panel inside the pane.** Clicking the icon opens a git panel *inside the
   same pane* (no new split, no new pane). Clicking again closes it. The terminal
   keeps running and stays usable underneath.
3. **Panel content, single repo.** When the pane's shell cwd is inside a git repo:
   repo name, checked-out branch (or detached SHA), ahead/behind relative to main,
   and whether there are uncommitted changes.
4. **Panel content, ROS workspace root.** When the cwd is the root of a ROS
   workspace, show the same for every git repo under `src/` (one row per repo).
5. **Current checkout only.** (Amended by the user round: the panel no longer lists
   all worktrees.) Per repo the panel shows only the checkout the pane is in, with
   branch, ahead/behind main and dirty flag.
6. **omp worktrees.** When an omp is live in this pane, additionally and ONLY the
   worktrees that omp is currently using are listed under their repo, marked as
   omp-in-use (a worktree equal to the current checkout is not listed twice; the
   repo row gets the omp badge instead).
7. **Claude usage bar.** A strip along the bottom of each Terminator window,
   independent of how the window is split into panes, showing the Claude usage of
   every account omp is logged into: per account, each limit window (5 Hour, 7 Day,
   7 Day (Fable), …) with percent used, status, and reset time.
8. **Usage comes from omp only.** Read omp's own records; never call Anthropic.

## Non-goals

- Other providers' usage is shown only if omp has an enabled login with usage rows for them (amended by the user round).
- No calls to Anthropic or any network API. No `git fetch`.
- No git actions from the panel (no checkout, commit, push). Read-only display.
- No changes to Terminator's packaged files under `/usr/lib/python3/dist-packages`.
- No support for Terminator versions other than 2.1.3 beyond failing safely.

## Anti-goals (would make it a failure even if it "works")

- Terminator freezing or stuttering: no git or SQLite work on the GTK main thread.
  A slow repo or a locked database must never block typing.
- Disturbing omp: the usage reader must not lock, write to, or migrate
  `agent.db`, and must not create WAL/SHM side effects that interfere with omp.
- Breaking Terminator's normal behaviour: splitting, closing, zoom, tabs, drag &
  drop, layouts, and the group button must keep working with the plugin enabled.
- Crashing Terminator when omp internals change: if the omp files are missing or
  their format differs, show "n/a" (usage) or skip highlighting (worktrees). Never
  raise into Terminator.

## Constraints and facts found on this machine

- Terminator 2.1.3 at `/usr/lib/python3/dist-packages/terminatorlib/`. Plugins
  load from `~/.config/terminator/plugins/*.py` (directory does not exist yet).
  Every top-level `.py` there is imported and must define `AVAILABLE`; the plugin
  directory is put on `sys.path`, so a sub-package directory next to the plugin
  file is importable and is *not* treated as a plugin. A plugin class only runs if
  its name is in `[plugins] enabled_plugins` in `~/.config/terminator/config`.
- Plugins load lazily (first from `Terminal` code paths), so some titlebars and
  windows already exist when the plugin loads. Those must be retrofitted, not
  only new ones.
- Terminator has no plugin hook for titlebar buttons or window chrome. The plugin
  must monkeypatch `terminatorlib` classes at load time. Relevant structure:
  - `Terminal(Gtk.VBox)` packs `self.titlebar` then `self.terminalbox` (order
    swaps when `title_at_bottom`). The git panel goes between them.
  - `Titlebar.__init__` builds a local `hbox` (group `ebox`, separator, label
    viewport, bell icon) and `self.add(hbox)`. The button is inserted at index 0
    of that hbox (`titlebar.get_child()`).
  - `Window(Container, Gtk.Window)` holds exactly one child and Terminator code
    relies on `window.get_child()` / `window.get_children()[0]` being the root
    Terminal/Paned/Notebook (`window.py`, `notebook.py`, `paned.py:286`,
    `terminal.py:1365`, `terminator.py:319,515`). `Window.add` calls
    `Gtk.Window.add(self, widget)` explicitly.
- Terminal ↔ tty: the shell PID is `Terminal.pid`; its tty is
  `readlink /proc/<pid>/fd/0` (e.g. `/dev/pts/10`). Cwd via `Terminal.get_cwd()`.
- omp writes `~/.omp/agent/terminal-sessions/pts-<N>` per terminal. Format (3
  lines): session cwd; absolute path of the session's `.jsonl` log; `cwdstat <dev>
  <ino>`. Files go stale when omp exits (they are not deleted).
- omp session logs are JSONL. Line 1 `type: title`; a `type: session` line has
  `.cwd`. Tool calls appear as `type: message` with
  `.message.content[].arguments.{path,cwd}`; results carry
  `.message.details.{cwd,path}`. Lines have ISO `timestamp`. Subagent logs are
  `*.jsonl` inside the directory next to the log with the same stem (17 in one
  real session). Logs can be large; only the tail is relevant.
- All running `omp` processes report `~/ros_ws` as their process cwd even when
  their agents work in worktrees, so process cwd cannot identify the worktree.
- omp usage records: SQLite `~/.omp/agent/agent.db` (WAL mode), table
  `usage_history` with columns including `provider`, `email`, `label`,
  `window_label`, `used_fraction` (0..1), `status` (`ok`, `exhausted`, …),
  `resets_at` (epoch ms), `recorded_at` (epoch ms). Currently two Anthropic
  accounts (frederik@, colleague@) with labels `Claude 5 Hour`, `Claude 7 Day`,
  `Claude 7 Day (Fable)`. `sqlite3` CLI is not installed; Python `sqlite3` is.
- Repos here have `origin/HEAD -> origin/main`.

## Decisions made by Frederik

- Git panel toggles open/closed with the same button, inside the current pane,
  never as a new split.
- Show all worktrees; highlight the ones the omp *in this pane* is using.
- Claude only; usage always read from omp's data, not from Anthropic.
- Usage strip at the bottom of the main window, independent of the panes.
- ROS workspace root → show every repo inside the workspace.

## Design

### Components

1. **`ai_glance/` core package (no GTK imports).** Pure functions, unit
   tested:
   - `git_state(path) -> RepoState | None`: repo root, branch/detached SHA,
     ahead/behind vs base, dirty flag, list of `WorktreeState`.
   - `ros_workspace_repos(path) -> list[Path] | None`: returns repos under `src/`
     when `path` is a ROS workspace root, else `None`.
   - `pane_omp_paths(tty) -> set[Path]`: paths the omp in that tty touched
     recently (see "omp highlight").
   - `claude_usage(db_path) -> UsageSnapshot`: latest row per (account, label).
2. **`ai_glance_plugin.py` plugin file.** Defines `AVAILABLE = ['AiGlance']`, the
   `AiGlance(plugin.Plugin)` class, the monkeypatches, the GTK widgets, and the
   background-thread scheduling. Thin layer over the core package.
3. **Install.** Symlink `ai_glance_plugin.py` and `ai_glance/` from the repo into
   `~/.config/terminator/plugins/`, and add `AiGlance` to `enabled_plugins` in
   `~/.config/terminator/config` (keeping existing entries / defaults).

### Git button and panel

- Button: small flat `Gtk.Button` (or `EventBox` + icon) with a git glyph,
  inserted at index 0 of the titlebar hbox, tooltip "Git status". Shown on every
  pane, including panes created before the plugin loaded.
- Click toggles a per-terminal `GitPanel` widget packed into the `Terminal` VBox
  directly below the titlebar (above `terminalbox`; respect `title_at_bottom`).
  Open state is per pane, not persisted across Terminator restarts.
- Panel height grows with content up to a cap (~40 % of the pane height, my call),
  then scrolls (`Gtk.ScrolledWindow`).
- While open, the panel refreshes every ~3 s (my call) and immediately on open.
  Refresh runs in a worker thread; results are applied with `GLib.idle_add`. At
  most one refresh in flight per panel; a refresh started while one is running is
  skipped. Closing the panel or the pane stops its timer.
- The panel follows the shell's cwd at each refresh (`Terminal.get_cwd()`), so
  `cd` into another repo updates it.

### What the panel shows

- Cwd is a **ROS workspace root**: directory contains `src/` and at least one of
  `build/`, `install/`, `log/`. Repos = every git repo found under `src/`
  (directories containing `.git` file or dir; do not descend into a repo once
  found; skip `build/ install/ log/`). Also treat a worktree workspace like
  `ros_ws/.worktrees/v2-ws` the same way (it has `src/`).
- Otherwise, cwd inside a **git repo**: that one repo (`git rev-parse
  --show-toplevel`). Inside a repo always wins over the workspace view.
- Otherwise, cwd **below a ROS workspace root but not in a repo** (e.g.
  `~/ros_ws/src`): workspace view of the nearest ancestor workspace root (my call).
- Otherwise: "Not a git repository: <cwd>".
- Per repo row: name (dir name), branch, `↑ahead ↓behind` vs base, dirty marker.
- `gitinfo` still computes all worktrees (`git worktree list --porcelain`; the in-use
  check needs their paths) but the panel renders only the current checkout per repo
  plus, with omp live in the pane, the in-use worktrees (short path `~/…`, branch,
  ahead/behind vs base, dirty marker) as indented rows with a 3 px accent bar.
- Visual design (user round): a dark card (`#1e1f24`, 8 px padding, CSS scoped to the
  panel's own class, never global). Header `GIT · vs <base>` plus an `omp` badge when
  omp is live. Per repo: branch glyph, bold name, branch pill, green `↑N` and
  orange/red `↓N` badges (red above 10) when non-zero, an `in sync` pill when both are
  zero, an amber `●` for uncommitted changes, `detached @sha` in amber, `?` with the
  error as tooltip. Rows have the full path as tooltip.
- Base for ahead/behind (my call): the remote default branch from
  `git symbolic-ref refs/remotes/origin/HEAD` (here `origin/main`); fall back to
  local `main`, then `master`; if none exists show no ahead/behind. Never fetch;
  label the base once in the panel header (e.g. "vs origin/main").
- Git commands run with a per-command timeout (~2 s, my call), `GIT_OPTIONAL_LOCKS=0`
  so `git status` does not take the index lock, and `--no-optional-locks`.
- Odd states (my call): a detached HEAD shows `detached @<short sha>` and still
  gets ahead/behind; a missing base ref shows no counts; prunable worktrees
  (directory gone) are shown greyed out with no counts and no dirty flag; locked
  worktrees get a lock marker; a failing/timeouting git command shows `?` in that
  row only. Worktrees are always listed under the repo they belong to, wherever
  their directory lives (e.g. `~/.omp/wt/...`).

### omp highlight

1. Pane tty: `readlink /proc/<Terminal.pid>/fd/0` → `/dev/pts/N`.
2. Read `~/.omp/agent/terminal-sessions/pts-N`; take line 2 (session log path).
   If missing → no highlight.
3. Liveness: only highlight if an `omp` process is attached to that tty right now
   (e.g. some process whose `/proc/<pid>/fd/0` is that tty and whose cmdline
   contains `omp`). Otherwise the file is stale → no highlight.
4. Read the tail of the session log and of every `*.jsonl` in its sibling
   directory (only the last N KB per file, my call: 512 KB; skip files whose mtime
   is older than the window). Collect paths from tool-call
   `arguments.path`/`arguments.cwd` and `details.cwd`/`details.path`, plus the
   session `cwd`, for lines whose `timestamp` is within the last 15 minutes (my
   call). Relative paths resolve against the session cwd.
5. A worktree is "in use" if any collected path is inside it; with nested
   worktrees (`ros_ws/.worktrees/v2-ws/src/robot_driver` inside
   `ros_ws/.worktrees/v2-ws`), the deepest (longest) matching worktree wins.
6. Cache parsed results per file by (size, mtime) so refreshes every few seconds
   stay cheap.
7. Any parse error → treat as "no highlight", log once via Terminator's `dbg`.

### Claude usage bar

- One bar per Terminator `Window`, at the bottom, outside the pane tree, so it is
  always visible regardless of splits, zoom, or tabs.
- Mechanism (builder may refine, constraints above): keep `Window`'s real GTK
  child a `Gtk.VBox` containing a slot container (holds the Terminal/Paned/
  Notebook) plus the usage bar. Patch `Window.add`, `Window.remove`,
  `Window.get_child`, `Window.get_children` so all Terminator code still sees the
  Terminal/Paned/Notebook as the window's only child. Existing windows at plugin
  load are rewrapped once; if a window cannot be rewrapped safely it simply gets
  no usage bar (logged via `dbg`), never an error (my call). Zoom/unzoom, new
  tabs, closing the last pane, layouts and drag-and-drop must still work
  (explicit acceptance tests below).
- Accounts shown = omp's current logins: every enabled (`disabled_cause IS NULL`)
  `auth_credentials` row that is not an MCP credential (`provider NOT LIKE
  'mcp_oauth:%'`) and has `usage_history` rows with the same `provider` and
  `account_id = json_extract(data, '$.accountId')`; accounts without usage rows
  are not shown. Ordered by provider name, then email. Only
  `accountId`/`email`/`orgName` may be extracted from `data` (it holds OAuth tokens;
  never read, log, or display anything else). As of writing this is only
  `you@example.com`; `colleague@` has `disabled_cause = 'deleted by user'`
  and must not be shown even though it still has `usage_history` rows.
- Data: open `agent.db` read-only (`file:…?mode=ro` URI, short busy timeout).
  If that open/read fails (e.g. omp not running and the `-shm` file absent), retry
  once with `?immutable=1` (safe then, since no writer exists) (my call). For each
  `(account, label)` take the row with max `recorded_at`. Refresh every 30 s (my
  call) in a worker thread. Close the connection after each read.
- Display, one segment per account, starting with the provider display name and
  credential kind (`oauth` → `subscription`, `api_key`/`apikey` → `API key`, else the
  raw value; `anthropic`→Anthropic, `openai`/`openai-codex`→OpenAI, `xai`→xAI,
  `cursor`→Cursor, `google`/`gemini`→Google, else capitalised), e.g.
  `Anthropic subscription · frederik@  5h 100% ⛔ resets 14:20 · 7d 17% · 7d Fable 0%   (updated 2 min ago)`;
  further accounts are appended with a `│` separator.
  Usage colours (Frederik): normal/green `#8ae234` below 60 %, warning/yellow
  `#fce94f` from 60 % up to 79 %, error/red `#ef2929` from 80 % or `status != 'ok'`.
  The bar has an opaque `#000000` background. Show the email local part; provider,
  kind, full email, `orgName` (if any) and exact reset time in the tooltip. "updated N min ago" uses the newest
  `recorded_at`, so stale data is visible when omp hasn't polled. If the DB is
  missing or unreadable: "Claude usage: n/a" with the reason in the tooltip.
- A reset time already in the past is shown as "reset" (the limit has rolled over
  since omp last looked).

### Active pane colours (user round)

- The active pane's title bar is painted with an animated two-hue gradient (hues
  120° apart, HSV S=0.70 V=0.62, base hue rotating once per 12 s) instead of the
  stock red, and the pane gets a 3 px frame with the same gradient, stroked inside
  the pane's allocation. Inactive panes, window focus-out and real broadcasting
  (`all`, or `group` for a pane that is in a group) keep the stock colours.
  One 100 ms timer runs only while an active pane's window has focus.
  Implemented in `ai_glance/gtk_titlebar_color.py`. The title bar height with the
  plugin equals stock (±1 px).

## Acceptance (observable)

1. With the plugin enabled, every pane in a new and in an already-open window
   shows the git button at the far left of the title bar.
2. Click → panel appears inside that pane, the terminal stays in the same pane and
   keeps working; click again → panel gone, terminal full height. No new split.
3. In `~/ros_ws/src/robot_driver`: panel shows branch `feat/my-feature`
   (as of writing), ahead/behind vs `origin/main` and the dirty flag (no worktree list).
4. In `~/ros_ws`: panel lists every repo under `src/`, one row each.
5. In a pane running omp that is working in `ros_ws/.worktrees/v2-ws/...`, that
   worktree is listed (and only the in-use ones); the same panel in a pane without omp
   lists no worktrees.
6. Outside any repo: "Not a git repository".
7. Usage bar visible at the bottom of each window; values match the latest
   `usage_history` rows; survives splitting, closing panes, zoom/unzoom, new tab,
   and closing the last pane (window closes normally).
8. Renaming `agent.db` temporarily → bar shows "n/a", Terminator keeps working.
9. Typing in a pane stays smooth while a panel refreshes a large workspace.
10. Disabling the plugin in Preferences and restarting Terminator returns
    Terminator to stock behaviour.

## Left to the builder

- Exact widget types, icon (theme icon vs. bundled SVG), CSS styling.
- Exact monkeypatching technique, as long as the anti-goals hold and the patches
  are applied once and are idempotent.
- How to detect "omp process on this tty" robustly.
- Unit-test layout for the core package (pytest with temp git repos / temp SQLite
  DB / synthetic session JSONL). GTK parts verified by running Terminator.
