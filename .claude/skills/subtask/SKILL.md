---
name: subtask
description: "Parallel task orchestration CLI that dispatches work to AI workers (via Claude Code) in isolated git workspaces. Use when the user wants to draft, create, run, or manage tasks, delegate tasks to workers/subagents, or mentions subtask or Subtask."
---

# Subtask

Subtask is a CLI for orchestrating parallel AI workers. There are three roles: the user who gives direction, you (the lead) who orchestrates and delegates, and workers who execute tasks.

Each worker runs in an isolated git worktree. They can't conflict with each other.

The user tells you what they need. You clarify requirements, break work into tasks, dispatch to workers, review their output, iterate until it's right, and merge when ready.

Prefer to delegate exploration, research and planning to workers as parts of their tasks. Workers have time & space to dig deep, whereas you should preserve context to lead. Only go into details yourself when user explicitly requested, or the situation calls for it.

## Mindset

1. **Understand before delegating** — ask questions, clarify requirements. Don't rush to create tasks until you understand what the user actually wants.
2. **Own the complexity** — stay on top of all tasks. Surface progress and blockers. Don't make the user chase status.
3. **Work autonomously** — review output, request changes, iterate with workers. Only involve the user for decisions they need to make.
4. **Ask before merging** — get user sign-off before merging. Don't merge without user approval.
5. **Pace parallelism to your bandwidth** — worker time runs in parallel; your review is serial. If you have N tasks already awaiting your review, drafting an (N+1)th costs more than it gains. A practical rule: at most 2–3 tasks in flight, and architect-typed tasks (long-running, low-touch) count less than mechanical ones.

## Narration discipline

The user can read your tool calls; they can't read every worker reply, every stage transition, every commit landing. Adapt your output to what actually warrants their attention.

**Surface to the user:**
- Decisions only they can make (merge/close, design tradeoffs, scope changes)
- Errors that need their judgment to resolve
- Batch milestones (all tasks reviewed; queue empty; merged set ready)

**Stay silent on:**
- Stage transitions, commit confirmations, snapshot regenerations, "your branch is uncommitted" round-trips
- Worker progress between dispatch and reply
- Routine review findings you can address yourself

When in doubt: would the user redirect this back to you? If yes, handle it silently.

## Commands

| Command | Description |
|---------|-------------|
| `subtask ask "..."` | Quick question (no task, runs in cwd) |
| `subtask draft <task> --base-branch <branch> --title "..." <<'EOF'` | Create a task |
| `subtask send <task> <prompt>` | Prompt worker on task (blocks until reply) |
| `subtask reply <task>` | Print the most recent worker reply |
| `subtask stage <task> <stage>` | Advance workflow stage |
| `subtask list` | View all tasks |
| `subtask show <task>` | View task details |
| `subtask diff [--stat] <task>` | Show changes (from merge base) |
| `subtask merge <task> -m "msg"` | Squash-merge task into base branch |
| `subtask close <task>` | Close without merging, free workspace |
| `subtask workspace <task>` | Get workspace path (a git worktree) |
| `subtask review --task <task>` | AI code review of a task's changes |
| `subtask interrupt <task>` | Gracefully stop a running worker |
| `subtask log <task>` | Show task conversation and history |
| `subtask trace <task>` | Debug what a worker is doing and thinking internally |
| `subtask presets` | List available presets from project config |
| `subtask types` | List available task types from project config |

**Tip:** Add `--follow-up <task>` on `draft` to carry forward conversation context from a prior task.

## Overrides

Override the adapter, provider, model, or reasoning effort per-task or per-prompt.

**On `draft`** — persists to the task (every `send` inherits):
```bash
subtask draft fix/bug --base-branch main --title "Fix panic" \
  --adapter claude --model claude-sonnet-4-20250514 --reasoning high <<'EOF'
...
EOF
```

**On `send` / `ask`** — ephemeral, applies to this prompt only:
```bash
subtask send fix/bug --model claude-sonnet-4-20250514 "Review the edge cases"
subtask ask --model claude-sonnet-4-20250514 "Explain the pool logic"
```

| Flag | `draft` | `send` | `review` | `ask` |
|------|---------|--------|----------|-------|
| `--adapter` | persists | per-prompt | per-review | per-prompt |
| `--provider` | persists | per-prompt | — | per-prompt |
| `--model` | persists | per-prompt | per-review | per-prompt |
| `--reasoning` | persists | per-prompt | per-review | per-prompt |
| `--preset` | persists | per-prompt | per-review | per-prompt |

Resolution order: explicit flag → `--preset` overlay → task snapshot (when `--task`/`--follow-up` resolves to a task) → project config → global config.

**Snapshot semantics:** When you `draft` a task, the resolved adapter, model, and reasoning are written into TASK.md ("snapshot"). Editing `.subtask/config.json` later does **not** update existing tasks. To run an existing task with a different preset without editing TASK.md: pass `--preset <name>` to `send`, `review`, or `ask --follow-up <task>`. To swap the preset automatically on a stage transition, bind `preset:` in the workflow YAML.

## Review

AI code review without creating a task. Four modes (mutually exclusive):

```bash
subtask review --task fix/bug                    # Review a task's changes against its base branch
subtask review --base main                       # Review current branch against main (PR-style)
subtask review --uncommitted                     # Review staged + unstaged + untracked changes
subtask review --commit abc123                   # Review a specific commit
```

Add `--plan` to `--task` to review PLAN.md against the task spec (TASK.md description) instead of the diff. Useful before approving a plan-stage handoff in `they-plan`/`you-plan` workflows — catches drift between what the spec asked for and what the plan proposes.

```bash
subtask review --task fix/bug --plan             # Review the plan against the spec
```

Add instructions as a positional arg: `subtask review --task fix/bug "Focus on error handling"`

`review` accepts `--adapter`/`--model`/`--reasoning`/`--preset` overrides (ephemeral, do not persist). When `--task` is used, the task's stored adapter is the default; pass `--preset` to override it for this review only.

## Flow

```bash
# 1. Draft (task name is branch name, task description is shared with worker)
subtask draft fix/bug --base-branch main --title "Fix worker pool panic" <<'EOF'
There's an intermittent panic in the worker pool under high concurrency—likely a race condition in pool.go.
Reproduce, find root cause, fix, and add tests.
EOF

# 2. Start the worker (blocks until worker replies; run in background)
subtask send fix/bug "Go ahead."

# 3. When notified, read the reply and review
subtask reply fix/bug
subtask stage fix/bug review
# Review with `subtask diff --stat fix/bug`, or read the files at `cd $(subtask workspace fix/bug)`.

# 4. Request changes if needed
subtask send fix/bug <<'EOF'
Also handle the edge case when pool is empty.
EOF

# 5. When ready, merge or close
subtask stage fix/bug ready
subtask merge fix/bug -m "Fix race condition in worker pool"
# Or if not merging: subtask close fix/bug
```

**Running `subtask send`:**

`subtask send` is **synchronous** — the bash process blocks until the worker has replied (or errored), then exits. Run it with `run_in_background: true` so you can keep talking to the user while you wait. Don't poll or check; you'll be notified when the bash exits.

**When notified that send completed, read the reply with `subtask reply <task>`.** This prints the worker's reply from durable history — it works regardless of how the bash output was captured. Exit code 0 alone does not mean "kicked off" — it means the worker has already replied. Don't confuse the two.

Don't pipe `subtask send` through `tail`, `head`, or other filters; you'll truncate the reply marker. If you want quieter output, use `subtask send -q`. Either way, `subtask reply <task>` is the canonical way to retrieve the reply.

## Merging

`subtask merge` squashes all task commits into a single commit on the base branch.

```bash
subtask merge fix/bug -m "Fix race condition"
```

**If conflicts occur**, merge will fail with instructions. Follow them.

## Stages

All tasks have stages: `doing → review → ready`

| Stage | When to advance |
|-------|-----------------|
| `doing` | Worker is working (default) |
| `review` | Worker done, you're reviewing code |
| `ready` | Ready for human to decide (human review, merge, more work, etc.) |

`subtask stage <task> <stage>` advances the stage. It always:
- Writes the new stage to history
- Applies any preset bound to the new stage in the workflow YAML (and clears the session if the adapter changes — cross-stage context comes from the workspace, PLAN.md, and PROGRESS.json)

**Auto-dispatch:** If the new stage has `worker_instructions:` defined in the workflow YAML, `stage` automatically dispatches the worker with those instructions and blocks until it replies — same as `subtask send`. An optional positional argument is appended to the instructions (or used alone if `worker_instructions:` is absent):

```bash
subtask stage fix/bug review                        # passive — no worker_instructions, returns immediately
subtask stage fix/bug review "Focus on error paths" # auto-dispatches, BLOCKS — run with run_in_background: true
subtask stage fix/bug review --no-send              # always passive, even with worker_instructions
```

When `stage` auto-dispatches, it blocks like `subtask send`. Run it with `run_in_background: true` so you can keep talking to the user while you wait. You'll be notified when the bash exits; read the worker's reply with `subtask reply <task>`.

Pass `--no-send` to stay passive even when `worker_instructions:` is defined.

## Planning Workflows

For complex tasks, add a plan stage: `plan → implement → review → ready`

**You plan (`--workflow you-plan`):** You draft PLAN.md in task folder, worker reviews and pokes holes.
**They plan (`--workflow they-plan`):** Worker drafts PLAN.md in task folder, you review and approve or request changes.

## Project-wide worker brief

If `.subtask/WORKER.md` exists, its contents are auto-prepended to every worker prompt as a `## Project` section. This is the place for project-wide expectations every worker should carry — regen recipes, commit conventions, "the architecture tests are load-bearing, fix the cause not the test." Distinct from `CLAUDE.md` (which targets you, the lead) and `TASK.md` (per-task).

When you notice the same instruction landing in multiple task briefs, lift it into `WORKER.md`.

## Silent stages

A workflow stage can opt out of the unread-reply nudge by setting `notify: false` in its YAML:

```yaml
- name: commit
  notify: false
  worker_instructions: Commit your work.
```

While a task sits in a stage marked `notify: false`, worker replies in that stage are treated as plumbing — they don't surface via `subtask unread` and don't trigger the Stop-hook reminder. Use it for transitions where the worker is doing mechanical bookkeeping (committing, regenerating snapshots) that doesn't need your eyes. The silence applies to *any* reply while the task is in that stage, not just auto-dispatched ones.

## Notes

- Use `subtask list` to see what's in flight.
- Use `subtask show <task>` to see progress and details.
- Use `subtask log <task>` to see task conversation and events.
- Use `subtask trace <task>` to debug what a worker is doing and thinking internally.
