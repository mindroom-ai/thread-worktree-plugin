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

- Records which git worktree a Matrix thread works in, as room state: by itself when a shell call of the agent makes a worktree in a thread that has none, or when the agent ties the thread with a tool.
- Reminds that agent on every turn in the thread, in its system prompt, where the worktree is and how to work in it.
- Tells the agent, in the toolkit's instructions, to give a coding task its own worktree before the first change.

The pull request client plugin of MindRoom Chat ([`mindroom-ai/pull-request-client-plugin`](https://github.com/mindroom-ai/pull-request-client-plugin)) shows the branch, and the repository when the tie names one, on the thread's card.

So several threads can work on different branches of the same repository without touching each other.

## What it does not do

It does not change where MindRoom runs tools. `shell` still starts every command in the agent's workspace, and the agent's commands `cd` into the worktree themselves. `file` and `coding` take workspace-relative paths, which name the worktree's files with its prefix, such as `worktrees/<name>/src/app.py`.

That is on purpose: the directory a command runs in is part of the command, so a tool approval shows exactly what will run and where, and nothing outside the command decides it.

The plugin does not make or remove worktrees either; the agent does that with its shell. So git, and whatever a repository's hooks and setup run, stay where the agent's own commands run, which for a sandboxed agent is its worker.

## How it works

### A new worktree ties itself

The agent makes the worktree with its shell, the way its own instructions say: `git worktree add`, [`agent-cli dev new`](https://github.com/basnijholt/agent-cli) (which also fills submodules from the clone and installs dependencies), or a script of its own.

After every shell call that says "worktree", in its command or in what it printed, the `tie-made-worktree` hook looks at the absolute paths the call names, from the last one back (at most 20). A path may start with `~`, `$HOME` or `$MINDROOM_AGENT_WORKSPACE`. It ties the thread to the first one that is all of this:

- a directory the agent may tie (see "What is refused");
- a linked git worktree: its `.git` is a file that names a git directory ending in `worktrees/<name>`, whose `HEAD` the agent may read;
- made by this call: that `.git` file was written after the call started;
- the thread has no tie yet (the hook never replaces a tie; `use_worktree` does);
- and the agent's tools name the `thread_worktree` toolkit, its own or the default tools it includes, so that it can look at the tie and leave it.

So the text of a command and its output only says where to look. What is tied is a worktree that exists on disk and is new. The branch comes from the worktree's `HEAD`, and the repository's name from its git directory: `<repo>/.git/worktrees/<name>` for a clone, `.git/modules/<repo>/worktrees/<name>` for a submodule. The plugin reads those two small files as MindRoom reads a file for the agent's own tools (inside the workspace unless the agent's file access is unrestricted) and never runs git.

Not tied by the hook: a worktree named by a relative path or below another variable, one that existed before the call, and one that MindRoom's primary process cannot see, such as a path that exists only inside a worker. The agent then calls `use_worktree`.

### By hand

1. The agent makes a worktree with its shell, with absolute paths built from `$MINDROOM_AGENT_WORKSPACE`:
   `git -C "$MINDROOM_AGENT_WORKSPACE/<clone>" worktree add "$MINDROOM_AGENT_WORKSPACE/worktrees/<name>" -b <branch>`.
   Git resolves a relative path against the clone, not the workspace, so a relative `worktrees/<name>` would land inside the clone.
2. It calls `use_worktree("worktrees/<name>", "<branch>")`, with the path relative to the workspace. The tool checks that the directory exists inside the workspace and writes the state event. Without a branch it stores the one the worktree's `HEAD` names, and it stores the repository's name as the hook does. When the hook tied the thread already, the call changes nothing.
3. From the thread's next turn, the `thread-worktree-instruction` hook adds this to the agent's system prompt (for `worktrees/<name>` on branch `<branch>`):

   > This thread works in the git worktree `worktrees/<name>` (branch `<branch>`), a directory inside your workspace. Start every shell command for this thread's work with `cd "$MINDROOM_AGENT_WORKSPACE/worktrees/<name>" &&`. For file tools, name files with the prefix `worktrees/<name>/`. If the directory is missing, tell the user and call leave_worktree.

   Without a branch the instruction leaves out the branch clause. It is the same on every turn of the thread, so it is returned as a required item (`minimal_required`) with the `stable` cache policy. Until the next turn the agent starts its shell commands with that `cd` anyway (the tool's answer says so).
4. The agent calls `leave_worktree` before it removes the worktree. That clears the event, and the thread's turns get no instruction any more.

### Outside the workspace

An agent whose [`file_access`](https://docs.mindroom.chat/architecture/security-posture/#file-access) is `unrestricted` may tie a directory outside its workspace, which is where the worktrees of a clone outside the workspace usually are. The tie then holds the absolute path, and the instruction names it as it is: ``cd "/srv/worktrees/<name>" &&``. The path is checked where MindRoom's primary process runs, so this is for an agent whose shell runs there too, not in a worker. Any other agent is confined to its workspace: the tools and the hook refuse such a directory, read no git file outside the workspace, and a state event that names such a directory, written by hand, gives a confined agent no instruction.

The tie belongs to the agent that made it: the directory is in that agent's workspace, or where its file access reaches, so only that agent is told about it, and other agents in the thread are not. A thread has one tie, so `use_worktree` by another agent replaces it (the last one wins), and its answer names the previous `path` and `set_by` under `replaced`. A private agent's path is checked in the workspace of the turn's requester, since each requester has their own. In a thread that several people write in, the tie is recorded under the private agent's name, so every requester's turn of that agent gets the instruction, pointing into that requester's own workspace; tie worktrees in threads one person works in.

What is refused:

- `use_worktree` refuses a path that is not an existing directory, the workspace itself, for an agent without unrestricted file access a directory outside its workspace (also one a link inside it leads to), and a path with a backtick, a double quote, `$`, a backslash or a newline, which could bend the instruction. A state event with such a path, or one that breaks the length or segment rules, written by hand, reads as no tie; a hand-written path that is valid text is taken as it is, because the hook does not look at the filesystem, which is why the instruction tells the agent what to do when the directory is missing.
- `use_worktree` checks the directory in the workspace as MindRoom's primary process sees it. A static runner that does not mount the agents' storage makes the worktree in its own copy of the workspace, which the primary cannot see, so `use_worktree` refuses it.
- Writing the tie needs the right to send that state event in the room. When the agent's account lacks it, MindRoom retries through the router account; if that fails too, the tool answers an error.
- A team's turn gets no instruction, and outside a thread there is nothing to tie, so the tools refuse and the hook does nothing.

In [minimal mode](https://docs.mindroom.chat/tools/agent-cli/), MindRoom gives system enrichment to the agent through the CLI's enrichment context document, which it may not read; this instruction is marked `minimal_required`, so it stays in the prompt there too.

## Cost

The turn waits for the hook, which bounds its state read to 2.5 seconds within its 3 second timeout. Each turn of an agent in a thread reads the tie: a thread that never had a tie costs two state reads (the agent's read finds nothing, and MindRoom repeats it through the router account), a tied thread, and one whose tie was cleared (`{}`), one. A team's turn and a turn outside a thread read nothing.

A failure never reaches the turn: a read that fails or takes longer than 2.5 seconds logs one warning with the room and the thread, and that turn gets no instruction. An error response from the homeserver reads as no tie, with nothing logged.

The `tie-made-worktree` hook runs after every tool call and returns at once for anything but a shell call that says "worktree". For such a call it checks at most 20 paths on disk, and reads the room's state only when one of them is a new worktree. Its failures, and a room that does not answer within 4 seconds, are logged as one warning too. As with any `tool:after_call` hook, MindRoom copies each tool call's arguments and result for it.

## Agent Tools

The toolkit is named `thread_worktree`.

| Tool | Purpose |
|------|---------|
| `use_worktree(path, branch="")` | Tie the current thread to a directory of the workspace, given relative to the workspace root, replacing an earlier tie. The branch is optional and read from the worktree without it. With unrestricted file access also a directory outside the workspace, by its absolute path |
| `leave_worktree()` | Remove the tie. When another agent made it, the answer names that agent under `cleared_set_by` |
| `get_worktree()` | Show the current thread's directory, branch, repository, `set_by` and `set_at`, if it has any, with `exists` (it is an existing directory the caller may tie) and `applies` (it exists and the caller made the tie) |

All three tools are thread-scoped only. They refuse to operate at room level.

## Hooks

| Hook | Event | Purpose |
|------|-------|---------|
| `thread-worktree-instruction` | `system:enrich` | Tell the tying agent, in its system prompt, where its thread's worktree is |
| `tie-made-worktree` | `tool:after_call` | Tie a thread without a worktree to the git worktree a shell call in it made |

## Storage

- Event type: `com.mindroom.thread.worktree`
- State key: the thread's root event ID
- Payload fields: `path` (relative to the agent's workspace, or absolute for a directory outside it; POSIX separators), `branch` (can be empty), `repo` (the repository's name for display, can be empty), `set_by` (the name of the agent that made the tie), `set_at`
- `path` holds only `A-Z a-z 0-9 . _ - /` (at most 200 characters, no empty, `.` or `..` segment; one leading `/` makes it absolute), and `branch` and `repo` only `A-Z a-z 0-9 . _ - /` (at most 100 characters); anything else reads as no worktree, or as no branch or repository, because the path and the branch go into the system prompt
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

Add `thread_worktree` to the tools list of each agent that should work in worktrees, together with `shell` (to make them and work in them). The instruction hook works for every agent once the plugin is loaded, and the hook that ties only for agents with the toolkit. There is nothing else to configure.

Update to the latest commit later with:

```bash
mindroom plugins update thread-worktree-plugin
```

The command pins the exact installed commit in `.mindroom-plugin.lock.json` and strictly validates the plugin before activating it.
