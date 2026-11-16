# ruff: noqa: INP001
"""Which directory an agent may tie a thread to, and how the tie names it."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from .state import valid_worktree_path

# `agent-cli dev new` puts a repository's worktrees beside its clone, in `<repo>-worktrees/<name>`.
_WORKTREES_SUFFIX = "-worktrees"


def unrestricted(config: Any, agent_name: str) -> bool:  # noqa: ANN401 - MindRoom's config, read by two attributes.
    """Return whether the agent's `file_access` is `unrestricted`: its own setting, else the default."""
    agent = config.agents.get(agent_name)
    own = agent.file_access if agent is not None else None
    return (own if own is not None else config.defaults.file_access) == "unrestricted"


def tied_path(root: Path, path: str, *, outside: bool) -> str | None:
    """Return how a tie names the existing directory `path`, or None when it may not be tied.

    A directory inside the workspace `root` is named relative to it. One outside is named by its absolute path, and
    only with `outside`, which an agent with unrestricted file access has. `path` is relative to the root or absolute.
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
    elif outside:
        tied = resolved.as_posix()
    else:
        return None
    return tied if valid_worktree_path(tied) else None


def repository_of(tied: str) -> str:
    """Return the repository of a worktree at `<repo>-worktrees/<name>`, or an empty string for any other place."""
    parent = PurePosixPath(tied).parent.name
    return (
        parent.removesuffix(_WORKTREES_SUFFIX)
        if parent.endswith(_WORKTREES_SUFFIX)
        else ""
    )
