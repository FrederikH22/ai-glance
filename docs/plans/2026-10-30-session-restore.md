# Session restore Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Terminator restores its windows, tabs, splits, folders and omp / Claude Code sessions (reopened, waiting) on every start, from a snapshot saved every 10 s.

**Architecture:** GTK-free `ai_glance/session_store.py` (snapshot file, lock, agent records, resume commands, layout cleaning) plus `ai_glance/gtk_session_restore.py` (startup restore through Terminator's own `create_layout`, 10 s snapshot timer with a worker thread), installed from `ai_glance_plugin.py`.

**Tech Stack:** Python 3, PyGObject/GTK 3, Terminator 2.1.3 `terminatorlib`, pytest, Xvfb smokes.

**Spec:** `docs/specs/2026-10-30-session-restore-design.md`

## Global Constraints

- Snapshot: `$XDG_STATE_HOME/ai-glance/session.json` (default `~/.local/state/ai-glance/session.json`), lock `…/session.lock` via `fcntl.flock(LOCK_EX | LOCK_NB)` held for the process lifetime.
- Snapshot format: `{"version": 1, "saved_at": <ISO>, "layout": <flat Terminator layout>, "agents": {<terminal uuid>: {"agent": "omp"|"Claude Code", "session": <omp session.jsonl path | claude session id>, "cwd": <agent cwd>}}}`.
- Atomic write (temp file in same dir, `fsync`, `os.replace`); skip when unchanged; never write zero windows; drop every window `title` key.
- Save interval 10 s; carry-forward of restored agent records 120 s.
- Agents resume with `--resume` only (never `--restore`/`--continue`). Command: `exec "$SHELL" -ic '<agent> --resume <id>; exec "$SHELL" -i'`, inner args `shlex.quote`d, agent executable `omp` / `claude`.
- Only the lock holder restores/saves; saving starts only after the restore decision; skipped-on-purpose or failed restore ⇒ that process never saves.
- Plain start = no `layout` other than `default`, no `execute`, `command`, `working_directory`, `profile`, `config`, `config_json` in `terminatorlib.optionparse.options`, exactly one window with one pane at restore time, `AI_GLANCE_NO_RESTORE` unset.
- No /proc scans or file I/O on the GTK main thread except reading widget state; nothing raises into Terminator; log via `terminatorlib.util.dbg`.
- Core modules stay GTK-free. Public repo: tests/docs use neutral example paths only, never the user's real project names. No AI attribution in commits.
- GUI runs only under `XDG_CONFIG_HOME=$(mktemp -d) XDG_STATE_HOME=$(mktemp -d) xvfb-run -a`, never the user's DISPLAY or real state dir.

## Review Focus

- Half-written or corrupt `session.json` (freeze during write, truncated JSON, unknown version) → restore skipped, normal startup window, file left untouched for inspection.
- Omp session file deleted since the snapshot / Claude session id unknown → pane is a plain shell in its folder, other panes still restored.
- Folder from the snapshot no longer exists → pane opens in `$HOME`, no exception.
- Second Terminator process (lock held elsewhere) → no restore, no save, snapshot bytes unchanged.
- Restored window title: still follows the focused pane (no `force_title`).

---

### Task 1: Session store (GTK-free)

**Files:**
- Create: `ai_glance/session_store.py`, `tests/test_session_store.py`
- Modify: `ai_glance/ompsessions.py` (`pane_agent` also returns the agent pid), its callers in `ai_glance/` and `tests/test_ompsessions.py`

**Interfaces:**
- Produces:
```python
OMP, CLAUDE  # re-exported from ompsessions
state_dir() -> Path
snapshot_path() -> Path
class SessionLock:            # .acquire() -> bool (non-blocking), .release(); keeps fd open
def agent_record(shell_pid: int | None, proc=Path('/proc'), sessions_dir=..., claude_dir=...) -> dict | None
    # {'agent', 'session', 'cwd'} via ompsessions.pane_agent (+ claude session id from sessions/<pid>.json); never raises
def clean_layout(layout: dict) -> dict            # deep copy, window 'title' keys dropped
def window_count(layout: dict) -> int
def resume_command(record: dict, exists=os.path.exists) -> str | None
    # None when omp session file is gone or record malformed
def apply_agents(layout: dict, agents: dict, exists=os.path.exists) -> dict
    # copy of layout: agent panes (matched by 'uuid') get 'command' and 'directory' = record cwd;
    # missing directories replaced by $HOME
def read_snapshot(path: Path) -> dict | None      # None on missing/corrupt/unknown version; never raises
def write_snapshot(path: Path, layout: dict, agents: dict, previous: bytes | None) -> bytes | None
    # atomic; returns written bytes, or None when skipped (unchanged or zero windows)
def merge_agents(detected: dict, restored: dict, restored_at: float, now: float, live_uuids: set) -> dict
    # detected wins; restored records kept for live uuids not detected while now - restored_at < 120
```
- `ompsessions.pane_agent` returns `(name, log, cwd, pid)`.

- [ ] **Step 1: Write failing tests** in `tests/test_session_store.py`: lock exclusivity across two `SessionLock` instances on the same path (second `acquire()` False until first releases); `clean_layout` drops window `title`, keeps terminal `title`; `resume_command` for omp/claude exact strings (e.g. `exec "$SHELL" -ic 'omp --resume /s/x.jsonl; exec "$SHELL" -i'`), quoting of a path with a space and a `'`, None for missing omp file; `apply_agents` sets command + directory by uuid and `$HOME` for a missing folder; `read_snapshot` None for missing, truncated JSON, version 2; `write_snapshot` atomic (no temp file left), returns None for zero windows and for unchanged content, previous file intact when skipped; `merge_agents` carry-forward boundary (119 s kept, 121 s dropped, dead uuid dropped, detected overrides); `agent_record` for fake omp and claude panes (reuse the fake `/proc` helpers from `tests/test_ompsessions.py`).
- [ ] **Step 2: Run** `python3 -m pytest tests/test_session_store.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement** `session_store.py`; extend `pane_agent` to return the pid and update its callers (`pane_agent_paths`, tests).
- [ ] **Step 4: Run** `python3 -m pytest tests -q` → all PASS.
- [ ] **Step 5: Commit** `feat(restore): GTK-free session snapshot store`

### Task 2: Restore at startup and periodic snapshot

**Review:** per-task — concurrency (worker thread + GLib timers) and startup behaviour change

**Files:**
- Create: `ai_glance/gtk_session_restore.py`, `tests/smoke_session_restore.py`
- Modify: `ai_glance_plugin.py`

**Interfaces:**
- Consumes: everything from Task 1.
- Produces: `install()` (idempotent; schedules the startup decision with `GLib.idle_add`), `SAVE_SECONDS = 10`, module state readable by the smoke (`state()` → `'restored'|'no-snapshot'|'skipped'|'failed'|'not-owner'`, `saving()` → bool).

- [ ] **Step 1: Write the smoke** (Xvfb, temp config + state dirs, fake agent records via monkeypatched `session_store.agent_record`): process A builds two windows (window 1: vertical + horizontal split, window 2: two tabs), panes in distinct temp folders, one pane marked omp (existing temp `.jsonl`), one claude, one plain; waits for a save; checks `session.json` (no window title, agents keyed by uuid). Process B (fresh interpreter, same state dir) starts like `/usr/bin/terminator` (default layout, then plugin load) and checks: same window/tab/split structure, ratios within 0.05, folders, profiles; agent panes' `layout_command` equals `resume_command(...)`, plain pane has none; the startup window is gone; `state()=='restored'`; window title of a restored window changes with the focused pane. Process C while B holds the lock: `state()=='not-owner'`, snapshot bytes unchanged after 12 s. Process D with `AI_GLANCE_NO_RESTORE=1`: no restore, never saves. Corrupt snapshot: normal window, `state()=='failed'` or `'no-snapshot'`, file unchanged. `kill -9` of a saving process then restart restores (acceptance 4).
- [ ] **Step 2: Run** the smoke → FAIL.
- [ ] **Step 3: Implement** `gtk_session_restore.py` per the spec ("Saving", "Restoring", "One owner"); call `install()` from `ai_glance_plugin.py` like the other modules.
- [ ] **Step 4: Run** the smoke and all existing smokes → ALL PASS, clean stderr.
- [ ] **Step 5: Commit** `feat(restore): reopen panes and agent sessions on Terminator start`

### Task 3: Real launch check and docs

**Files:**
- Modify: `README.md`

- [ ] **Step 1:** Real launch under Xvfb with isolated XDG dirs and the plugin installed in the temp config: `terminator -u`, build splits, wait 12 s, `kill -9`, start `terminator -u` again; screenshot before and after (`import -window root`), check equal layout. Use fake agent commands only (no real omp/claude processes inside Xvfb).
- [ ] **Step 2:** README: what is restored, what is not (scrollback, non-agent commands), agents wait (`--resume`), snapshot location, `AI_GLANCE_NO_RESTORE=1`, second-process rule, closing-windows-one-by-one note.
- [ ] **Step 3: Run** `python3 -m pytest tests -q` and every `tests/smoke_*.py` → PASS.
- [ ] **Step 4: Commit** `docs: session restore`
