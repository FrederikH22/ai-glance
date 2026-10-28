# Terminator git panel + Claude usage bar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Terminator 2.1.3 plugin that adds a per-pane git toggle panel (repos, worktrees, omp-in-use highlight) and a per-window bottom bar with omp's Claude usage.

**Architecture:** GTK-free core package `terminator_omp/` (git, omp session, usage readers + text formatting), unit tested with pytest. Two GTK modules monkeypatch `terminatorlib` (titlebar button + in-pane panel; window wrapper + usage bar), doing all I/O in worker threads and applying results via `GLib.idle_add`. A thin plugin file `omp_panel.py` installs both.

**Tech Stack:** Python 3 (system), PyGObject/GTK 3.24, Terminator 2.1.3 `terminatorlib`, `git` CLI, stdlib `sqlite3` (SQLite 3.45.1), pytest 7.4.4.

**Spec:** `docs/specs/2026-10-28-terminator-git-usage-design.md`

## Global Constraints

- Never modify files under `/usr/lib/python3/dist-packages/terminatorlib/`; patch at runtime only, idempotently.
- No git or SQLite work on the GTK main thread. No `git fetch`. No network calls.
- Git: `GIT_OPTIONAL_LOCKS=0`, `--no-optional-locks`, per-command timeout 2 s.
- `agent.db`: open `file:<path>?mode=ro` (URI), busy timeout ≤ 500 ms; on failure retry once with `?immutable=1`; never write. From `auth_credentials.data` extract only `$.accountId` and `$.email`.
- Accounts = `auth_credentials` rows with `provider='anthropic' AND disabled_cause IS NULL`.
- Refresh: git panel every 3 s while open; usage bar every 30 s.
- omp highlight window: 15 minutes; tail read 512 KiB per JSONL file.
- Usage colours: normal < 0.75, warning ≥ 0.75, error ≥ 1.0 or `status != 'ok'`.
- Every failure in plugin code degrades to "n/a"/no highlight/`?`; nothing raises into Terminator. Log via `terminatorlib.util.dbg`.
- Core package must not import `gi`.
- No AI attribution in commits.

## Review Focus

- A JSONL tail that starts mid-line (tail cut) or ends with a half-written line → those lines skipped, the rest parsed. Test in Task 2.
- A pane closed (or panel toggled off) while a refresh thread is still running → result discarded, no GTK warnings, no callbacks on destroyed widgets. Smoke in Task 4.
- A git command hanging or failing in one repo of a workspace → only that row shows `?`, other repos render. Test in Task 1.
- `agent.db` missing, locked, or `-shm` unavailable → bar shows "Claude usage: n/a" with reason in tooltip, Terminator unaffected. Test in Task 3.
- A pane whose shell already exited (`Terminal.pid` dead or `None`) → panel still renders git info for last known cwd or "n/a", no highlight, no exception. Smoke in Task 4.

## File Structure

```
omp_panel.py                       Terminator plugin entry (Task 6)
install.sh                         symlinks + enable plugin (Task 6)
README.md                          usage, install, uninstall (Task 6)
terminator_omp/__init__.py         empty (Task 1)
terminator_omp/gitinfo.py          git + ROS workspace model (Task 1)
terminator_omp/ompsessions.py      tty → omp session → paths in use (Task 2)
terminator_omp/usage.py            agent.db reader + bar text (Task 3)
terminator_omp/gtk_git_panel.py    titlebar button + in-pane panel (Task 4)
terminator_omp/gtk_usage_bar.py    window wrapper + bottom bar (Task 5)
tests/test_gitinfo.py, tests/test_ompsessions.py, tests/test_usage.py
tests/smoke_git_panel.py          live-display GTK smoke, run manually (Task 4)
tests/smoke_usage_bar.py          live-display GTK smoke, run manually (Task 5)
```

Dependencies: Tasks 1, 2, 3 share no files (wave 1, parallel). Task 4 consumes 1+2 and Task 5 consumes 3; they share no files (wave 2, parallel). Task 6 consumes 4+5 (wave 3).

---

### Task 1: Git and ROS workspace model

**Files:**
- Create: `terminator_omp/__init__.py`, `terminator_omp/gitinfo.py`
- Test: `tests/test_gitinfo.py`

**Interfaces:**
- Produces:
```python
@dataclass(frozen=True)
class WorktreeState:
    path: Path              # resolved
    branch: str | None      # None when detached
    head: str               # short sha ('' if unknown)
    ahead: int | None
    behind: int | None
    dirty: bool | None      # None = unknown/prunable
    prunable: bool
    locked: bool
    error: str | None       # set → UI shows '?'

@dataclass(frozen=True)
class RepoState:
    root: Path              # main worktree path (resolved)
    name: str               # root.name
    base: str | None        # e.g. 'origin/main'
    worktrees: tuple[WorktreeState, ...]   # main worktree first
    error: str | None

@dataclass(frozen=True)
class PanelModel:
    kind: Literal['workspace', 'repo', 'none']
    path: Path              # workspace root / repo toplevel / cwd
    repos: tuple[RepoState, ...]

def is_ros_workspace_root(path: Path) -> bool
def find_ros_workspace_root(path: Path) -> Path | None   # path itself or nearest ancestor
def workspace_repos(ws_root: Path) -> list[Path]          # sorted; git dirs under src/
def repo_state(repo_path: Path, timeout: float = 2.0) -> RepoState   # never raises
def build_model(cwd: Path, timeout: float = 2.0) -> PanelModel       # never raises
```
`repo_state` takes any path inside a repo or linked worktree; it lists *all* worktrees of that repository (`git worktree list --porcelain`). The row a pane is "in" is identified by the UI comparing paths, not by the model.

- [ ] **Step 1: Write failing tests** in `tests/test_gitinfo.py`. Fixture builds in `tmp_path`: bare `origin.git`; clone `ws/src/repo_a` with commit on `main`, pushed, `origin/HEAD` set (`git remote set-head origin main`); a linked worktree `ws/.worktrees/w1/src/repo_a` on branch `feat` with 2 extra commits; `ws/build/`; a second repo `ws/src/nested/repo_b` without remote, branch `master`. Set `GIT_AUTHOR_*`/`GIT_COMMITTER_*` env. Tests:
  - `test_workspace_root_detection`: `is_ros_workspace_root(ws)` True; `ws/src` False; `find_ros_workspace_root(ws/src)` == ws; `find_ros_workspace_root(tmp_path)` is None.
  - `test_workspace_repos_finds_nested_and_stops_at_repo`: == `[ws/src/nested/repo_b, ws/src/repo_a]`.
  - `test_repo_state_lists_worktrees_main_first`: `repo_state(ws/src/repo_a)`: `base == 'origin/main'`, `worktrees[0].path == repo_a`, `worktrees[1].branch == 'feat'`, `worktrees[1].ahead == 2`, `behind == 0`.
  - `test_dirty_flag`: touch untracked file in worktree → that worktree `dirty is True`, other `False`.
  - `test_detached_head`: detach in main worktree → `branch is None`, `head` == short sha, counts still ints.
  - `test_base_fallback_master`: `repo_b` → `base == 'master'`.
  - `test_prunable_worktree`: `rm -rf` the linked worktree dir → `prunable is True`, `dirty is None`, `ahead is None`.
  - `test_build_model_kinds`: `build_model(ws)` kind `'workspace'` with 2 repos; `build_model(ws/src)` kind `'workspace'`, path ws; `build_model(repo_a/subdir)` kind `'repo'`; `build_model(tmp_path/'empty')` kind `'none'`.
  - `test_git_failure_isolated` (Review Focus): monkeypatch the module's subprocess runner so calls with `cwd` under `repo_b` raise `subprocess.TimeoutExpired` → `build_model(ws)` still returns both repos; `repo_b` state has `error` set; `repo_a` has none.
- [ ] **Step 2: Run** `pytest tests/test_gitinfo.py -v` → FAIL (module missing).
- [ ] **Step 3: Implement** `gitinfo.py`. All git calls go through one private `_git(args, cwd, timeout) -> str` using `subprocess.run([... 'git', '--no-optional-locks', ...], env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}, timeout=timeout, capture_output=True, text=True, check=True)`. Base: `git symbolic-ref --short refs/remotes/origin/HEAD`, else first existing of `main`, `master` (`git rev-parse --verify --quiet`). Counts: `git rev-list --left-right --count <base>...HEAD` run in each worktree (left = behind, right = ahead). Dirty: `git status --porcelain --untracked-files=normal` non-empty. Workspace scan skips `build`, `install`, `log` and dot-dirs, does not descend below a dir containing `.git`.
- [ ] **Step 4: Run** `pytest tests/test_gitinfo.py -v` → all PASS.
- [ ] **Step 5: Commit** `git add terminator_omp/__init__.py terminator_omp/gitinfo.py tests/test_gitinfo.py && git commit -m "feat: git and ROS workspace model"`

### Task 2: omp session → worktrees in use

**Files:**
- Create: `terminator_omp/ompsessions.py`
- Test: `tests/test_ompsessions.py`

**Interfaces:**
- Produces:
```python
TERMINAL_SESSIONS = Path.home() / '.omp/agent/terminal-sessions'

def pane_tty(shell_pid: int | None, proc: Path = Path('/proc')) -> str | None   # '/dev/pts/10'
def session_log_for_tty(tty: str, sessions_dir: Path = TERMINAL_SESSIONS) -> Path | None
def omp_running_on_tty(tty: str, proc: Path = Path('/proc')) -> bool
def recent_paths(log: Path, now: datetime, window: timedelta = timedelta(minutes=15),
                 tail_bytes: int = 512 * 1024) -> set[Path]
def worktrees_in_use(paths: Iterable[Path], worktrees: Iterable[Path]) -> set[Path]
def pane_worktrees_in_use(shell_pid: int | None, worktrees: Iterable[Path], now: datetime,
                          proc: Path = Path('/proc'),
                          sessions_dir: Path = TERMINAL_SESSIONS) -> set[Path]   # never raises
```
`now` is timezone-aware UTC. Session file `pts-N`: line 1 cwd, line 2 log path. `omp_running_on_tty`: some `/proc/<pid>` whose `fd/0` resolves to `tty` and whose `cmdline` argv has an element with basename `omp`.

- [ ] **Step 1: Write failing tests** with fake `/proc` and sessions dirs in `tmp_path` (symlinks `proc/<pid>/fd/0 -> /dev/pts/7`, `cmdline` with NUL-separated argv) and synthetic JSONL logs (lines shaped like spec: `{"type":"session","cwd":...,"timestamp":...}`, `{"type":"message","timestamp":...,"message":{"content":[{"arguments":{"path":...}}]}}`, `{"type":"message","timestamp":...,"message":{"details":{"cwd":...}}}`), subagent log in sibling dir `<stem>/sub.jsonl`:
  - `test_pane_tty_reads_fd0`: pid 100 → `'/dev/pts/7'`; pid None → None; missing pid → None.
  - `test_session_log_for_tty`: `pts-7` content → returns line-2 path; missing file → None.
  - `test_omp_running_on_tty`: argv `bun\0/home/u/.bun/bin/omp\0` on pts/7 → True; same on pts/8 → False for pts/7; argv `vim\0omp_notes.md\0` → False.
  - `test_recent_paths_window`: entries at now-5min and now-30min → only the 5-min paths; includes subagent log paths; relative `arguments.path` resolved against session `cwd`.
  - `test_recent_paths_tail_cut` (Review Focus): `tail_bytes` smaller than file so the tail starts mid-line, and the last line is truncated JSON → no exception; complete in-window lines inside the tail are returned.
  - `test_worktrees_in_use_deepest_wins`: worktrees `/w/ws` and `/w/ws/src/repo`; path `/w/ws/src/repo/x.cpp` → `{/w/ws/src/repo}`; path `/w/ws/README` → `{/w/ws}`; paths compared after `resolve()` (symlinked dir in tmp_path maps to its target).
  - `test_pane_worktrees_in_use_stale_session`: session file present, no omp process on tty → `set()`.
- [ ] **Step 2: Run** `pytest tests/test_ompsessions.py -v` → FAIL.
- [ ] **Step 3: Implement.** Tail read: seek to `max(0, size - tail_bytes)`, drop the first partial line when offset > 0, `json.loads` per line inside `try`. Skip sibling `*.jsonl` whose mtime < now - window. Cache parsed `(path, size, mtime) -> list[(timestamp, Path)]` in a module-level dict guarded by a `threading.Lock` (called from worker threads).
- [ ] **Step 4: Run** `pytest tests/test_ompsessions.py -v` → PASS.
- [ ] **Step 5: Commit** `git add terminator_omp/ompsessions.py tests/test_ompsessions.py && git commit -m "feat: map pane tty to omp worktrees in use"`

### Task 3: Claude usage reader and bar text

**Files:**
- Create: `terminator_omp/usage.py`
- Test: `tests/test_usage.py`

**Interfaces:**
- Produces:
```python
DEFAULT_DB = Path.home() / '.omp/agent/agent.db'

@dataclass(frozen=True)
class UsageWindow:
    label: str              # 'Claude 7 Day (Fable)'
    short: str              # '7d Fable'
    used_fraction: float
    status: str
    resets_at: datetime | None    # aware UTC
    recorded_at: datetime         # aware UTC

@dataclass(frozen=True)
class AccountUsage:
    email: str
    windows: tuple[UsageWindow, ...]   # order: 5h, 7d, then others by label

@dataclass(frozen=True)
class UsageSnapshot:
    accounts: tuple[AccountUsage, ...]
    updated_at: datetime | None   # newest recorded_at
    error: str | None

Severity = Literal['normal', 'warning', 'error']

def read_usage(db_path: Path = DEFAULT_DB) -> UsageSnapshot          # never raises
def short_label(label: str) -> str
def severity(w: UsageWindow, now: datetime) -> Severity
def format_bar(snap: UsageSnapshot, now: datetime) -> tuple[str, str, Severity]
    # (Pango markup, plain tooltip, overall worst severity)
```
`short_label`: `'Claude 5 Hour'→'5h'`, `'Claude 7 Day'→'7d'`, `'Claude 7 Day (Fable)'→'7d Fable'`. Segment text: `frederik@  5h 100% ⛔ resets 14:20 · 7d 17% · 7d Fable 0%`, accounts joined by `  │  `, suffix `  (updated N min ago)`; reset time in local time, shown only for non-ok/≥ 0.75 windows; a reset time already past shows `reset`. Error: text `Claude usage: n/a`, tooltip = error. Each window segment gets its own colour span by severity.

- [ ] **Step 1: Write failing tests** with a temp DB (WAL mode) created with the real column names of `usage_history` (`id, recorded_at, provider, account_key, email, account_id, limit_id, label, window_label, used_fraction, status, resets_at`) and `auth_credentials` (`id, provider, credential_type, data, disabled_cause, identity_key, created_at, updated_at`), `data` JSON containing `accountId`, `email`, `access: "SECRET-TOKEN"`:
  - `test_only_enabled_accounts`: frederik enabled, colleague `disabled_cause='deleted by user'`, both with usage rows → only frederik.
  - `test_latest_row_per_label`: two rows for `Claude 5 Hour` → newest `used_fraction` wins; `updated_at` == newest `recorded_at`.
  - `test_secret_never_surfaces`: `'SECRET-TOKEN' not in repr(snapshot)` and not in either `format_bar` string.
  - `test_short_labels`: the three mappings above.
  - `test_format_bar_exhausted`: 5h `1.0`/`exhausted` resets in future → markup contains `5h 100%`, `resets`, severity `'error'`; 7d `0.17` → normal.
  - `test_reset_in_past`: shows `reset`.
  - `test_missing_db` (Review Focus): nonexistent path → `error` set, `accounts == ()`; `format_bar` text `Claude usage: n/a`.
  - `test_locked_db` (Review Focus): another connection holds `PRAGMA locking_mode=EXCLUSIVE` + write txn → `read_usage` returns within 2 s with `error` set (or data via fallback), never raises.
  - `test_no_side_files_created`: DB closed cleanly (no `-wal`/`-shm` present) → after `read_usage`, directory listing unchanged and data still returned (exercises the `immutable=1` fallback when needed).
- [ ] **Step 2: Run** `pytest tests/test_usage.py -v` → FAIL.
- [ ] **Step 3: Implement** `read_usage`: one SQL query joining latest rows per `(account_id, label)` with `json_extract(c.data,'$.accountId')` / `'$.email'` filtered as in Global Constraints; connection opened per call and closed in `finally`.
- [ ] **Step 4: Run** `pytest tests/test_usage.py -v` → PASS. Also run once against the real DB: `python3 -c "from terminator_omp.usage import *;from datetime import *;print(format_bar(read_usage(), datetime.now(timezone.utc))[0])"` → shows `frederik@` only.
- [ ] **Step 5: Commit** `git add terminator_omp/usage.py tests/test_usage.py && git commit -m "feat: read Claude usage from omp agent.db"`

### Task 4: Titlebar git button and in-pane panel

**Review:** per-task — concurrency

**Files:**
- Create: `terminator_omp/gtk_git_panel.py`, `tests/smoke_git_panel.py`

**Interfaces:**
- Consumes: `gitinfo.build_model`, `PanelModel`/`RepoState`/`WorktreeState`; `ompsessions.pane_worktrees_in_use`.
- Produces:
```python
def install() -> None            # idempotent; patches Titlebar.__init__, retrofits existing terminals
def toggle(terminal) -> None     # open/close panel in that Terminal
class GitPanel(Gtk.Box): ...     # internal
```

- [ ] **Step 1: Write the smoke** `tests/smoke_git_panel.py` (needs a display; not part of pytest): builds Terminator objects the way `/usr/bin/terminator` does (`Terminator()`, `Window()`, `Terminal()`, `window.add(term)`, `term.spawn_child()`), with cwd `~/ros_ws/src/robot_driver`; calls `install()` *after* the first terminal exists (retrofit path). Checks, printing PASS/FAIL per line and exiting non-zero on failure:
  - every terminal's titlebar hbox child 0 is the git button;
  - `toggle(term)` → a `GitPanel` is a child of `term` directly after the titlebar; after ≤ 5 s main-loop iterations it contains a row for branch of `git -C <cwd> branch --show-current`; `term.vte` is still a descendant of `term`; no new `Paned` created;
  - `toggle(term)` again → panel gone;
  - split (`term.key_split_vert()`), new terminal also gets a button;
  - open panel, then immediately close the terminal (`term.close()`) while refresh in flight; run loop 5 s → no exception, no `Gtk-CRITICAL` on stderr (Review Focus);
  - terminal whose child was killed (`os.kill(term.pid, SIGKILL)`), toggle → panel renders, no exception (Review Focus).
- [ ] **Step 2: Run** `python3 tests/smoke_git_panel.py` → FAIL (module missing).
- [ ] **Step 3: Implement.** Patch: wrap `Titlebar.__init__` to insert the button at index 0 of `self.get_child()`; button click → `toggle(self.terminal)`. Panel packing: `term.pack_start(panel, False, True, 0)` then `term.reorder_child(panel, index_of_titlebar + 1)` (or before titlebar when `title_at_bottom`); `Gtk.ScrolledWindow` with max height 40 % of `term.get_allocated_height()`. Refresh: `GLib.timeout_add_seconds(3, ...)` + immediate first run; worker `threading.Thread(daemon=True)` computes `(build_model(cwd), pane_worktrees_in_use(term.pid, all worktree paths, now))`; result applied with `GLib.idle_add` only if the panel is still open and its generation counter matches (drop stale results). One in-flight refresh per panel. On close/destroy: remove timeout source, bump generation. Rendering per spec "What the panel shows": header `vs <base>`; rows with `↑n ↓n`, `●` dirty, `🔒` locked, greyed prunable, `?` on error, highlighted rows bold + accent + `◆` marker. Use `Gtk.Label` with Pango markup (escape all paths/branches with `GLib.markup_escape_text`).
- [ ] **Step 4: Run** `python3 tests/smoke_git_panel.py` → all PASS. Also `pytest tests/ -q` still green.
- [ ] **Step 5: Commit** `git add terminator_omp/gtk_git_panel.py tests/smoke_git_panel.py && git commit -m "feat: titlebar git button with in-pane panel"`

### Task 5: Window usage bar

**Review:** per-task — concurrency

**Files:**
- Create: `terminator_omp/gtk_usage_bar.py`, `tests/smoke_usage_bar.py`

**Interfaces:**
- Consumes: `usage.read_usage`, `usage.format_bar`.
- Produces: `def install() -> None` (idempotent; patches `terminatorlib.window.Window`, rewraps existing windows).

- [ ] **Step 1: Write the smoke** `tests/smoke_usage_bar.py` (display required), same bootstrap as Task 4, `install()` after the first window exists. Checks:
  - `Gtk.Bin.get_child(window)` is the wrapper box; `window.get_child()` and `window.get_children()[0]` return the Terminal; bar label text after ≤ 2 s main-loop matches `format_bar(read_usage(), now)[0]` (modulo the "updated" minute);
  - split vert, split horiz, `window.zoom(term)`/`window.unzoom()`, `window.tab_new()` (Notebook becomes window child via patched API, bar still present), close tab, close all terminals → window emits `destroy` (hoover works);
  - `DEFAULT_DB` monkeypatched to a missing path → bar shows `Claude usage: n/a` (Review Focus);
  - destroy window while a read is in flight → no exception / `Gtk-CRITICAL`.
- [ ] **Step 2: Run** `python3 tests/smoke_usage_bar.py` → FAIL.
- [ ] **Step 3: Implement.** Per window: outer `Gtk.VBox` = `[slot: Gtk.Box (expand, holds ≤ 1 child), Gtk.Separator, UsageBar]`, placed with the real `Gtk.Window.add`. Do **not** subclass or replace `terminatorlib.window.Window` (Factory's `maker.isinstance` compares against the module attribute, so existing windows would stop matching). Instead patch methods on the class:
  - `Window.add` / `Window.remove`: call the saved originals (so Terminator's signal wiring/`disconnect_child` run exactly once) inside a context manager that temporarily replaces `Gtk.Window.add` / `Gtk.Window.remove` with a function that, for *this* window and any widget other than the wrapper, forwards to `slot.add`/`slot.remove`, and otherwise calls the real method. Safe because GTK calls are main-thread and synchronous; always restored in `finally`.
  - `Window.get_child` and `Window.get_children`: return the slot's child (`[child]` / `[]` semantics as the originals) when the window is wrapped.
  - Rewrap existing windows: `Gtk.Window.remove(window, child)`, build wrapper, `slot.add(child)`, `Gtk.Window.add(window, wrapper)`, `show_all` on wrapper only, restore focus to the previously focused terminal; on any exception put the child back directly and skip the bar.
  - Refresh: `GLib.timeout_add_seconds(30, ...)` + immediate; `read_usage` in a daemon thread; apply with `GLib.idle_add` only if the window is not destroyed; a 60 s GLib timer re-renders "updated N min ago" from the cached snapshot without I/O; remove both sources on window `destroy`. Severity → CSS class `warning`/`error` on the label.
- [ ] **Step 4: Run** `python3 tests/smoke_usage_bar.py` → all PASS; `python3 tests/smoke_git_panel.py` → still PASS (both patches together).
- [ ] **Step 5: Commit** `git add terminator_omp/gtk_usage_bar.py tests/smoke_usage_bar.py && git commit -m "feat: per-window Claude usage bar"`

### Task 6: Plugin entry, install, docs, live acceptance

**Files:**
- Create: `omp_panel.py`, `install.sh`, `README.md`

**Interfaces:**
- Consumes: `gtk_git_panel.install()`, `gtk_usage_bar.install()`.
- Produces: Terminator plugin class `OmpPanel` (in `AVAILABLE = ['OmpPanel']`).

- [ ] **Step 1: Implement** `omp_panel.py`: `class OmpPanel(plugin.Plugin)`, `capabilities = ['omp_panel']`, `__init__` calls both `install()`s inside `try/except Exception` (log with `err`, never raise). `install.sh`: `mkdir -p ~/.config/terminator/plugins`; `ln -sfn` `omp_panel.py` and `terminator_omp/` there; then add `OmpPanel` to `enabled_plugins` via `python3 -c` using `terminatorlib.config.Config` (`cfg['enabled_plugins']` + `'OmpPanel'` if absent, `cfg.save()`), keeping existing entries; backup config to `config.bak-omp-panel` first; print "restart Terminator". `--uninstall` removes the symlinks and the `OmpPanel` entry. README: what it does, install/uninstall, the monkeypatch/Terminator-update risk, where data comes from.
- [ ] **Step 2: Run** `bash install.sh` then `python3 -c "from terminatorlib.config import Config;print('OmpPanel' in Config()['enabled_plugins'])"` → `True`; `ls -l ~/.config/terminator/plugins` shows both symlinks.
- [ ] **Step 3: Live acceptance.** Start a separate instance: `terminator -u --working-directory ~/ros_ws/src/robot_driver &` (`-u` = no DBus, so the user's running Terminator is untouched). Walk spec acceptance items 1–10; take a screenshot of the window with panel open (`import -window <id>` or `gnome-screenshot -w`) and of `~/ros_ws` workspace view; record results in the task report. Item 5: start `omp` in a pane, have it read a file under a worktree, open the panel, check highlight.
- [ ] **Step 4: Run** `pytest tests/ -q`, `python3 tests/smoke_git_panel.py`, `python3 tests/smoke_usage_bar.py` → all PASS.
- [ ] **Step 5: Commit** `git add omp_panel.py install.sh README.md && git commit -m "feat: Terminator plugin entry, installer, docs"`
