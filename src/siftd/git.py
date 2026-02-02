"""Git repository utilities for workspace identity.

Provides functions to extract and normalize git remote URLs for use
as canonical workspace identifiers. Also handles git worktrees by
resolving them to their main repository.

Note: Bare repository worktrees are not supported. This module only
handles worktrees created from non-bare repositories where the layout
is `<repo>/.git/worktrees/<name>`. Bare repos use `<bare>/worktrees/<name>`
which lacks the `.git` directory marker we rely on.
"""

import re
import subprocess
from functools import lru_cache
from pathlib import Path


def _is_submodule_gitdir(gitdir_path: Path) -> bool:
    """Check if gitdir path indicates a submodule (not a worktree).

    Submodules have gitdir like: /parent/.git/modules/submod
    We check for the exact `.git/modules` path segment to avoid
    false positives on paths like /home/user/modules/project/.git/worktrees/wt
    """
    parts = gitdir_path.parts
    for i, part in enumerate(parts):
        if part == ".git" and i + 1 < len(parts) and parts[i + 1] == "modules":
            return True
    return False


def _is_worktree_gitdir(gitdir_path: Path) -> bool:
    """Check if gitdir path indicates a worktree.

    Worktrees have gitdir like: /main/.git/worktrees/<name>
    We check for the exact `.git/worktrees` path segment.
    """
    parts = gitdir_path.parts
    for i, part in enumerate(parts):
        if part == ".git" and i + 1 < len(parts) and parts[i + 1] == "worktrees":
            return True
    return False


def resolve_worktree_to_main(path: str | Path) -> Path | None:
    """If path is inside a git worktree, return the main repository path.

    Git worktrees have a `.git` file (not directory) containing a gitdir
    reference like: "gitdir: /path/to/main/.git/worktrees/<name>"

    Returns None if:
    - Path is not inside a git worktree (regular repo or not a repo)
    - Unable to determine main repository
    - Path is inside a submodule (gitdir has .git/modules pattern)
    - The resolved main repository path doesn't exist
    """
    path = Path(path)

    # Find the .git file/directory for this path
    current = path if path.is_dir() else path.parent
    git_path = None

    while current != current.parent:
        candidate = current / ".git"
        if candidate.exists():
            git_path = candidate
            break
        current = current.parent

    if git_path is None:
        return None

    # If .git is a directory, this is not a worktree
    if git_path.is_dir():
        return None

    # .git is a file - parse it
    try:
        content = git_path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None

    if not content.startswith("gitdir:"):
        return None

    gitdir = content[7:].strip()  # Remove "gitdir: " prefix
    gitdir_path = Path(gitdir)

    # Handle relative paths
    if not gitdir_path.is_absolute():
        gitdir_path = (git_path.parent / gitdir_path).resolve()

    # Validate gitdir exists (catches stale .git files)
    if not gitdir_path.exists():
        return None

    # Check if this is a submodule - submodules are separate repos
    if _is_submodule_gitdir(gitdir_path):
        return None

    # Check if this is a worktree
    if not _is_worktree_gitdir(gitdir_path):
        return None

    # gitdir is like /main/.git/worktrees/<name>
    # Main repo root is the parent of the .git directory
    # Find the .git directory by looking for worktrees in the path
    parts = gitdir_path.parts
    for i, part in enumerate(parts):
        if part == ".git" and i + 1 < len(parts) and parts[i + 1] == "worktrees":
            # parts[:i] gives path up to but not including .git
            main_repo = Path(*parts[:i]) if i > 0 else Path("/")

            # Validate main repo exists
            if not main_repo.exists():
                return None

            return main_repo

    return None


# Cache workspace path resolution to avoid repeated filesystem walks
# during batch operations like peek scanning
@lru_cache(maxsize=256)
def get_canonical_workspace_path(path: str) -> str:
    """Resolve workspace path, handling worktrees.

    If path is inside a git worktree, returns the main repository path.
    Otherwise returns the resolved absolute path.

    This ensures that sessions from worktrees are associated with the
    main repository workspace, not fragmented across worktree paths.

    Results are cached to avoid repeated filesystem walks during batch
    operations like peek scanning.
    """
    resolved = Path(path).resolve()

    # Check if this path is inside a worktree
    main_repo = resolve_worktree_to_main(resolved)
    if main_repo:
        return str(main_repo)

    return str(resolved)


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

    Handles git worktrees by resolving to the main repository before
    looking up the remote. This ensures worktree sessions are associated
    with the main repository workspace.

    Args:
        path: Directory path to identify.

    Returns:
        Tuple of (git_remote, normalized_path) where:
        - git_remote: Normalized origin URL, or None if no origin remote
        - normalized_path: Resolved path (main repo path if inside worktree)
    """
    # First, resolve worktree to main repo if applicable
    canonical_path = get_canonical_workspace_path(path)

    # Try git remote at canonical path
    git_remote = get_git_remote_url(canonical_path)
    if git_remote:
        return (git_remote, canonical_path)

    # Walk up to find enclosing git repo
    current = Path(canonical_path)
    while current != current.parent:
        if (current / ".git").exists() or (current / ".git").is_file():
            git_remote = get_git_remote_url(str(current))
            if git_remote:
                return (git_remote, canonical_path)
            break  # Found a .git but no origin remote, stop walking
        current = current.parent

    return (None, canonical_path)
