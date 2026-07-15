"""Tests for the exceptions target of scripts/gen_docs.py (taxonomy reference).

The generator renders what the architecture ratchet enforces — these tests pin
that coupling: every taxonomy member and permanent carve-out the ratchet knows
appears in the doc, so the reference can't silently drop classes the ratchet
gains.
"""

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GEN_DOCS = _REPO_ROOT / "scripts" / "gen_docs.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: gen_docs defines dataclasses whose type-hint
    # resolution looks the module up in sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gen_docs = _load(_GEN_DOCS, "gen_docs")
ratchet = _load(_REPO_ROOT / "tests" / "architecture" / "test_exceptions.py", "_ratchet")


def test_every_taxonomy_member_is_documented():
    doc = gen_docs.generate_exceptions_docs()
    classes = ratchet._collect_classes()
    _, members = ratchet._exception_classes(classes)
    assert members, "ratchet returned no taxonomy members — classifier broken?"
    for _, name in members:
        assert f"`{name}`" in doc, f"taxonomy member {name} missing from exceptions.md"


def test_every_carveout_is_documented():
    doc = gen_docs.generate_exceptions_docs()
    for _, name in ratchet.PERMANENT_CARVEOUTS:
        assert f"`{name}`" in doc, f"carve-out {name} missing from exceptions.md"


def test_presentation_contract_stated():
    doc = gen_docs.generate_exceptions_docs()
    assert "UserInputError (exit 2, HTTP 400)" in doc
    assert "DriftError (exit 1, HTTP 503)" in doc
    assert "(HTTP 501)" in doc  # the EmbeddingsNotAvailable override


def test_generation_is_deterministic():
    assert gen_docs.generate_exceptions_docs() == gen_docs.generate_exceptions_docs()
