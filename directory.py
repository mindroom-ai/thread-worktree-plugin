# ruff: noqa: INP001
"""Which directory an agent may tie a thread to, and how the tie names it."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from mindroom.plugin_api import open_directory_within_root

from .state import valid_worktree_path

UNRESTRICTED = "unrestricted"


def file_access(config: Any, agent_name: str) -> str:  # noqa: ANN401 - MindRoom's config, read by two attributes.
    """Return the agent's `file_access`, `workspace` or `unrestricted`: its own setting, else the default."""
    agent = config.agents.get(agent_name)
    own = agent.file_access if agent is not None else None
    return own if own is not None else config.defaults.file_access


def _inside(root: Path, path: str) -> str | None:
    """Return `path` relative to the workspace `root` as it is written, or None when it is not written inside it.

    Nothing is asked of the filesystem: an absolute path counts when it starts with the root as MindRoom spells it.
    """
    written = PurePosixPath(path)
    if written.is_absolute():
        if not written.is_relative_to(root):
            return None
        written = written.relative_to(root)
    # The workspace itself is no directory to tie, and a `..` part is not followed.
    if ".." in written.parts or not written.parts:
        return None
    relative = written.as_posix()
    return relative if valid_worktree_path(relative) else None


def tied_path(root: Path, path: str, access: str) -> str | None:
    """Return how a tie names the existing directory `path`, or None when it may not be tied.

    `path` is relative to the workspace `root` or absolute. A directory inside the workspace is named relative to
    it. One outside is named by its absolute path, and only for an agent whose file access is unrestricted.

    This runs in MindRoom's primary process, and the worker of a confined agent controls every file of its
    workspace. So a confined agent's path is walked from the root without following a link, in one pass that a
    swapped directory cannot redirect: it then says nothing about what is outside the workspace. The walk opens each
    directory, so MindRoom must be allowed to read the one it ends in. An agent with unrestricted file access may
    read any path, so its path is resolved, links included.
    """
    try:
        if access != UNRESTRICTED:
            relative = _inside(root, path)
            if relative is None:
                return None
            with open_directory_within_root(root, relative):
                return relative
        base = root.resolve()
        resolved = (base / path).resolve()
        if not resolved.is_dir() or resolved == base:
            return None
        inside = resolved.is_relative_to(base)
        tied = (resolved.relative_to(base) if inside else resolved).as_posix()
    except (ValueError, OSError):
        return None
    return tied if valid_worktree_path(tied) else None
