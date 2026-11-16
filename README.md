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

- Records which git worktree a Matrix thread works in, as room state: by itself when the agent makes the worktree with [`agent-cli dev new`](https://github.com/basnijholt/agent-cli), or when the agent ties the thread to a worktree it made another way.
- Reminds that agent on every turn in the thread, in its system prompt, where the worktree is and how to work in it.
- Tells the agent, in the toolkit's instructions, to give a coding task its own worktree before the first change.

The pull request client plugin of MindRoom Chat ([`mindroom-ai/pull-request-client-plugin`](https://github.com/mindroom-ai/pull-request-client-plugin)) shows the branch, and the repository when the tie names one, on the thread's card.

So several threads can work on different branches of the same repository without touching each other.

## What it does not do

It does not change where MindRoom runs tools. `shell` still starts every command in the agent's workspace, and the agent's commands `cd` into the worktree themselves. `file` and `coding` take workspace-relative paths, which name the worktree's files with its prefix, such as `worktrees/<name>/src/app.py`.

That is on purpose: the directory a command runs in is part of the command, so a tool approval shows exactly what will run and where, and nothing outside the command decides it.

The plugin does not make or remove worktrees either; the agent does that with its shell. So git, and whatever a repository's hooks and setup run, stay where the agent's own commands run, which for a sandboxed agent is its worker.

## How it works

### With `agent-cli`

Where [`agent-cli`](https://github.com/basnijholt/agent-cli) is installed in the agent's shell, one command does it:

```bash
cd <clone> && agent-cli dev new <branch>
```

`agent-cli dev new` fetches, branches from the remote's default branch, checks out an existing branch instead when there is one, fills submodules from the clone's own copies, pulls Git LFS files, copies env files and installs dependencies (`--no-setup` skips that). It puts the worktree beside the clone, in `<clone>-worktrees/<branch>`.

The `tie-created-worktree` hook reads that shell call's output (`Created worktree at <path>` and `Creating worktree for branch '<branch>'`) and ties the thread to the worktree, with the repository's name taken from the `<repo>-worktrees` directory. No tool call is needed. When one call makes several worktrees the last one is tied, and a thread has one tie.

The hook ties only a directory the agent may tie (see "What is refused"), and one that MindRoom's primary process can see: a path that exists only inside a worker ties nothing, and the agent calls `use_worktree` as below.

### By hand

1. The agent makes a worktree with its shell, with absolute paths built from `$MINDROOM_AGENT_WORKSPACE`:
   `git -C "$MINDROOM_AGENT_WORKSPACE/<clone>" worktree add "$MINDROOM_AGENT_WORKSPACE/worktrees/<name>" -b <branch>`.
   Git resolves a relative path against the clone, not the workspace, so a relative `worktrees/<name>` would land inside the clone.
2. It calls `use_worktree("worktrees/<name>", "<branch>")`, with the path relative to the workspace. The tool checks that the directory exists inside the workspace and writes the state event. A third argument names the repository.
3. From the thread's next turn, the `thread-worktree-instruction` hook adds this to the agent's system prompt (for `worktrees/<name>` on branch `<branch>`):

   > This thread works in the git worktree `worktrees/<name>` (branch `<branch>`), a directory inside your workspace. Start every shell command for this thread's work with `cd "$MINDROOM_AGENT_WORKSPACE/worktrees/<name>" &&`. For file tools, name files with the prefix `worktrees/<name>/`. If the directory is missing, tell the user and call leave_worktree.

   Without a branch the instruction leaves out the branch clause. It is the same on every turn of the thread, so it is returned as a required item (`minimal_required`) with the `stable` cache policy. Until the next turn the agent starts its shell commands with that `cd` anyway (the tool's answer says so).
4. The agent calls `leave_worktree` before it removes the worktree. That clears the event, and the thread's turns get no instruction any more.

### Outside the workspace

An agent whose [`file_access`](https://docs.mindroom.chat/architecture/security-posture/#file-access) is `unrestricted` may tie a directory outside its workspace, which is where `agent-cli dev new` puts the worktrees of a clone that is not in the workspace. The tie then holds the absolute path, and the instruction names it as it is: ``cd "/srv/<repo>-worktrees/<name>" &&``. Any other agent is confined to its workspace: the tools and the hook refuse such a directory, and a state event that names one, written by hand, gives a confined agent no instruction.

The tie belongs to the agent that made it: the directory is in that agent's workspace, so only that agent is told about it, and other agents in the thread are not. A thread has one tie, so `use_worktree` by another agent replaces it (the last one wins), and its answer names the previous `path` and `set_by` under `replaced`. A private agent's path is checked in the workspace of the turn's requester, since each requester has their own. In a thread that several people write in, the tie is recorded under the private agent's name, so every requester's turn of that agent gets the instruction, pointing into that requester's own workspace; tie worktrees in threads one person works in.

What is refused:

- `use_worktree` refuses a path that is not an existing directory, the workspace itself, for an agent without unrestricted file access a directory outside its workspace (also one a link inside it leads to), and a path with a backtick, a double quote, `$`, a backslash or a newline, which could bend the instruction. A state event with such a path, or one that breaks the length or segment rules, written by hand, reads as no tie; a hand-written path that is valid text is taken as it is, because the hook does not look at the filesystem, which is why the instruction tells the agent what to do when the directory is missing.
- `use_worktree` checks the directory in the workspace as MindRoom's primary process sees it. A static runner that does not mount the agents' storage makes the worktree in its own copy of the workspace, which the primary cannot see, so `use_worktree` refuses it.
- Writing the tie needs the right to send that state event in the room. When the agent's account lacks it, MindRoom retries through the router account; if that fails too, the tool answers an error.
- A team's turn gets no instruction, and outside a thread there is nothing to tie, so the tools refuse and the hook does nothing.

In [minimal mode](https://docs.mindroom.chat/tools/agent-cli/), MindRoom gives system enrichment to the agent through the CLI's enrichment context document, which it may not read; this instruction is marked `minimal_required`, so it stays in the prompt there too.

## Cost

The turn waits for the hook, which bounds its state read to 2.5 seconds within its 3 second timeout. Each turn of an agent in a thread reads the tie: a thread that never had a tie costs two state reads (the agent's read finds nothing, and MindRoom repeats it through the router account), a tied thread, and one whose tie was cleared (`{}`), one. A team's turn and a turn outside a thread read nothing.

A failure never reaches the turn: a read that fails or takes longer than 2.5 seconds logs one warning with the room and the thread, and that turn gets no instruction. An error response from the homeserver reads as no tie, with nothing logged.

## Agent Tools

The toolkit is named `thread_worktree`.

| Tool | Purpose |
|------|---------|
| `use_worktree(path, branch="", repo="")` | Tie the current thread to a directory of the workspace, given relative to the workspace root, with an optional branch and repository name. With unrestricted file access also a directory outside the workspace, by its absolute path |
| `leave_worktree()` | Remove the tie. When another agent made it, the answer names that agent under `cleared_set_by` |
| `get_worktree()` | Show the current thread's directory, branch, repository, `set_by` and `set_at`, if it has any, with `exists` (it is an existing directory the caller may tie) and `applies` (it exists and the caller made the tie) |

All three tools are thread-scoped only. They refuse to operate at room level.

## Hooks

| Hook | Event | Purpose |
|------|-------|---------|
| `thread-worktree-instruction` | `system:enrich` | Tell the tying agent, in its system prompt, where its thread's worktree is |
| `tie-created-worktree` | `tool:after_call` | Tie the thread to the worktree that `agent-cli dev new` made in a shell call |

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

Add `thread_worktree` to the tools list of each agent that should work in worktrees, together with `shell` (to make them and work in them). The hooks work for every agent once the plugin is loaded. There is nothing else to configure. `agent-cli` is optional: install it where the agent's shell runs (`uv tool install agent-cli`) to get the one-command flow.

Update to the latest commit later with:

```bash
mindroom plugins update thread-worktree-plugin
```

The command pins the exact installed commit in `.mindroom-plugin.lock.json` and strictly validates the plugin before activating it.
