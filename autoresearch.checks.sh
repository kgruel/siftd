#!/bin/bash
set -euo pipefail

# Run full test suite to ensure nothing is broken
.venv/bin/python -m pytest tests/ -x -q --tb=short -m "not embeddings and not serve" 2>&1 | tail -30

# Verify no trivial tests (every test function must have at least one assert)
.venv/bin/python -c "
import ast, sys

errors = []
tree = ast.parse(open('tests/adapters/test_infra.py').read())
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
            errors.append(f'test_infra.py:{node.lineno} {node.name} has no assertions')

if errors:
    print('TRIVIAL TESTS DETECTED:')
    for e in errors:
        print(f'  {e}')
    sys.exit(1)
"

# Lint check
.venv/bin/python -m ruff check src/siftd/adapters/sdk.py tests/adapters/test_infra.py 2>&1 | tail -20
