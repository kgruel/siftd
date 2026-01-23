---
description: Initialize Subtask for this repository
---

# Setup Subtask

Initialize Subtask for the current repository.

## Check available harnesses
Check if `git` is installed and if we're inside a Git repository. If not, let user know that Subtask requires a Git repository and stop.

## Check available harnesses

```bash
codex --version
claude --version
```

**Important:** The "worker harness" is the AI that will execute tasks in parallel workspaces - NOT you (Claude Code). You are the lead; the harness is your worker.

## Ask the user which harness to use

| Harness | Command | Notes |
|---------|---------|-------|
| **Codex CLI** | `codex` | Recommended - more reliable at autonomous multi-step tasks |
| **Claude Code CLI** | `claude` | Good alternative if Codex isn't installed |

If only Claude Code is available, use it. If both are available, ask user and recommend Codex.

## Initialize

```bash
subtask init --harness <codex|claude> -n 20
```

This creates `.subtask/config.json`. Workspaces are created on demand at `~/.subtask/workspaces/`.

## Done

Tell the user:

> Subtask is ready!
>
> Example usage:
> - "fix the login bug with Subtask"
> - "run these 3 features in parallel"
> - "plan and implement the new API endpoint with Subtask"
>
> I'll draft tasks, dispatch workers in isolated workspaces and let you know when they're done.
