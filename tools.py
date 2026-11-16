# ruff: noqa: INP001
"""Agent-facing tools for the MindRoom thread-worktree plugin."""

from __future__ import annotations

import json
from datetime import UTC, datetime

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

from .directory import checkout, file_access, tied_path
from .state import (
    MAX_BRANCH_LENGTH,
    MAX_PATH_LENGTH,
    ThreadWorktree,
    clear_thread_worktree,
    read_thread_worktree,
    shell_path,
    valid_branch,
    valid_worktree_path,
    write_thread_worktree,
)


_INSTRUCTIONS = """\
A coding task gets its own git worktree before the first change, in a thread that has none:
1. Make it where and how your own instructions say. Without any, use the shell with absolute paths: \
`git -C "$MINDROOM_AGENT_WORKSPACE/<clone>" worktree add "$MINDROOM_AGENT_WORKSPACE/worktrees/<name>" -b <branch>` \
(`agent-cli dev new <branch>`, where it is installed, also fills submodules and installs dependencies).
2. Call `use_worktree("worktrees/<name>", "<branch>")`; the path is relative to your workspace. A thread without a \
worktree is tied to a new one by itself when the command names its absolute path, and the call then answers \
`changed: false`.
3. Start every shell command for this work with `cd` into the worktree, such as \
`cd "$MINDROOM_AGENT_WORKSPACE/worktrees/<name>" &&`, and give file tools paths inside it; \
from the thread's next turn your system prompt reminds you.
4. Call `leave_worktree` before you remove the worktree."""

_REFUSAL = (
    "Expected an existing directory inside your workspace, other than the workspace itself, "
    "given relative to the workspace root, such as `worktrees/<name>` for `$MINDROOM_AGENT_WORKSPACE/worktrees/<name>`,"
    f" whose path has at most {MAX_PATH_LENGTH} characters, only A-Z a-z 0-9 . _ - and /, and no empty, `.` or `..` segment."
    " Only an agent with unrestricted file access may tie a directory outside its workspace, by its absolute path."
)


def _note(path: str) -> str:
    return (
        f'Start shell commands for this work with `cd "{shell_path(path)}" &&`, and give file tools paths '
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


def _tied_path(context: ToolRuntimeContext, path: str) -> str | None:
    """Return how a tie names the directory `path` for the calling agent, or None when it may not tie it."""
    return tied_path(
        context.agent_workspace_root(),
        path,
        file_access(context.config, context.agent_name),
    )


class ThreadWorktreeTools(Toolkit):
    """Toolkit for tying a Matrix thread to a directory the agent works in."""

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
        """Tie the current thread to a directory you work in, such as a git worktree you made.

        Start shell commands for this work with `cd` into it, as the answer's note says. Other agents in the thread are
        not told. A thread's earlier tie is replaced.

        Args:
            path: An existing directory inside your workspace, relative to the workspace root, such as
                `worktrees/<name>`; with unrestricted file access also one outside it, by its absolute path.
            branch: The worktree's branch, shown with the thread; optional, and read from the worktree without it.

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
        tied = _tied_path(context, path)
        if tied is None:
            return _error("use", _REFUSAL)
        branch = branch.strip()
        if not valid_branch(branch):
            return _error(
                "use",
                f"The branch must have at most {MAX_BRANCH_LENGTH} characters, only A-Z a-z 0-9 . _ - and /.",
            )
        # What git says is in the directory: the branch unless one was given, and the repository's name.
        found = checkout(
            context.agent_workspace_root(),
            tied,
            file_access(context.config, context.agent_name),
        )
        branch = branch or (found.branch if found is not None else "")
        repo = found.repo if found is not None else ""

        current = await read_thread_worktree(
            bindings.room_state_querier, room_id, thread_id
        )
        tie = (tied, branch, repo, context.agent_name)
        if (
            current is not None
            and (current.path, current.branch, current.repo, current.set_by) == tie
        ):
            return _payload(
                "ok",
                action="use",
                thread_id=thread_id,
                path=tied,
                branch=branch,
                repo=repo,
                changed=False,
                note=_note(tied),
            )

        # The tie is one agent's; another agent's tie of this thread is replaced, and the answer says whose it was.
        worktree = ThreadWorktree(tied, branch, context.agent_name, _now_iso(), repo)
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
            path=tied,
            branch=branch,
            repo=repo,
            changed=True,
            note=_note(tied),
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

        `exists` says whether the directory is an existing directory you may work in, and `applies` whether the tie is
        yours: you made it and its directory exists.
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
        exists = _tied_path(context, current.path) is not None
        return _payload(
            "ok",
            action="get",
            thread_id=thread_id,
            path=current.path,
            branch=current.branch,
            repo=current.repo,
            set_by=current.set_by,
            set_at=current.set_at,
            exists=exists,
            applies=exists and current.set_by == context.agent_name,
        )


@register_tool_with_metadata(
    name="thread_worktree",
    file_access=ToolFileAccess.NONE,
    display_name="Thread Worktree",
    description="Tie the current Matrix thread to the git worktree directory the agent works in.",
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
