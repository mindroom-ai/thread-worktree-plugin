# ruff: noqa: INP001
"""Which directory an agent may tie a thread to, how the tie names it, and what git checkout is in it."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

from mindroom.plugin_api import resolve_agent_file

from .state import valid_branch, valid_worktree_path

UNRESTRICTED = "unrestricted"
_GITDIR = "gitdir: "
_BRANCH = "ref: refs/heads/"
# A `.git` file and a `HEAD` hold one short line.
_MAX_LINE_BYTES = 1024


class Checkout(NamedTuple):
    """The git checkout in a directory: its branch and its repository's name, each empty when unknown."""

    branch: str
    repo: str
    # When `git worktree add` made it, which is when it wrote the worktree's `.git` file; None for any other checkout.
    made_at: float | None


def file_access(config: Any, agent_name: str) -> str:  # noqa: ANN401 - MindRoom's config, read by two attributes.
    """Return the agent's `file_access`, `workspace` or `unrestricted`: its own setting, else the default."""
    agent = config.agents.get(agent_name)
    own = agent.file_access if agent is not None else None
    return own if own is not None else config.defaults.file_access


def tied_path(root: Path, path: str, access: str) -> str | None:
    """Return how a tie names the existing directory `path`, or None when it may not be tied.

    A directory inside the workspace `root` is named relative to it. One outside is named by its absolute path, and
    only for an agent whose file access is unrestricted. `path` is relative to the root or absolute.
    """
    try:
        root = root.resolve()
        # Resolving follows every link, so a link that leaves the workspace ends up outside the root.
        resolved = (root / path).resolve()
        if not resolved.is_dir() or resolved == root:
            return None
    except (ValueError, OSError):
        return None
    if resolved.is_relative_to(root):
        tied = resolved.relative_to(root).as_posix()
    elif access == UNRESTRICTED:
        tied = resolved.as_posix()
    else:
        return None
    return tied if valid_worktree_path(tied) else None


def _first_line(path: str, root: Path, access: str) -> tuple[str, float] | None:
    """Return the first line and the modification time of the file at `path`, or None when the agent may not read it.

    MindRoom opens it as it opens a file for the agent's own tools: inside the workspace unless the agent's file
    access is unrestricted, without following a link out of it and without waiting on a pipe.
    """
    try:
        authorized = resolve_agent_file(
            path, workspace_root=root, file_access=access, field_name="path"
        )
        with authorized.open() as file:
            line = file.readline(_MAX_LINE_BYTES).decode().strip()
            return line, os.fstat(file.fileno()).st_mtime
    except (ValueError, OSError):
        return None


def _repository(gitdir: PurePosixPath) -> str:
    """Return the name of the repository a git directory belongs to: a clone's, a submodule's, or a worktree's."""
    parts = gitdir.parts
    if len(parts) > 2 and parts[-2] == "worktrees":
        parts = parts[:-2]
    if parts[-1] == ".git":
        parts = parts[:-1]
    return parts[-1] if len(parts) > 1 else ""


def checkout(root: Path, tied: str, access: str) -> Checkout | None:
    """Return the git checkout in a tied directory, or None when it holds none that the agent may read.

    Only the checkout's `.git` file and its `HEAD` are read; git is not run.
    """
    directory = tied if tied.startswith("/") else f"{root.resolve().as_posix()}/{tied}"
    link = _first_line(f"{directory}/.git", root, access)
    made_at = None
    if link is not None and link[0].startswith(_GITDIR):
        # A worktree's and a submodule's `.git` is a file that names the git directory, absolute or relative to it.
        gitdir = PurePosixPath(
            os.path.normpath(os.path.join(directory, link[0].removeprefix(_GITDIR)))
        )
        if len(gitdir.parts) > 2 and gitdir.parts[-2] == "worktrees":
            made_at = link[1]
    else:
        gitdir = PurePosixPath(directory, ".git")
    head = _first_line(f"{gitdir}/HEAD", root, access)
    if head is None:
        return None
    branch = head[0].removeprefix(_BRANCH) if head[0].startswith(_BRANCH) else ""
    repo = _repository(gitdir)
    return Checkout(
        branch if valid_branch(branch) else "",
        repo if valid_branch(repo) else "",
        made_at,
    )
