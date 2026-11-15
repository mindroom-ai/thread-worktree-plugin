# ruff: noqa: INP001
"""Tests for the hooks that run a thread's turns in its worktree."""

from __future__ import annotations

import asyncio
import sys
from importlib import util
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from mindroom.hooks.decorators import get_hook_metadata

if TYPE_CHECKING:
    from types import ModuleType

PACKAGE_NAME = f"mindroom_plugin_{Path(__file__).resolve().parents[1].name.replace('-', '_')}"
EVENT_TYPE = "com.mindroom.thread.worktree"
PAYLOAD = {"path": "worktrees/fix", "branch": "fix", "set_by": "coder", "set_at": "2026-11-15T10:00:00+00:00"}


def _load_hooks_module() -> ModuleType:
    """Load the plugin hooks module under its synthetic package name."""
    hooks_path = Path(__file__).resolve().parents[1] / "hooks.py"
    module_name = f"{PACKAGE_NAME}.hooks"
    sys.modules.pop(module_name, None)
    spec = util.spec_from_file_location(module_name, hooks_path)
    assert spec is not None
    assert spec.loader is not None
    module = util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _context(thread_id: str | None = "$thread-root", **overrides: Any) -> SimpleNamespace:  # noqa: ANN401
    """Build a minimal turn lifecycle context stub."""
    values: dict[str, Any] = {
        "envelope": SimpleNamespace(
            room_id="!room:localhost",
            agent_name="coder",
            target=SimpleNamespace(resolved_thread_id=thread_id),
        ),
        "query_room_state": AsyncMock(return_value=dict(PAYLOAD)),
        "set_workdir": Mock(),
        "logger": MagicMock(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("function", "event", "name"),
    [
        ("work_in_thread_worktree", "turn:started", "thread-worktree-started"),
        ("work_in_thread_worktree_after_approval", "turn:resumed", "thread-worktree-resumed"),
    ],
)
def test_hook_metadata(function: str, event: str, name: str) -> None:
    """Both hooks run on their turn event under their name with a 3 second timeout."""
    metadata = get_hook_metadata(getattr(_load_hooks_module(), function))

    assert metadata is not None
    assert metadata.event_name == event
    assert metadata.hook_name == name
    assert metadata.timeout_ms == 3000


@pytest.mark.asyncio
@pytest.mark.parametrize("function", ["work_in_thread_worktree", "work_in_thread_worktree_after_approval"])
async def test_thread_with_worktree_sets_workdir(function: str) -> None:
    """A thread with a worktree gets its path as the turn's working directory."""
    ctx = _context()

    await getattr(_load_hooks_module(), function)(ctx)

    ctx.query_room_state.assert_awaited_once_with("!room:localhost", EVENT_TYPE, "$thread-root")
    ctx.set_workdir.assert_called_once_with("worktrees/fix")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [None, {}, {"path": 5}, {"path": "../outside"}, {"path": "/abs"}, "garbage"],
)
async def test_no_or_malformed_worktree_sets_nothing(content: object) -> None:
    """No event, an empty payload or a malformed one leaves the turn in the workspace."""
    ctx = _context(query_room_state=AsyncMock(return_value=content))

    await _load_hooks_module().work_in_thread_worktree(ctx)

    ctx.set_workdir.assert_not_called()
    ctx.logger.warning.assert_not_called()


@pytest.mark.asyncio
async def test_no_thread_sets_nothing_and_reads_nothing() -> None:
    """A turn outside a thread has no worktree."""
    ctx = _context(thread_id=None)

    await _load_hooks_module().work_in_thread_worktree(ctx)

    ctx.query_room_state.assert_not_awaited()
    ctx.set_workdir.assert_not_called()


@pytest.mark.asyncio
async def test_failed_read_logs_one_warning_and_raises_nothing() -> None:
    """A failed state read never reaches the turn."""
    ctx = _context(query_room_state=AsyncMock(side_effect=RuntimeError("boom")))

    await _load_hooks_module().work_in_thread_worktree(ctx)

    ctx.set_workdir.assert_not_called()
    ctx.logger.warning.assert_called_once()
    assert ctx.logger.warning.call_args.kwargs == {
        "room_id": "!room:localhost",
        "thread_id": "$thread-root",
        "error": "RuntimeError('boom')",
    }


@pytest.mark.asyncio
async def test_slow_read_is_cut_off_with_one_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """A read slower than the hook's own bound ends in one warning, and the turn runs in the workspace."""
    hooks = _load_hooks_module()
    monkeypatch.setattr(hooks, "STATE_READ_TIMEOUT_SECONDS", 0.01)
    never = asyncio.Event()

    async def hang(*_args: object) -> None:
        await never.wait()

    ctx = _context(query_room_state=AsyncMock(side_effect=hang))

    # The guard only keeps a hook without its own bound from hanging the test.
    await asyncio.wait_for(hooks.work_in_thread_worktree(ctx), timeout=1)

    ctx.set_workdir.assert_not_called()
    ctx.logger.warning.assert_called_once()
    assert ctx.logger.warning.call_args.kwargs == {
        "room_id": "!room:localhost",
        "thread_id": "$thread-root",
        "error": "TimeoutError()",
    }
