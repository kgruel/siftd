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

from _golden import GOLDEN_TZ
from conftest import _golden_cases, assert_golden, pinned_tz


@pytest.mark.parametrize("adapter_name,case", _golden_cases())
def test_golden(adapter_name, case, tmp_path):
    adapter = importlib.import_module(f"siftd.adapters.{adapter_name}")
    # Same zone `./dev gen-adapter-fixture` generated under — otherwise an
    # adapter that resolves a naive timestamp against the host zone (aider)
    # makes expected.json a property of the machine running the suite.
    with pinned_tz(GOLDEN_TZ):
        assert_golden(adapter, adapter_name, case, tmp_path)
