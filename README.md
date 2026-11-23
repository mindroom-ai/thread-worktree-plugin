# Thread Worktree

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![CI](https://github.com/mindroom-ai/thread-worktree-plugin/actions/workflows/plugin-ci.yml/badge.svg)](https://github.com/mindroom-ai/thread-worktree-plugin/actions/workflows/plugin-ci.yml)
[![Docs](https://img.shields.io/badge/docs-plugins-blue)](https://docs.mindroom.chat/plugins/)
[![Tools](https://img.shields.io/badge/docs-tools-blue)](https://docs.mindroom.chat/tools/)
[![Hooks](https://img.shields.io/badge/docs-hooks-blue)](https://docs.mindroom.chat/hooks/)

<picture>
  <source media="(prefers-reduced-motion: no-preference)" srcset="https://raw.githubusercontent.com/mindroom-ai/mindroom/main/assets/logo/logo-mark-animated.svg" />
  <img src="https://raw.githubusercontent.com/mindroom-ai/mindroom/main/assets/logo/logo-mark.svg" alt="MindRoom Logo" align="right" width="120" />
</picture>

A [MindRoom](https://github.com/mindroom-ai/mindroom) tool and hook plugin that binds each Matrix thread to its own git worktree so parallel threads can work on separate branches without colliding.

Once an agent ties a thread with `use_worktree`, every subsequent turn injects a stable reminder into that agent's system prompt:

> This thread works in the git worktree `worktrees/<name>` (branch `<branch>`), a directory inside your workspace. Start every shell command for this thread's work with `cd "$MINDROOM_AGENT_WORKSPACE/worktrees/<name>" &&`. For file tools, name files with the prefix `worktrees/<name>/`. If the directory is missing, tell the user and call leave_worktree.

- **Per-thread isolation:** records the thread's worktree directory, optional branch, and optional repository name in Matrix room state.
- **Always in context:** injects the reminder with `cache_policy="stable"` and `minimal_required=True`, so it stays active even in [minimal mode](https://docs.mindroom.chat/tools/agent-cli/).
- **Branch chips in MindRoom Chat:** MindRoom Chat's bundled [`pull-request-client-plugin`](https://github.com/mindroom-ai/pull-request-client-plugin) shows the active branch chip on thread cards (prefixed with the repository name in rooms spanning multiple repos).
- **Agent-guided workflow:** toolkit instructions prompt the agent to create a worktree before making its first edit and call `leave_worktree` before removing it.

## Quick start

```bash
mindroom plugins install thread-worktree-plugin
```

Enable the plugin and attach both `thread_worktree` and `shell` to each coding agent in `config.yaml`:

```yaml
plugins:
  - path: plugins/thread-worktree-plugin

agents:
  coder:
    tools:
      - thread_worktree
      - shell
```

Update later with `mindroom plugins update thread-worktree-plugin`.
Requires MindRoom plugin API 1.30 or later. No plugin settings are required; the hook runs automatically once loaded.

## What it does and does not do

**What it does:**

1. The agent creates a worktree via `shell` using absolute paths from `$MINDROOM_AGENT_WORKSPACE` (git resolves relative paths against the clone, not the workspace):
   `git -C "$MINDROOM_AGENT_WORKSPACE/<clone>" worktree add "$MINDROOM_AGENT_WORKSPACE/worktrees/<name>" -b <branch>`
2. The agent calls `use_worktree("worktrees/<name>", "<branch>", "<repo>")`. The plugin verifies the directory exists and writes room state; `branch` and `repo` are optional labels (spell one repo consistently across threads so UI clients group them cleanly).
3. From the next turn onward, `thread-worktree-instruction` adds the system-prompt reminder above (omitting the branch clause if empty, or naming the absolute path for unrestricted outside-workspace ties).
4. Before deleting the worktree, the agent calls `leave_worktree()` to clear the tie.

**What it does not do:**

- **Does not change tool working directories:** `shell` still starts in the agent's workspace so every command's `cd` stays explicit in tool approvals, and `file`/`coding` tools keep using workspace-relative paths such as `worktrees/<name>/src/app.py`.
- **Does not run `git` or manage worktree files:** the agent creates and removes worktrees in its own shell (inside its worker if sandboxed), keeping git hooks and repo setup in the agent's execution environment.
- **Does not apply outside threads or on team turns:** all three tools refuse room-level calls, and the hook ignores room-level messages and team turns.

## Tools

Toolkit name: `thread_worktree` (all three tools are thread-scoped only).

| Tool | Purpose |
|------|---------|
| `use_worktree(path, branch="", repo="")` | Tie the current thread to an existing workspace-relative directory (or an absolute path when `file_access: unrestricted`), with optional `branch` and `repo` labels. Replaces any existing tie (`replaced` names the previous owner). |
| `leave_worktree()` | Clear the thread's worktree tie. Reports `cleared_set_by` if another agent owned it. |
| `get_worktree()` | Show the thread's `path`, `branch`, `repo`, `set_by`, and `set_at`, plus `exists` (directory exists and caller may tie it) and `applies` (`exists` and caller made the tie). |

## Hooks

| Hook | Event | Purpose |
|------|-------|---------|
| `thread-worktree-instruction` | `system:enrich` | Add the worktree reminder (`minimal_required`, `stable` cache policy) to the tying agent's system prompt on each thread turn. |

<details>
<summary><b>Security, path validation, and outside-workspace access</b></summary>

- **Allowed characters and segments:** `path` allows only `A-Z a-z 0-9 . _ - /` (no spaces or shell metacharacters), at most 200 characters, with no empty, `.`, or `..` segments (one leading `/` marks an absolute path). `branch` and `repo` allow only `A-Z a-z 0-9 . _ - /` (at most 100 characters). For relative paths, `use_worktree` strips a leading `./` and trailing `/` before validation; `branch` and `repo` are trimmed.
- **Workspace-confined agents (default):** paths are verified from `$MINDROOM_AGENT_WORKSPACE` part-by-part via `open_directory_within_root` without following symlinks. The workspace root itself, non-existent directories, directories outside the workspace, symlinks (even inside the workspace), and directories MindRoom cannot read are refused. Hand-written state events pointing outside the workspace produce no instruction for confined agents.
- **Unrestricted agents (`file_access: unrestricted`):** may tie an existing directory outside the workspace by absolute path (resolved on MindRoom's primary host, so intended for agents whose shell also runs there rather than in an isolated worker). See [file access posture](https://docs.mindroom.chat/architecture/security-posture/#file-access).
- **Primary-process visibility:** `use_worktree` checks directories as MindRoom's primary process sees them; a static runner without shared agent storage cannot have its local worktrees verified.
- **Ownership and private agents:** only the tying agent (`set_by`) gets the system-prompt reminder; another agent calling `use_worktree` overwrites the tie (last writer wins). For private agents, `use_worktree` validates against the requester's workspace while storing the tie under the agent's name—tie worktrees in single-user threads.
- **Hand-written state and permissions:** the hook validates syntax only (without touching the filesystem), ignoring malformed events; if a syntactically valid directory is missing, the instruction tells the agent to inform the user and call `leave_worktree`. Writing ties requires room state-event permissions (MindRoom retries through the router account if the agent lacks them).

</details>

<details>
<summary><b>Latency and failure handling</b></summary>

- **Per-turn state reads:** `thread-worktree-instruction` bounds its room-state query to `2.5s` inside its `3s` timeout (`timeout_ms=3000`). A thread that has never been tied incurs two reads (agent account, then router fallback); a tied or cleared (`{}`) thread incurs one. Room-level turns and team turns perform zero reads.
- **Fail-open behavior:** if a state read fails or exceeds `2.5s`, the hook logs one warning with `room_id` and `thread_id` and skips the instruction without breaking the turn. Homeserver error responses read as no tie without logging.

</details>

<details>
<summary><b>Matrix room-state storage</b></summary>

- **Event type:** `com.mindroom.thread.worktree`
- **State key:** the thread's root event ID
- **Payload fields:** `path` (POSIX separators; relative to workspace or absolute), `branch` (may be empty), `repo` (repository display name; may be empty), `set_by` (tying agent name), `set_at` (UTC ISO-8601 timestamp)
- **Clearing:** `leave_worktree()` writes an empty object (`{}`) under that state key; absent, empty, or invalid payloads read as no tie.

</details>
