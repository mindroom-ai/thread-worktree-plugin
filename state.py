# ruff: noqa: INP001
"""The worktree directory of a thread, backed by Matrix room state."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
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


def _valid_path(path: object) -> bool:
    if not isinstance(path, str) or not path:
        return False
    parsed = PurePosixPath(path)
    return not parsed.is_absolute() and ".." not in parsed.parts


async def read_thread_worktree(
    query_room_state: RoomStateQuerier,
    room_id: str,
    thread_id: str,
) -> ThreadWorktree | None:
    """Read the thread's worktree, or None when there is none or it is invalid."""
    content = await query_room_state(room_id, THREAD_WORKTREE_EVENT_TYPE, thread_id)
    if not isinstance(content, Mapping) or not _valid_path(content.get("path")):
        return None
    branch = content.get("branch")
    set_by = content.get("set_by")
    set_at = content.get("set_at")
    return ThreadWorktree(
        content["path"],
        branch if isinstance(branch, str) else "",
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
