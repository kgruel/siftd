"""Tests for the shared CLI filter argument group."""

import argparse

import pytest

from siftd.cli._filters import add_filter_args, extract_filter_args


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    add_filter_args(p, include_tool=True, include_tool_tag=True, include_search=True)
    return p


def test_on_flag_appends_to_tag_kind():
    args = _parser().parse_args(["--on", "response", "--on", "tool_call"])
    fa = extract_filter_args(args)
    assert fa.tag_kind == ["response", "tool_call"]


def test_on_flag_default_is_none():
    args = _parser().parse_args([])
    fa = extract_filter_args(args)
    assert fa.tag_kind is None


def test_on_flag_rejects_unknown_kind(capsys):
    with pytest.raises(SystemExit):
        _parser().parse_args(["--on", "workspace"])
    err = capsys.readouterr().err
    assert "invalid choice" in err.lower()


def test_on_flag_combined_with_tag():
    args = _parser().parse_args(["-l", "review", "--on", "response"])
    fa = extract_filter_args(args)
    assert fa.tag == ["review"]
    assert fa.tag_kind == ["response"]
