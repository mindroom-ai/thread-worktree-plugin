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
    MAX_BRANCH_LENGTH,
    MAX_PATH_LENGTH,
    ThreadWorktree,
    clear_thread_worktree,
    read_thread_worktree,
    valid_branch,
    valid_worktree_path,
    write_thread_worktree,
)


_INSTRUCTIONS = """\
To work on a task in its own git worktree:
1. Make it with the shell, with absolute paths: \
`git -C "$MINDROOM_AGENT_WORKSPACE/<clone>" worktree add "$MINDROOM_AGENT_WORKSPACE/worktrees/<name>" -b <branch>`.
2. Call `use_worktree("worktrees/<name>", "<branch>")`; the path is relative to your workspace.
3. Start every shell command for this work with \
`cd "$MINDROOM_AGENT_WORKSPACE/worktrees/<name>" &&`, and give file tools paths with the prefix `worktrees/<name>/`; \
from the thread's next turn your system prompt reminds you.
4. Call `leave_worktree` before you remove the worktree."""

_REFUSAL = (
    "Expected an existing directory inside your workspace, other than the workspace itself, "
    "given relative to the workspace root, such as `worktrees/<name>` for `$MINDROOM_AGENT_WORKSPACE/worktrees/<name>`,"
    f" whose path has at most {MAX_PATH_LENGTH} characters, only A-Z a-z 0-9 . _ - and /, and no empty, `.` or `..` segment."
)


def _note(path: str) -> str:
    return (
        f'Start shell commands for this work with `cd "$MINDROOM_AGENT_WORKSPACE/{path}" &&`, and give file tools paths '
        f"with the prefix `{path}/`; from the thread's next turn your system prompt reminds you."
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
        return _error(
            action, "Thread worktree tool context is unavailable in this runtime path."
        )
    if context.resolved_thread_id is None:
        return _error(action, f"An active thread is required to {action} a worktree.")
    bindings = context.hooks
    if bindings.room_state_querier is None or (
        require_putter and bindings.room_state_putter is None
    ):
        return _error(
            action,
            "Thread worktree room-state access is unavailable in this runtime path.",
        )
    return context, context.room_id, context.resolved_thread_id, bindings


def _workspace_relative(root: Path, path: str) -> str | None:
    """Return the POSIX path of an existing subdirectory of the workspace, or None when it is not one."""
    try:
        root = root.resolve()
        resolved = (root / path).resolve()
        # Resolving follows every link, so a link that leaves the workspace ends up outside the root.
        if (
            not resolved.is_dir()
            or not resolved.is_relative_to(root)
            or resolved == root
        ):
            return None
    except (ValueError, OSError):
        return None
    return resolved.relative_to(root).as_posix()


class ThreadWorktreeTools(Toolkit):
    """Toolkit for tying a Matrix thread to a directory in the agent's workspace."""

    def __init__(self) -> None:
        super().__init__(
            name="thread_worktree",
            instructions=_INSTRUCTIONS,
            tools=[
                self.use_worktree,
                self.leave_worktree,
                self.get_worktree,
            ],
        )

    async def use_worktree(self, path: str, branch: str = "") -> str:
        """Tie the current thread to a directory of your workspace, such as a git worktree you made.

        Start shell commands for this work with `cd "$MINDROOM_AGENT_WORKSPACE/<path>" &&`. Other agents in the thread
        are not told.

        Args:
            path: An existing directory inside your workspace, relative to the workspace root, such as `worktrees/<name>`.
            branch: The worktree's branch, shown with the thread; optional.

        """
        resolved = _resolve_state_access(action="use", require_putter=True)
        if isinstance(resolved, str):
            return resolved
        context, room_id, thread_id, bindings = resolved
        # A relative path is checked as the state checks it before the filesystem is touched; an absolute one only
        # becomes relative once it is resolved.
        if not path.startswith("/") and not valid_worktree_path(
            path.removeprefix("./").removesuffix("/")
        ):
            return _error("use", _REFUSAL)
        relative = _workspace_relative(context.agent_workspace_root(), path)
        if relative is None or not valid_worktree_path(relative):
            return _error("use", _REFUSAL)
        branch = branch.strip()
        if not valid_branch(branch):
            return _error(
                "use",
                f"The branch must have at most {MAX_BRANCH_LENGTH} characters, only A-Z a-z 0-9 . _ - and /.",
            )

        current = await read_thread_worktree(
            bindings.room_state_querier, room_id, thread_id
        )
        tie = (relative, branch, context.agent_name)
        if (
            current is not None
            and (current.path, current.branch, current.set_by) == tie
        ):
            return _payload(
                "ok",
                action="use",
                thread_id=thread_id,
                path=relative,
                branch=branch,
                changed=False,
                note=_note(relative),
            )

        # The tie is one agent's; another agent's tie of this thread is replaced, and the answer says whose it was.
        worktree = ThreadWorktree(relative, branch, context.agent_name, _now_iso())
        if not await write_thread_worktree(
            bindings.room_state_putter, room_id, thread_id, worktree
        ):
            return _error("use", "Failed to write the worktree state event.")
        replaced = (
            {"replaced": {"path": current.path, "set_by": current.set_by}}
            if current is not None and current.set_by != context.agent_name
            else {}
        )
        return _payload(
            "ok",
            action="use",
            thread_id=thread_id,
            path=relative,
            branch=branch,
            changed=True,
            note=_note(relative),
            **replaced,
        )

    async def leave_worktree(self) -> str:
        """Untie the current thread from its worktree; from its next turn your system prompt no longer names it.

        Call it before you remove the worktree.
        """
        resolved = _resolve_state_access(action="leave", require_putter=True)
        if isinstance(resolved, str):
            return resolved
        context, room_id, thread_id, bindings = resolved

        current = await read_thread_worktree(
            bindings.room_state_querier, room_id, thread_id
        )
        if current is None:
            return _payload("ok", action="leave", thread_id=thread_id, changed=False)
        if not await clear_thread_worktree(
            bindings.room_state_putter, room_id, thread_id
        ):
            return _error("leave", "Failed to clear the worktree state event.")
        # Another agent's tie is cleared too, and the answer says whose it was.
        cleared = (
            {"cleared_set_by": current.set_by}
            if current.set_by != context.agent_name
            else {}
        )
        return _payload(
            "ok", action="leave", thread_id=thread_id, changed=True, **cleared
        )

    async def get_worktree(self) -> str:
        """Show the worktree directory of the current thread, if it has one.

        `exists` says whether the directory is an existing directory inside your workspace, and `applies` whether the
        tie is yours: you made it and its directory exists.
        """
        resolved = _resolve_state_access(action="get", require_putter=False)
        if isinstance(resolved, str):
            return resolved
        context, room_id, thread_id, bindings = resolved

        current = await read_thread_worktree(
            bindings.room_state_querier, room_id, thread_id
        )
        if current is None:
            return _payload("ok", action="get", thread_id=thread_id)
        exists = (
            _workspace_relative(context.agent_workspace_root(), current.path)
            is not None
        )
        return _payload(
            "ok",
            action="get",
            thread_id=thread_id,
            path=current.path,
            branch=current.branch,
            set_by=current.set_by,
            set_at=current.set_at,
            exists=exists,
            applies=exists and current.set_by == context.agent_name,
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
    requires_room_context=True,
)
def thread_worktree_factory() -> type[ThreadWorktreeTools]:
    """Factory function for the thread-worktree toolkit."""
    return ThreadWorktreeTools
