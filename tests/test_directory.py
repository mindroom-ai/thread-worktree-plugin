# ruff: noqa: INP001
"""Tests for which directory a tie may name."""

from __future__ import annotations

import sys
from importlib import util
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

PACKAGE_NAME = (
    f"mindroom_plugin_{Path(__file__).resolve().parents[1].name.replace('-', '_')}"
)


def _load_directory() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "directory.py"
    name = f"{PACKAGE_NAME}.directory"
    sys.modules.pop(name, None)
    spec = util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


directory = _load_directory()


def _config(default: str, **agents: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        defaults=SimpleNamespace(file_access=default),
        agents={name: SimpleNamespace(file_access=own) for name, own in agents.items()},
    )


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (_config("workspace"), False),
        (_config("unrestricted"), True),
        (_config("workspace", code=None), False),
        (_config("unrestricted", code=None), True),
        (_config("workspace", code="unrestricted"), True),
        (_config("unrestricted", code="workspace"), False),
        (_config("workspace", other="unrestricted"), False),
    ],
)
def test_unrestricted_is_the_agents_setting_else_the_default(
    config: SimpleNamespace, expected: bool
) -> None:
    """An agent's own `file_access` decides; an agent without one, or a name no agent has, follows the default."""
    assert directory.unrestricted(config, "code") is expected


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "workspace"
    (root / "worktrees" / "a").mkdir(parents=True)
    outside = tmp_path / "repo-worktrees" / "fix"
    outside.mkdir(parents=True)
    return root, outside


def test_directory_inside_the_workspace_is_named_relative(tmp_path: Path) -> None:
    """Inside the workspace a tie names the directory relative to the root, however the path was given."""
    root, _ = _tree(tmp_path)

    for outside in (False, True):
        assert (
            directory.tied_path(root, "worktrees/a", outside=outside) == "worktrees/a"
        )
        assert (
            directory.tied_path(root, str(root / "worktrees" / "a"), outside=outside)
            == "worktrees/a"
        )


def test_directory_outside_the_workspace_needs_unrestricted_access(
    tmp_path: Path,
) -> None:
    """Outside the workspace a tie names the directory by its absolute path, and only when the agent may leave it."""
    root, outside = _tree(tmp_path)
    (root / "link").symlink_to(outside)

    assert directory.tied_path(root, str(outside), outside=True) == outside.as_posix()
    assert directory.tied_path(root, "link", outside=True) == outside.as_posix()
    assert (
        directory.tied_path(root, "../repo-worktrees/fix", outside=True)
        == outside.as_posix()
    )
    assert directory.tied_path(root, str(outside), outside=False) is None
    assert directory.tied_path(root, "link", outside=False) is None
    assert directory.tied_path(root, "../repo-worktrees/fix", outside=False) is None


def test_no_tie_for_what_is_not_a_plain_directory(tmp_path: Path) -> None:
    """A missing path, a file, the workspace itself and a path with a forbidden character are never tied."""
    root, outside = _tree(tmp_path)
    (outside / "file").write_text("x")
    (tmp_path / "a b").mkdir()

    for path in (
        "missing",
        str(outside / "missing"),
        str(outside / "file"),
        ".",
        str(root),
        str(tmp_path / "a b"),
        "a\x00b",
    ):
        assert directory.tied_path(root, path, outside=True) is None


@pytest.mark.parametrize(
    ("tied", "repository"),
    [
        ("/srv/mindroom-worktrees/fix", "mindroom"),
        ("clones/chat-worktrees/fix-1", "chat"),
        ("chat-worktrees/fix", "chat"),
        ("worktrees/fix", ""),
        ("/srv/-worktrees/fix", ""),
        ("/srv/mindroom-worktrees", ""),
        ("fix", ""),
    ],
)
def test_repository_of_a_worktree_beside_its_clone(tied: str, repository: str) -> None:
    """A worktree at `<repo>-worktrees/<name>` belongs to `<repo>`; any other place names no repository."""
    assert directory.repository_of(tied) == repository
