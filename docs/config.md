# Configuration

strata uses a TOML config file for user preferences.

## File Location

```
~/.config/strata/config.toml
```

Follows XDG Base Directory spec. Override with `XDG_CONFIG_HOME`.

## Format

```toml
[ask]
formatter = "verbose"  # default formatter for strata ask
```

## Available Settings

| Key | Values | Description |
|-----|--------|-------------|
| `ask.formatter` | `default`, `verbose`, `full`, `thread`, `conversations`, `json`, or any drop-in name | Default output formatter for `strata ask` |

## CLI Commands

```bash
# Show all config
strata config

# Show config file path
strata config path

# Get a value
strata config get ask.formatter

# Set a value
strata config set ask.formatter verbose
```

## Precedence

CLI flags always override config:

```
CLI flag > config file > hardcoded default
```

Examples:
- `strata ask "query"` with `ask.formatter = "verbose"` → uses verbose
- `strata ask --json "query"` with `ask.formatter = "verbose"` → uses json (CLI wins)
- `strata ask --format thread "query"` → uses thread (CLI wins)
