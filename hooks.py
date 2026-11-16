# ruff: noqa: INP001
"""Hook entrypoint for the MindRoom thread-worktree plugin."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mindroom.plugin_api import hook

from .state import read_thread_worktree

if TYPE_CHECKING:
    from mindroom.plugin_api import TurnLifecycleContext

# Below the hooks' 3 second timeout, so a slow read ends in this plugin's warning, not in a failed hook.
STATE_READ_TIMEOUT_SECONDS = 2.5


async def _set_thread_workdir(ctx: TurnLifecycleContext) -> None:
    """Run the turn in the worktree directory of its thread, when the thread has one."""
    thread_id = ctx.envelope.target.resolved_thread_id
    if thread_id is None:
        return
    room_id = ctx.envelope.room_id
    try:
        async with asyncio.timeout(STATE_READ_TIMEOUT_SECONDS):
            worktree = await read_thread_worktree(ctx.query_room_state, room_id, thread_id)
    except Exception as exc:  # a hook must never break the turn
        ctx.logger.warning(
            "Failed to read the thread's worktree; the turn runs in the workspace",
            room_id=room_id,
            thread_id=thread_id,
            error=repr(exc),
        )
        return
    # The directory is in the workspace of the agent that tied the thread, so only that agent's turns work there.
    if worktree is not None and worktree.set_by == ctx.envelope.agent_name:
        ctx.set_workdir(worktree.path)


@hook(event="turn:started", name="thread-worktree-started", timeout_ms=3000)
async def work_in_thread_worktree(ctx: TurnLifecycleContext) -> None:
    """Start the turn in its thread's worktree."""
    await _set_thread_workdir(ctx)


__all__ = ["work_in_thread_worktree"]
