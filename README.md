# Thread Worktree

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-plugins-blue)](https://docs.mindroom.chat/plugins/)
[![Hooks](https://img.shields.io/badge/docs-hooks-blue)](https://docs.mindroom.chat/hooks/)

<picture>
  <source media="(prefers-reduced-motion: no-preference)" srcset="https://raw.githubusercontent.com/mindroom-ai/mindroom/main/assets/logo/logo-mark-animated.svg" />
  <img src="https://raw.githubusercontent.com/mindroom-ai/mindroom/main/assets/logo/logo-mark.svg" alt="MindRoom Logo" align="right" width="120" />
</picture>

One git worktree per thread for the agents of [MindRoom](https://github.com/mindroom-ai/mindroom).

An agent makes a git worktree inside its workspace and ties the current Matrix thread to it. From the thread's next turn, that agent's turns in the thread work in that directory, so several threads can work on different branches of the same repository without touching each other. The tie is stored as Matrix room state, so a chat client can show it. The pull request client plugin of MindRoom Chat ([`mindroom-ai/pull-request-client-plugin`](https://github.com/mindroom-ai/pull-request-client-plugin)) shows the branch on the thread's card.

This plugin needs a MindRoom with plugin API 1.31 (`"mindroom_plugin_api": "1.31"`), the first that lets a hook choose a turn's working directory.

## Features

- Stores each thread's worktree as a Matrix state event under `com.mindroom.thread.worktree`
- Runs every turn the tying agent takes in a tied thread in its directory
- Exposes thread-scoped tools to tie, untie and show the worktree by hand
- Records the branch, `set_by` and `set_at` with every tie

## How It Works

1. The agent makes a worktree with its shell tool, with absolute paths built from `$MINDROOM_AGENT_WORKSPACE`, which holds the workspace both in MindRoom's own process and in a worker:
   `git -C "$MINDROOM_AGENT_WORKSPACE/<clone>" worktree add "$MINDROOM_AGENT_WORKSPACE/worktrees/<name>" -b <branch>`.
   Git resolves a relative path against the clone, not the workspace, so a relative `worktrees/<name>` would land inside the clone.
2. It calls `use_worktree("worktrees/<name>", "<branch>")`, with the path relative to the workspace. The tool checks that the directory exists inside the workspace and writes the state event.
3. The tie applies from the thread's next turn, not the turn that made it. Until then the agent starts its shell commands with `cd "$MINDROOM_AGENT_WORKSPACE/worktrees/<name>" &&`.
4. From the thread's next turn, the `thread-worktree-started` hook reads the event and asks MindRoom to run the turn there.
5. In that turn `shell` runs in the directory, and `file`, `coding` and `python`'s file functions resolve paths there. Which tools follow it depends on the runtime: where no tool call can run in a worker, every tool follows it; where tool calls can run in a worker (a Docker or Kubernetes worker backend, or a sandbox runner), only the worker-routed tools work in the directory, and every tool that runs in MindRoom's own process keeps the workspace. There, route `shell`, `file` and `coding` to the worker together, so they share the directory. With `file_access: workspace`, `file` and `coding` are confined to the worktree, plus read-only access to the agent's saved tool outputs. `$MINDROOM_AGENT_WORKSPACE` still holds the workspace, and so does `HOME` in a worker; in MindRoom's own process `HOME` is that process's `HOME`.
6. The agent calls `leave_worktree` before it removes the worktree. That clears the event, and the thread's turns run in the workspace again.

Turns in other threads, and turns outside any thread, stay in the workspace.

The tie belongs to the agent that made it: the directory is in that agent's workspace, so only that agent's turns in the thread work there, and other agents in the thread are not affected. A thread has one tie, so `use_worktree` by another agent replaces it (the last one wins), and its answer names the previous `path` and `set_by` under `replaced`. A private agent's path is looked up in the workspace of each turn's requester, since each requester has their own.

What is refused:

- `use_worktree` refuses a path that is not an existing directory inside the agent's workspace, the workspace itself, and a path that leaves the workspace through a link. The answer asks for an existing directory inside the workspace, given relative to the workspace root.
- `use_worktree` checks the directory in the workspace as MindRoom's primary process sees it. A static runner that does not mount the agents' storage makes the worktree in its own copy of the workspace, which the primary cannot see, so `use_worktree` refuses it.
- Writing the tie needs the right to send that state event in the room. When the agent's account lacks it, MindRoom retries through the router account; if that fails too, the tool answers an error.
- MindRoom checks the path again when a turn starts. If the directory is gone, or no longer inside the workspace, it logs a warning and runs the turn in the workspace.
- A turn resumed after a tool approval works where its paused part worked, because MindRoom stores the directory with the approval; a `turn:resumed` hook cannot change it. If that directory is gone or no longer inside the workspace, the resumed turn fails with an error naming the directory, and the approved calls do not run rather than run elsewhere. So do not remove a worktree while one of the thread's approvals is waiting.
- A team's turn ignores the request: a hook cannot give a team a working directory.
- Outside a thread there is nothing to tie, so the tools refuse and the hooks do nothing.

A failure of a hook never reaches the turn: a state read that fails, or takes longer than 2.5 seconds, logs one warning with the room and the thread, and that turn runs in the workspace. An error response from the homeserver reads as no worktree, with nothing logged.

## Agent Tools

The toolkit is named `thread_worktree`.

| Tool | Purpose |
|------|---------|
| `use_worktree(path, branch="")` | Tie the current thread to a directory of the workspace, given relative to the workspace root, with an optional branch name |
| `leave_worktree()` | Remove the tie; the thread's turns run in the workspace again. When another agent made the tie, the answer names it under `cleared_set_by` |
| `get_worktree()` | Show the current thread's directory, branch, `set_by` and `set_at`, if it has any, with `exists` (it is an existing directory inside the caller's workspace) and `applies` (it exists and the caller made the tie) |

All three tools are thread-scoped only. They refuse to operate at room level.

## Hooks

| Hook | Event | Purpose |
|------|-------|---------|
| `thread-worktree-started` | `turn:started` | Run the tying agent's turn in its thread's worktree |

The turn waits for the hooks, so they bound their state read to 2.5 seconds, within their 3 second timeout. The cost is per threaded turn of every agent, before the turn starts: a thread without a tie costs two state reads (the agent's read, then MindRoom's retry through the router account), a tied thread one.

## Storage

- Event type: `com.mindroom.thread.worktree`
- State key: the thread's root event ID
- Payload fields: `path` (relative to the agent's workspace, POSIX separators), `branch` (can be empty), `set_by` (the name of the agent that made the tie), `set_at`
- Leaving writes an empty payload (`{}`) for that state key; a thread without a tie has no event or an empty one

## Install

Vendor this plugin with the MindRoom CLI:

```bash
mindroom plugins install thread-worktree-plugin
```

Then reference it from `config.yaml`:

```yaml
plugins:
  - path: plugins/thread-worktree-plugin
```

Add `thread_worktree` to the tools list of each agent that should work in worktrees, together with `shell` (to make them). The hooks work for every agent once the plugin is loaded. There is nothing else to configure.

Update to the latest commit later with:

```bash
mindroom plugins update thread-worktree-plugin
```

The command pins the exact installed commit in `.mindroom-plugin.lock.json` and strictly validates the plugin before activating it.
