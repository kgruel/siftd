"""Git repository utilities for workspace identity.

Provides functions to extract and normalize git remote URLs for use
as canonical workspace identifiers.
"""

import re
import subprocess
from pathlib import Path


def get_git_remote_url(path: str) -> str | None:
    """Extract origin remote URL from a git repository.

    Args:
        path: Path to check for git repository.

    Returns:
        Normalized canonical URL or None if not a git repo or has no origin remote.
    """
    try:
        result = subprocess.run(
            ["git", "-C", path, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            raw_url = result.stdout.strip()
            if raw_url:
                return normalize_remote_url(raw_url)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return None


def normalize_remote_url(url: str) -> str:
    """Normalize git URL to canonical form.

    Strips protocol, credentials, port, and .git suffix to create a consistent
    identifier across different URL formats.

    Examples:
        git@github.com:user/repo.git -> github.com/user/repo
        https://github.com/user/repo.git -> github.com/user/repo
        ssh://git@github.com/user/repo -> github.com/user/repo
        ssh://git@github.com:22/user/repo -> github.com/user/repo
        https://user:pass@github.com/user/repo.git -> github.com/user/repo
        https://github.com/user/repo/ -> github.com/user/repo
        file:///path/to/repo -> /path/to/repo
        /path/to/repo -> /path/to/repo
    """
    # Handle file:// protocol - return path as-is (local remotes)
    if url.startswith("file://"):
        path = url[7:]  # Remove "file://"
        return path.rstrip("/")

    # Handle bare local paths (starts with /)
    if url.startswith("/"):
        return url.rstrip("/")

    # Strip protocol (https://, ssh://, git://)
    url = re.sub(r"^(https?|ssh|git)://", "", url)

    # Strip git@ prefix (for SSH URLs like git@github.com:user/repo)
    url = re.sub(r"^git@", "", url)

    # Strip credentials (user:pass@)
    url = re.sub(r"^[^@]+@", "", url)

    # Handle port in URL (host:port/path -> host/path)
    # Match host:port/ where port is numeric, then remove port
    url = re.sub(r"^([^/:]+):(\d+)/", r"\1/", url)

    # Normalize SSH colon separator to slash (github.com:user/repo -> github.com/user/repo)
    # Only do this if there's no slash yet (to distinguish from host:port which was already handled)
    if "/" not in url:
        url = url.replace(":", "/", 1)
    elif ":" in url.split("/")[0]:
        # Handle remaining colon in host part (SSH-style without port)
        parts = url.split("/", 1)
        host = parts[0].replace(":", "/")
        url = host + "/" + parts[1] if len(parts) > 1 else host

    # Strip .git suffix
    url = re.sub(r"\.git$", "", url)

    # Strip trailing slash
    url = url.rstrip("/")

    # Remove any double slashes that may have been introduced
    url = re.sub(r"//+", "/", url)

    return url


def get_canonical_workspace_identity(path: str) -> tuple[str | None, str]:
    """Return (git_remote, normalized_path) for workspace identity.

    Resolves the path and attempts to find a git remote URL. If the path
    itself is not a git repo, walks up the directory tree to find an
    enclosing repository.

    Args:
        path: Directory path to identify.

    Returns:
        Tuple of (git_remote, normalized_path) where git_remote may be None
        if no git repository with an origin remote is found.
    """
    normalized = str(Path(path).resolve())

    # Try git remote at this exact path
    git_remote = get_git_remote_url(path)
    if git_remote:
        return (git_remote, normalized)

    # Walk up to find enclosing git repo
    current = Path(path).resolve()
    while current != current.parent:
        if (current / ".git").exists() or (current / ".git").is_file():
            git_remote = get_git_remote_url(str(current))
            if git_remote:
                return (git_remote, normalized)
            break  # Found a .git but no origin remote, stop walking
        current = current.parent

    return (None, normalized)
