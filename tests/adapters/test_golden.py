"""Parametrized golden-fixture tests for adapter parse() contracts.

Each test case is an (adapter, case) pair auto-discovered from
tests/fixtures/adapters/<adapter>/<case>/. Adding a new case directory
with an input file and expected.json is sufficient to add a new test.

To add a fixture:
    ./dev gen-adapter-fixture <adapter> <case>

To update an expected.json after an intentional adapter change:
    ./dev gen-adapter-fixture <adapter> <case>
    # Review the diff, then commit.
"""

import importlib

import pytest

from conftest import _golden_cases, assert_golden


@pytest.mark.parametrize("adapter_name,case", _golden_cases())
def test_golden(adapter_name, case, tmp_path):
    adapter = importlib.import_module(f"siftd.adapters.{adapter_name}")
    assert_golden(adapter, adapter_name, case, tmp_path)
