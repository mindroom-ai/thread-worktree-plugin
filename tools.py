# ruff: noqa: INP001
"""Agent-facing tools for the MindRoom thread-worktree plugin."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from agno.tools import Toolkit
from mindroom.plugin_api import (
    HookBindings,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolRuntimeContext,
    ToolStatus,
    get_tool_runtime_context,
    register_tool_with_metadata,
)

from .state import (
    ThreadWorktree,
    clear_thread_worktree,
    read_thread_worktree,
    write_thread_worktree,
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _payload(status: str, **kwargs: object) -> str:
    payload: dict[str, object] = {"status": status, "tool": "thread_worktree"}
    payload.update(kwargs)
    return json.dumps(payload, sort_keys=True)


def _error(action: str, message: str) -> str:
    return _payload("error", action=action, message=message)


def _resolve_state_access(
    *,
    action: str,
    require_putter: bool,
) -> tuple[ToolRuntimeContext, str, str, HookBindings] | str:
    """Resolve the active thread scope and room-state bindings."""
    context = get_tool_runtime_context()
    if context is None:
        return _error(action, "Thread worktree tool context is unavailable in this runtime path.")
    if context.resolved_thread_id is None:
        return _error(action, f"An active thread is required to {action} a worktree.")
    bindings = context.hooks
    if bindings.room_state_querier is None or (require_putter and bindings.room_state_putter is None):
        return _error(action, "Thread worktree room-state access is unavailable in this runtime path.")
    return context, context.room_id, context.resolved_thread_id, bindings


def _workspace_relative(root: Path, path: str) -> str | None:
    """Return the POSIX path of an existing subdirectory of the workspace, or None when it is not one."""
    root = root.resolve()
    candidate = Path(path)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    # Resolving follows every link, so a link that leaves the workspace ends up outside the root.
    if not resolved.is_dir() or not resolved.is_relative_to(root) or resolved == root:
        return None
    return resolved.relative_to(root).as_posix()


class ThreadWorktreeTools(Toolkit):
    """Toolkit for tying a Matrix thread to a directory in the agent's workspace."""

    def __init__(self) -> None:
        super().__init__(
            name="thread_worktree",
            instructions=(
                "To work on a task in its own git worktree, first make it with the shell: "
                "`git -C <clone> worktree add <path inside your workspace> -b <branch>`. "
                "Then call `use_worktree(path, branch)`. From this thread's next turn your tools run in that directory. "
                "Call `leave_worktree` when the work is merged."
            ),
            tools=[
                self.use_worktree,
                self.leave_worktree,
                self.get_worktree,
            ],
        )

    async def use_worktree(self, path: str, branch: str = "") -> str:
        """Make the current thread work in a directory of your workspace, such as a git worktree you created.

        The directory applies from the thread's next turn.
        """
        resolved = _resolve_state_access(action="use", require_putter=True)
        if isinstance(resolved, str):
            return resolved
        context, room_id, thread_id, bindings = resolved
        relative = _workspace_relative(context.agent_workspace_root(), path)
        if relative is None:
            return _error("use", "Expected an existing directory inside your workspace, other than the workspace itself.")
        branch = branch.strip()

        current = await read_thread_worktree(bindings.room_state_querier, room_id, thread_id)
        if current is not None and (current.path, current.branch) == (relative, branch):
            return _payload("ok", action="use", thread_id=thread_id, path=relative, branch=branch, changed=False)

        worktree = ThreadWorktree(relative, branch, context.agent_name or context.requester_id, _now_iso())
        if not await write_thread_worktree(bindings.room_state_putter, room_id, thread_id, worktree):
            return _error("use", "Failed to write the worktree state event.")
        return _payload(
            "ok",
            action="use",
            thread_id=thread_id,
            path=relative,
            branch=branch,
            changed=True,
            note="The directory applies from this thread's next turn.",
        )

    async def leave_worktree(self) -> str:
        """Stop the current thread from working in a worktree directory; its turns run in the workspace again."""
        resolved = _resolve_state_access(action="leave", require_putter=True)
        if isinstance(resolved, str):
            return resolved
        _, room_id, thread_id, bindings = resolved

        if await read_thread_worktree(bindings.room_state_querier, room_id, thread_id) is None:
            return _payload("ok", action="leave", thread_id=thread_id, changed=False)
        if not await clear_thread_worktree(bindings.room_state_putter, room_id, thread_id):
            return _error("leave", "Failed to clear the worktree state event.")
        return _payload("ok", action="leave", thread_id=thread_id, changed=True)

    async def get_worktree(self) -> str:
        """Show the worktree directory of the current thread, if it has one."""
        resolved = _resolve_state_access(action="get", require_putter=False)
        if isinstance(resolved, str):
            return resolved
        _, room_id, thread_id, bindings = resolved

        current = await read_thread_worktree(bindings.room_state_querier, room_id, thread_id)
        if current is None:
            return _payload("ok", action="get", thread_id=thread_id)
        return _payload(
            "ok",
            action="get",
            thread_id=thread_id,
            path=current.path,
            branch=current.branch,
            set_by=current.set_by,
            set_at=current.set_at,
        )


@register_tool_with_metadata(
    name="thread_worktree",
    file_access=ToolFileAccess.NONE,
    display_name="Thread Worktree",
    description="Tie the current Matrix thread to a git worktree directory in the agent's workspace.",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="LuGitBranch",
    icon_color="text-green-600",
)
def thread_worktree_factory() -> type[ThreadWorktreeTools]:
    """Factory function for the thread-worktree toolkit."""
    return ThreadWorktreeTools
