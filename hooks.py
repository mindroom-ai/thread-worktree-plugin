# ruff: noqa: INP001
"""Hook entrypoints for the MindRoom thread-worktree plugin."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mindroom.plugin_api import EnrichmentItem, get_tool_runtime_context, hook

from .directory import repository_of, tied_path, unrestricted
from .state import (
    ThreadWorktree,
    read_thread_worktree,
    shell_path,
    valid_branch,
    write_thread_worktree,
)

if TYPE_CHECKING:
    from mindroom.plugin_api import SystemEnrichContext, ToolAfterCallContext

# Below the hook's 3 second timeout, so a slow read ends in this plugin's warning, not in a failed hook.
STATE_READ_TIMEOUT_SECONDS = 2.5

_SHELL_TOOL = "run_shell_command"
# `agent-cli dev new`, also as `ag dev new` or through `uvx`, and the two lines of its output that name what it made.
_CREATE_COMMAND = "dev new"
_CREATED_PATH = re.compile(r"Created worktree at (\S+)")
_CREATED_BRANCH = re.compile(r"Creating worktree for branch '([^']*)'")


def _worktree_instruction(worktree: ThreadWorktree) -> str:
    """Return the instruction that tells the agent where its thread's worktree is and how to work in it."""
    path = worktree.path
    branch = f" (branch `{worktree.branch}`)" if worktree.branch else ""
    place = "outside" if path.startswith("/") else "inside"
    return (
        f"This thread works in the git worktree `{path}`{branch}, a directory {place} your workspace. "
        f'Start every shell command for this thread\'s work with `cd "{shell_path(path)}" &&`. '
        f"For file tools, name files with the prefix `{path}/`. "
        "If the directory is missing, tell the user and call leave_worktree."
    )


@hook(event="system:enrich", name="thread-worktree-instruction", timeout_ms=3000)
async def add_thread_worktree_instruction(
    ctx: SystemEnrichContext,
) -> EnrichmentItem | None:
    """Tell the agent that tied its thread to a worktree where that worktree is, on every turn in the thread."""
    # A team's turn has no one workspace the worktree is in.
    if ctx.target_member_names is not None:
        return None
    thread_id = ctx.envelope.target.resolved_thread_id
    if thread_id is None:
        return None
    room_id = ctx.envelope.room_id
    try:
        async with asyncio.timeout(STATE_READ_TIMEOUT_SECONDS):
            worktree = await read_thread_worktree(
                ctx.query_room_state, room_id, thread_id
            )
    except Exception as exc:  # a hook must never break the turn
        ctx.logger.warning(
            "Failed to read the thread's worktree; the turn gets no worktree instruction",
            room_id=room_id,
            thread_id=thread_id,
            error=repr(exc),
        )
        return None
    # The directory is in the workspace of the agent that tied the thread, so only that agent is told about it. Room
    # state is not proof of who wrote it, so only an agent that may leave its workspace is sent outside it.
    if (
        worktree is not None
        and worktree.set_by == ctx.target_entity_name
        and (
            not worktree.path.startswith("/")
            or unrestricted(ctx.config, ctx.target_entity_name)
        )
    ):
        # Required in minimal mode, where other system items reach the agent only through a document it may not read.
        return EnrichmentItem(
            key="thread_worktree",
            text=_worktree_instruction(worktree),
            cache_policy="stable",
            minimal_required=True,
        )
    return None


def _created_worktree(ctx: ToolAfterCallContext) -> tuple[str, str] | None:
    """Return the path and branch of the worktree a shell call's `agent-cli dev new` made: the last, if several."""
    if ctx.tool_name != _SHELL_TOOL:
        return None
    command = ctx.arguments.get("args")
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    if not isinstance(command, str) or _CREATE_COMMAND not in command:
        return None
    output = str(ctx.result)
    paths = _CREATED_PATH.findall(output)
    if not paths:
        return None
    branches = _CREATED_BRANCH.findall(output)
    branch = branches[-1] if branches else ""
    return paths[-1], branch if valid_branch(branch) else ""


@hook(event="tool:after_call", name="tie-created-worktree", timeout_ms=5000)
async def tie_created_worktree(ctx: ToolAfterCallContext) -> None:
    """Tie the thread a shell call ran in to the worktree its `agent-cli dev new` made."""
    if (
        ctx.error is not None
        or ctx.blocked
        or ctx.result is None
        or ctx.room_id is None
        or ctx.thread_id is None
    ):
        return
    created = _created_worktree(ctx)
    runtime = get_tool_runtime_context()
    if created is None or runtime is None:
        return
    path, branch = created
    try:
        # The path is the shell's, so one that is no directory here, such as a worker's, ties nothing.
        tied = tied_path(
            runtime.agent_workspace_root(),
            path,
            outside=unrestricted(ctx.config, ctx.agent_name),
        )
        if tied is None:
            return
        repo = repository_of(tied)
        current = await read_thread_worktree(
            ctx.query_room_state, ctx.room_id, ctx.thread_id
        )
        if current is not None and (
            current.path,
            current.branch,
            current.repo,
            current.set_by,
        ) == (tied, branch, repo, ctx.agent_name):
            return
        worktree = ThreadWorktree(
            tied, branch, ctx.agent_name, datetime.now(UTC).isoformat(), repo
        )
        if not await write_thread_worktree(
            ctx.put_room_state, ctx.room_id, ctx.thread_id, worktree
        ):
            ctx.logger.warning("Failed to tie the created worktree", path=tied)
    except Exception as exc:  # a hook must never break the turn
        ctx.logger.warning("Failed to tie the created worktree", error=repr(exc))


__all__ = ["add_thread_worktree_instruction", "tie_created_worktree"]
