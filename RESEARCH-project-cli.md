# Project CLI Harness Research

## Current State

### Existing Scripts

| Script | Purpose | Notes |
|--------|---------|-------|
| `scripts/lint.sh` | Type check (ty) + lint (ruff --fix) | Auto-calls setup-worktree.sh if .venv missing |
| `scripts/setup-worktree.sh` | Create venv, sync deps | ~10 lines, uv-based |
| `scripts/gen_docs.py` | Generate reference docs (CLI, API, schema) | Standalone Python script |
| `plugin/scripts/session-start.sh` | Hook for Claude Code compaction | Registers sessions for live tagging |

### CI Workflow (.github/workflows/ci.yml)

```
test job:
  - uv sync --extra dev
  - uv run ruff check src/
  - uv run pytest tests/ -v --tb=short --ignore=tests/test_embeddings.py -m "not embeddings"

test-with-embeddings job:
  - uv sync --extra dev --extra embed
  - cache fastembed models
  - uv run pytest tests/ -v --tb=short -m embeddings
```

### What's Missing

1. **No ty in CI** — `lint.sh` runs ty, but CI only runs ruff
2. **No docs validation** — `gen_docs.py` exists but nothing checks if docs are stale
3. **Embeddings bootstrap for worktrees** — `setup-worktree.sh` only syncs deps, doesn't handle:
   - Installing embed extra
   - Warming fastembed cache (model download)
   - Running initial ingest

---

## Task Runner Options

### 1. Makefile (Status quo adjacent)

**Pros:**
- Universal familiarity
- Zero dependencies (pre-installed everywhere)
- Works fine with uv

**Cons:**
- Windows compatibility issues
- Syntax quirks (tabs, .PHONY, etc.)
- No conditional logic without pain

**Verdict:** Viable but dated. The codebase already uses shell scripts, so Makefile would just be a wrapper.

### 2. Just (Modern Makefile replacement)

**Pros:**
- Clean syntax, looks like Makefile but isn't
- Cross-platform
- Built-in variable handling
- Fast (Rust binary)

**Cons:**
- External dependency (brew/cargo install)
- Yet another tool for contributors to learn

**Example:**
```just
# justfile

lint:
    uv run ty check src/
    uv run ruff check src/ --fix

test:
    uv run pytest tests/ -v

setup-worktree:
    #!/usr/bin/env bash
    [ ! -d ".venv" ] && uv venv .venv
    uv sync
```

### 3. Invoke (Python-native)

**Pros:**
- Pure Python — no new syntax
- Already familiar to Python devs
- Handles cross-platform differences

**Cons:**
- Adds dependency to project
- Verbose compared to Makefile/Just

**Example:**
```python
# tasks.py
from invoke import task

@task
def lint(c):
    c.run("uv run ty check src/")
    c.run("uv run ruff check src/ --fix")

@task
def test(c):
    c.run("uv run pytest tests/ -v")
```

### 4. Nox (Testing-focused)

**Pros:**
- Standard in scientific Python
- Explicit, no magic
- Good for multi-environment testing

**Cons:**
- Overkill for simple task running
- CI-focused, not daily dev workflow

**Verdict:** Great for CI test matrices, but siftd doesn't need multi-Python testing.

### 5. pyproject.toml [project.scripts] + Custom CLI

**Pros:**
- Zero external dependencies
- Follows existing pattern (siftd already has a CLI)
- Discoverable via `siftd dev <cmd>` or `./dev <cmd>`

**Cons:**
- Need to write the harness code

**Example:**
```toml
[project.scripts]
siftd = "siftd.cli:main"
siftd-dev = "siftd.dev:main"  # or just a standalone script
```

---

## Recommendation: Minimal Shell Script + pyproject.toml

Given that:
1. siftd already uses uv exclusively
2. Most tasks are 1-2 commands
3. Worktree agents need fast, deterministic setup
4. The project isn't complex enough for Nox/Invoke

**Proposed approach:** A single `./dev` script (Python or shell) that unifies existing functionality.

### Proposed Interface

```bash
./dev setup      # Setup worktree for agent work
./dev lint       # Run ty + ruff (what lint.sh does)
./dev test       # Run tests (non-embedding)
./dev test-all   # Run all tests including embeddings
./dev docs       # Generate docs
./dev check      # Run lint + test (CI equivalent)
```

### Worktree Agent Bootstrap (`./dev setup`)

```
1. Create .venv if missing
2. uv sync --extra dev
3. If embeddings needed:
   - uv sync --extra embed
   - Warm fastembed cache (download model)
4. Run ingest to populate database
5. Print ready status
```

### Implementation Options

**Option A: Python script in repo root**
```python
#!/usr/bin/env python3
# ./dev
import subprocess
import sys

def setup():
    ...
def lint():
    ...
```

**Option B: Shell script calling into CLI**
```bash
#!/usr/bin/env bash
# ./dev
case "$1" in
    setup) ./scripts/setup-worktree.sh && siftd ingest ;;
    lint) ./scripts/lint.sh ;;
    ...
esac
```

**Option C: Extend siftd CLI with dev commands**

Add `siftd dev setup`, `siftd dev lint`, etc. This is the cleanest long-term but couples dev tooling to the package.

---

## Specific Recommendations

### 1. Keep it simple — shell script for now

Create `./dev` (a single shell script) that consolidates:
- `scripts/lint.sh`
- `scripts/setup-worktree.sh`
- `scripts/gen_docs.py` invocation
- Test running

### 2. Worktree agent setup flow

```bash
./dev setup [--embed]
```

Steps:
1. `uv venv .venv` if missing
2. `uv sync --extra dev`
3. If `--embed` or detected as needed:
   - `uv sync --extra embed`
   - Run: `python -c "from fastembed import TextEmbedding; TextEmbedding(model_name='BAAI/bge-small-en-v1.5')"` to warm cache
4. `siftd ingest` (populate from current user's logs)
5. Print: "Ready. Run `./dev check` to verify."

### 3. CI alignment

Update CI to use `./dev lint` and `./dev test` instead of raw commands. This ensures local and CI behavior match.

### 4. Docs check

Add `./dev docs --check` that:
1. Runs `gen_docs.py`
2. Checks if any files changed (via git diff)
3. Fails if docs are stale

---

## Future Considerations

If the project grows to need:
- Multi-Python version testing → Add nox
- Complex workflows → Consider Just
- Contributors complain about shell → Convert to Invoke

But for now, shell scripts with a unified `./dev` entry point is sufficient.

---

## Sources

- [Scientific Python Task Runners Guide](https://learn.scientific-python.org/development/guides/tasks/)
- [pyOpenSci Python Packaging Guide - Task Runners](https://www.pyopensci.org/python-package-guide/maintain-automate/task-runners.html)
- [Just - Stop Using Makefile](https://theorangeone.net/posts/just-stop-using-makefile/)
- [Why uv makes Make less essential](https://pydevtools.com/blog/why-uv-makes-make-less-essential-for-python-projects/)
- [Replacing Makefiles with Invoke](https://medium.com/@matbrizolla/replacing-makefiles-in-python-projects-with-invoke-b25fa464ebe5)
