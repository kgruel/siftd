# scripts

<!-- TODO(preamble): authored in slice 3 -->
Dev harness commands, discovered as `./dev <name>`.

<!-- gen:begin scripts -->
<sub>generated from script DESC headers — run <code>./dev docs</code></sub>

| Command | Description |
|---------|-------------|
| [agent-close](agent-close.sh) | Cleanup agent worktrees after merge |
| [agent](agent.sh) | Launch agent in a worktree with prompt template |
| [browser-smoke](browser-smoke.sh) | Run the real-browser CSP smoke (T3) against a from-source server |
| [check](check.sh) | Run lint + test + optional lanes (CI equivalent, quiet by default) |
| [docs](docs.sh) | Generate docs; --check fails if stale |
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
