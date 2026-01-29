"""Shell command categorization logic.

Classifies shell commands into semantic categories (test, lint, vcs, etc.)
for auto-tagging at ingest time.
"""

import re

# Namespace prefix for auto-generated shell tags
SHELL_TAG_PREFIX = "shell:"

# Categories and their identifying commands/patterns
SHELL_CATEGORIES = {
    "test": {
        "keywords": ["pytest", "jest", "vitest", "mocha"],
        "patterns": [r"\bcargo\s+test\b", r"\bgo\s+test\b", r"\bnpm\s+test\b"],
    },
    "lint": {
        "commands": ["ruff", "eslint", "mypy", "pylint", "flake8", "black", "isort"],
        "patterns": [r"\buv\s+run\s+ty\b", r"\buv\s+run\s+ruff\b"],
    },
    "vcs": {
        "commands": ["git", "yadm", "gh"],
    },
    "search": {
        "commands": ["grep", "rg", "find", "ag"],
        "pipe_commands": ["grep", "rg"],
    },
    "file": {
        "commands": ["ls", "cat", "head", "tail", "mv", "cp", "rm", "mkdir", "tree", "wc", "nl", "touch", "chmod", "chown", "ln", "sed", "awk"],
        "pipe_commands": ["head", "tail", "wc", "nl", "sed", "awk"],
    },
    "remote": {
        "commands": ["ssh", "scp", "rsync", "curl", "wget", "ping", "dig", "nc", "netstat"],
    },
    "db": {
        "commands": ["sqlite3", "sqlite-utils", "psql", "mysql"],
        "pipe_commands": ["sqlite3"],
    },
    "infra": {
        "commands": ["docker", "terraform", "ansible", "kubectl", "k9s", "helm"],
    },
    "ai": {
        "commands": ["claude", "gemini", "aider", "codex"],
    },
    "python": {
        "commands": ["python", "python3"],
        "patterns": [r"\buv\s+run\s+python"],
    },
    "node": {
        "commands": ["npm", "node", "yarn", "pnpm", "npx", "bun"],
    },
    "package": {
        "commands": ["pip", "brew", "apt", "cargo"],
        "patterns": [r"^uv\s+(?!run)"],  # uv but not uv run
    },
    "shell": {
        "commands": ["echo", "sleep", "source", ".", "date", "which", "pwd", "env", "export",
                     "bash", "zsh", "sh", "tmux", "screen", "open", "pbcopy", "pbpaste",
                     "for", "while", "if", "case", "test", "["],
    },
}


def categorize_shell_command(cmd: str) -> str | None:
    """Categorize a shell command string into a category.

    Returns the category name (without prefix) or None if uncategorized.
    """
    if not cmd:
        return None

    # Normalize: strip leading "cd <path> && " pattern
    cmd_norm = re.sub(r"^cd\s+[^&]+&&\s*", "", cmd).strip()
    parts = cmd_norm.split()
    first_word = parts[0] if parts else ""

    # Check each category in order of specificity
    # Test/lint first (they often use other tools like uv run)
    for category in ["test", "lint"]:
        spec = SHELL_CATEGORIES[category]

        # Check keywords anywhere in command
        if "keywords" in spec:
            for kw in spec["keywords"]:
                if kw in cmd:
                    return category

        # Check regex patterns
        if "patterns" in spec:
            for pattern in spec["patterns"]:
                if re.search(pattern, cmd):
                    return category

        # Check first-word commands
        if "commands" in spec and first_word in spec["commands"]:
            return category

    # Check remaining categories
    for category, spec in SHELL_CATEGORIES.items():
        if category in ("test", "lint"):
            continue  # Already checked

        # Check first-word commands
        if "commands" in spec and first_word in spec["commands"]:
            return category

        # Check pipe commands (| cmd)
        if "pipe_commands" in spec:
            for pipe_cmd in spec["pipe_commands"]:
                if re.search(rf"\|\s*{pipe_cmd}\b", cmd):
                    return category

        # Check regex patterns
        if "patterns" in spec:
            for pattern in spec["patterns"]:
                if re.search(pattern, cmd):
                    return category

    return None
