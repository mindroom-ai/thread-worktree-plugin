# ruff: noqa: INP001
"""Hook entrypoint for the MindRoom thread-worktree plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.plugin_api import hook

from .state import read_thread_worktree

if TYPE_CHECKING:
    from mindroom.plugin_api import TurnLifecycleContext


async def _set_thread_workdir(ctx: TurnLifecycleContext) -> None:
    """Run the turn in the worktree directory of its thread, when the thread has one."""
    thread_id = ctx.envelope.target.resolved_thread_id
    if thread_id is None:
        return
    try:
        worktree = await read_thread_worktree(ctx.query_room_state, ctx.envelope.room_id, thread_id)
        if worktree is not None:
            ctx.set_workdir(worktree.path)
    except Exception as exc:  # a hook must never break the turn
        ctx.logger.warning("Failed to set the thread's worktree as working directory", error=str(exc))


@hook(event="turn:started", name="thread-worktree-started", timeout_ms=3000)
async def work_in_thread_worktree(ctx: TurnLifecycleContext) -> None:
    """Start the turn in its thread's worktree."""
    await _set_thread_workdir(ctx)


@hook(event="turn:resumed", name="thread-worktree-resumed", timeout_ms=3000)
async def work_in_thread_worktree_after_approval(ctx: TurnLifecycleContext) -> None:
    """Resume the turn after an approval in its thread's worktree."""
    await _set_thread_workdir(ctx)


__all__ = ["work_in_thread_worktree", "work_in_thread_worktree_after_approval"]
