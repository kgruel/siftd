"""Tests for git utilities and workspace identity."""

import pytest

from siftd.git import get_git_remote_url, normalize_remote_url


class TestNormalizeRemoteUrl:
    """Tests for normalize_remote_url()."""

    def test_https_url(self):
        """HTTPS URLs are normalized correctly."""
        assert normalize_remote_url("https://github.com/user/repo.git") == "github.com/user/repo"

    def test_https_url_without_git_suffix(self):
        """HTTPS URLs without .git suffix are normalized."""
        assert normalize_remote_url("https://github.com/user/repo") == "github.com/user/repo"

    def test_ssh_url_with_colon(self):
        """SSH URLs with colon separator are normalized."""
        assert normalize_remote_url("git@github.com:user/repo.git") == "github.com/user/repo"

    def test_ssh_url_with_protocol(self):
        """SSH URLs with protocol prefix are normalized."""
        assert normalize_remote_url("ssh://git@github.com/user/repo") == "github.com/user/repo"

    def test_git_protocol(self):
        """Git protocol URLs are normalized."""
        assert normalize_remote_url("git://github.com/user/repo.git") == "github.com/user/repo"

    def test_https_with_credentials(self):
        """HTTPS URLs with credentials are normalized."""
        assert normalize_remote_url("https://user:pass@github.com/user/repo.git") == "github.com/user/repo"

    def test_gitlab_url(self):
        """GitLab URLs are normalized the same way."""
        assert normalize_remote_url("git@gitlab.com:group/project.git") == "gitlab.com/group/project"

    def test_nested_path(self):
        """Nested paths (e.g., GitLab groups) are preserved."""
        assert normalize_remote_url("git@gitlab.com:org/group/project.git") == "gitlab.com/org/group/project"

    def test_self_hosted(self):
        """Self-hosted git servers work correctly."""
        assert normalize_remote_url("git@git.example.com:team/repo.git") == "git.example.com/team/repo"

    def test_ssh_with_port(self):
        """SSH URLs with explicit port are normalized."""
        assert normalize_remote_url("ssh://git@github.com:22/user/repo.git") == "github.com/user/repo"

    def test_https_with_port(self):
        """HTTPS URLs with explicit port are normalized."""
        assert normalize_remote_url("https://github.com:443/user/repo.git") == "github.com/user/repo"

    def test_trailing_slash(self):
        """Trailing slashes are stripped."""
        assert normalize_remote_url("https://github.com/user/repo/") == "github.com/user/repo"
        assert normalize_remote_url("git@github.com:user/repo/") == "github.com/user/repo"

    def test_file_protocol(self):
        """file:// URLs return the path."""
        assert normalize_remote_url("file:///path/to/repo") == "/path/to/repo"
        assert normalize_remote_url("file:///path/to/repo.git") == "/path/to/repo.git"

    def test_local_path(self):
        """Local paths are returned as-is."""
        assert normalize_remote_url("/path/to/repo") == "/path/to/repo"
        assert normalize_remote_url("/path/to/repo/") == "/path/to/repo"


class TestGetGitRemoteUrl:
    """Tests for get_git_remote_url()."""

    def test_returns_none_for_nonexistent_path(self, tmp_path):
        """Returns None for paths that don't exist."""
        assert get_git_remote_url(str(tmp_path / "nonexistent")) is None

    def test_returns_none_for_non_git_directory(self, tmp_path):
        """Returns None for directories without git."""
        assert get_git_remote_url(str(tmp_path)) is None

    def test_returns_none_for_git_without_origin(self, tmp_path):
        """Returns None for git repos without origin remote."""
        import subprocess
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        assert get_git_remote_url(str(tmp_path)) is None

    def test_returns_normalized_url_for_git_with_origin(self, tmp_path):
        """Returns normalized URL for git repos with origin."""
        import subprocess
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "remote", "add", "origin", "git@github.com:user/repo.git"],
            check=True, capture_output=True
        )
        assert get_git_remote_url(str(tmp_path)) == "github.com/user/repo"
