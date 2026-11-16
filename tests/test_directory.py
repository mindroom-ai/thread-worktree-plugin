# ruff: noqa: INP001
"""Tests for which directory a tie may name."""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from importlib import util
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

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
        (_config("workspace"), "workspace"),
        (_config("unrestricted"), "unrestricted"),
        (_config("workspace", code=None), "workspace"),
        (_config("unrestricted", code=None), "unrestricted"),
        (_config("workspace", code="unrestricted"), "unrestricted"),
        (_config("unrestricted", code="workspace"), "workspace"),
        (_config("workspace", other="unrestricted"), "workspace"),
    ],
)
def test_file_access_is_the_agents_setting_else_the_default(
    config: SimpleNamespace, expected: str
) -> None:
    """An agent's own `file_access` decides; an agent without one, or a name no agent has, follows the default."""
    assert directory.file_access(config, "code") == expected


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "workspace"
    (root / "worktrees" / "a").mkdir(parents=True)
    outside = tmp_path / "repo-worktrees" / "fix"
    outside.mkdir(parents=True)
    return root, outside


def test_directory_inside_the_workspace_is_named_relative(tmp_path: Path) -> None:
    """Inside the workspace a tie names the directory relative to the root, however the path was given."""
    root, _ = _tree(tmp_path)

    for access in ("workspace", "unrestricted"):
        for path in ("worktrees/a", "./worktrees/a/", str(root / "worktrees" / "a")):
            assert directory.tied_path(root, path, access) == "worktrees/a"


def test_directory_outside_the_workspace_needs_unrestricted_access(
    tmp_path: Path,
) -> None:
    """Outside the workspace a tie names the directory by its absolute path, and only when the agent may leave it."""
    root, outside = _tree(tmp_path)
    (root / "link").symlink_to(outside)

    for path in (str(outside), "link"):
        assert directory.tied_path(root, path, "unrestricted") == outside.as_posix()
        assert directory.tied_path(root, path, "workspace") is None


def test_confined_agents_path_follows_no_link(tmp_path: Path) -> None:
    """For a confined agent no part of the path may be a link, wherever the link leads, and no `..` is followed."""
    root, outside = _tree(tmp_path)
    (root / "inside-link").symlink_to(root / "worktrees")
    (root / "worktrees" / "last-link").symlink_to(root / "worktrees" / "a")
    (tmp_path / "plain").mkdir()
    (tmp_path / "link").symlink_to(outside)
    # A link that leaves the workspace and comes back, through a directory that exists or one that does not.
    (root / "gate").symlink_to(tmp_path / "plain" / ".." / "workspace" / "worktrees")
    (root / "gate-missing").symlink_to(
        tmp_path / "missing" / ".." / "workspace" / "worktrees"
    )

    for path in (
        "inside-link/a",
        "worktrees/last-link",
        "gate/a",
        "gate-missing/a",
        f"{tmp_path}/plain/../workspace/worktrees/a",
        f"{tmp_path}/link/../workspace/worktrees/a",
        f"{tmp_path}/missing/../workspace/worktrees/a",
        "worktrees/../worktrees/a",
        f"{root}/worktrees/../worktrees/a",
    ):
        assert directory.tied_path(root, path, "workspace") is None, path
    # An agent that may read any path gets links resolved.
    assert directory.tied_path(root, "inside-link/a", "unrestricted") == "worktrees/a"
    assert directory.tied_path(root, "gate/a", "unrestricted") == "worktrees/a"


def test_confined_agents_directory_swapped_for_a_link_is_never_followed(
    tmp_path: Path,
) -> None:
    """While a part of the path flips between a directory and a link out of the workspace, as a worker can make
    it, a confined agent's path never ties what the link leads to."""
    root, outside = _tree(tmp_path)
    (outside / "b").mkdir()
    spot = root / "a"
    stop = threading.Event()

    def flip() -> None:
        while not stop.is_set():
            with contextlib.suppress(OSError):
                spot.mkdir()
            with contextlib.suppress(OSError):
                spot.rmdir()
            with contextlib.suppress(OSError):
                spot.symlink_to(outside)
            with contextlib.suppress(OSError):
                spot.unlink()

    flipper = threading.Thread(target=flip)
    flipper.start()
    try:
        tied = {directory.tied_path(root, "a/b", "workspace") for _ in range(3000)}
    finally:
        stop.set()
        flipper.join()

    assert tied == {None}


def test_workspace_root_may_be_below_a_link_but_not_be_one(tmp_path: Path) -> None:
    """A workspace reached through a link above it works, also by an absolute path that starts with the root as it
    is spelled; a root that is itself a link is not followed for a confined agent."""
    root, _ = _tree(tmp_path)
    (tmp_path / "alias").symlink_to(tmp_path)
    below = tmp_path / "alias" / "workspace"
    (tmp_path / "root-link").symlink_to(root)

    for access in ("workspace", "unrestricted"):
        for path in ("worktrees/a", f"{below}/worktrees/a"):
            assert directory.tied_path(below, path, access) == "worktrees/a", path
    # Another spelling of the root is not looked up for a confined agent; resolving finds it for any other.
    assert directory.tied_path(below, f"{root}/worktrees/a", "workspace") is None
    assert directory.tied_path(below, f"{root}/worktrees/a", "unrestricted") == (
        "worktrees/a"
    )
    assert (
        directory.tied_path(tmp_path / "root-link", "worktrees/a", "workspace") is None
    )
    assert (
        directory.tied_path(tmp_path / "root-link", "worktrees/a", "unrestricted")
        == "worktrees/a"
    )


def test_confined_agents_path_is_checked_as_text_before_it_is_walked(
    tmp_path: Path,
) -> None:
    """A path the tie could not hold is refused without opening any directory."""
    root, _ = _tree(tmp_path)
    (root / "a b").mkdir()
    long = "/".join(["d"] * 150)

    with patch.object(
        directory, "open_directory_within_root", side_effect=AssertionError("walked")
    ):
        for path in ("a b", long, f"{root}/a b", "../x", str(tmp_path), ""):
            assert directory.tied_path(root, path, "workspace") is None, path


@pytest.mark.skipif(os.geteuid() == 0, reason="root may open any directory")
def test_confined_agents_directory_must_be_readable(tmp_path: Path) -> None:
    """The walk opens the directory, so one MindRoom may enter but not read is refused for a confined agent."""
    root, _ = _tree(tmp_path)
    closed = root / "worktrees" / "closed"
    closed.mkdir(mode=0o311)
    try:
        assert directory.tied_path(root, "worktrees/closed", "workspace") is None
        assert (
            directory.tied_path(root, "worktrees/closed", "unrestricted")
            == "worktrees/closed"
        )
    finally:
        closed.chmod(0o755)


def test_no_tie_for_what_is_not_a_plain_directory(tmp_path: Path) -> None:
    """A missing path, a file, the workspace itself and a path with a forbidden character are never tied."""
    root, outside = _tree(tmp_path)
    (outside / "file").write_text("x")
    (root / "file").write_text("x")
    (tmp_path / "a b").mkdir()
    (root / "a b").mkdir()
    (root / "loop").symlink_to(root / "loop")

    for access in ("workspace", "unrestricted"):
        for path in (
            "missing",
            "file",
            "loop",
            "a b",
            str(outside / "missing"),
            str(outside / "file"),
            "",
            ".",
            str(root),
            str(tmp_path / "a b"),
            "a\x00b",
        ):
            assert directory.tied_path(root, path, access) is None, path
