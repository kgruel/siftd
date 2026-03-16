"""Tests for tool-oriented query parsing."""

import argparse

import pytest

from siftd.tool_query import (
    KNOWN_FIELDS,
    build_fts5_query,
    expand_tool_names_for_matching,
    parse_tool_query,
)


class TestParseToolQuery:
    def test_plain_text_query_stays_bare(self):
        parsed = parse_tool_query("grep journal_mode")

        assert parsed.fields == {}
        assert parsed.unknown_fields == {}
        assert parsed.bare_terms == ["grep", "journal_mode"]
        assert parsed.free_text == "grep journal_mode"
        assert not parsed.has_fields

    def test_fielded_terms_are_extracted(self):
        parsed = parse_tool_query("tool:file.read path:pyproject.toml")

        assert parsed.fields == {
            "tool": ["file.read"],
            "path": ["pyproject.toml"],
        }
        assert parsed.bare_terms == []
        assert parsed.has_fields

    def test_mixed_query_keeps_bare_terms_for_ranking(self):
        parsed = parse_tool_query("tool:shell.execute status:error git")

        assert parsed.fields == {
            "tool": ["shell.execute"],
            "status": ["error"],
        }
        assert parsed.bare_terms == ["git"]
        assert parsed.free_text == "git"

    def test_repeated_same_field_preserves_all_values(self):
        parsed = parse_tool_query("status:error status:timeout tool:shell.execute")

        assert parsed.fields["status"] == ["error", "timeout"]
        assert parsed.fields["tool"] == ["shell.execute"]

    def test_unknown_field_is_tracked_separately(self):
        parsed = parse_tool_query("owner:kaygee tool:file.read")

        assert parsed.fields == {"tool": ["file.read"]}
        assert parsed.unknown_fields == {"owner": ["kaygee"]}
        assert parsed.has_fields

    def test_empty_query_returns_empty_structure(self):
        parsed = parse_tool_query("   ")

        assert parsed.raw == "   "
        assert parsed.fields == {}
        assert parsed.terms == []
        assert parsed.bare_terms == []
        assert parsed.free_text == ""

    def test_known_fields_are_case_insensitive(self):
        parsed = parse_tool_query("TOOL:file.read Workspace:siftd")

        assert parsed.fields == {
            "tool": ["file.read"],
            "workspace": ["siftd"],
        }

    def test_tool_aliases_are_normalized(self):
        parsed = parse_tool_query("tool:bash tool:read")

        assert parsed.fields["tool"] == ["shell.execute", "file.read"]

    def test_tool_matching_expands_canonical_and_raw_aliases(self):
        shell_names = expand_tool_names_for_matching("shell.execute")
        read_names = expand_tool_names_for_matching("file.read")

        assert "shell.execute" in shell_names
        assert "bash" in shell_names
        assert "run_experiment" in shell_names
        assert "file.read" in read_names
        assert "read" in read_names
        assert "Read" in read_names

    def test_inline_common_filters_are_normalized(self):
        parsed = parse_tool_query(
            "workspace:siftd since:7d before:yesterday all-tags:research:auth no-tag:archived tool-tag:shell:test"
        )

        assert parsed.fields["workspace"] == ["siftd"]
        assert parsed.fields["since"]
        assert parsed.fields["before"]
        assert parsed.fields["all_tags"] == ["research:auth"]
        assert parsed.fields["no_tag"] == ["archived"]
        assert parsed.fields["tool_tag"] == ["shell:test"]

    @pytest.mark.parametrize(
        ("field_alias", "expected_field"),
        [
            ("all-tags", "all_tags"),
            ("all_tag", "all_tags"),
            ("all-tag", "all_tags"),
            ("no-tag", "no_tag"),
            ("tool-tag", "tool_tag"),
        ],
    )
    def test_inline_field_aliases_normalize_to_canonical_names(self, field_alias, expected_field):
        parsed = parse_tool_query(f"{field_alias}:value")

        assert parsed.fields == {expected_field: ["value"]}

    def test_repeated_inline_field_values_preserve_or_semantics(self):
        parsed = parse_tool_query("workspace:siftd workspace:other tag:bug tag:docs")

        assert parsed.fields["workspace"] == ["siftd", "other"]
        assert parsed.fields["tag"] == ["bug", "docs"]

    def test_invalid_inline_dates_raise_parse_error(self):
        with pytest.raises(argparse.ArgumentTypeError, match="invalid date format"):
            parse_tool_query("since:not-a-date")

    def test_known_field_vocabulary_matches_roadmap_baseline(self):
        assert {
            "tool", "tool_family", "status", "path", "basename", "ext",
            "cmd", "pattern", "arg", "result", "result_status",
            "workspace", "tag", "all_tags", "no_tag", "tool_tag",
            "model", "since", "before", "provider", "harness",
        } <= KNOWN_FIELDS


class TestBuildFts5Query:
    def test_quotes_shellish_tokens_safely(self):
        assert build_fts5_query(["./dev", "check", "pyproject.toml"]) == '"./dev" "check" "pyproject.toml"'

    def test_escapes_embedded_quotes(self):
        assert build_fts5_query(['say"hi']) == '"say""hi"'

    def test_drops_punctuation_only_low_signal_terms(self):
        assert build_fts5_query(["...", "()", ":", "./dev"]) == '"./dev"'

    def test_returns_empty_query_for_all_punctuation_terms(self):
        assert build_fts5_query(["...", "()", ":"]) == ""
