# Thread Worktree

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-plugins-blue)](https://docs.mindroom.chat/plugins/)
[![Hooks](https://img.shields.io/badge/docs-hooks-blue)](https://docs.mindroom.chat/hooks/)

<picture>
  <source media="(prefers-reduced-motion: no-preference)" srcset="https://raw.githubusercontent.com/mindroom-ai/mindroom/main/assets/logo/logo-mark-animated.svg" />
  <img src="https://raw.githubusercontent.com/mindroom-ai/mindroom/main/assets/logo/logo-mark.svg" alt="MindRoom Logo" align="right" width="120" />
</picture>

One git worktree per thread for the agents of [MindRoom](https://github.com/mindroom-ai/mindroom).

## What it does

- Records which git worktree a Matrix thread works in, as room state, when the agent that made the worktree ties the thread to it.
- Reminds that agent on every turn in the thread, in its system prompt, where the worktree is and how to work in it.
- Lets MindRoom Chat show the branch on the thread's card through the pull request client plugin ([`mindroom-ai/pull-request-client-plugin`](https://github.com/mindroom-ai/pull-request-client-plugin)).

So several threads can work on different branches of the same repository without touching each other.

## What it does not do

It does not change where MindRoom runs tools. `shell` still starts every command in the agent's workspace, and the agent's commands `cd` into the worktree themselves. `file` and `coding` take workspace-relative paths, which name the worktree's files with its prefix, such as `worktrees/<name>/src/app.py`.

That is on purpose: the directory a command runs in is part of the command, so a tool approval shows exactly what will run and where, and nothing outside the command decides it.

The plugin does not make or remove worktrees either; the agent does that with its shell.

## How it works

1. The agent makes a worktree with its shell, with absolute paths built from `$MINDROOM_AGENT_WORKSPACE`:
   `git -C "$MINDROOM_AGENT_WORKSPACE/<clone>" worktree add "$MINDROOM_AGENT_WORKSPACE/worktrees/<name>" -b <branch>`.
   Git resolves a relative path against the clone, not the workspace, so a relative `worktrees/<name>` would land inside the clone.
2. It calls `use_worktree("worktrees/<name>", "<branch>")`, with the path relative to the workspace. The tool checks that the directory exists inside the workspace and writes the state event.
3. From the thread's next turn, the `thread-worktree-instruction` hook adds this to the agent's system prompt (for `worktrees/<name>` on branch `<branch>`):

   > This thread works in the git worktree `worktrees/<name>` (branch `<branch>`), a directory inside your workspace. Start every shell command for this thread's work with `cd "$MINDROOM_AGENT_WORKSPACE/worktrees/<name>" &&`. For file tools, name files with the prefix `worktrees/<name>/`. If the directory is missing, tell the user and call leave_worktree.

   Without a branch the instruction leaves out the branch clause. It is the same on every turn of the thread, so it is added with the `stable` cache policy. Until the next turn, and always, the agent starts its shell commands with that `cd` (the tool's answer says so).
4. The agent calls `leave_worktree` before it removes the worktree. That clears the event, and the thread's turns get no instruction any more.

The tie belongs to the agent that made it: the directory is in that agent's workspace, so only that agent is told about it, and other agents in the thread are not. A thread has one tie, so `use_worktree` by another agent replaces it (the last one wins), and its answer names the previous `path` and `set_by` under `replaced`. A private agent's path is checked in the workspace of the turn's requester, since each requester has their own.

What is refused:

- `use_worktree` refuses a path that is not an existing directory inside the agent's workspace, the workspace itself, a path that leaves the workspace through a link, and a path with a backtick, a double quote, `$`, a backslash or a newline, which could bend the instruction. A state event with such a path, written by hand, reads as no tie.
- `use_worktree` checks the directory in the workspace as MindRoom's primary process sees it. A static runner that does not mount the agents' storage makes the worktree in its own copy of the workspace, which the primary cannot see, so `use_worktree` refuses it.
- Writing the tie needs the right to send that state event in the room. When the agent's account lacks it, MindRoom retries through the router account; if that fails too, the tool answers an error.
- The hook does not check that the directory still exists; the instruction tells the agent what to do when it is gone.
- A team's turn gets no instruction, and outside a thread there is nothing to tie, so the tools refuse and the hook does nothing.

In [minimal mode](https://docs.mindroom.chat/tools/agent-cli/), MindRoom gives system enrichment, this instruction included, to the agent through the CLI's enrichment context document instead of the prompt.

## Cost

The turn waits for the hook, which bounds its state read to 2.5 seconds within its 3 second timeout. Each turn of an agent in a thread reads the tie: a thread without a tie costs two state reads (the agent's read, then MindRoom's retry through the router account), a tied one one. A team's turn and a turn outside a thread read nothing.

A failure never reaches the turn: a read that fails or takes longer than 2.5 seconds logs one warning with the room and the thread, and that turn gets no instruction. An error response from the homeserver reads as no tie, with nothing logged.

## Agent Tools

The toolkit is named `thread_worktree`.

| Tool | Purpose |
|------|---------|
| `use_worktree(path, branch="")` | Tie the current thread to a directory of the workspace, given relative to the workspace root, with an optional branch name |
| `leave_worktree()` | Remove the tie. When another agent made it, the answer names that agent under `cleared_set_by` |
| `get_worktree()` | Show the current thread's directory, branch, `set_by` and `set_at`, if it has any, with `exists` (it is an existing directory inside the caller's workspace) and `applies` (it exists and the caller made the tie) |

All three tools are thread-scoped only. They refuse to operate at room level.

## Hooks

| Hook | Event | Purpose |
|------|-------|---------|
| `thread-worktree-instruction` | `system:enrich` | Tell the tying agent, in its system prompt, where its thread's worktree is |

## Storage

- Event type: `com.mindroom.thread.worktree`
- State key: the thread's root event ID
- Payload fields: `path` (relative to the agent's workspace, POSIX separators), `branch` (can be empty), `set_by` (the name of the agent that made the tie), `set_at`
- `path` holds only `A-Z a-z 0-9 . _ - /` (at most 200 characters, no empty, `.` or `..` segment) and `branch` only `A-Z a-z 0-9 . _ - /` (at most 100 characters); anything else reads as no worktree, or as no branch, because both go into the system prompt
- Leaving writes an empty payload (`{}`) for that state key; a thread without a tie has no event or an empty one

## Install

The plugin needs no special MindRoom version beyond its manifest's (`"mindroom_plugin_api": "1.30"`).

Vendor it with the MindRoom CLI:

```bash
mindroom plugins install thread-worktree-plugin
```

Then reference it from `config.yaml`:

```yaml
plugins:
  - path: plugins/thread-worktree-plugin
```

Add `thread_worktree` to the tools list of each agent that should work in worktrees, together with `shell` (to make them and work in them). The hook works for every agent once the plugin is loaded. There is nothing else to configure.

Update to the latest commit later with:

```bash
mindroom plugins update thread-worktree-plugin
```

The command pins the exact installed commit in `.mindroom-plugin.lock.json` and strictly validates the plugin before activating it.
