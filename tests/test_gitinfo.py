import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ai_glance import gitinfo
from ai_glance.gitinfo import (
    build_model,
    find_ros_workspace_root,
    is_ros_workspace_root,
    repo_state,
    workspace_repos,
)

ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, env={**os.environ, **ENV},
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def commit(cwd, name):
    (Path(cwd) / name).write_text(name)
    git(cwd, "add", name)
    git(cwd, "commit", "-m", name)


@pytest.fixture
def env(tmp_path):
    tmp_path = tmp_path.resolve()
    git(tmp_path, "init", "--bare", "-b", "main", "origin.git")
    ws = tmp_path / "ws"
    (ws / "src").mkdir(parents=True)
    (ws / "build").mkdir()
    repo_a = ws / "src" / "repo_a"
    git(tmp_path, "clone", str(tmp_path / "origin.git"), str(repo_a))
    git(repo_a, "checkout", "-b", "main")
    commit(repo_a, "a0")
    git(repo_a, "push", "-u", "origin", "main")
    git(repo_a, "remote", "set-head", "origin", "main")
    w1 = ws / ".worktrees" / "w1" / "src" / "repo_a"
    git(repo_a, "worktree", "add", "-b", "feat", str(w1))
    commit(w1, "f1")
    commit(w1, "f2")
    repo_b = ws / "src" / "nested" / "repo_b"
    repo_b.mkdir(parents=True)
    git(repo_b, "init", "-b", "master")
    commit(repo_b, "b0")
    return tmp_path, ws, repo_a, w1, repo_b


def test_workspace_root_detection(env):
    tmp_path, ws, *_ = env
    assert is_ros_workspace_root(ws)
    assert not is_ros_workspace_root(ws / "src")
    assert find_ros_workspace_root(ws / "src") == ws
    assert find_ros_workspace_root(tmp_path) is None


def test_workspace_repos_finds_nested_and_stops_at_repo(env):
    _, ws, repo_a, _, repo_b = env
    (repo_a / "inner" / ".git").mkdir(parents=True)  # must not be descended into
    assert workspace_repos(ws) == [repo_b, repo_a]


def test_repo_state_lists_worktrees_main_first(env):
    _, _, repo_a, w1, _ = env
    st = repo_state(repo_a)
    assert st.error is None
    assert st.base == "origin/main"
    assert st.worktrees[0].path == repo_a
    assert st.worktrees[1].path == w1
    assert st.worktrees[1].branch == "feat"
    assert st.worktrees[1].ahead == 2
    assert st.worktrees[1].behind == 0
    assert repo_state(w1).worktrees[0].path == repo_a
    assert repo_state(w1).current.path == w1
    assert repo_state(repo_a).current.path == repo_a


def test_current_is_deepest_containing_worktree(env):
    _, _, repo_a, w1, _ = env
    nested = w1 / "deep" / "dir"
    nested.mkdir(parents=True)
    assert repo_state(nested).current.path == w1
    assert repo_state(repo_a).current.branch == "main"


def test_worktree_workspace_repo_row_uses_linked_worktree(env):
    _, ws, repo_a, w1, _ = env
    wsw = ws / ".worktrees" / "w1"
    (wsw / "build").mkdir()
    m = build_model(wsw)
    assert m.kind == "workspace" and m.path == wsw
    assert [r.name for r in m.repos] == ["repo_a"]
    assert m.repos[0].current.path == w1
    assert m.repos[0].current.branch == "feat"
    assert m.repos[0].worktrees[0].branch == "main"


def test_dirty_flag(env):
    _, _, repo_a, w1, _ = env
    (w1 / "untracked").write_text("x")
    st = repo_state(repo_a)
    assert st.worktrees[1].dirty is True
    assert st.worktrees[0].dirty is False


def test_detached_head(env):
    _, _, repo_a, _, _ = env
    git(repo_a, "checkout", "--detach")
    wt = repo_state(repo_a).worktrees[0]
    assert wt.branch is None
    assert wt.head == git(repo_a, "rev-parse", "--short", "HEAD")
    assert isinstance(wt.ahead, int) and isinstance(wt.behind, int)


def test_base_fallback_master(env):
    *_, repo_b = env
    assert repo_state(repo_b).base == "master"


def test_prunable_worktree(env):
    _, _, repo_a, w1, _ = env
    shutil.rmtree(w1)
    wt = repo_state(repo_a).worktrees[1]
    assert wt.prunable is True
    assert wt.dirty is None
    assert wt.ahead is None


def test_build_model_kinds(env):
    tmp_path, ws, repo_a, _, _ = env
    m = build_model(ws)
    assert m.kind == "workspace" and len(m.repos) == 2
    m = build_model(ws / "src")
    assert m.kind == "workspace" and m.path == ws
    (repo_a / "subdir").mkdir()
    m = build_model(repo_a / "subdir")
    assert m.kind == "repo" and m.path == repo_a
    (tmp_path / "empty").mkdir()
    assert build_model(tmp_path / "empty").kind == "none"


def test_git_failure_isolated(env, monkeypatch):
    _, ws, repo_a, _, repo_b = env
    real = gitinfo._git

    def fake(args, cwd, timeout):
        if repo_b in (Path(cwd).resolve(), *Path(cwd).resolve().parents):
            raise subprocess.TimeoutExpired("git", timeout)
        return real(args, cwd, timeout)

    monkeypatch.setattr(gitinfo, "_git", fake)
    m = build_model(ws)
    by_name = {r.name: r for r in m.repos}
    assert set(by_name) == {"repo_a", "repo_b"}
    assert by_name["repo_b"].error
    assert by_name["repo_a"].error is None


def _wt(path, branch):
    return gitinfo.WorktreeState(Path(path), branch, 'abc1234', 0, 0, False, False, False, None)


def _repo(current_index=0):
    wts = tuple(_wt(f'/w/{n}', n) for n in ('main', 'a', 'b', 'c'))
    return gitinfo.RepoState(Path('/w/main'), 'r', 'origin/main', wts, None, wts[current_index])


def test_repo_rows_no_omp_shows_only_current():
    repo = _repo(1)
    current, extra, current_in_use = gitinfo.repo_rows(repo, set())
    assert current is repo.worktrees[1] and extra == [] and current_in_use is False


def test_repo_rows_in_use_exactly_those():
    repo = _repo(0)
    wa, wb = repo.worktrees[1], repo.worktrees[2]
    current, extra, current_in_use = gitinfo.repo_rows(repo, {wa.path, wb.path})
    assert current is repo.worktrees[0] and extra == [wa, wb] and current_in_use is False


def test_repo_rows_current_in_use_not_duplicated():
    repo = _repo(1)
    cur, other = repo.worktrees[1], repo.worktrees[3]
    current, extra, current_in_use = gitinfo.repo_rows(repo, {cur.path, other.path})
    assert current is cur and extra == [other] and current_in_use is True
    assert cur not in extra


def test_repo_rows_ignores_unknown_paths_and_empty_repo():
    repo = _repo(0)
    assert gitinfo.repo_rows(repo, {Path('/elsewhere')})[1] == []
    empty = gitinfo.RepoState(Path('/x'), 'x', None, (), 'boom', None)
    assert gitinfo.repo_rows(empty, {Path('/x')}) == (None, [], False)
