# ruff: noqa: INP001
"""Hook entrypoints for the MindRoom thread-worktree plugin."""

from __future__ import annotations

import asyncio
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.plugin_api import EnrichmentItem, get_tool_runtime_context, hook

from .directory import UNRESTRICTED, Checkout, checkout, file_access, tied_path
from .state import (
    ThreadWorktree,
    read_thread_worktree,
    shell_path,
    write_thread_worktree,
)

if TYPE_CHECKING:
    from mindroom.plugin_api import SystemEnrichContext, ToolAfterCallContext

# Below the hook's 3 second timeout, so a slow read ends in this plugin's warning, not in a failed hook.
STATE_READ_TIMEOUT_SECONDS = 2.5

_SHELL_TOOL = "run_shell_command"
# An absolute path as a shell call's command or output names it: plain path characters, after `~`, `$HOME` or the
# workspace variable when it starts with one. A path below any other variable is not known here.
_PATH = re.compile(
    r"(?<![\w.~$}/-])"
    r"(~|\$HOME|\$\{HOME\}|\$MINDROOM_AGENT_WORKSPACE|\$\{MINDROOM_AGENT_WORKSPACE\})?"
    r"(/[A-Za-z0-9._/-]+)"
)
# How many of the paths a call names are looked at, from the last one back.
_MAX_PATHS = 20
# A worktree counts as made by a call when its `.git` file is no older than the call's start, less this.
_CLOCK_SLACK_SECONDS = 5.0


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
            or file_access(ctx.config, ctx.target_entity_name) == UNRESTRICTED
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


def _made_worktree(
    text: str, root: Path, access: str, since: float
) -> tuple[str, Checkout] | None:
    """Return the tie's path and the checkout of a worktree that `text` names and that was made after `since`.

    The text only says where to look: what is tied is a directory the agent may tie that holds a git worktree whose
    `.git` file was written after `since`. Of several, the one named last.
    """
    seen: list[str] = []
    for prefix, rest in reversed(_PATH.findall(text)):
        start = (
            ""
            if not prefix
            else str(root)
            if "WORKSPACE" in prefix
            else str(Path.home())
        )
        path = (start + rest).rstrip(".")
        if path in seen:
            continue
        if len(seen) == _MAX_PATHS:
            break
        seen.append(path)
        tied = tied_path(root, path, access)
        made = None if tied is None else checkout(root, tied, access)
        if made is not None and made.made_at is not None and made.made_at >= since:
            return tied, made
    return None


@hook(event="tool:after_call", name="tie-made-worktree", timeout_ms=5000)
async def tie_made_worktree(ctx: ToolAfterCallContext) -> None:
    """Tie a thread that has no worktree to the git worktree a shell call in it made."""
    if (
        ctx.tool_name != _SHELL_TOOL
        or ctx.error is not None
        or ctx.blocked
        or ctx.result is None
        or ctx.room_id is None
        or ctx.thread_id is None
    ):
        return
    command = ctx.arguments.get("args")
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    text = f"{command}\n{ctx.result}"
    # `git worktree add` and `agent-cli dev new` both say "worktree", in the command or in what they print.
    runtime = get_tool_runtime_context()
    if "worktree" not in text or runtime is None:
        return
    try:
        made = _made_worktree(
            text,
            runtime.agent_workspace_root(),
            file_access(ctx.config, ctx.agent_name),
            time.time() - ctx.duration_ms / 1000 - _CLOCK_SLACK_SECONDS,
        )
        # A tie is replaced only by `use_worktree`, never by this hook.
        if (
            made is None
            or await read_thread_worktree(
                ctx.query_room_state, ctx.room_id, ctx.thread_id
            )
            is not None
        ):
            return
        tied, (branch, repo, _) = made
        worktree = ThreadWorktree(
            tied, branch, ctx.agent_name, datetime.now(UTC).isoformat(), repo
        )
        if not await write_thread_worktree(
            ctx.put_room_state, ctx.room_id, ctx.thread_id, worktree
        ):
            ctx.logger.warning("Failed to tie the made worktree", path=tied)
    except Exception as exc:  # a hook must never break the turn
        ctx.logger.warning("Failed to tie the made worktree", error=repr(exc))


__all__ = ["add_thread_worktree_instruction", "tie_made_worktree"]
