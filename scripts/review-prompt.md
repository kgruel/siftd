# Review: {{branch}}

{{context}}

## Changes
```
{{diff_stat}}
```

## Dev Commands
```
./dev check          # Lint + test (quiet)
./dev check -v       # Lint + test (verbose)
./dev lint           # Type check + lint only
./dev test           # Run tests only
```

## Review Focus
1. Does the implementation match the task description?
2. Are there any architectural violations (check CLAUDE.md)?
3. Is error handling consistent with existing patterns?
4. Are tests comprehensive?
5. Run `./dev check` to verify lint and tests pass.

Start by reading the task context, then review the diff: `git diff main`
