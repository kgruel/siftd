# scripts/lib - Shared Shell Utilities

Common patterns for the ./dev harness scripts.

## Quick Start

For dev scripts, source `lib/dev.sh` - it pulls in everything:

```bash
#!/usr/bin/env bash
# my-script.sh
# DESC: Does something useful
source "$(dirname "$0")/lib/dev.sh"

main() {
    ensure_venv
    log_info "Starting..."
}

main "$@"
```

## Modules

| File | Purpose | Dependencies |
|------|---------|--------------|
| dev.sh | **Entry point** - sources all libs, adds project helpers | none |
| log.sh | Colored logging helpers | none |
| cli.sh | Argument parsing + usage helpers | none |
| paths.sh | Script + XDG path helpers | none |
| templates.sh | Template loading + placeholder injection | python3 (for template_inject_env) |

## Function Reference

### dev.sh (project-specific)

| Function | Usage | Description |
|----------|-------|-------------|
| `ensure_venv` | `ensure_venv [--embed]` | Auto-setup venv if missing |
| `run_uv` | `run_uv sync` | Run uv in project root |
| `require_command` | `require_command jq "brew install jq"` | Check command exists |

Also sets `DEV_ROOT` to the project root directory.

### log.sh

| Function | Usage | Description |
|----------|-------|-------------|
| `log_info` | `log_info "message"` | Blue [INFO] prefix |
| `log_success` | `log_success "message"` | Green [OK] prefix |
| `log_warn` | `log_warn "message"` | Yellow [WARN] prefix, to stderr |
| `log_error` | `log_error "message"` | Red [ERROR] prefix, to stderr |

Color variables: `RED`, `GREEN`, `YELLOW`, `BLUE`, `BOLD`, `NC`.

### cli.sh

| Function | Usage | Description |
|----------|-------|-------------|
| `cli_error` | `cli_error "message"` | Print error to stderr |
| `cli_unknown_flag` | `cli_unknown_flag "--flag"` | Standard unknown flag error |
| `cli_require_value` | `cli_require_value "--flag" "$val"` | Enforce required flag value |
| `cli_usage` | `cli_usage <<EOF ... EOF` | Print usage block to stdout |
| `cli_usage_error` | `cli_usage_error "msg" <<EOF ... EOF` | Error + usage to stderr |

### paths.sh

| Function | Usage | Description |
|----------|-------|-------------|
| `paths_init` | `paths_init "${BASH_SOURCE[0]}"` | Set SCRIPT_PATH, SCRIPT_DIR, SCRIPT_NAME |
| `xdg_config_home` | `xdg_config_home` | Resolve XDG config dir (~/.config) |
| `xdg_cache_home` | `xdg_cache_home` | Resolve XDG cache dir (~/.cache) |
| `xdg_data_home` | `xdg_data_home` | Resolve XDG data dir (~/.local/share) |
| `xdg_state_home` | `xdg_state_home` | Resolve XDG state dir (~/.local/state) |

### templates.sh

| Function | Usage | Description |
|----------|-------|-------------|
| `template_read` | `template_read /path/to/file` | Load template content |
| `template_inject` | `template_inject "$tmpl" KEY val` | Replace {{KEY}} placeholders |
| `template_inject_env` | `TPL_KEY=val template_inject_env file` | Replace via env vars (multi-line safe) |

## Script Header Convention

All scripts should include a header block:

```bash
#!/usr/bin/env bash
# script-name.sh
# DESC: One-line description for ./dev help
# Usage: ./dev script-name [options]
# Dependencies: python3, uv
# Idempotent: Yes
```

## Adding a New Script

1. Create `scripts/<name>.sh`
2. Add header with `# DESC:` line (required for ./dev discovery)
3. Source dev.sh: `source "$(dirname "$0")/lib/dev.sh"`
4. Implement `usage()` and `main()` functions
5. End with `main "$@"`
