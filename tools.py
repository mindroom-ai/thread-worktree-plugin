# ruff: noqa: INP001
"""Agent-facing tools for the MindRoom thread-worktree plugin."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Literal

from mindroom.plugin_api import (
    HookBindings,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    Toolkit,
    ToolRuntimeContext,
    ToolStatus,
    get_tool_runtime_context,
    register_tool_with_metadata,
    tool,
)

from .directory import file_access, tied_path
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
`git -C "$MINDROOM_AGENT_WORKSPACE/<clone>" worktree add "$MINDROOM_AGENT_WORKSPACE/worktrees/<name>" -b <branch>`.
2. Call `thread_worktree(action="use", path="worktrees/<name>", branch="<branch>", repo="<repository>")`; \
the path is relative to your workspace, or absolute for a worktree outside it, which only an agent with \
unrestricted file access may tie.
3. Start every shell command for this work with `cd` into the worktree, such as \
`cd "$MINDROOM_AGENT_WORKSPACE/worktrees/<name>" &&`, and give file tools paths inside it; \
from the thread's next turn your system prompt reminds you.
4. Call `thread_worktree(action="leave")` before you remove the worktree."""

_REFUSAL = (
    "Expected an existing directory inside your workspace, other than the workspace itself, "
    "given relative to the workspace root, such as `worktrees/<name>` for `$MINDROOM_AGENT_WORKSPACE/worktrees/<name>`,"
    f" whose path has at most {MAX_PATH_LENGTH} characters, only A-Z a-z 0-9 . _ - and /, and no empty, `.` or `..` segment."
    " With unrestricted file access also an existing directory outside the workspace, given by its absolute path,"
    " whose resolved form has the same characters; without it, no part of the path may be a link, and MindRoom must"
    " be allowed to read the directory."
)
_NAME_RULE = f"at most {MAX_BRANCH_LENGTH} characters, only A-Z a-z 0-9 . _ - and /"
_ALLOWED_PARAMS: dict[str, frozenset[str]] = {
    "use": frozenset({"path", "branch", "repo"}),
    "get": frozenset(),
    "leave": frozenset(),
}


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


def _validate_action_params(
    action: object,
    *,
    path: str | None,
    branch: str | None,
    repo: str | None,
) -> str | None:
    """Validate `action` and reject unexpected or missing parameters."""
    if not isinstance(action, str) or action not in _ALLOWED_PARAMS:
        expected = ", ".join(_ALLOWED_PARAMS)
        return _payload(
            "error",
            message=f"Unsupported thread_worktree action {action!r}. Expected one of: {expected}.",
        )
    provided = {"path": path, "branch": branch, "repo": repo}
    allowed = _ALLOWED_PARAMS[action]
    unexpected = [
        name
        for name, value in provided.items()
        if value is not None and name not in allowed
    ]
    if unexpected:
        names = ", ".join(f"'{name}'" for name in unexpected)
        return _error(
            action,
            f"Parameter(s) {names} cannot be used with action={action!r}.",
        )
    if action == "use" and (not isinstance(path, str) or not path.strip()):
        return _error(action, "Parameter 'path' is required for action='use'.")
    return None


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


async def _tied_path(context: ToolRuntimeContext, path: str) -> str | None:
    """Return how a tie names the directory `path` for the calling agent, or None when it may not tie it."""
    access = file_access(context.config, context.agent_name)
    # Off the event loop: finding the workspace and checking the path both ask the filesystem, which can be slow.
    return await asyncio.to_thread(
        lambda: tied_path(context.agent_workspace_root(), path, access)
    )


class ThreadWorktreeTools(Toolkit):
    """Toolkit for tying a Matrix thread to a directory the agent works in."""

    offer_background_after = False
    offer_output_path = False

    def __init__(self) -> None:
        super().__init__(
            name="thread_worktree",
            instructions=_INSTRUCTIONS,
        )

    @tool
    async def thread_worktree(
        self,
        action: Literal["use", "get", "leave"],
        path: str | None = None,
        branch: str | None = None,
        repo: str | None = None,
    ) -> str:
        """Bind, inspect, or clear this thread's git worktree.

        - `use`: tie the current thread to an existing directory (`path`, optional `branch` and `repo`).
          Replaces any earlier tie.
        - `get`: show the thread's current worktree (`exists` and `applies`).
        - `leave`: clear the thread's worktree tie before removing the worktree.

        Args:
            action: Action to perform (`use`, `get`, or `leave`).
            path: Workspace-relative directory (e.g. `worktrees/<name>`), or absolute path with
                unrestricted file access (`use` only).
            branch: Optional branch label (`use` only).
            repo: Optional repository label (`use` only; spell consistently across threads).

        """
        if (
            error := _validate_action_params(
                action, path=path, branch=branch, repo=repo
            )
        ) is not None:
            return error
        if action == "use":
            assert path is not None
            return await self._use_worktree(path, branch=branch, repo=repo)
        if action == "get":
            return await self._get_worktree()
        return await self._leave_worktree()

    async def _use_worktree(
        self,
        path: str,
        *,
        branch: str | None = None,
        repo: str | None = None,
    ) -> str:
        resolved = _resolve_state_access(action="use", require_putter=True)
        if isinstance(resolved, str):
            return resolved
        context, room_id, thread_id, bindings = resolved
        if branch is not None and not isinstance(branch, str):
            return _error("use", f"The branch must have {_NAME_RULE}.")
        if repo is not None and not isinstance(repo, str):
            return _error("use", f"The repository must have {_NAME_RULE}.")
        branch = "" if branch is None else branch.strip()
        repo = "" if repo is None else repo.strip()
        if not valid_branch(branch):
            return _error("use", f"The branch must have {_NAME_RULE}.")
        if not valid_branch(repo):
            return _error("use", f"The repository must have {_NAME_RULE}.")
        # A relative path is checked as the state checks it before the filesystem is touched.
        if not path.startswith("/") and not valid_worktree_path(
            path.removeprefix("./").removesuffix("/")
        ):
            return _error("use", _REFUSAL)
        tied = await _tied_path(context, path)
        if tied is None:
            return _error("use", _REFUSAL)

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

    async def _leave_worktree(self) -> str:
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

    async def _get_worktree(self) -> str:
        resolved = _resolve_state_access(action="get", require_putter=False)
        if isinstance(resolved, str):
            return resolved
        context, room_id, thread_id, bindings = resolved

        current = await read_thread_worktree(
            bindings.room_state_querier, room_id, thread_id
        )
        if current is None:
            return _payload("ok", action="get", thread_id=thread_id)
        # The tie's own spelling: a path written by hand that only resolves to a directory is not one the hook uses.
        exists = await _tied_path(context, current.path) == current.path
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
    # `thread_worktree(action="use", ...)` checks a directory the model names, inside the workspace unless the
    # agent's file access is unrestricted.
    file_access=ToolFileAccess.AGENT,
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
