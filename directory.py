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


def _inside(root: Path, path: str) -> PurePosixPath | None:
    """Return `path` relative to the workspace `root` as it is written, or None when it is not written inside it."""
    written = PurePosixPath(path)
    if written.is_absolute():
        # An absolute spelling of a directory of the workspace, by the root's own spelling or its resolved one.
        for spelling in (root, root.resolve()):
            if written.is_relative_to(spelling):
                written = written.relative_to(spelling)
                break
        else:
            return None
    # The workspace itself is no directory to tie, and a `..` part is not followed.
    return None if ".." in written.parts or not written.parts else written


def tied_path(root: Path, path: str, access: str) -> str | None:
    """Return how a tie names the existing directory `path`, or None when it may not be tied.

    `path` is relative to the workspace `root` or absolute. A directory inside the workspace is named relative to
    it. One outside is named by its absolute path, and only for an agent whose file access is unrestricted.

    This runs in MindRoom's primary process, and a confined agent's worker controls every file of its workspace. So
    a confined agent's path is walked from the root without following a link, in one pass that a swapped directory
    cannot redirect: it then says nothing about what is outside the workspace. An agent with unrestricted file
    access may read any path, so its path is resolved, links included.
    """
    try:
        if access == UNRESTRICTED:
            base = root.resolve()
            resolved = (base / path).resolve()
            if not resolved.is_dir() or resolved == base:
                return None
            inside = resolved.is_relative_to(base)
            tied = (resolved.relative_to(base) if inside else resolved).as_posix()
        else:
            relative = _inside(root, path)
            if relative is None:
                return None
            with open_directory_within_root(root, relative):
                tied = relative.as_posix()
    except (ValueError, OSError):
        return None
    return tied if valid_worktree_path(tied) else None
