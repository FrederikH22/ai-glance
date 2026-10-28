import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_glance import ompsessions as om

NOW = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)


def ts(delta):
    return (NOW - delta).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def make_proc(root, pid, tty, argv):
    d = root / str(pid)
    (d / 'fd').mkdir(parents=True)
    (d / 'fd' / '0').symlink_to(tty)
    (d / 'cmdline').write_bytes(b'\0'.join(a.encode() for a in argv) + b'\0')


def write_lines(path, objs):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(o) + '\n' for o in objs))


def msg(t, **arguments):
    return {'type': 'message', 'timestamp': t,
            'message': {'content': [{'arguments': arguments}]}}


def test_pane_tty_reads_fd0(tmp_path):
    make_proc(tmp_path, 100, '/dev/pts/7', ['bash'])
    assert om.pane_tty(100, tmp_path) == '/dev/pts/7'
    assert om.pane_tty(None, tmp_path) is None
    assert om.pane_tty(999, tmp_path) is None


def test_session_log_for_tty(tmp_path):
    (tmp_path / 'pts-7').write_text('/home/u/ws\n/home/u/log.jsonl\ncwdstat 1 2\n')
    assert om.session_log_for_tty('/dev/pts/7', tmp_path) == Path('/home/u/log.jsonl')
    assert om.session_log_for_tty('/dev/pts/8', tmp_path) is None


def test_omp_running_on_tty(tmp_path):
    make_proc(tmp_path, 1, '/dev/pts/7', ['bun', '/home/u/.bun/bin/omp'])
    make_proc(tmp_path, 2, '/dev/pts/9', ['vim', 'omp_notes.md'])
    assert om.omp_running_on_tty('/dev/pts/7', tmp_path)
    assert not om.omp_running_on_tty('/dev/pts/8', tmp_path)
    assert not om.omp_running_on_tty('/dev/pts/9', tmp_path)


def test_recent_paths_window(tmp_path):
    log = tmp_path / 's.jsonl'
    write_lines(log, [
        {'type': 'session', 'cwd': '/w/ws', 'timestamp': ts(timedelta(hours=1))},
        msg(ts(timedelta(minutes=30)), path='/old/file'),
        msg(ts(timedelta(minutes=5)), path='/new/file'),
        msg(ts(timedelta(minutes=4)), path='rel/x.py'),
        {'type': 'message', 'timestamp': ts(timedelta(minutes=3)),
         'message': {'details': {'cwd': '/det/cwd'}}},
    ])
    sub = tmp_path / 's' / 'sub.jsonl'
    write_lines(sub, [msg(ts(timedelta(minutes=2)), path='/sub/file')])
    (tmp_path / 's' / 'x.log').write_text('/ignored\n')
    stale = tmp_path / 's' / 'stale.jsonl'
    write_lines(stale, [msg(ts(timedelta(minutes=1)), path='/stale/file')])
    old = (NOW - timedelta(hours=2)).timestamp()
    os.utime(stale, (old, old))
    got = om.recent_paths(log, NOW)
    assert got == {Path('/new/file'), Path('/w/ws/rel/x.py'),
                   Path('/det/cwd'), Path('/sub/file')}


def test_recent_paths_tail_cut(tmp_path):
    log = tmp_path / 's.jsonl'
    lines = [json.dumps(msg(ts(timedelta(minutes=5)), path=f'/p/{i}')) for i in range(20)]
    log.write_text('\n'.join(lines) + '\n{"type":"message","timestamp":"' + ts(timedelta(minutes=1)) + '","mess')
    got = om.recent_paths(log, NOW, tail_bytes=300)
    assert Path('/p/19') in got
    assert Path('/p/0') not in got


def test_worktrees_in_use_deepest_wins(tmp_path):
    ws = tmp_path / 'ws'
    repo = ws / 'src' / 'repo'
    repo.mkdir(parents=True)
    link = tmp_path / 'link'
    link.symlink_to(repo)
    assert om.worktrees_in_use([repo / 'x.cpp'], [ws, repo]) == {repo.resolve()}
    assert om.worktrees_in_use([ws / 'README'], [ws, repo]) == {ws.resolve()}
    assert om.worktrees_in_use([link / 'y'], [ws, repo]) == {repo.resolve()}
    assert om.worktrees_in_use([tmp_path / 'other'], [ws, repo]) == set()


def test_pane_worktrees_in_use_stale_session(tmp_path):
    proc = tmp_path / 'proc'
    proc.mkdir()
    sessions = tmp_path / 'sess'
    log = tmp_path / 's.jsonl'
    write_lines(log, [msg(ts(timedelta(minutes=1)), path='/w/a')])
    sessions.mkdir()
    (sessions / 'pts-7').write_text(f'/w\n{log}\ncwdstat 1 2\n')
    make_proc(proc, 100, '/dev/pts/7', ['bash'])
    assert om.pane_worktrees_in_use(100, [Path('/w')], NOW, proc, sessions) == set()
    make_proc(proc, 101, '/dev/pts/7', ['bun', '/x/omp'])
    assert om.pane_worktrees_in_use(100, [Path('/w')], NOW, proc, sessions) == {Path('/w')}
