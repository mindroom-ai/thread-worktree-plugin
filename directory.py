# ruff: noqa: INP001
"""Which directory an agent may tie a thread to, how the tie names it, and what git checkout is in it."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Any

from mindroom.plugin_api import resolve_agent_file

from .state import valid_branch, valid_worktree_path

UNRESTRICTED = "unrestricted"
_GITDIR = "gitdir: "
_BRANCH = "ref: refs/heads/"
# A `.git` file and a `HEAD` hold one short line.
_MAX_LINE_BYTES = 1024


def file_access(config: Any, agent_name: str) -> str:  # noqa: ANN401 - MindRoom's config, read by two attributes.
    """Return the agent's `file_access`, `workspace` or `unrestricted`: its own setting, else the default."""
    agent = config.agents.get(agent_name)
    own = agent.file_access if agent is not None else None
    return own if own is not None else config.defaults.file_access


def tied_path(root: Path, path: str, access: str) -> str | None:
    """Return how a tie names the existing directory `path`, or None when it may not be tied.

    `path` is relative to the workspace `root` or absolute. A directory inside the workspace is named relative to
    it. One outside is named by its absolute path, and only for an agent whose file access is unrestricted.

    This runs in MindRoom's primary process, and a confined agent's worker controls every file of its workspace. So
    for a confined agent the path is taken as written: it must be inside the workspace without a `..` part, and no
    part of it may be a link, because following a link here would tell the agent what its target is.
    """
    try:
        root = root.resolve()
        if access == UNRESTRICTED:
            # Resolving follows every link, which an agent that may read any path may do.
            resolved = (root / path).resolve()
            relative = (
                resolved.relative_to(root) if resolved.is_relative_to(root) else None
            )
        else:
            written = PurePosixPath(os.path.normpath(root / path))
            if ".." in PurePosixPath(path).parts or not written.is_relative_to(root):
                return None
            relative = written.relative_to(root)
            # A path without links is its own resolution; nothing below is asked of the filesystem otherwise.
            resolved = Path(written)
            if resolved.resolve() != resolved:
                return None
        if not resolved.is_dir() or resolved == root:
            return None
    except (
        ValueError,
        OSError,
        RuntimeError,
    ):  # RuntimeError: a link loop, before Python 3.13
        return None
    tied = resolved.as_posix() if relative is None else relative.as_posix()
    return tied if valid_worktree_path(tied) else None


def _first_line(path: str) -> str | None:
    """Return the first line of the small regular file at `path`, or None when there is none to read."""
    try:
        authorized = resolve_agent_file(
            path, workspace_root=None, file_access=UNRESTRICTED, field_name="path"
        )
        with authorized.open() as file:
            return file.readline(_MAX_LINE_BYTES).decode().strip()
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


def checkout(directory: str) -> tuple[str, str]:
    """Return the branch and the repository's name of the git checkout in `directory`, each empty when unknown.

    Only for an agent with unrestricted file access, whose own tools may read these files too: the checkout's `.git`
    file and its `HEAD` are read, and git is not run. `directory` is absolute.
    """
    link = _first_line(f"{directory}/.git")
    if link is not None and link.startswith(_GITDIR):
        # A worktree's and a submodule's `.git` is a file that names the git directory, absolute or relative to it.
        gitdir = PurePosixPath(
            os.path.normpath(os.path.join(directory, link.removeprefix(_GITDIR)))
        )
    else:
        gitdir = PurePosixPath(directory, ".git")
    head = _first_line(f"{gitdir}/HEAD")
    if head is None:
        return "", ""
    branch = head.removeprefix(_BRANCH) if head.startswith(_BRANCH) else ""
    repo = _repository(gitdir)
    return branch if valid_branch(branch) else "", repo if valid_branch(repo) else ""
