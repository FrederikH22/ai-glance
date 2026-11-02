# ai-glance: restore panes and agent sessions after a restart

Date: 2026-10-30 · Target: Terminator 2.1.3 with the ai-glance plugin

## Why

The laptop sometimes freezes under load from several coding agents (omp, Claude Code)
and has to be restarted. Afterwards the user rebuilds every Terminator window, split
and tab by hand, has to remember which agent session ran where, and resumes each one
manually. Terminator's own "save last session" plugin only saves when a pane is closed
normally, so a freeze loses everything.

Success: after any restart (crash or normal), starting Terminator brings back the same
windows, tabs and splits, every pane in its folder, and every pane that ran an agent
has that agent session reopened, sitting at its prompt and waiting for the user.

## Decisions made by the user

- Agents reopen and **wait**. Nothing continues on its own (the freeze was caused by
  agents overloading the laptop). omp is reopened with `--resume`, never `--restore`.
- The restore runs **every time Terminator starts** (crash or not).

## Goals

1. **Snapshot while working.** Periodically save the layout of all windows of the
   Terminator process: windows (size, position, maximised/fullscreen), tabs, splits
   (orientation, ratio), and per pane: profile, custom title, group, working folder.
2. **Agent per pane.** For each pane running an agent, record which agent and which
   session:
   - omp: the session log path (from `~/.omp/agent/terminal-sessions/pts-N`, the same
     lookup `ompsessions.pane_agent` already does) and the omp process's cwd.
   - Claude Code: the session id from `~/.claude/sessions/<pid>.json` and the claude
     process's cwd.
3. **Restore on every start.** When Terminator starts, rebuild the saved layout. Agent
   panes start the agent with its session reopened; when the agent exits, the pane is an
   interactive shell in that folder. Other panes start a plain shell in their folder.
4. **Survive a freeze.** At most one snapshot interval of changes is lost; a half-written
   snapshot never replaces a good one.

## Non-goals

- Re-running non-agent commands (`ssh …`, `ros2 launch …`, editors). Those panes get a
  shell in their folder only.
- Restoring scrollback (text on screen), shell history, environment variables.
- Continuing interrupted agent turns or background agents (`omp --restore`, any
  "continue" flag).
- Agents other than omp and Claude Code.

## Anti-goals (failure even if it "works")

- An agent that resumes work on its own after the restart.
- The same agent session opened twice at once (e.g. a second Terminator process started
  with `terminator -u` while the first still runs, restoring the same sessions again).
- Losing the saved state because Terminator was closed: closing the last window must not
  overwrite the snapshot with "nothing open".
- Freezing or stuttering Terminator: no /proc scans or file writes on the GTK main thread
  beyond reading widget state.
- Breaking the window-title fix: a restored window must not get a forced (frozen) title.
  Terminator's `Window.describe_layout` stores `title`, and `create_layout` turns it into
  `force_title`, so the window `title` key is dropped from snapshots.
- Writing work data into the public repo (tests and docs use neutral example paths).

## Facts found (Terminator 2.1.3, omp 18.13, Claude Code 2.1)

- `Terminator().describe_layout(save_cwd=True)` returns the flat layout dict Terminator's
  config uses (`window0`, `child1`, `terminal2`, … with `type`, `parent`, `order`,
  `ratio`, `size`, `position`, `maximised`, `profile`, `directory`, `uuid`, tab `labels`).
- `Terminator().create_layout(name)` builds windows from a layout stored in `Config`
  (`config.layout_get_config(name)`); `Terminal.create_layout` honours per-terminal
  `command` (run as `$SHELL -c <command>`), `directory`, `profile`, `title`, `group`.
- Plugins load lazily while the first terminal is constructed (inside Terminator's own
  startup `create_layout('default')`), so the restore must run after startup finishes
  (e.g. from a `GLib.idle_add` scheduled at plugin load), then close the untouched
  initial window.
- Command-line options are in `terminatorlib.optionparse.options`.
- `omp --resume <session.jsonl path>` reopens a session and waits; `claude --resume
  <session-id>` does the same. Claude looks sessions up per project, so it must start in
  the folder it ran in (the claude process's cwd).
- Terminator's PATH may not contain `~/.bun/bin` (where `omp` lives); `$SHELL -c` does not
  read `.bashrc`.

## Design

### Snapshot file

`~/.local/state/ai-glance/session.json` (XDG state dir; `$XDG_STATE_HOME` honoured):

```json
{"version": 1, "saved_at": "<ISO time>",
 "layout": { "<Terminator flat layout dict>" },
 "agents": {"<terminal uuid>": {"agent": "omp", "session": "/…/x.jsonl", "cwd": "/…"}}}
```

- Written atomically (temp file in the same dir + `fsync` + `os.replace`).
- Not written when nothing changed since the last write.
- Never written with zero windows (keeps the last real state across a normal close).
  "Last state" is what the last save saw (my call): closing windows one by one leaves the
  last remaining window as the saved state; a shutdown/logout/freeze that takes all
  windows at once keeps all of them.
- Window `title` keys removed before saving.

### Saving

- Every 10 s (my call), and only in a process that is allowed to save:
  - it holds the session lock, **and**
  - its startup restore has finished (success, or "no snapshot existed"). A process that
    skipped the restore on purpose (options, `AI_GLANCE_NO_RESTORE`, more than one window
    at startup) or whose restore failed never saves, so the good snapshot survives.
  The timer starts only after that decision, so the untouched startup window is never
  saved over a real snapshot.
- Main thread: `describe_layout(save_cwd=True)` and the (uuid, shell pid) of every pane.
- Worker thread: agent detection per pane, then the atomic write. At most one save in
  flight. Agent detection needs the agent's pid and cwd for both agents:
  `ompsessions.pane_agent` is extended to also return the agent pid (its callers are
  updated); the cwd is `/proc/<agent pid>/cwd`.
- **Agent pane folder:** for an agent pane the saved `directory` is the agent process's
  cwd (it wins over the shell's cwd), so the agent resumes where it ran and the shell left
  after it exits is in the same folder.
- **Carry-forward after a restore (my call):** for 120 s after a restore, a restored pane
  whose agent is not (yet) detected keeps its restored agent record, so a crash right
  after the restore still has the sessions. After that only detected agents are saved.

### Restoring

- Runs once per Terminator process, after startup, only when **all** hold:
  - this process holds the session lock (below);
  - the start was a plain start (my call): `optionparse.options` has no `layout` other
    than `default`, no `execute`/`command`, no `working_directory`, no `profile`, no
    `config`/`config_json`; any of these means the user asked for something specific;
  - at that moment the process has exactly one window with exactly one pane (the
    untouched startup window);
  - a snapshot with at least one window exists;
  - `AI_GLANCE_NO_RESTORE` is not set (escape hatch, my call).
- Steps: add the snapshot layout to the in-memory config under a private name (never
  saved to the config file), set each agent pane's `command`, call
  `create_layout`/`layout_done`, remove the private layout again, then close the
  untouched startup window.
- Agent command (my call), run by Terminator as `$SHELL -c <command>`:
  `exec "$SHELL" -ic '<agent> --resume <quoted id>; exec "$SHELL" -i'` (inner arguments
  with `shlex.quote`). The `exec`s keep a single process as the pane's `Terminal.pid`, so
  later snapshots read the right cwd and tty; the interactive shell loads the user's PATH
  (`omp` lives in `~/.bun/bin`); after the agent exits the pane is the user's own
  interactive shell. For omp the agent is `omp`, for Claude Code `claude`. If the omp
  session file no longer exists, the pane gets a plain shell. A profile with
  `use_custom_command` overrides layout commands in Terminator; such panes get no agent
  (accepted limitation, the user's profiles don't use it).
- If anything fails, the user keeps the normal startup window; errors go to `dbg`.

### One owner per desktop session

`~/.local/state/ai-glance/session.lock`, `fcntl.flock(LOCK_EX | LOCK_NB)` held for the
process lifetime. Only the lock holder restores and saves. A second Terminator process
(`terminator -u` while one runs) does neither. New windows of the same process (via
DBus) are part of the owner's snapshot.

### Components

- `ai_glance/session_store.py` (GTK-free): snapshot read/write, lock, agent records,
  resume command building, layout cleaning. Unit tested.
- `ai_glance/gtk_session_restore.py`: timer, worker thread, restore at startup,
  `install()` called from `ai_glance_plugin.py`.

## Acceptance

1. With two windows (one with a vertical and a horizontal split, one with two tabs),
   after quitting and starting Terminator again the same windows, splits (ratios within a
   few %), tabs and folders come back, with the same profiles.
2. A pane that ran omp comes back running `omp --resume` on the same session file and
   waits at its prompt; a pane that ran Claude Code comes back with `claude --resume` on
   the same session id in the same folder. Exiting the agent leaves a shell in that folder.
3. A pane that ran `ssh …` comes back as a shell in its folder; nothing is re-run.
4. Killing Terminator with `kill -9` (simulated freeze) and starting again restores the
   state from at most 10 s before the kill.
5. A second `terminator -u` while one runs opens normally, restores nothing, and does not
   change the snapshot.
6. `AI_GLANCE_NO_RESTORE=1 terminator` and `terminator -l <layout>` start without restore.
7. The window title still follows the focused pane after a restore (not frozen).
8. Closing all windows normally and starting again restores the last state with windows.

## Left to the builder

- How the untouched startup window is closed (or reused) with the least flicker.
- Exact snapshot comparison for "unchanged".
- How acceptance 1–4 are driven under Xvfb (fake agent processes are fine for 2).
