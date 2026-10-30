"""Regenerate the README screenshots (docs/screenshots/) with demo data.

    xvfb-run -a -s '-screen 0 1600x900x24' python3 tools/make_screenshots.py

Runs a real Terminator 2.1.3 in-process with the plugin loaded by Terminator's
PluginRegistry, in a throwaway HOME and XDG_CONFIG_HOME. Your Terminator config
is copied (so your profile colours apply) and its font is pinned to your
desktop's monospace font. Everything shown is demo data: git repos created
under the throwaway HOME, a made-up usage snapshot, and faked agent state /
worktree lookups (no omp or Claude Code runs). Never touches ~/.config, a running
Terminator, omp or the network. Needs ImageMagick (`convert`) for the GIF.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

if not os.environ.get('DISPLAY'):
    sys.exit("run under xvfb-run -a -s '-screen 0 1600x900x24'")

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'docs' / 'screenshots'
REAL_HOME = Path.home()
REAL_CFG = REAL_HOME / '.config/terminator/config'
WIDTH, HEIGHT = 1600, 900


def _desktop_font():
    try:
        font = subprocess.run(['gsettings', 'get', 'org.gnome.desktop.interface',
                               'monospace-font-name'], capture_output=True, text=True,
                              timeout=5).stdout.strip().strip("'")
    except Exception:
        font = ''
    return font or 'Monospace 12'


def _gtk_theme():
    try:
        return subprocess.run(['gsettings', 'get', 'org.gnome.desktop.interface', 'gtk-theme'],
                              capture_output=True, text=True, timeout=5).stdout.strip().strip("'")
    except Exception:
        return ''


FONT = _desktop_font()
THEME = _gtk_theme()

# --- throwaway HOME with a demo ROS workspace ----------------------------------------
TMP = Path(tempfile.mkdtemp(prefix='ai-glance-shots-'))
HOME = TMP / 'dev'
HOME.mkdir()
os.environ.update({
    'HOME': str(HOME), 'XDG_CONFIG_HOME': str(HOME / '.config'),
    'GIT_CONFIG_GLOBAL': str(HOME / '.gitconfig'), 'GIT_CONFIG_NOSYSTEM': '1',
    'GIT_AUTHOR_DATE': '2026-10-30T10:00:00', 'GIT_COMMITTER_DATE': '2026-10-30T10:00:00',
})
if THEME:
    os.environ['GTK_THEME'] = THEME
(HOME / '.gitconfig').write_text('[user]\n\tname = Demo\n\temail = dev@example.com\n'
                                 '[init]\n\tdefaultBranch = main\n[advice]\n\tdetachedHead = false\n')
(HOME / '.bashrc').write_text(
    "PS1='\\[\\e]0;dev@workstation: \\w\\a\\]\\[\\e[01;32m\\]dev@workstation\\[\\e[00m\\]:"
    "\\[\\e[01;34m\\]\\w\\[\\e[00m\\]\\$ '\n"
    "alias ls='ls --color=auto'\n")


def git(cwd, *args):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True)


def commit(repo, path, text, msg):
    p = Path(repo) / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    git(repo, 'add', '-A')
    git(repo, 'commit', '-qm', msg)


def make_repo(name, root):
    """A repo under ``root`` with a bare origin and main pushed; returns its path."""
    origin = HOME / '.remotes' / f'{name}.git'
    origin.parent.mkdir(exist_ok=True)
    git(HOME, 'init', '-q', '--bare', str(origin))
    repo = root / name
    git(HOME, 'clone', '-q', str(origin), str(repo))
    commit(repo, 'package.xml', f'<package><name>{name}</name></package>\n', 'initial package')
    commit(repo, 'CMakeLists.txt', f'project({name})\n', 'build setup')
    git(repo, 'push', '-q', 'origin', 'main')
    git(repo, 'remote', 'set-head', 'origin', 'main')  # as a normal clone has it
    return repo


def upstream_commits(name, n):
    """Push ``n`` commits to origin/main from another clone (the local repo falls behind)."""
    other = TMP / f'other-{name}'
    git(HOME, 'clone', '-q', str(HOME / '.remotes' / f'{name}.git'), str(other))
    for i in range(n):
        commit(other, f'upstream_{i}.txt', f'{i}\n', f'upstream change {i}')
    git(other, 'push', '-q', 'origin', 'main')


WS = HOME / 'ros_ws'
SRC = WS / 'src'
SRC.mkdir(parents=True)
for marker in ('build', 'install', 'log'):  # what makes it a ROS workspace root
    (WS / marker).mkdir()

nav = make_repo('nav_stack', SRC)
git(nav, 'switch', '-qc', 'feat/costmap-layers')
commit(nav, 'src/costmap.cpp', '// layers\n', 'costmap: inflation layer')
commit(nav, 'src/costmap.cpp', '// layers v2\n', 'costmap: obstacle layer')
git(nav, 'push', '-q', '-u', 'origin', 'feat/costmap-layers')
NAV_WT = HOME / 'worktrees' / 'nav_stack-planner-fix'
git(nav, 'worktree', 'add', '-q', '-b', 'fix/planner-timeout', str(NAV_WT), 'origin/main')
commit(NAV_WT, 'src/planner.cpp', '// timeout\n', 'planner: respect timeout')
(NAV_WT / 'src/planner.cpp').write_text('// timeout, wip\n')

drv = make_repo('robot_driver', SRC)
upstream_commits('robot_driver', 3)
git(drv, 'fetch', '-q')
(drv / 'CMakeLists.txt').write_text('project(robot_driver)\nadd_compile_options(-Wall)\n')

per = make_repo('perception', SRC)
git(per, 'switch', '-qc', 'feat/lidar-filter')
commit(per, 'src/filter.cpp', '// voxel\n', 'lidar: voxel filter')
upstream_commits('perception', 2)
git(per, 'fetch', '-q')

TOOLS = HOME / 'tools'
TOOLS.mkdir()
sim = make_repo('sim_launch', TOOLS)
git(sim, 'switch', '-qc', 'feat/warehouse-world')
(sim / 'worlds.yaml').write_text('warehouse: true\n')

# --- isolated Terminator config: yours, plugin enabled, pinned font --------------------
conf = HOME / '.config' / 'terminator'
conf.mkdir(parents=True)
cfg_text = REAL_CFG.read_text() if REAL_CFG.exists() else '[global_config]\n[profiles]\n  [[default]]\n'
(conf / 'config').write_text(cfg_text)
subprocess.run(['bash', str(ROOT / 'install.sh')], check=True, stdout=subprocess.DEVNULL)

# Pin the desktop font in every profile (no dconf here, so the "system font" would differ).
lines, out = (conf / 'config').read_text().splitlines(), []
in_profiles = False
for line in lines:
    s = line.strip()
    if s.startswith('[') and not s.startswith('[['):
        in_profiles = s == '[profiles]'
    if in_profiles and s.split('=')[0].strip() in ('use_system_font', 'font'):
        continue
    out.append(line)
    if in_profiles and s.startswith('[[') and not s.startswith('[[['):
        out += ['    use_system_font = False', f'    font = {FONT}']
(conf / 'config').write_text('\n'.join(out) + '\n')

_errfile = tempfile.TemporaryFile(mode='w+b')
os.dup2(_errfile.fileno(), 2)

import gi  # noqa: E402
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk, Gtk  # noqa: E402

# Same code as the plugin dir (install.sh symlinks this checkout's package there);
# imported first so the demo fakes below are in place before the plugin's first poll.
sys.path.insert(0, str(ROOT))
from ai_glance import ompsessions, usage  # noqa: E402

from terminatorlib.plugin import PluginRegistry  # noqa: E402
from terminatorlib.terminator import Terminator  # noqa: E402

Gtk.main_quit = lambda *a: None

# --- demo fakes ------------------------------------------------------------------------
AGENT_STATE = {}   # shell pid -> ompsessions.WORKING / NEEDS_INPUT
AGENT_PATHS = {}   # shell pid -> paths the pane's agent touched


def _fake_state(shell_pid, *a, **k):
    return AGENT_STATE.get(shell_pid)


def _fake_paths(shell_pid, now, *a, **k):
    paths = AGENT_PATHS.get(shell_pid)
    return (ompsessions.OMP, list(paths)) if paths else (None, [])


NOW = datetime.now(timezone.utc)


def _win(label, frac, hours):
    return usage.UsageWindow(label, usage.short_label(label), frac, 'ok',
                             NOW + timedelta(hours=hours), NOW)


DEMO_USAGE = usage.UsageSnapshot((
    usage.AccountUsage('dev@example.com', (
        _win('Claude 5 Hour', 0.42, 2.6), _win('Claude 7 Day', 0.68, 61),
        _win('Claude 7 Day (Opus)', 0.86, 61)), 'anthropic', 'oauth'),
    usage.AccountUsage('dev@example.com', (
        _win('Codex 5 Hour', 0.12, 4.1), _win('Codex 7 Day', 0.31, 97)), 'openai-codex', 'oauth'),
), NOW, None)

ompsessions.pane_agent_state = _fake_state
ompsessions.pane_agent_paths = _fake_paths
usage.read_current_usage = lambda *a, **k: DEMO_USAGE


# --- helpers ---------------------------------------------------------------------------
def pump(seconds=0.2, until=None):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if until is not None and until():
            return True
        time.sleep(0.01)
    return until() if until is not None else True


def focus(term):
    # No window manager under Xvfb: emit the focus-in Terminator would get from X.
    term.vte.grab_focus()
    term.emit('focus-in')
    pump(0.3)


def run(term, command):
    term.vte.feed_child(f'{command}\n'.encode())


def show(term, text):
    """Print demo agent output in a pane and keep its shell busy (no prompt redraws)."""
    path = TMP / f'agent-{term.pid}.txt'
    path.write_text('\x1b[?25l' + text)  # hidden cursor, like an agent UI
    run(term, f'clear; cat {path}; sleep 1d')


def click_git(term):
    ev = Gdk.Event.new(Gdk.EventType.BUTTON_PRESS)
    ev.button = 1
    term.titlebar.get_child().get_children()[0].emit('button-press-event', ev)


def grab():
    win = window.get_window()
    return Gdk.pixbuf_get_from_window(win, 0, 0, win.get_width(), win.get_height())


def shot(name):
    pump(0.6)
    path = OUT / name
    grab().savev(str(path), 'png', [], [])
    print('wrote', path.relative_to(ROOT))


def frames(seconds, fps, into, start=0):
    """Capture frames at ``fps`` into ``into`` while pumping events; returns next index."""
    step = 1.0 / fps
    n = start
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        t0 = time.monotonic()
        grab().savev(str(into / f'f{n:04d}.png'), 'png', [], [])
        n += 1
        pump(max(0.0, step - (time.monotonic() - t0)))
    return n


# --- bootstrap like /usr/bin/terminator -----------------------------------------------
os.chdir(WS)
terminator = Terminator()
terminator.set_origcwd(str(WS))
terminator.reconfigure()
terminator.create_layout('default')
terminator.layout_done()
pump(1.5)
if not PluginRegistry().is_enabled('AiGlance'):
    _errfile.seek(0)
    sys.stdout.write(_errfile.read().decode(errors='replace'))
    sys.exit('AiGlance did not load')

window = terminator.windows[0]
window.move(0, 0)
window.resize(WIDTH, HEIGHT)
pump(0.5)
# Xvfb starts the pointer mid-screen, where it would pop up a tooltip; park it in a
# terminal area (bottom right pane, no tooltips there).
_display = Gdk.Display.get_default()
_display.get_default_seat().get_pointer().warp(_display.get_default_screen(),
                                               WIDTH - 60, HEIGHT // 2 + 200)

# 2x2: ws (top left), nav_stack agent working (top right),
#      nav_stack agent waiting (bottom left), robot_driver shell (bottom right)
a = terminator.terminals[0]
a.emit('split-vert', None)
pump(0.8)
b = [t for t in terminator.terminals if t is not a][0]
a.emit('split-horiz', None)
pump(0.8)
c = [t for t in terminator.terminals if t not in (a, b)][0]
b.emit('split-horiz', None)
pump(0.8)
d = [t for t in terminator.terminals if t not in (a, b, c)][0]
pump(1.0)

run(a, 'clear; ls src; git -C src/nav_stack log --oneline -3')
run(b, f'cd {nav}')
run(c, f'cd {per}')
run(d, f'cd {drv} && clear && git status -sb')
pump(2.5, lambda: all(t.get_cwd() for t in (a, b, c, d)))
pump(1.0)

AGENT_STATE[b.pid] = ompsessions.WORKING
AGENT_PATHS[b.pid] = [NAV_WT / 'src/planner.cpp', sim / 'worlds.yaml']
AGENT_STATE[c.pid] = ompsessions.NEEDS_INPUT
AGENT_PATHS[c.pid] = [per / 'src/filter.cpp']
show(b, ' \x1b[1momp\x1b[0m  nav_stack\n\n'
        ' \x1b[32m\u25cf\x1b[0m Read  ~/worktrees/nav_stack-planner-fix/src/planner.cpp\n'
        ' \x1b[32m\u25cf\x1b[0m Edit  src/planner.cpp  \x1b[32m+14\x1b[0m \x1b[31m-3\x1b[0m\n'
        ' \x1b[33m\u25cb\x1b[0m Bash  colcon test --packages-select nav_stack \u2026\n')
show(c, ' \x1b[1momp\x1b[0m  perception\n\n'
        ' \x1b[32m\u25cf\x1b[0m lidar: voxel filter committed on feat/lidar-filter\n'
        ' \x1b[32m\u25cf\x1b[0m colcon test: 18 passed\n\n'
        ' \x1b[1;33m?\x1b[0m Rebase onto origin/main (2 behind) and push?  [y/n]\n')
focus(a)
pump(2.5)  # state poll (2 s) + first usage tick

OUT.mkdir(parents=True, exist_ok=True)
shot('overview.png')

click_git(a)
pump(3.0)
shot('git-panel-workspace.png')
click_git(a)
pump(0.5)

click_git(b)
pump(3.0)
shot('git-panel-agent.png')
click_git(b)
pump(0.5)

# --- behaviour GIF: gradient + pulse, open the panel, move focus ------------------------
FR = TMP / 'frames'
FR.mkdir()
focus(a)
n = frames(3.0, 8, FR)
click_git(a)
n = frames(3.0, 8, FR, n)
click_git(a)
focus(d)
n = frames(3.0, 8, FR, n)
gif = OUT / 'behaviour.gif'
subprocess.run(['convert', '-delay', '12', '-loop', '0', str(FR / 'f*.png'),
                '-resize', '960x', '-layers', 'Optimize', '-fuzz', '2%', str(gif)], check=True)
print('wrote', gif.relative_to(ROOT), f'({n} frames, {gif.stat().st_size // 1024} KiB)')

for w in list(terminator.windows):
    w.emit('destroy')
pump(0.3)
shutil.rmtree(TMP, ignore_errors=True)
