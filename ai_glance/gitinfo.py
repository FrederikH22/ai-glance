"""Git and ROS workspace model. GTK-free; safe to call from worker threads."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_SKIP_DIRS = frozenset({"build", "install", "log"})
_WS_MARKERS = ("build", "install", "log")


@dataclass(frozen=True)
class WorktreeState:
    path: Path
    branch: str | None
    head: str
    ahead: int | None
    behind: int | None
    dirty: bool | None
    prunable: bool
    locked: bool
    error: str | None


@dataclass(frozen=True)
class RepoState:
    root: Path
    name: str
    base: str | None
    worktrees: tuple[WorktreeState, ...]
    error: str | None
    current: WorktreeState | None = None


@dataclass(frozen=True)
class PanelModel:
    kind: Literal["workspace", "repo", "none"]
    path: Path
    repos: tuple[RepoState, ...]


def repo_rows(repo: RepoState, in_use) -> tuple[WorktreeState | None, list[WorktreeState], bool]:
    """What the panel renders for ``repo``: ``(current, omp_worktrees, current_in_use)``.

    Only the checkout the pane is in; additionally, when omp is live in the pane
    (``in_use`` non-empty), exactly the in-use worktrees, never ``current`` twice
    (its in-use state is returned as the third item instead).
    """
    current = repo.current or (repo.worktrees[0] if repo.worktrees else None)
    current_path = current.path if current is not None else None
    extra = [wt for wt in repo.worktrees if wt.path in in_use and wt.path != current_path]
    return current, extra, current_path is not None and current_path in in_use


def _git(args: list[str], cwd: Path, timeout: float) -> str:
    return subprocess.run(
        ["git", "--no-optional-locks", *args],
        cwd=cwd,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        timeout=timeout,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _isdir(p: Path) -> bool:
    try:
        return p.is_dir()
    except OSError:
        return False


def is_ros_workspace_root(path: Path) -> bool:
    return _isdir(path / "src") and any(_isdir(path / m) for m in _WS_MARKERS)


def find_ros_workspace_root(path: Path) -> Path | None:
    path = path.resolve()
    for cand in (path, *path.parents):
        if is_ros_workspace_root(cand):
            return cand
    return None


def workspace_repos(ws_root: Path) -> list[Path]:
    found: list[Path] = []
    stack = [ws_root.resolve() / "src"]
    while stack:
        d = stack.pop()
        try:
            if (d / ".git").exists():
                found.append(d)
                continue
            with os.scandir(d) as it:
                for e in it:
                    if (
                        e.name not in _SKIP_DIRS
                        and not e.name.startswith(".")
                        and e.is_dir(follow_symlinks=False)
                    ):
                        stack.append(Path(e.path))
        except OSError:
            continue
    return sorted(found)


def _find_repo_toplevel(path: Path) -> Path | None:
    for cand in (path, *path.parents):
        try:
            if (cand / ".git").exists():
                return cand
        except OSError:
            return None
    return None


def _base_ref(repo: Path, timeout: float) -> str | None:
    try:
        ref = _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], repo, timeout).strip()
        if ref:
            return ref
    except (subprocess.SubprocessError, OSError):
        pass
    for name in ("main", "master"):
        try:
            _git(["rev-parse", "--verify", "--quiet", name], repo, timeout)
            return name
        except subprocess.CalledProcessError:
            continue
    return None


def _parse_worktrees(text: str) -> list[dict]:
    entries: list[dict] = []
    for block in text.split("\n\n"):
        entry: dict = {}
        for line in block.splitlines():
            key, _, val = line.partition(" ")
            if key == "worktree":
                entry["path"] = val
            elif key == "HEAD":
                entry["head"] = val
            elif key == "branch":
                entry["branch"] = val.removeprefix("refs/heads/")
            elif key in ("detached", "bare", "locked", "prunable"):
                entry[key] = True
        if "path" in entry:
            entries.append(entry)
    return entries


def _worktree_state(entry: dict, base: str | None, timeout: float) -> WorktreeState:
    path = Path(entry["path"]).resolve()
    branch = entry.get("branch")
    locked = bool(entry.get("locked"))
    if entry.get("prunable") or not _isdir(path):
        return WorktreeState(path, branch, entry.get("head", "")[:7], None, None, None, True, locked, None)
    try:
        head = _git(["rev-parse", "--short", "HEAD"], path, timeout).strip()
    except subprocess.CalledProcessError:
        head = ""  # unborn branch
    except Exception as exc:
        return WorktreeState(path, branch, "", None, None, None, False, locked, _describe(exc))
    try:
        dirty = bool(_git(["status", "--porcelain", "--untracked-files=normal"], path, timeout).strip())
        ahead = behind = None
        if base is not None and head:
            out = _git(["rev-list", "--left-right", "--count", f"{base}...HEAD"], path, timeout)
            left, right = out.split()
            behind, ahead = int(left), int(right)
    except Exception as exc:
        return WorktreeState(path, branch, head, None, None, None, False, locked, _describe(exc))
    return WorktreeState(path, branch, head, ahead, behind, dirty, False, locked, None)


def _describe(exc: BaseException) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return "git timed out"
    if isinstance(exc, subprocess.CalledProcessError):
        return (exc.stderr or "").strip() or f"git exited {exc.returncode}"
    return str(exc) or type(exc).__name__


def repo_state(repo_path: Path, timeout: float = 2.0) -> RepoState:
    """State of the repository containing repo_path. Never raises."""
    try:
        repo_path = Path(repo_path).resolve()
    except Exception:
        pass
    try:
        entries = [e for e in _parse_worktrees(_git(["worktree", "list", "--porcelain"], repo_path, timeout)) if not e.get("bare")]
        if not entries:
            raise RuntimeError("no worktrees listed")
        root = Path(entries[0]["path"]).resolve()
        base = _base_ref(repo_path, timeout)
        worktrees = tuple(_worktree_state(e, base, timeout) for e in entries)
        current = None
        for wt in worktrees:
            if (repo_path == wt.path or wt.path in repo_path.parents) and (
                current is None or len(wt.path.parts) > len(current.path.parts)
            ):
                current = wt
        return RepoState(root, root.name, base, worktrees, None, current)
    except Exception as exc:
        return RepoState(repo_path, repo_path.name, None, (), _describe(exc), None)


def build_model(cwd: Path, timeout: float = 2.0) -> PanelModel:
    """Model for the panel at cwd. Never raises."""
    try:
        cwd = Path(cwd).resolve()
        top = _find_repo_toplevel(cwd)
        if top is not None:
            return PanelModel("repo", top, (repo_state(top, timeout),))
        ws = find_ros_workspace_root(cwd)
        if ws is not None:
            return PanelModel("workspace", ws, tuple(repo_state(r, timeout) for r in workspace_repos(ws)))
        return PanelModel("none", cwd, ())
    except Exception:
        return PanelModel("none", Path(cwd), ())
