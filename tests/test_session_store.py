import json
import os
import shlex
from pathlib import Path

from ai_glance import session_store as ss

from test_ompsessions import make_proc, write_lines


def test_lock_is_exclusive_until_released(tmp_path):
    path = tmp_path / 'state' / 'session.lock'
    a, b = ss.SessionLock(path), ss.SessionLock(path)
    assert a.acquire() is True
    assert b.acquire() is False
    a.release()
    assert b.acquire() is True
    b.release()


def test_state_paths_honour_xdg_state_home(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    assert ss.state_dir() == tmp_path / 'ai-glance'
    assert ss.snapshot_path() == tmp_path / 'ai-glance' / 'session.json'
    monkeypatch.delenv('XDG_STATE_HOME')
    assert ss.state_dir() == Path.home() / '.local/state/ai-glance'


LAYOUT = {
    'window0': {'type': 'Window', 'parent': '', 'title': 'frozen title', 'size': [800, 600]},
    'terminal1': {'type': 'Terminal', 'parent': 'window0', 'title': 'mine', 'uuid': 'u1',
                  'directory': '/w/a'},
}


def test_clean_layout_drops_window_title_only():
    out = ss.clean_layout(LAYOUT)
    assert 'title' not in out['window0']
    assert out['window0']['size'] == [800, 600]
    assert out['terminal1']['title'] == 'mine'
    assert LAYOUT['window0']['title'] == 'frozen title'  # deep copy, input untouched
    out['terminal1']['directory'] = '/x'
    assert LAYOUT['terminal1']['directory'] == '/w/a'


def test_window_count():
    assert ss.window_count(LAYOUT) == 1
    assert ss.window_count({}) == 0
    assert ss.window_count({'terminal1': LAYOUT['terminal1']}) == 0


def exists_all(_p):
    return True


def test_resume_command_exact_strings():
    omp = {'agent': ss.OMP, 'session': '/s/x.jsonl', 'cwd': '/w'}
    assert ss.resume_command(omp, exists_all) == \
        '''exec "$SHELL" -ic 'omp --resume /s/x.jsonl; exec "$SHELL" -i\''''
    claude = {'agent': ss.CLAUDE, 'session': 'sid-1', 'cwd': '/w'}
    assert ss.resume_command(claude, exists_all) == \
        '''exec "$SHELL" -ic 'claude --resume sid-1; exec "$SHELL" -i\''''


def test_resume_command_quotes_awkward_path():
    rec = {'agent': ss.OMP, 'session': "/s/a b'c.jsonl", 'cwd': '/w'}
    outer = shlex.split(ss.resume_command(rec, exists_all))
    assert outer[:3] == ['exec', '$SHELL', '-ic']
    assert shlex.split(outer[3]) == ['omp', '--resume', "/s/a b'c.jsonl;", 'exec', '$SHELL', '-i']


def test_resume_command_none_cases():
    assert ss.resume_command({'agent': ss.OMP, 'session': '/s/x.jsonl', 'cwd': '/w'},
                             lambda p: False) is None
    assert ss.resume_command({'agent': 'vim', 'session': 'x', 'cwd': '/w'}, exists_all) is None
    assert ss.resume_command({'agent': ss.CLAUDE, 'session': '', 'cwd': '/w'}, exists_all) is None
    assert ss.resume_command({'agent': ss.CLAUDE}, exists_all) is None
    assert ss.resume_command(None, exists_all) is None


def test_apply_agents_sets_command_and_directory_by_uuid():
    layout = {
        'window0': {'type': 'Window'},
        'terminal1': {'type': 'Terminal', 'uuid': 'u1', 'directory': '/w/shell'},
        'terminal2': {'type': 'Terminal', 'uuid': 'u2', 'directory': '/w/other'},
    }
    agents = {'u1': {'agent': ss.OMP, 'session': '/s/x.jsonl', 'cwd': '/w/agent'}}
    out = ss.apply_agents(layout, agents, exists_all)
    assert out['terminal1']['directory'] == '/w/agent'
    assert 'omp --resume /s/x.jsonl' in out['terminal1']['command']
    assert 'command' not in out['terminal2']
    assert out['terminal2']['directory'] == '/w/other'
    assert 'command' not in layout['terminal1']  # input untouched


def test_apply_agents_missing_folder_falls_back_to_home():
    layout = {'terminal1': {'type': 'Terminal', 'uuid': 'u1', 'directory': '/w/shell'}}
    agents = {'u1': {'agent': ss.CLAUDE, 'session': 'sid', 'cwd': '/gone'}}
    out = ss.apply_agents(layout, agents, lambda p: p != '/gone')
    assert out['terminal1']['directory'] == os.path.expanduser('~')
    assert 'claude --resume sid' in out['terminal1']['command']


def test_apply_agents_gone_omp_session_leaves_plain_shell_in_folder():
    layout = {'terminal1': {'type': 'Terminal', 'uuid': 'u1', 'directory': '/w/shell'}}
    agents = {'u1': {'agent': ss.OMP, 'session': '/s/x.jsonl', 'cwd': '/w/agent'}}
    out = ss.apply_agents(layout, agents, lambda p: p != '/s/x.jsonl')
    assert 'command' not in out['terminal1']
    assert out['terminal1']['directory'] == '/w/agent'


def test_read_snapshot_missing_corrupt_and_unknown_version(tmp_path):
    p = tmp_path / 'session.json'
    assert ss.read_snapshot(p) is None
    p.write_text('{"version": 1, "layo')
    assert ss.read_snapshot(p) is None
    p.write_text(json.dumps({'version': 2, 'layout': LAYOUT, 'agents': {}}))
    assert ss.read_snapshot(p) is None
    p.write_text('[1, 2]')
    assert ss.read_snapshot(p) is None
    p.write_text(json.dumps({'version': 1, 'layout': LAYOUT, 'agents': {}}))
    snap = ss.read_snapshot(p)
    assert snap['layout'] == LAYOUT and snap['agents'] == {}


def test_write_snapshot_atomic_roundtrip(tmp_path):
    p = tmp_path / 'd' / 'session.json'
    agents = {'u1': {'agent': ss.OMP, 'session': '/s/x.jsonl', 'cwd': '/w'}}
    written = ss.write_snapshot(p, LAYOUT, agents, None)
    assert isinstance(written, bytes) and p.read_bytes() == written
    assert [f.name for f in p.parent.iterdir()] == ['session.json']  # no temp file left
    data = json.loads(written)
    assert data['version'] == 1 and data['saved_at']
    assert 'title' not in data['layout']['window0']
    assert data['agents'] == agents
    snap = ss.read_snapshot(p)
    assert snap['agents'] == agents


def test_write_snapshot_skips_unchanged_and_zero_windows(tmp_path):
    p = tmp_path / 'session.json'
    first = ss.write_snapshot(p, LAYOUT, {}, None)
    before = p.read_bytes()
    assert ss.write_snapshot(p, LAYOUT, {}, first) is None
    assert p.read_bytes() == before
    changed = ss.write_snapshot(p, LAYOUT, {'u1': {'agent': ss.OMP, 'session': 's', 'cwd': '/w'}}, first)
    assert changed is not None and changed != before
    good = p.read_bytes()
    assert ss.write_snapshot(p, {}, {}, changed) is None
    assert ss.write_snapshot(p, {'terminal1': LAYOUT['terminal1']}, {}, changed) is None
    assert p.read_bytes() == good
    assert [f.name for f in tmp_path.iterdir()] == ['session.json']


def test_merge_agents_carry_forward_boundary():
    rec = {'agent': ss.OMP, 'session': '/s/x.jsonl', 'cwd': '/w'}
    new = {'agent': ss.CLAUDE, 'session': 'sid', 'cwd': '/w'}
    restored = {'u1': rec, 'u2': rec, 'u3': rec}
    live = {'u1', 'u2'}
    assert ss.merge_agents({}, restored, 1000.0, 1119.0, live) == {'u1': rec, 'u2': rec}
    assert ss.merge_agents({}, restored, 1000.0, 1121.0, live) == {}
    assert ss.merge_agents({}, restored, 1000.0, 1120.0, live) == {}
    assert ss.merge_agents({'u1': new}, restored, 1000.0, 1119.0, live) == {'u1': new, 'u2': rec}


def fake_cwd(proc, pid, cwd):
    (proc / str(pid) / 'cwd').symlink_to(cwd)


def test_agent_record_omp(tmp_path):
    proc = tmp_path / 'proc'
    proc.mkdir()
    sessions = tmp_path / 'sessions'
    sessions.mkdir()
    log = tmp_path / 'logs' / 'x.jsonl'
    write_lines(log, [{'type': 'session', 'cwd': '/w/shell'}])
    (sessions / 'pts-7').write_text(f'/w/shell\n{log}\n')
    make_proc(proc, 100, '/dev/pts/7', ['bash'])
    make_proc(proc, 101, '/dev/pts/7', ['bun', '/x/bin/omp'])
    fake_cwd(proc, 101, '/w/agent')
    rec = ss.agent_record(100, proc, sessions, tmp_path / 'claude')
    assert rec == {'agent': ss.OMP, 'session': str(log), 'cwd': '/w/agent'}


def test_agent_record_claude(tmp_path):
    proc = tmp_path / 'proc'
    proc.mkdir()
    claude = tmp_path / 'claude'
    (claude / 'sessions').mkdir(parents=True)
    (claude / 'sessions' / '300.json').write_text(json.dumps({'pid': 300, 'sessionId': 'sid-1'}))
    write_lines(claude / 'projects' / '-w' / 'sid-1.jsonl', [{'type': 'user'}])
    make_proc(proc, 200, '/dev/pts/9', ['bash'])
    make_proc(proc, 300, '/dev/pts/9', ['claude'])
    fake_cwd(proc, 300, '/w/proj')
    rec = ss.agent_record(200, proc, tmp_path / 'none', claude)
    assert rec == {'agent': ss.CLAUDE, 'session': 'sid-1', 'cwd': '/w/proj'}


def test_agent_record_none_and_never_raises(tmp_path):
    proc = tmp_path / 'proc'
    proc.mkdir()
    make_proc(proc, 100, '/dev/pts/7', ['bash'])
    assert ss.agent_record(100, proc, tmp_path / 'none', tmp_path / 'none') is None
    assert ss.agent_record(None, proc) is None
    assert ss.agent_record(4242, proc) is None
