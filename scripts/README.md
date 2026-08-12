# scripts

The `./dev` harness at the repo root is a thin dispatcher: it discovers every
command from `scripts/*.sh` and forwards `./dev <name> [args]` to
`scripts/<name>.sh`. There is no central registry — the filename *is* the
command name, and the one-line `# DESC:` header near the top of each script is
its help text. Files whose names begin with `_` are excluded from discovery, so
helpers and fixtures that are not commands can live here without cluttering
`./dev help`.

Shared shell helpers live in `lib/` and are pulled in with
`source "$(dirname "$0")/lib/dev.sh"` at the top of each command; `dev.sh` in
turn wires up logging (`log.sh`), the CLI-usage helpers (`cli.sh`), and
`ensure_venv`. See `lib/README.md` for the helper surface.

To add a command, create `scripts/<name>.sh` with a `# DESC: <one line>` header,
source `lib/dev.sh`, and implement a `main()` that parses `--help` and does the
work — copy an existing script such as `test.sh` as the template. It becomes
`./dev <name>` immediately, with no other file to edit. Run `./dev help` for the
current command list.

<!-- gen:begin scripts -->
<sub>generated from script DESC headers — run <code>./dev docs</code></sub>

| Command | Description |
|---------|-------------|
| [agent-close](agent-close.sh) | Cleanup agent worktrees after merge |
| [agent](agent.sh) | Launch agent in a worktree with prompt template |
| [browser-smoke](browser-smoke.sh) | Run the real-browser CSP smoke (T3) against a from-source server |
| [check](check.sh) | Run lint + test + optional lanes (CI equivalent, quiet by default) |
| [docs](docs.sh) | Generate docs; --check fails if the result is not staged or committed |
| [gen-adapter-fixture](gen-adapter-fixture.sh) | Generate or update tests/fixtures/adapters/<adapter>/<case>/expected.json |
| [gen-schema-fixture](gen-schema-fixture.sh) | Dump current schema as fixture for tests/fixtures/schemas/v${SCHEMA_VERSION}.sql |
| [lint](lint.sh) | Run ty type checker + ruff linter (with autofix) |
| [setup](setup.sh) | Setup worktree (venv, deps, optional extras) |
| [smoke-homelab](smoke-homelab.sh) | End-to-end docker-compose homelab smoke harness driver |
| [test-all](test-all.sh) | Run all tests including optional extras |
| [test-embed](test-embed.sh) | Run embedding tests |
| [test-serve](test-serve.sh) | Run serve tests |
| [test-slow](test-slow.sh) | Run slow tests |
| [test](test.sh) | Run base tests |
<!-- gen:end -->
