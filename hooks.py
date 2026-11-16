# ruff: noqa: INP001
"""Hook entrypoint for the MindRoom thread-worktree plugin."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from mindroom.plugin_api import hook

from .state import ThreadWorktree, read_thread_worktree

if TYPE_CHECKING:
    from mindroom.plugin_api import SystemEnrichContext

# Below the hook's 3 second timeout, so a slow read ends in this plugin's warning, not in a failed hook.
STATE_READ_TIMEOUT_SECONDS = 2.5


def _worktree_instruction(worktree: ThreadWorktree) -> str:
    """Return the instruction that tells the agent where its thread's worktree is and how to work in it."""
    path = worktree.path
    branch = f" (branch `{worktree.branch}`)" if worktree.branch else ""
    return (
        f"This thread works in the git worktree `{path}`{branch}, a directory inside your workspace. "
        f'Start every shell command for this thread\'s work with `cd "$MINDROOM_AGENT_WORKSPACE/{path}" &&`. '
        f"For file tools, name files with the prefix `{path}/`. "
        "If the directory is missing, tell the user and call leave_worktree."
    )


@hook(event="system:enrich", name="thread-worktree-instruction", timeout_ms=3000)
async def add_thread_worktree_instruction(ctx: SystemEnrichContext) -> None:
    """Tell the agent that tied its thread to a worktree where that worktree is, on every turn in the thread."""
    # A team's turn has no one workspace the worktree is in.
    if ctx.target_member_names is not None:
        return
    thread_id = ctx.envelope.target.resolved_thread_id
    if thread_id is None:
        return
    room_id = ctx.envelope.room_id
    try:
        async with asyncio.timeout(STATE_READ_TIMEOUT_SECONDS):
            worktree = await read_thread_worktree(ctx.query_room_state, room_id, thread_id)
    except Exception as exc:  # a hook must never break the turn
        ctx.logger.warning(
            "Failed to read the thread's worktree; the turn gets no worktree instruction",
            room_id=room_id,
            thread_id=thread_id,
            error=repr(exc),
        )
        return
    # The directory is in the workspace of the agent that tied the thread, so only that agent is told about it.
    if worktree is not None and worktree.set_by == ctx.target_entity_name:
        ctx.add_instruction("thread_worktree", _worktree_instruction(worktree), cache_policy="stable")


__all__ = ["add_thread_worktree_instruction"]
