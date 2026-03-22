#!/bin/bash
set -euo pipefail

# Full test suite — all tests should pass on this branch (editable install)
uv run python -m pytest tests/ -x -q --tb=short -n auto \
    -m "not embeddings and not serve" \
    -k "not test_import_rules and not test_basics and not test_follow_session" \
    --override-ini="addopts=" 2>&1 | tail -30

# No trivial tests in target files
uv run python -c "
import ast, sys

errors = []
for test_file in sys.argv[1:]:
    tree = ast.parse(open(test_file).read())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test_'):
            has_assert = any(
                isinstance(n, ast.Assert) or
                (isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and
                 isinstance(n.value.func, ast.Attribute) and
                 n.value.func.attr.startswith('assert'))
                for n in ast.walk(node)
            )
            has_raises = any(
                isinstance(n, ast.Call) and
                (hasattr(n.func, 'attr') and n.func.attr == 'raises')
                for n in ast.walk(node)
            )
            if not has_assert and not has_raises:
                errors.append(f'{test_file}:{node.lineno} {node.name} has no assertions')

if errors:
    print('TRIVIAL TESTS DETECTED:')
    for e in errors:
        print(f'  {e}')
    sys.exit(1)
" tests/cli/test_cmd_peek.py

# Lint
uv run python -m ruff check tests/cli/test_cmd_peek.py 2>&1 | tail -10
