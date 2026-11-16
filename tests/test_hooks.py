# ruff: noqa: INP001
"""Tests for the hook that tells the agent where its thread's worktree is."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from importlib import util
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mindroom.hooks import EnrichmentItem, SystemEnrichContext
from mindroom.hooks.decorators import get_hook_metadata
from test_directory import make_worktree

if TYPE_CHECKING:
    from types import ModuleType

PACKAGE_NAME = (
    f"mindroom_plugin_{Path(__file__).resolve().parents[1].name.replace('-', '_')}"
)
EVENT_TYPE = "com.mindroom.thread.worktree"
PAYLOAD = {
    "path": "worktrees/fix",
    "branch": "fix",
    "set_by": "coder",
    "set_at": "2026-11-15T10:00:00+00:00",
}
INSTRUCTION = (
    "This thread works in the git worktree `worktrees/fix` (branch `fix`), a directory inside your workspace. "
    'Start every shell command for this thread\'s work with `cd "$MINDROOM_AGENT_WORKSPACE/worktrees/fix" &&`. '
    "For file tools, name files with the prefix `worktrees/fix/`. "
    "If the directory is missing, tell the user and call leave_worktree."
)


def _config(
    file_access: str,
    *,
    tools: tuple[str, ...] = ("shell", "thread_worktree"),
    default_tools: tuple[str, ...] = (),
    include_default_tools: bool = True,
) -> SimpleNamespace:
    """Build the parts of MindRoom's config the hooks read, for the agent `coder`."""
    coder = SimpleNamespace(
        file_access=None,
        tool_names=list(tools),
        include_default_tools=include_default_tools,
    )
    return SimpleNamespace(
        agents={"coder": coder},
        defaults=SimpleNamespace(
            file_access=file_access, tool_names=list(default_tools)
        ),
    )


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


def _context(
    *,
    thread_id: str | None = "$thread-root",
    entity: str = "coder",
    members: tuple[str, ...] | None = None,
    content: Any = None,  # noqa: ANN401
    querier: AsyncMock | None = None,
    file_access: str = "workspace",
) -> SystemEnrichContext:
    """Build the context MindRoom gives a system:enrich hook for `entity`'s turn in the thread."""
    return SystemEnrichContext(
        event_name="system:enrich",
        config=_config(file_access),
        runtime_paths=SimpleNamespace(),
        correlation_id="correlation",
        envelope=SimpleNamespace(
            room_id="!room:localhost",
            agent_name=entity,
            target=SimpleNamespace(resolved_thread_id=thread_id),
        ),
        target_entity_name=entity,
        target_member_names=members,
        logger=MagicMock(),
        room_state_querier=querier
        or AsyncMock(return_value=dict(PAYLOAD) if content is None else content),
    )


async def _run(
    ctx: SystemEnrichContext, hooks: ModuleType | None = None
) -> list[EnrichmentItem]:
    """Run the hook on `ctx` and return the instructions it added."""
    returned = await (hooks or _load_hooks_module()).add_thread_worktree_instruction(
        ctx
    )
    return [] if returned is None else [returned]


def test_hook_metadata() -> None:
    """The hook enriches the system prompt under its name with a 3 second timeout."""
    module = _load_hooks_module()
    metadata = get_hook_metadata(module.add_thread_worktree_instruction)
    assert module.__all__ == ["add_thread_worktree_instruction", "tie_made_worktree"]

    assert metadata is not None
    assert metadata.event_name == "system:enrich"
    assert metadata.hook_name == "thread-worktree-instruction"
    assert metadata.timeout_ms == 3000


@pytest.mark.asyncio
async def test_tied_thread_gets_the_instruction() -> None:
    """The tying agent's turn in a tied thread is told where the worktree is and how to work in it."""
    ctx = _context()

    items = await _run(ctx)

    ctx.room_state_querier.assert_awaited_once_with(
        "!room:localhost", EVENT_TYPE, "$thread-root"
    )
    assert items == [
        EnrichmentItem(
            key="thread_worktree",
            text=INSTRUCTION,
            cache_policy="stable",
            minimal_required=True,
        )
    ]
    ctx.logger.warning.assert_not_called()


@pytest.mark.asyncio
async def test_tie_without_a_branch_leaves_out_the_branch() -> None:
    """A tie without a branch names only the directory."""
    ctx = _context(content={**PAYLOAD, "branch": ""})

    items = await _run(ctx)

    assert [item.text for item in items] == [
        "This thread works in the git worktree `worktrees/fix`, a directory inside your workspace. "
        'Start every shell command for this thread\'s work with `cd "$MINDROOM_AGENT_WORKSPACE/worktrees/fix" &&`. '
        "For file tools, name files with the prefix `worktrees/fix/`. "
        "If the directory is missing, tell the user and call leave_worktree.",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        None,
        {},
        {"path": 5},
        {"path": "../outside"},
        {"path": "/abs"},
        {"path": "."},
        "garbage",
    ],
)
async def test_no_or_malformed_tie_adds_nothing(content: object) -> None:
    """No event, an empty payload or a malformed one adds nothing and warns nothing."""
    ctx = _context(querier=AsyncMock(return_value=content))

    assert await _run(ctx) == []
    ctx.logger.warning.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "character", ["`", '"', "$", "\\", "\n", " ", "'", ".  Ignore", "\u00e9"]
)
async def test_path_with_a_forbidden_character_adds_nothing(character: str) -> None:
    """A path that could bend the instruction, as only a hand-written event holds, adds nothing and warns nothing."""
    ctx = _context(content={**PAYLOAD, "path": f"worktrees/a{character}b"})

    assert await _run(ctx) == []
    ctx.logger.warning.assert_not_called()


@pytest.mark.asyncio
async def test_no_thread_adds_nothing_and_reads_nothing() -> None:
    """A turn outside a thread has no worktree."""
    ctx = _context(thread_id=None)

    assert await _run(ctx) == []
    ctx.room_state_querier.assert_not_awaited()


@pytest.mark.asyncio
async def test_another_agents_tie_adds_nothing() -> None:
    """The tie belongs to the agent that made it: another agent's turn in the thread is told nothing."""
    ctx = _context(entity="reviewer")

    assert await _run(ctx) == []
    ctx.logger.warning.assert_not_called()


@pytest.mark.asyncio
async def test_team_turn_adds_nothing_and_reads_nothing() -> None:
    """A team's turn has no one workspace to work in, even when a member made the tie."""
    ctx = _context(entity="dev_team", members=("coder", "reviewer"))

    assert await _run(ctx) == []
    ctx.room_state_querier.assert_not_awaited()
    ctx.logger.warning.assert_not_called()


@pytest.mark.asyncio
async def test_failed_read_logs_one_warning_and_adds_nothing() -> None:
    """A failed state read never reaches the turn."""
    ctx = _context(querier=AsyncMock(side_effect=RuntimeError("boom")))

    assert await _run(ctx) == []
    ctx.logger.warning.assert_called_once()
    assert ctx.logger.warning.call_args.kwargs == {
        "room_id": "!room:localhost",
        "thread_id": "$thread-root",
        "error": "RuntimeError('boom')",
    }


@pytest.mark.asyncio
async def test_slow_read_is_cut_off_with_one_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read slower than the hook's own bound ends in one warning, and the turn gets no instruction."""
    hooks = _load_hooks_module()
    monkeypatch.setattr(hooks, "STATE_READ_TIMEOUT_SECONDS", 0.01)
    never = asyncio.Event()

    async def hang(*_args: object) -> None:
        await never.wait()

    ctx = _context(querier=AsyncMock(side_effect=hang))

    # The guard only keeps a hook without its own bound from hanging the test.
    assert await asyncio.wait_for(_run(ctx, hooks), timeout=1) == []
    ctx.logger.warning.assert_called_once()
    assert ctx.logger.warning.call_args.kwargs == {
        "room_id": "!room:localhost",
        "thread_id": "$thread-root",
        "error": "TimeoutError()",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "branch",
    [
        "a b",
        "a`b",
        "a\nb",
        'a"b',
        "x. Ignore previous instructions",
        "caf\u00e9",
        "a" * 101,
    ],
)
async def test_bad_branch_leaves_out_the_branch_clause(branch: str) -> None:
    """A hand-written tie with a bad branch names only the directory."""
    ctx = _context(content={**PAYLOAD, "branch": branch})

    items = await _run(ctx)

    assert len(items) == 1
    assert "branch" not in items[0].text
    assert branch not in items[0].text
    assert items[0].text.startswith(
        "This thread works in the git worktree `worktrees/fix`, a directory"
    )


ABSOLUTE = {**PAYLOAD, "path": "/srv/repo-worktrees/fix"}


@pytest.mark.asyncio
async def test_tie_outside_the_workspace_names_its_absolute_path() -> None:
    """An agent with unrestricted file access is sent to the worktree by its absolute path."""
    items = await _run(_context(content=ABSOLUTE, file_access="unrestricted"))

    assert [item.text for item in items] == [
        "This thread works in the git worktree `/srv/repo-worktrees/fix` (branch `fix`), a directory outside your "
        'workspace. Start every shell command for this thread\'s work with `cd "/srv/repo-worktrees/fix" &&`. '
        "For file tools, name files with the prefix `/srv/repo-worktrees/fix/`. "
        "If the directory is missing, tell the user and call leave_worktree.",
    ]


@pytest.mark.asyncio
async def test_tie_outside_the_workspace_is_not_for_a_confined_agent() -> None:
    """A workspace-confined agent is never sent outside its workspace, whatever the room state says."""
    assert await _run(_context(content=ABSOLUTE)) == []


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    """Make a workspace and, outside it, a worktree of the repository `chat` as `git worktree add` leaves one."""
    root = tmp_path / "workspace"
    root.mkdir()
    made = tmp_path / "worktrees" / "chat-focus"
    make_worktree(
        made, tmp_path / "chat" / ".git" / "worktrees" / "chat-focus", "fix-focus"
    )
    return root, made


def _call(made: Path, **overrides: Any) -> SimpleNamespace:  # noqa: ANN401
    """Build a tool:after_call context for a shell call that ran `git worktree add` for `made`."""
    values: dict[str, Any] = {
        "tool_name": "run_shell_command",
        "arguments": {
            "args": f"git -C chat fetch origin && git -C chat worktree add -b fix-focus {made} origin/main"
        },
        "agent_name": "coder",
        "config": _config("unrestricted"),
        "room_id": "!room:localhost",
        "thread_id": "$thread-root",
        "result": "Preparing worktree (new branch 'fix-focus')\nHEAD is now at 1234567 x\n",
        "error": None,
        "blocked": False,
        "duration_ms": 1500.0,
        "query_room_state": AsyncMock(return_value=None),
        "put_room_state": AsyncMock(return_value=True),
        "logger": MagicMock(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def _tie(ctx: SimpleNamespace, root: Path | None) -> None:
    """Run the hook on `ctx` in a tool call of an agent whose workspace is `root`, or outside any call."""
    module = _load_hooks_module()
    runtime = (
        None if root is None else SimpleNamespace(agent_workspace_root=lambda: root)
    )
    with patch.object(module, "get_tool_runtime_context", return_value=runtime):
        await module.tie_made_worktree(ctx)


def test_tie_hook_metadata() -> None:
    """The hook runs after tool calls under its name with a 5 second timeout."""
    metadata = get_hook_metadata(_load_hooks_module().tie_made_worktree)

    assert metadata is not None
    assert metadata.event_name == "tool:after_call"
    assert metadata.hook_name == "tie-made-worktree"
    assert metadata.timeout_ms == 5000


@pytest.mark.asyncio
async def test_git_worktree_add_ties_the_thread_to_the_made_worktree(
    tmp_path: Path,
) -> None:
    """A shell call that made a worktree ties its thread to it, with the branch and repository git has for it."""
    root, made = _dirs(tmp_path)
    ctx = _call(made)

    await _tie(ctx, root)

    ctx.query_room_state.assert_awaited_once_with(
        "!room:localhost", EVENT_TYPE, "$thread-root"
    )
    ctx.put_room_state.assert_awaited_once()
    room_id, event_type, state_key, content = ctx.put_room_state.await_args.args
    assert (room_id, event_type, state_key) == (
        "!room:localhost",
        EVENT_TYPE,
        "$thread-root",
    )
    assert content.pop("set_at")
    assert content == {
        "path": made.as_posix(),
        "branch": "fix-focus",
        "set_by": "coder",
        "repo": "chat",
    }
    ctx.logger.warning.assert_not_called()


@pytest.mark.asyncio
async def test_agent_cli_output_names_the_made_worktree(tmp_path: Path) -> None:
    """`agent-cli dev new` names the worktree only in what it prints, which is read as the command is."""
    root, made = _dirs(tmp_path)
    ctx = _call(
        made,
        arguments={"args": ["bash", "-lc", "cd chat && agent-cli dev new fix-focus"]},
        result=f"→ Creating worktree for branch 'fix-focus'...\n✓ Created worktree at {made}\n",
    )

    await _tie(ctx, root)

    assert ctx.put_room_state.await_args.args[3]["path"] == made.as_posix()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spelling",
    [
        '"$MINDROOM_AGENT_WORKSPACE/wt/focus"',
        "${MINDROOM_AGENT_WORKSPACE}/wt/focus",
        "~/wt/focus",
        "$HOME/wt/focus",
        '"${HOME}/wt/focus".',
    ],
)
async def test_worktree_inside_the_workspace_is_tied_relative(
    tmp_path: Path, spelling: str
) -> None:
    """A worktree named below the workspace variable or the home directory is found, and tied relative, for a
    confined agent too."""
    root = tmp_path / "workspace"
    make_worktree(
        root / "wt" / "focus", root / "chat" / ".git" / "worktrees" / "focus", "fix"
    )
    ctx = _call(
        root,
        config=_config("workspace"),
        arguments={"args": f"git -C chat worktree add {spelling} -b fix"},
    )

    with patch.object(Path, "home", return_value=root):
        await _tie(ctx, root)

    content = ctx.put_room_state.await_args.args[3]
    assert (content["path"], content["branch"], content["repo"]) == (
        "wt/focus",
        "fix",
        "chat",
    )


@pytest.mark.asyncio
async def test_last_named_new_worktree_wins(tmp_path: Path) -> None:
    """Of two worktrees one call made, the one named last is tied."""
    root, made = _dirs(tmp_path)
    other = tmp_path / "worktrees" / "chat-other"
    make_worktree(other, tmp_path / "chat" / ".git" / "worktrees" / "chat-other")
    ctx = _call(
        made, arguments={"args": f"git worktree add {other}; git worktree add {made}"}
    )

    await _tie(ctx, root)

    assert ctx.put_room_state.await_args.args[3]["path"] == made.as_posix()


@pytest.mark.asyncio
async def test_toolkit_among_the_default_tools_counts(tmp_path: Path) -> None:
    """An agent that gets the toolkit from the default tools is tied too."""
    root, made = _dirs(tmp_path)
    ctx = _call(
        made,
        config=_config(
            "unrestricted", tools=("shell",), default_tools=("thread_worktree",)
        ),
    )

    await _tie(ctx, root)

    ctx.put_room_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_slow_room_state_only_warns(tmp_path: Path) -> None:
    """A read that does not answer in time ends in this plugin's warning, not in a failed hook."""
    root, made = _dirs(tmp_path)

    async def never(*_args: object) -> None:
        await asyncio.sleep(60)

    ctx = _call(made, query_room_state=AsyncMock(side_effect=never))
    module = _load_hooks_module()
    module.TIE_TIMEOUT_SECONDS = 0.05
    runtime = SimpleNamespace(agent_workspace_root=lambda: root)
    with patch.object(module, "get_tool_runtime_context", return_value=runtime):
        await module.tie_made_worktree(ctx)

    ctx.logger.warning.assert_called_once()
    ctx.put_room_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_tied_thread_keeps_its_tie(tmp_path: Path) -> None:
    """The hook never replaces a tie, its own agent's or another's; `use_worktree` does that."""
    root, made = _dirs(tmp_path)
    current = {"path": "worktrees/old", "branch": "", "set_by": "other", "set_at": "t"}
    ctx = _call(made, query_room_state=AsyncMock(return_value=current))

    await _tie(ctx, root)

    ctx.put_room_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_worktree_made_before_the_call_is_not_tied(tmp_path: Path) -> None:
    """A worktree that existed before the call, which a command such as `git worktree list` names, ties nothing."""
    root, made = _dirs(tmp_path)
    before = time.time() - 60
    os.utime(made / ".git", (before, before))
    listed = _call(
        made,
        arguments={"args": "git worktree list"},
        result=f"{made} 1234567 [fix-focus]\n",
    )
    long_call = _call(made, duration_ms=120_000.0)

    await _tie(listed, root)
    await _tie(long_call, root)

    listed.put_room_state.assert_not_awaited()
    listed.query_room_state.assert_not_awaited()
    # A call that started before the worktree was made, such as one that then installed dependencies, made it.
    long_call.put_room_state.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"tool_name": "save_file"},
        {"arguments": {"args": "ls /tmp"}, "result": "x\n"},
        {"arguments": {"args": "git worktree add ../relative -b fix"}},
        {"arguments": {"args": "git worktree add $OTHER/worktrees/chat-focus"}},
        {"arguments": {}, "result": "nothing\n"},
        {"result": None},
        {"error": RuntimeError("boom")},
        {"blocked": True},
        {"room_id": None},
        {"thread_id": None},
        {"config": _config("workspace")},
        {"config": _config("unrestricted", tools=("shell",))},
        {
            "config": _config(
                "unrestricted",
                tools=("shell",),
                default_tools=("thread_worktree",),
                include_default_tools=False,
            )
        },
        {"agent_name": "unknown"},
    ],
)
async def test_other_calls_tie_nothing(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    """Another tool or command, a path that is not absolute, a failed or declined call, a call outside a thread,
    a worktree outside the workspace of a confined agent, and an agent without the toolkit all leave the thread
    as it is."""
    root, made = _dirs(tmp_path)
    ctx = _call(made, **overrides)

    await _tie(ctx, root)

    ctx.put_room_state.assert_not_awaited()
    ctx.logger.warning.assert_not_called()


@pytest.mark.asyncio
async def test_what_is_no_new_worktree_ties_nothing(tmp_path: Path) -> None:
    """A plain new directory, a clone, a path cut at a space, a path that is no directory here (a worker's), and a
    call without a runtime context tie nothing."""
    root, made = _dirs(tmp_path)
    plain = tmp_path / "worktrees" / "plain"
    plain.mkdir()
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    (clone / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    spaced = tmp_path / "worktrees" / "chat-focus more"
    calls = [
        _call(plain, arguments={"args": f"git worktree add {plain}"}),
        _call(clone, arguments={"args": f"git clone x {clone} # then a worktree"}),
        _call(spaced, arguments={"args": f"git worktree add '{spaced}'"}),
        _call(made, arguments={"args": "git worktree add /worker/only/path"}),
    ]
    os.utime(made / ".git", (0, 0))

    for ctx in calls:
        await _tie(ctx, root)
    outside_a_call = _call(made)
    await _tie(outside_a_call, None)

    for ctx in [*calls, outside_a_call]:
        ctx.put_room_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_write_and_failed_read_only_warn(tmp_path: Path) -> None:
    """A write the room refuses and a read that raises are logged and never break the tool call."""
    root, made = _dirs(tmp_path)
    refused = _call(made, put_room_state=AsyncMock(return_value=False))
    broken = _call(made, query_room_state=AsyncMock(side_effect=RuntimeError("x")))

    await _tie(refused, root)
    await _tie(broken, root)

    refused.logger.warning.assert_called_once()
    broken.logger.warning.assert_called_once()
    broken.put_room_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_only_the_last_paths_of_a_long_output_are_looked_at(
    tmp_path: Path,
) -> None:
    """A call that names very many paths is looked at from its end, a bounded number of them."""
    root, made = _dirs(tmp_path)
    noise = "".join(f"/nowhere/{index} worktree\n" for index in range(50))
    at_the_end = _call(made, result=noise + f"{made}\n")
    too_early = _call(
        made, arguments={"args": "git worktree list"}, result=f"{made}\n" + noise
    )

    await _tie(at_the_end, root)
    await _tie(too_early, root)

    at_the_end.put_room_state.assert_awaited_once()
    too_early.put_room_state.assert_not_awaited()
