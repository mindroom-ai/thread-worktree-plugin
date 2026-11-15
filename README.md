# Thread Worktree

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-plugins-blue)](https://docs.mindroom.chat/plugins/)
[![Hooks](https://img.shields.io/badge/docs-hooks-blue)](https://docs.mindroom.chat/hooks/)

<picture>
  <source media="(prefers-reduced-motion: no-preference)" srcset="https://raw.githubusercontent.com/mindroom-ai/mindroom/main/assets/logo/logo-mark-animated.svg" />
  <img src="https://raw.githubusercontent.com/mindroom-ai/mindroom/main/assets/logo/logo-mark.svg" alt="MindRoom Logo" align="right" width="120" />
</picture>

One git worktree per thread for the agents of [MindRoom](https://github.com/mindroom-ai/mindroom).

An agent makes a git worktree inside its workspace and ties the current Matrix thread to it. From then on the thread's turns work in that directory, so several threads can work on different branches of the same repository without touching each other. The tie is stored as Matrix room state, so a chat client can show it. The pull request client plugin of MindRoom Chat ([`mindroom-ai/pull-request-client-plugin`](https://github.com/mindroom-ai/pull-request-client-plugin)) shows the branch on the thread's card.

This plugin needs a MindRoom with plugin API 1.31 (`"mindroom_plugin_api": "1.31"`), the first that lets a hook choose a turn's working directory.

## Features

- Stores each thread's worktree as a Matrix state event under `com.mindroom.thread.worktree`
- Runs every turn the tying agent takes in a tied thread in its directory, including a turn resumed after a tool approval
- Exposes thread-scoped tools to tie, untie and show the worktree by hand
- Records the branch, `set_by` and `set_at` with every tie

## How It Works

1. The agent makes a worktree with its shell tool, for example `git -C <clone> worktree add ../worktrees/<name> -b <branch>`, with paths relative to its workspace.
2. It calls `use_worktree` with the directory and the branch. The tool checks that the directory exists inside the workspace and writes the state event.
3. From the thread's next turn, the `thread-worktree-started` hook (and `thread-worktree-resumed` for a turn resumed after an approval) reads the event and asks MindRoom to run the turn there.
4. In that turn `shell` runs in the directory, and `file`, `coding` and `python` resolve paths there. `HOME` and `$MINDROOM_AGENT_WORKSPACE` stay on the workspace.
5. `leave_worktree` clears the event, and the thread's turns run in the workspace again.

A tie applies from the thread's next turn, not the turn that made it.

The tie belongs to the agent that made it: the directory is in that agent's workspace, so only that agent's turns in the thread work there, and other agents in the thread are not affected. A thread has one tie, so `use_worktree` by another agent replaces it (the last one wins), and its answer names the previous `path` and `set_by` under `replaced`.

What is refused:

- `use_worktree` refuses a path that is not an existing directory inside the agent's workspace, the workspace itself, and a path that leaves the workspace through a link.
- MindRoom checks the path again when a turn starts. If the directory is gone, or no longer inside the workspace, it logs a warning and runs the turn in the workspace.
- A team's turn ignores the request: a hook cannot give a team a working directory.
- Outside a thread there is nothing to tie, so the tools refuse and the hooks do nothing.

A failure of a hook never reaches the turn: a state read that fails, or takes longer than 2.5 seconds, logs one warning with the room and the thread, and that turn runs in the workspace. An error response from the homeserver reads as no worktree, with nothing logged.

## Agent Tools

The toolkit is named `thread_worktree`.

| Tool | Purpose |
|------|---------|
| `use_worktree(path, branch="")` | Tie the current thread to a directory of the workspace, with an optional branch name |
| `leave_worktree()` | Remove the tie; the thread's turns run in the workspace again |
| `get_worktree()` | Show the current thread's directory, branch, `set_by` and `set_at`, if it has any |

All three tools are thread-scoped only. They refuse to operate at room level.

## Hooks

| Hook | Event | Purpose |
|------|-------|---------|
| `thread-worktree-started` | `turn:started` | Run the turn in its thread's worktree |
| `thread-worktree-resumed` | `turn:resumed` | Run a turn resumed after a tool approval in its thread's worktree |

The turn waits for the hooks, so they bound their state read to 2.5 seconds, within their 3 second timeout. The cost is one state read per threaded turn of every agent, before the turn starts; MindRoom repeats a read that finds nothing through the router account.

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
