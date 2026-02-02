"""Tests for git utilities and workspace identity."""

import subprocess

import pytest

from siftd.git import (
    get_canonical_workspace_path,
    get_git_remote_url,
    normalize_remote_url,
    resolve_worktree_to_main,
)


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
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "remote", "add", "origin", "git@github.com:user/repo.git"],
            check=True, capture_output=True
        )
        assert get_git_remote_url(str(tmp_path)) == "github.com/user/repo"


class TestResolveWorktreeToMain:
    """Tests for resolve_worktree_to_main()."""

    def test_regular_repo_returns_none(self, tmp_path):
        """Regular git repos (with .git directory) return None."""
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        assert resolve_worktree_to_main(tmp_path) is None

    def test_non_git_directory_returns_none(self, tmp_path):
        """Non-git directories return None."""
        assert resolve_worktree_to_main(tmp_path) is None

    def test_worktree_returns_main_repo(self, tmp_path):
        """Worktrees return path to main repository."""
        main = tmp_path / "main"
        main.mkdir()
        subprocess.run(["git", "init", str(main)], check=True, capture_output=True)

        # Need an initial commit to create a worktree
        subprocess.run(
            ["git", "-C", str(main), "commit", "--allow-empty", "-m", "init"],
            check=True, capture_output=True
        )

        # Create worktree
        wt = tmp_path / "worktree"
        subprocess.run(
            ["git", "-C", str(main), "worktree", "add", str(wt), "-b", "test"],
            check=True, capture_output=True
        )

        result = resolve_worktree_to_main(wt)
        assert result == main

    def test_nested_path_in_worktree(self, tmp_path):
        """Paths nested inside worktree resolve to main repo."""
        main = tmp_path / "main"
        main.mkdir()
        subprocess.run(["git", "init", str(main)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(main), "commit", "--allow-empty", "-m", "init"],
            check=True, capture_output=True
        )

        wt = tmp_path / "worktree"
        subprocess.run(
            ["git", "-C", str(main), "worktree", "add", str(wt), "-b", "test"],
            check=True, capture_output=True
        )

        # Create nested directory in worktree
        nested = wt / "src" / "deep"
        nested.mkdir(parents=True)

        result = resolve_worktree_to_main(nested)
        assert result == main

    def test_broken_git_file_returns_none(self, tmp_path):
        """Broken .git file gracefully returns None."""
        git_file = tmp_path / ".git"
        git_file.write_text("invalid content")

        assert resolve_worktree_to_main(tmp_path) is None

    def test_gitdir_pointing_to_nonexistent_returns_none(self, tmp_path):
        """Gitdir pointing to non-worktree path returns None."""
        git_file = tmp_path / ".git"
        git_file.write_text("gitdir: /nonexistent/path")

        assert resolve_worktree_to_main(tmp_path) is None

    def test_submodule_not_treated_as_worktree(self, tmp_path):
        """Submodules (gitdir with .git/modules) are not treated as worktrees."""
        # Simulate a submodule .git file - create the gitdir path so it exists
        parent_git = tmp_path / "parent" / ".git" / "modules" / "submod"
        parent_git.mkdir(parents=True)

        submod = tmp_path / "submod"
        submod.mkdir()
        git_file = submod / ".git"
        git_file.write_text(f"gitdir: {parent_git}")

        assert resolve_worktree_to_main(submod) is None

    def test_modules_in_user_path_not_confused_with_submodule(self, tmp_path):
        """Paths like /home/user/modules/project don't false-positive as submodules."""
        # Create a directory structure with 'modules' in the user path
        modules_dir = tmp_path / "home" / "user" / "modules" / "project"
        modules_dir.mkdir(parents=True)

        # Create a real worktree inside this path
        main = modules_dir / "main"
        main.mkdir()
        subprocess.run(["git", "init", str(main)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(main), "commit", "--allow-empty", "-m", "init"],
            check=True, capture_output=True
        )

        wt = modules_dir / "worktree"
        subprocess.run(
            ["git", "-C", str(main), "worktree", "add", str(wt), "-b", "test"],
            check=True, capture_output=True
        )

        # Should still resolve correctly despite 'modules' in path
        result = resolve_worktree_to_main(wt)
        assert result == main

    def test_stale_gitdir_returns_none(self, tmp_path):
        """Stale .git file pointing to non-existent gitdir returns None."""
        git_file = tmp_path / ".git"
        # Point to a worktree-like path that doesn't exist
        git_file.write_text("gitdir: /nonexistent/repo/.git/worktrees/stale")

        assert resolve_worktree_to_main(tmp_path) is None

    def test_stale_main_repo_returns_none(self, tmp_path):
        """Worktree where main repo was deleted returns None."""
        # Create a fake gitdir structure
        gitdir = tmp_path / "gitdir" / ".git" / "worktrees" / "wt"
        gitdir.mkdir(parents=True)

        # Create worktree pointing to it
        wt = tmp_path / "wt"
        wt.mkdir()
        git_file = wt / ".git"
        git_file.write_text(f"gitdir: {gitdir}")

        # The gitdir exists but the main repo (tmp_path/gitdir) exists
        # However, let's test with a truly stale scenario by pointing to nonexistent main
        git_file.write_text("gitdir: /nonexistent/.git/worktrees/wt")

        assert resolve_worktree_to_main(wt) is None


class TestGetCanonicalWorkspacePath:
    """Tests for get_canonical_workspace_path()."""

    def test_regular_path_unchanged(self, tmp_path):
        """Non-worktree paths are returned resolved."""
        result = get_canonical_workspace_path(str(tmp_path))
        assert result == str(tmp_path.resolve())

    def test_non_git_path_unchanged(self, tmp_path):
        """Non-git directories return resolved path."""
        result = get_canonical_workspace_path(str(tmp_path))
        assert result == str(tmp_path.resolve())

    def test_regular_git_repo_unchanged(self, tmp_path):
        """Regular git repos return their own path."""
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        result = get_canonical_workspace_path(str(tmp_path))
        assert result == str(tmp_path.resolve())

    def test_worktree_resolves_to_main(self, tmp_path):
        """Worktree paths resolve to main repository."""
        main = tmp_path / "main"
        main.mkdir()
        subprocess.run(["git", "init", str(main)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(main), "commit", "--allow-empty", "-m", "init"],
            check=True, capture_output=True
        )

        wt = tmp_path / "worktree"
        subprocess.run(
            ["git", "-C", str(main), "worktree", "add", str(wt), "-b", "test"],
            check=True, capture_output=True
        )

        result = get_canonical_workspace_path(str(wt))
        assert result == str(main.resolve())

    def test_caching_returns_consistent_results(self, tmp_path):
        """Cached results are consistent across multiple calls."""
        # Clear cache before test
        get_canonical_workspace_path.cache_clear()

        main = tmp_path / "main"
        main.mkdir()
        subprocess.run(["git", "init", str(main)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(main), "commit", "--allow-empty", "-m", "init"],
            check=True, capture_output=True
        )

        wt = tmp_path / "worktree"
        subprocess.run(
            ["git", "-C", str(main), "worktree", "add", str(wt), "-b", "test"],
            check=True, capture_output=True
        )

        # Call multiple times - should return same result
        result1 = get_canonical_workspace_path(str(wt))
        result2 = get_canonical_workspace_path(str(wt))
        result3 = get_canonical_workspace_path(str(wt))

        assert result1 == result2 == result3 == str(main.resolve())

        # Check cache was used
        cache_info = get_canonical_workspace_path.cache_info()
        assert cache_info.hits >= 2  # At least 2 cache hits
