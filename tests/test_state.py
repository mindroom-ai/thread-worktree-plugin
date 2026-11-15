# ruff: noqa: INP001
"""Tests for the thread worktree state module."""

from __future__ import annotations

import sys
from importlib import util
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

PACKAGE_NAME = f"mindroom_plugin_{Path(__file__).resolve().parents[1].name.replace('-', '_')}"
ROOM = "!room:localhost"
THREAD = "$thread-root"


def _load_state() -> Any:  # noqa: ANN401
    path = Path(__file__).resolve().parents[1] / "state.py"
    name = f"{PACKAGE_NAME}.state_test"
    spec = util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


state = _load_state()


def test_event_type() -> None:
    """The event type is the documented one."""
    assert state.THREAD_WORKTREE_EVENT_TYPE == "com.mindroom.thread.worktree"


@pytest.mark.asyncio
async def test_read_parses_full_event() -> None:
    """A full event reads into a ThreadWorktree with one keyed read."""
    querier = AsyncMock(
        return_value={"path": "worktrees/a", "branch": "feat", "set_by": "code", "set_at": "2026-01-01T00:00:00+00:00"},
    )
    result = await state.read_thread_worktree(querier, ROOM, THREAD)
    assert result == state.ThreadWorktree("worktrees/a", "feat", "code", "2026-01-01T00:00:00+00:00")
    querier.assert_awaited_once_with(ROOM, "com.mindroom.thread.worktree", THREAD)


@pytest.mark.asyncio
async def test_missing_branch_reads_empty() -> None:
    """A missing branch reads as an empty string."""
    result = await state.read_thread_worktree(AsyncMock(return_value={"path": "w/a"}), ROOM, THREAD)
    assert result is not None
    assert result.branch == ""
    assert result.path == "w/a"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        None,
        {},
        {"branch": "x"},
        {"path": 3},
        {"path": ""},
        {"path": "/abs/dir"},
        {"path": "a/../b"},
        {"path": ".."},
        {"path": "."},
        {"path": "./"},
        {"path": "./."},
    ],
)
async def test_invalid_content_reads_none(content: Any) -> None:  # noqa: ANN401
    """Empty or invalid content, and a path that names the workspace itself, read as no worktree."""
    assert await state.read_thread_worktree(AsyncMock(return_value=content), ROOM, THREAD) is None


@pytest.mark.asyncio
async def test_write_and_clear() -> None:
    """Writing and clearing put the expected content under the thread root."""
    putter = AsyncMock(return_value=True)
    worktree = state.ThreadWorktree("w/a", "feat", "code", "t")
    assert await state.write_thread_worktree(putter, ROOM, THREAD, worktree) is True
    putter.assert_awaited_with(
        ROOM,
        "com.mindroom.thread.worktree",
        THREAD,
        {"path": "w/a", "branch": "feat", "set_by": "code", "set_at": "t"},
    )
    assert await state.clear_thread_worktree(putter, ROOM, THREAD) is True
    putter.assert_awaited_with(ROOM, "com.mindroom.thread.worktree", THREAD, {})


@pytest.mark.asyncio
async def test_non_string_attribution_reads_empty() -> None:
    """A set_by or set_at that is not a string reads as an empty string."""
    content = {"path": "w/a", "set_by": 5, "set_at": ["t"]}
    result = await state.read_thread_worktree(AsyncMock(return_value=content), ROOM, THREAD)
    assert result is not None
    assert (result.set_by, result.set_at) == ("", "")
