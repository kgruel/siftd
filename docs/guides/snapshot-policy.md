# CLI snapshot test policy

Snapshots in `tests/snapshots/` capture `siftd --help` output for every
subcommand. Argparse formats usage text differently between Python versions (line
wrapping, spacing), so snapshots are stored in per-version directories:

```
tests/snapshots/__snapshots__/py312/test_help.ambr
tests/snapshots/__snapshots__/py313/test_help.ambr
tests/snapshots/__snapshots__/py314/test_help.ambr
```

`tests/conftest.py` has a `pytest_configure` hook that redirects syrupy's
`--snapshot-dirname` to the running Python's version directory (`pyXY`). CI runs
on all matrix versions (3.12, 3.13, 3.14), so each version's snapshot file must
be present and correct.

**Updating snapshots after intentional help text changes:**

```bash
uv run --python 3.12 pytest tests/snapshots/ --snapshot-update -n 0
uv run --python 3.13 pytest tests/snapshots/ --snapshot-update -n 0
uv run --python 3.14 pytest tests/snapshots/ --snapshot-update -n 0
```

The `-n 0` flag disables xdist parallelism during update — parallel workers race
on `.ambr` writes. Normal test runs (no `--snapshot-update`) are safe with xdist.

Commit the updated `.ambr` files in the same PR as the help text change.

**Adding a new Python version to the matrix:**

1. Add it to the `test` job matrix in `.github/workflows/ci.yml`.
2. Seed its snapshots: `uv run --python X.Y pytest tests/snapshots/ --snapshot-update -n 0`
3. Commit both changes together.

Never update snapshots on only one version — CI will fail on the others.

For full implementation details, see `tests/conftest.py` (hook) and
`tests/snapshots/test_help.py` (tests).
