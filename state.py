# ruff: noqa: INP001
"""The worktree directory of a thread, backed by Matrix room state."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

THREAD_WORKTREE_EVENT_TYPE = "com.mindroom.thread.worktree"

type RoomStateQuerier = Callable[[str, str, str | None], Awaitable[dict[str, Any] | None]]
type RoomStatePutter = Callable[[str, str, str, dict[str, Any]], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class ThreadWorktree:
    """The directory a thread works in."""

    path: str
    branch: str
    set_by: str
    set_at: str


# Both values go into the agent's system prompt, so they hold only plain path characters and no prose or markup.
_PLAIN_PATH = re.compile(r"[A-Za-z0-9._/-]+")
MAX_PATH_LENGTH = 200
MAX_BRANCH_LENGTH = 100


def valid_worktree_path(path: object) -> bool:
    """Return whether `path` names a directory inside the workspace that the worktree instruction can quote."""
    if not isinstance(path, str) or len(path) > MAX_PATH_LENGTH or _PLAIN_PATH.fullmatch(path) is None:
        return False
    # An empty, `.` or `..` segment, an absolute path or a path without parts names no directory inside the workspace.
    return path[0] != "/" and all(segment not in {"", ".", ".."} for segment in path.split("/"))


def valid_branch(branch: object) -> bool:
    """Return whether `branch` is empty or holds only plain path characters, at most 100 of them."""
    return isinstance(branch, str) and len(branch) <= MAX_BRANCH_LENGTH and (not branch or _PLAIN_PATH.fullmatch(branch) is not None)


async def read_thread_worktree(
    query_room_state: RoomStateQuerier,
    room_id: str,
    thread_id: str,
) -> ThreadWorktree | None:
    """Read the thread's worktree, or None when there is none or it is invalid."""
    content = await query_room_state(room_id, THREAD_WORKTREE_EVENT_TYPE, thread_id)
    if not isinstance(content, Mapping) or not valid_worktree_path(content.get("path")):
        return None
    branch = content.get("branch")
    set_by = content.get("set_by")
    set_at = content.get("set_at")
    return ThreadWorktree(
        content["path"],
        branch if valid_branch(branch) else "",
        set_by if isinstance(set_by, str) else "",
        set_at if isinstance(set_at, str) else "",
    )


async def write_thread_worktree(
    put_room_state: RoomStatePutter,
    room_id: str,
    thread_id: str,
    worktree: ThreadWorktree,
) -> bool:
    """Write the thread's worktree."""
    return await put_room_state(
        room_id,
        THREAD_WORKTREE_EVENT_TYPE,
        thread_id,
        {"path": worktree.path, "branch": worktree.branch, "set_by": worktree.set_by, "set_at": worktree.set_at},
    )


async def clear_thread_worktree(put_room_state: RoomStatePutter, room_id: str, thread_id: str) -> bool:
    """Clear the thread's worktree by writing an empty payload."""
    return await put_room_state(room_id, THREAD_WORKTREE_EVENT_TYPE, thread_id, {})
