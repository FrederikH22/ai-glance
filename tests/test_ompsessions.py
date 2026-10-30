import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_glance import ompsessions as om


def in_use(pid, trees, now, *args, **kw):
    return om.worktrees_in_use(om.pane_agent_paths(pid, now, *args, **kw)[1], trees)

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


def test_agent_pid_on_tty(tmp_path):
    make_proc(tmp_path, 1, '/dev/pts/7', ['bun', '/home/u/.bun/bin/omp'])
    make_proc(tmp_path, 2, '/dev/pts/9', ['vim', 'omp_notes.md'])
    assert om._agent_pid_on_tty('/dev/pts/7', tmp_path)[0] == om.OMP
    assert om._agent_pid_on_tty('/dev/pts/8', tmp_path) is None
    assert om._agent_pid_on_tty('/dev/pts/9', tmp_path) is None


def test_working_paths_window_anchored_on_last_activity(tmp_path):
    log = tmp_path / 's.jsonl'
    # Work ended 2 h ago, then the session only did bookkeeping (resume, idle).
    write_lines(log, [
        {'type': 'session', 'cwd': '/w/ws', 'timestamp': ts(timedelta(hours=3))},
        msg(ts(timedelta(minutes=150)), path='/old/file'),       # 30 min before the last work
        msg(ts(timedelta(minutes=125)), path='/new/file'),
        msg(ts(timedelta(minutes=124)), path='rel/x.py'),
        {'type': 'message', 'timestamp': ts(timedelta(minutes=123)),
         'message': {'details': {'cwd': '/det/cwd'}}},
        {'type': 'custom', 'customType': 'session_resume', 'timestamp': ts(timedelta(minutes=1))},
    ])
    sub = tmp_path / 's' / 'sub.jsonl'
    write_lines(sub, [msg(ts(timedelta(minutes=120)), path='/sub/file')])
    (tmp_path / 's' / 'x.log').write_text('/ignored\n')
    got = om.working_paths(log, NOW)
    assert got == {Path('/new/file'), Path('/w/ws/rel/x.py'),
                   Path('/det/cwd'), Path('/sub/file')}
    # entries after ``now`` do not count
    write_lines(sub, [msg(ts(timedelta(minutes=-5)), path='/future/file')])
    assert Path('/future/file') not in om.working_paths(log, NOW)


def test_working_paths_tail_cut(tmp_path):
    log = tmp_path / 's.jsonl'
    lines = [json.dumps(msg(ts(timedelta(minutes=5)), path=f'/p/{i}')) for i in range(20)]
    log.write_text('\n'.join(lines) + '\n{"type":"message","timestamp":"' + ts(timedelta(minutes=1)) + '","mess')
    got = om.working_paths(log, NOW, tail_bytes=300)
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
    assert in_use(100, [Path('/w')], NOW, proc, sessions) == set()
    make_proc(proc, 101, '/dev/pts/7', ['bun', '/x/omp'])
    assert in_use(100, [Path('/w')], NOW, proc, sessions) == {Path('/w')}


def title_first_log(path, n=40):
    """Real omp layout: title on line 1, session record (cwd) on line 2."""
    objs = [{'type': 'title', 'title': 'x'},
            {'type': 'session', 'cwd': '/w/ws', 'timestamp': ts(timedelta(hours=3))}]
    objs += [msg(ts(timedelta(minutes=5)), path=f'pad/{i}') for i in range(n)]
    objs.append(msg(ts(timedelta(minutes=1)), path='.worktrees/v2/src/a.cpp'))
    write_lines(path, objs)


def test_relative_path_resolved_when_tail_cut_title_first(tmp_path):
    log = tmp_path / 's.jsonl'
    title_first_log(log)
    got = om.working_paths(log, NOW, tail_bytes=1500)
    assert Path('/w/ws/.worktrees/v2/src/a.cpp') in got
    assert Path('/w/ws/pad/0') not in got  # really cut


def test_relative_path_falls_back_to_pts_cwd(tmp_path):
    log = tmp_path / 's.jsonl'
    write_lines(log, [{'type': 'title', 'title': 'x'},
                      msg(ts(timedelta(minutes=1)), path='rel/a.py')])
    assert om.working_paths(log, NOW) == set()
    assert om.working_paths(log, NOW, base_cwd='/pts/cwd') == {Path('/pts/cwd/rel/a.py')}


def test_subagent_uses_own_session_else_parent_cwd(tmp_path):
    log = tmp_path / 's.jsonl'
    write_lines(log, [{'type': 'session', 'cwd': '/parent', 'timestamp': ts(timedelta(hours=1))}])
    sub_dir = tmp_path / 's'
    write_lines(sub_dir / 'own.jsonl', [
        {'type': 'session', 'cwd': '/own', 'timestamp': ts(timedelta(hours=1))},
        msg(ts(timedelta(minutes=1)), path='x.py')])
    write_lines(sub_dir / 'bare.jsonl', [msg(ts(timedelta(minutes=1)), path='y.py')])
    got = om.working_paths(log, NOW)
    assert got == {Path('/own/x.py'), Path('/parent/y.py')}


def test_semicolon_separated_paths_are_split(tmp_path):
    log = tmp_path / 's.jsonl'
    write_lines(log, [msg(ts(timedelta(minutes=1)), path='/a/b;rel/c; /d/e;;')])
    assert om.working_paths(log, NOW, base_cwd='/w') == {
        Path('/a/b'), Path('/w/rel/c'), Path('/d/e')}


def test_cache_is_bounded(tmp_path):
    om._cache.clear()
    for i in range(om._CACHE_MAX + 10):
        log = tmp_path / f'{i}.jsonl'
        write_lines(log, [msg(ts(timedelta(minutes=1)), path=f'/p/{i}')])
        om.working_paths(log, NOW)
    assert len(om._cache) <= om._CACHE_MAX


def test_parse_failure_is_reported_not_raised(tmp_path, monkeypatch):
    log = tmp_path / 's.jsonl'
    write_lines(log, [msg(ts(timedelta(minutes=1)), path='/p')])
    om._cache.clear()

    def boom(*a, **k):
        raise ValueError('bad')

    monkeypatch.setattr(om, '_parse_log', boom)
    errors = []
    assert om.working_paths(log, NOW, errors=errors) == set()
    assert [type(e) for e in errors] == [ValueError]

    proc = tmp_path / 'proc'
    proc.mkdir()
    sessions = tmp_path / 'sess'
    sessions.mkdir()
    (sessions / 'pts-7').write_text(f'/w\n{log}\ncwdstat 1 2\n')
    make_proc(proc, 100, '/dev/pts/7', ['bun', '/x/omp'])
    errors = []
    assert in_use(100, [Path('/p')], NOW, proc, sessions, errors) == set()
    assert [type(e) for e in errors] == [ValueError]


# --- session state -------------------------------------------------------------

def assistant(stop, *tools):
    content = [{'type': 'text', 'text': 'hi'}] + [{'type': 'toolCall', 'name': t} for t in tools]
    return {'type': 'message', 'message': {'role': 'assistant', 'stopReason': stop,
                                           'content': content}}


def tool_start(name):
    return {'type': 'custom', 'customType': 'tool_execution_start', 'data': {'toolName': name}}


TITLE = {'type': 'title', 'title': 'x'}
USER = {'type': 'message', 'message': {'role': 'user', 'content': [{'type': 'text'}]}}
RESULT = {'type': 'message', 'message': {'role': 'toolResult', 'content': [{'type': 'text'}]}}


def test_session_state_from_last_turn_event(tmp_path):
    log = tmp_path / 's.jsonl'
    cases = [
        ([TITLE, USER], om.WORKING),                                   # model is thinking
        ([TITLE, USER, assistant('toolUse', 'bash'), tool_start('bash')], om.WORKING),
        ([TITLE, USER, assistant('toolUse', 'bash'), tool_start('bash'), RESULT], om.WORKING),
        ([TITLE, USER, assistant('stop')], om.NEEDS_INPUT),            # turn over
        ([TITLE, USER, assistant('error')], om.NEEDS_INPUT),
        ([TITLE, USER, assistant('aborted')], om.NEEDS_INPUT),
        ([TITLE, USER, assistant('toolUse', 'ask'), tool_start('ask')], om.NEEDS_INPUT),
        ([TITLE, USER, assistant('stop'), {'type': 'title_change'},
          {'type': 'custom', 'customType': 'todo_hud_state'}], om.NEEDS_INPUT),  # bookkeeping skipped
        ([TITLE, USER, assistant('stop'), {'type': 'custom', 'customType': 'session_exit'}], None),
        ([TITLE], None),
    ]
    for events, want in cases:
        write_lines(log, events)
        assert om.session_state(log, now=1e12) == want, events


def test_turn_over_but_subagents_writing_counts_as_working(tmp_path):
    log = tmp_path / 's.jsonl'
    write_lines(log, [TITLE, USER, assistant('stop')])
    sub = tmp_path / 's' / 'Worker.jsonl'
    write_lines(sub, [USER])
    now = sub.stat().st_mtime
    assert om.session_state(log, now=now + 1) == om.WORKING
    assert om.session_state(log, now=now + om.SUBAGENT_ACTIVE_S + 1) == om.NEEDS_INPUT


def test_session_state_tail_cut_and_half_written_line(tmp_path, monkeypatch):
    monkeypatch.setattr(om, 'STATE_TAIL_BYTES', 300)
    log = tmp_path / 's.jsonl'
    write_lines(log, [TITLE] + [RESULT] * 20 + [assistant('stop')])
    with open(log, 'a') as f:
        f.write('{"type": "message", "mess')  # being written right now
    assert om.session_state(log, now=1e12) == om.NEEDS_INPUT


def test_pane_agent_state_needs_live_omp(tmp_path):
    proc = tmp_path / 'proc'
    proc.mkdir()
    sessions = tmp_path / 'sess'
    sessions.mkdir()
    log = tmp_path / 's.jsonl'
    write_lines(log, [TITLE, USER, assistant('stop')])
    (sessions / 'pts-7').write_text(f'/w\n{log}\ncwdstat 1 2\n')
    make_proc(proc, 100, '/dev/pts/7', ['bash'])
    assert om.pane_agent_state(100, proc, sessions) is None      # stale pts file, no omp
    make_proc(proc, 101, '/dev/pts/7', ['bun', '/x/omp'])
    assert om.pane_agent_state(100, proc, sessions, now=1e12) == om.NEEDS_INPUT
    assert om.pane_agent_state(None, proc, sessions) is None


# --- Claude Code panes -------------------------------------------------------------

def claude_record(t, cwd, **tool_input):
    rec = {'type': 'assistant', 'timestamp': t, 'cwd': cwd, 'message': {'content': []}}
    if tool_input:
        rec['message']['content'].append({'type': 'tool_use', 'name': 'Edit', 'input': tool_input})
    return rec


def test_claude_code_pane_worktrees(tmp_path):
    proc = tmp_path / 'proc'
    proc.mkdir()
    claude = tmp_path / 'claude'
    (claude / 'sessions').mkdir(parents=True)
    (claude / 'sessions' / '300.json').write_text(json.dumps({'pid': 300, 'sessionId': 'sid-1'}))
    log = claude / 'projects' / '-home-u' / 'sid-1.jsonl'
    write_lines(log, [
        claude_record(ts(timedelta(minutes=200)), '/w/old'),
        claude_record(ts(timedelta(minutes=95)), '/w/a', file_path='/w/b/src/x.cpp'),
        claude_record(ts(timedelta(minutes=90)), '/home/u'),  # idle since, cwd outside
    ])
    write_lines(claude / 'projects' / '-home-u' / 'sid-1' / 'subagents' / 'agent-1.jsonl',
                [claude_record(ts(timedelta(minutes=91)), '/w/c/src')])
    make_proc(proc, 200, '/dev/pts/9', ['bash'])
    make_proc(proc, 300, '/dev/pts/9', ['claude', '--resume', 'sid-1'])
    trees = [Path('/w/a'), Path('/w/b'), Path('/w/c'), Path('/w/old')]
    assert om.pane_agent(200, proc, tmp_path / 'none', claude)[:2] == (om.CLAUDE, log)
    assert in_use(200, trees, NOW, proc, tmp_path / 'none',
                                    claude_dir=claude) == {Path('/w/a'), Path('/w/b'), Path('/w/c')}
    # no session record for that pid -> no agent, no worktrees
    (claude / 'sessions' / '300.json').unlink()
    assert om.pane_agent(200, proc, tmp_path / 'none', claude) is None


def test_idle_omp_pane_keeps_its_worktree(tmp_path):
    proc = tmp_path / 'proc'
    proc.mkdir()
    sessions = tmp_path / 'sess'
    sessions.mkdir()
    log = tmp_path / 's.jsonl'
    write_lines(log, [TITLE, msg(ts(timedelta(hours=20)), path='/w/wt/src/a.py'),
                      {'type': 'custom', 'customType': 'session_resume',
                       'timestamp': ts(timedelta(minutes=1))}])
    (sessions / 'pts-7').write_text(f'/w\n{log}\ncwdstat 1 2\n')
    make_proc(proc, 100, '/dev/pts/7', ['bash'])
    make_proc(proc, 101, '/dev/pts/7', ['omp', '--resume'])
    assert om.pane_agent(100, proc, sessions)[0] == om.OMP
    assert in_use(100, [Path('/w/wt')], NOW, proc, sessions) == {Path('/w/wt')}


def test_claude_code_pane_state(tmp_path):
    proc = tmp_path / 'proc'
    proc.mkdir()
    claude = tmp_path / 'claude'
    (claude / 'sessions').mkdir(parents=True)
    make_proc(proc, 200, '/dev/pts/9', ['bash'])
    make_proc(proc, 300, '/dev/pts/9', ['claude'])
    record = claude / 'sessions' / '300.json'
    for status, want in (('busy', om.WORKING), ('waiting', om.NEEDS_INPUT),
                         ('idle', om.NEEDS_INPUT), ('something-new', None)):
        record.write_text(json.dumps({'pid': 300, 'sessionId': 's', 'status': status}))
        assert om.pane_agent_state(200, proc, tmp_path / 'none', claude_dir=claude) == want, status
    record.unlink()
    assert om.pane_agent_state(200, proc, tmp_path / 'none', claude_dir=claude) is None


def bash_call(t, command):
    return {'type': 'message', 'timestamp': t,
            'message': {'role': 'assistant', 'content': [
                {'type': 'toolCall', 'name': 'bash', 'arguments': {'command': command}}]}}


def test_omp_bash_commands_name_the_worktree(tmp_path):
    log = tmp_path / 's.jsonl'
    home = str(Path.home())
    write_lines(log, [
        {'type': 'session', 'cwd': '/w/ws', 'timestamp': ts(timedelta(hours=1))},
        bash_call(ts(timedelta(minutes=3)), 'cd /w/ws/.worktrees/v2/src/repo; git log -1'),
        bash_call(ts(timedelta(minutes=2)),
                  "git -C ~/other/repo status && curl -s https://example.com/x > /dev/null"),
        msg(ts(timedelta(minutes=1)), path='proc://bg_5/kill'),           # omp internal URL
        msg(ts(timedelta(minutes=1)), path='/w/ws/src/a.py:85-113'),      # read selector
    ])
    got = om.working_paths(log, NOW)
    assert Path('/w/ws/.worktrees/v2/src/repo') in got
    assert Path(home + '/other/repo') in got
    assert Path('/w/ws/src/a.py') in got
    assert not any('proc:' in str(p) or 'example.com' in str(p) for p in got)
    assert om.worktrees_in_use(got, [Path('/w/ws'), Path('/w/ws/.worktrees/v2/src/repo')]) == {
        Path('/w/ws'), Path('/w/ws/.worktrees/v2/src/repo')}
