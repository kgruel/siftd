"""Tests for categorize_shell_command — pure function with 15 categories."""

import pytest

from siftd.domain.shell_categories import categorize_shell_command


@pytest.mark.parametrize(
    "cmd,expected",
    [
        # test
        ("pytest tests/", "test"),
        ("jest --watch", "test"),
        ("npm test", "test"),
        ("cargo test", "test"),
        ("go test ./...", "test"),
        ("vitest run", "test"),
        # lint
        ("ruff check .", "lint"),
        ("eslint src/", "lint"),
        ("mypy src/", "lint"),
        ("black --check .", "lint"),
        ("uv run ruff check", "lint"),
        ("uv run ty check", "lint"),
        # vcs
        ("git status", "vcs"),
        ("git commit -m 'fix'", "vcs"),
        ("gh pr create", "vcs"),
        # search
        ("grep -r pattern .", "search"),
        ("rg pattern", "search"),
        ("find . -name '*.py'", "search"),
        # file
        ("ls -la", "file"),
        ("cat README.md", "file"),
        ("mkdir -p src/new", "file"),
        ("rm -rf build/", "file"),
        # remote
        ("ssh user@host", "remote"),
        ("curl https://api.example.com", "remote"),
        ("wget https://example.com/file", "remote"),
        # db
        ("sqlite3 test.db", "db"),
        ("psql -d mydb", "db"),
        # infra
        ("docker build .", "infra"),
        ("kubectl get pods", "infra"),
        ("terraform plan", "infra"),
        # ai
        ("claude --help", "ai"),
        # python
        ("python script.py", "python"),
        ("python3 -c 'print(1)'", "python"),
        ("uv run python script.py", "python"),
        # node
        ("npm install", "node"),
        ("npx create-react-app", "node"),
        ("bun run dev", "node"),
        # package
        ("pip install requests", "package"),
        ("brew install jq", "package"),
        ("cargo build", "package"),
        # shell
        ("echo hello", "shell"),
        ("pwd", "shell"),
        ("export FOO=bar", "shell"),
    ],
    ids=lambda v: str(v)[:40],
)
def test_categorized_commands(cmd, expected):
    assert categorize_shell_command(cmd) == expected


@pytest.mark.parametrize(
    "cmd",
    [
        "myunknowncommand --flag",
        "customtool do-thing",
        "",
    ],
    ids=["unknown", "custom", "empty"],
)
def test_uncategorized_commands(cmd):
    assert categorize_shell_command(cmd) is None


class TestEdgeCases:
    def test_cd_prefix_stripped(self):
        """cd <path> && <cmd> normalizes to just <cmd>."""
        assert categorize_shell_command("cd /tmp && pytest") == "test"
        assert categorize_shell_command("cd /project && git status") == "vcs"

    def test_pipe_commands(self):
        """Commands after | are recognized."""
        assert categorize_shell_command("cat foo.txt | grep pattern") == "search"
        assert categorize_shell_command("echo data | head -5") == "file"
        assert categorize_shell_command("cat file | wc -l") == "file"

    def test_none_input(self):
        assert categorize_shell_command(None) is None
