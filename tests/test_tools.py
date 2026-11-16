# ruff: noqa: INP001
"""Tests for the thread-worktree toolkit."""

from __future__ import annotations

import json
import sys
from contextlib import ExitStack
from importlib import util
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from mindroom.hooks import HookBindings
from mindroom.message_target import MessageTarget
from mindroom.tool_system.registry import plugin_module_registrations
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context

if TYPE_CHECKING:
    from types import ModuleType

PACKAGE_NAME = f"mindroom_plugin_{Path(__file__).resolve().parents[1].name.replace('-', '_')}"
EVENT_TYPE = "com.mindroom.thread.worktree"
ROOM = "!room:localhost"
THREAD = "$thread-root"


def _load_tools_module() -> ModuleType:
    """Load the plugin tools module under its synthetic package name."""
    tools_path = Path(__file__).resolve().parents[1] / "tools.py"
    module_name = f"{PACKAGE_NAME}.tools_test_{uuid4().hex}"
    sys.modules.pop(module_name, None)
    spec = util.spec_from_file_location(module_name, tools_path)
    assert spec is not None
    assert spec.loader is not None
    module = util.module_from_spec(spec)
    sys.modules[module_name] = module
    # A plugin module registers its tools while it loads, as the plugin loader runs it.
    with plugin_module_registrations(module_name):
        spec.loader.exec_module(module)
    return module


def _tool_context(
    *,
    agent_name: str = "code",
    thread_id: str | None = THREAD,
    resolved_thread_id: str | None = THREAD,
    room_state_querier: AsyncMock | None = None,
    room_state_putter: AsyncMock | None = None,
) -> ToolRuntimeContext:
    return ToolRuntimeContext(
        agent_name=agent_name,
        target=MessageTarget(
            room_id=ROOM,
            source_thread_id=thread_id,
            resolved_thread_id=resolved_thread_id,
            reply_to_event_id=None,
            session_id="session-1",
        ),
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=SimpleNamespace(),
        runtime_paths=SimpleNamespace(),
        plugins=SimpleNamespace(),
        conversation_reader=AsyncMock(),
        relations=AsyncMock(),
        hooks=HookBindings(room_state_querier=room_state_querier, room_state_putter=room_state_putter),
    )


class FakeRoomState:
    """In-memory room state with the querier and putter the runtime provides."""

    def __init__(self, *, put_result: bool = True) -> None:
        self.state: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.put_result = put_result
        self.querier = AsyncMock(side_effect=self._query)
        self.putter = AsyncMock(side_effect=self._put)

    async def _query(self, room_id: str, event_type: str, state_key: str | None) -> dict[str, Any] | None:
        if state_key is not None:
            return self.state.get((room_id, event_type, state_key))
        return {key: content for (room, kind, key), content in self.state.items() if (room, kind) == (room_id, event_type)}

    async def _put(self, room_id: str, event_type: str, state_key: str, content: dict[str, Any]) -> bool:
        if self.put_result:
            self.state[(room_id, event_type, state_key)] = dict(content)
        return self.put_result

    def context(self, root: Path, **kwargs: Any) -> Any:  # noqa: ANN401
        """Enter the runtime context with `root` as the agent's workspace root."""
        stack = ExitStack()
        stack.enter_context(patch.object(ToolRuntimeContext, "agent_workspace_root", lambda _self, **_kw: root))
        stack.enter_context(
            tool_runtime_context(_tool_context(room_state_querier=self.querier, room_state_putter=self.putter, **kwargs)),
        )
        return stack


def _key() -> tuple[str, str, str]:
    return (ROOM, EVENT_TYPE, THREAD)


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "worktrees" / "a").mkdir(parents=True)
    return root


def test_instructions_make_the_worktree_in_the_workspace() -> None:
    """The instructions build the worktree from the workspace variable and tie it by its workspace-relative path."""
    instructions = _load_tools_module().ThreadWorktreeTools().instructions

    assert (
        'git -C "$MINDROOM_AGENT_WORKSPACE/<clone>" worktree add "$MINDROOM_AGENT_WORKSPACE/worktrees/<name>" -b <branch>'
        in instructions
    )
    assert 'use_worktree("worktrees/<name>", "<branch>")' in instructions
    assert 'cd "$MINDROOM_AGENT_WORKSPACE/worktrees/<name>" &&' in instructions
    assert "`leave_worktree` before" in instructions


@pytest.mark.asyncio
async def test_use_worktree_writes_and_is_idempotent(tmp_path: Path) -> None:
    """Setting a directory writes it once; the same call again writes nothing."""
    tools = _load_tools_module().ThreadWorktreeTools()
    room = FakeRoomState()
    root = _workspace(tmp_path)

    with room.context(root):
        first = json.loads(await tools.use_worktree("worktrees/a", " feature-a "))
        again = json.loads(await tools.use_worktree("worktrees/a", "feature-a"))

    assert first["status"] == "ok"
    assert first["tool"] == "thread_worktree"
    assert first["action"] == "use"
    assert first["thread_id"] == THREAD
    assert first["path"] == "worktrees/a"
    assert first["branch"] == "feature-a"
    assert first["changed"] is True
    assert "next turn" in first["note"]
    assert 'cd "$MINDROOM_AGENT_WORKSPACE/worktrees/a" &&' in first["note"]
    assert again["changed"] is False
    assert again["note"] == first["note"]
    content = room.state[_key()]
    assert content["path"] == "worktrees/a"
    assert content["branch"] == "feature-a"
    assert content["set_by"] == "code"
    assert content["set_at"]
    assert room.putter.await_count == 1


@pytest.mark.asyncio
async def test_use_worktree_changed_branch_rewrites(tmp_path: Path) -> None:
    """A different branch for the same directory is a change."""
    tools = _load_tools_module().ThreadWorktreeTools()
    room = FakeRoomState()
    root = _workspace(tmp_path)

    with room.context(root):
        await tools.use_worktree("worktrees/a", "one")
        result = json.loads(await tools.use_worktree("worktrees/a", "two"))

    assert result["changed"] is True
    assert room.state[_key()]["branch"] == "two"
    assert "replaced" not in result


@pytest.mark.asyncio
async def test_another_agent_replaces_the_tie_and_is_told_whose(tmp_path: Path) -> None:
    """A second agent's tie replaces the first, even for the same directory, and the answer names the previous one."""
    tools = _load_tools_module().ThreadWorktreeTools()
    room = FakeRoomState()
    root = _workspace(tmp_path)

    with room.context(root, agent_name="code"):
        await tools.use_worktree("worktrees/a", "feat")
    with room.context(root, agent_name="reviewer"):
        result = json.loads(await tools.use_worktree("worktrees/a", "feat"))

    assert result["status"] == "ok"
    assert result["changed"] is True
    assert result["replaced"] == {"path": "worktrees/a", "set_by": "code"}
    assert room.state[_key()]["set_by"] == "reviewer"
    assert room.putter.await_count == 2


@pytest.mark.asyncio
async def test_absolute_path_inside_workspace_is_stored_relative(tmp_path: Path) -> None:
    """An absolute path inside the workspace is stored relative to it."""
    tools = _load_tools_module().ThreadWorktreeTools()
    room = FakeRoomState()
    root = _workspace(tmp_path)

    with room.context(root):
        result = json.loads(await tools.use_worktree(str(root / "worktrees" / "a")))

    assert result["status"] == "ok"
    assert room.state[_key()]["path"] == "worktrees/a"
    assert room.state[_key()]["branch"] == ""


@pytest.mark.asyncio
async def test_symlink_inside_workspace_is_stored_as_its_target(tmp_path: Path) -> None:
    """A link to another directory of the workspace is stored as the target's relative path."""
    tools = _load_tools_module().ThreadWorktreeTools()
    room = FakeRoomState()
    root = _workspace(tmp_path)
    (root / "alias").symlink_to(root / "worktrees" / "a")

    with room.context(root):
        result = json.loads(await tools.use_worktree("alias"))

    assert result["status"] == "ok"
    assert room.state[_key()]["path"] == "worktrees/a"


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["./worktrees/a", "worktrees/a/"])
async def test_path_spellings_are_normalized(tmp_path: Path, spelling: str) -> None:
    """A leading dot-slash or a trailing slash does not change the stored path."""
    tools = _load_tools_module().ThreadWorktreeTools()
    room = FakeRoomState()
    root = _workspace(tmp_path)

    with room.context(root):
        result = json.loads(await tools.use_worktree(spelling))

    assert result["status"] == "ok"
    assert room.state[_key()]["path"] == "worktrees/a"


@pytest.mark.asyncio
async def test_refused_directories_write_nothing(tmp_path: Path) -> None:
    """Missing, file, escaping, outside, link-out and workspace-root paths all error."""
    tools = _load_tools_module().ThreadWorktreeTools()
    room = FakeRoomState()
    root = _workspace(tmp_path)
    (root / "file.txt").write_text("x")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    (root / "worktrees" / "inner-link").symlink_to(outside)

    candidates = [
        "worktrees/missing",
        "file.txt",
        "../x",
        "../outside",
        str(outside),
        "link",
        "link/",
        "worktrees/inner-link",
        ".",
        "",
        str(root),
        "a\0b",
    ]
    with room.context(root):
        for candidate in candidates:
            result = json.loads(await tools.use_worktree(candidate))
            assert result["status"] == "error", candidate
            assert result["tool"] == "thread_worktree"
            assert result["action"] == "use"
            assert "existing directory inside your workspace" in result["message"]
            assert "relative to the workspace root" in result["message"]

    assert room.state == {}
    room.putter.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_write_errors(tmp_path: Path) -> None:
    """A failed write answers an error."""
    tools = _load_tools_module().ThreadWorktreeTools()
    room = FakeRoomState(put_result=False)
    root = _workspace(tmp_path)

    with room.context(root):
        result = json.loads(await tools.use_worktree("worktrees/a"))

    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_leave_clears_only_when_set(tmp_path: Path) -> None:
    """Leaving writes an empty payload only when a worktree was set."""
    tools = _load_tools_module().ThreadWorktreeTools()
    room = FakeRoomState()
    root = _workspace(tmp_path)

    with room.context(root):
        none_set = json.loads(await tools.leave_worktree())
        room.putter.assert_not_awaited()
        await tools.use_worktree("worktrees/a", "b")
        left = json.loads(await tools.leave_worktree())

    assert none_set["status"] == "ok"
    assert none_set["action"] == "leave"
    assert none_set["changed"] is False
    assert left["changed"] is True
    assert room.state[_key()] == {}
    assert room.putter.await_count == 2


@pytest.mark.asyncio
async def test_leave_failed_write_errors(tmp_path: Path) -> None:
    """A failed clear answers an error."""
    tools = _load_tools_module().ThreadWorktreeTools()
    room = FakeRoomState(put_result=False)
    room.state[_key()] = {"path": "worktrees/a", "branch": "", "set_by": "x", "set_at": "t"}

    with room.context(_workspace(tmp_path)):
        result = json.loads(await tools.leave_worktree())

    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_get_worktree(tmp_path: Path) -> None:
    """Getting reads the thread's worktree, or reports none."""
    tools = _load_tools_module().ThreadWorktreeTools()
    room = FakeRoomState()
    root = _workspace(tmp_path)

    with room.context(root):
        empty = json.loads(await tools.get_worktree())
        await tools.use_worktree("worktrees/a", "b")
        found = json.loads(await tools.get_worktree())

    assert empty["status"] == "ok"
    assert empty["action"] == "get"
    assert empty["thread_id"] == THREAD
    assert "path" not in empty
    assert "exists" not in empty
    assert found["path"] == "worktrees/a"
    assert found["branch"] == "b"
    assert found["set_by"] == "code"
    assert found["exists"] is True
    assert found["applies"] is True


@pytest.mark.asyncio
async def test_get_worktree_reports_a_removed_directory(tmp_path: Path) -> None:
    """A tie whose directory is gone neither exists nor applies."""
    tools = _load_tools_module().ThreadWorktreeTools()
    room = FakeRoomState()
    root = _workspace(tmp_path)

    with room.context(root):
        await tools.use_worktree("worktrees/a", "b")
        (root / "worktrees" / "a").rmdir()
        found = json.loads(await tools.get_worktree())

    assert found["path"] == "worktrees/a"
    assert found["exists"] is False
    assert found["applies"] is False


@pytest.mark.asyncio
async def test_get_worktree_reports_another_agents_tie(tmp_path: Path) -> None:
    """Another agent's tie does not apply to the caller, even when the caller has that directory too."""
    tools = _load_tools_module().ThreadWorktreeTools()
    room = FakeRoomState()
    root = _workspace(tmp_path)

    with room.context(root, agent_name="code"):
        await tools.use_worktree("worktrees/a", "b")
    with room.context(root, agent_name="reviewer"):
        found = json.loads(await tools.get_worktree())

    assert found["set_by"] == "code"
    assert found["exists"] is True
    assert found["applies"] is False


@pytest.mark.asyncio
async def test_every_tool_errors_outside_a_thread(tmp_path: Path) -> None:
    """Outside a thread every tool errors and writes nothing."""
    tools = _load_tools_module().ThreadWorktreeTools()
    room = FakeRoomState()
    root = _workspace(tmp_path)

    with room.context(root, thread_id=None, resolved_thread_id=None):
        results = [
            json.loads(await tools.use_worktree("worktrees/a")),
            json.loads(await tools.leave_worktree()),
            json.loads(await tools.get_worktree()),
        ]

    assert [result["status"] for result in results] == ["error"] * 3
    assert room.state == {}
    room.putter.assert_not_awaited()
