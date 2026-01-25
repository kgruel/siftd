# Configuration

tbd uses a TOML config file for user preferences.

## File Location

```
~/.config/tbd/config.toml
```

Follows XDG Base Directory spec. Override with `XDG_CONFIG_HOME`.

## Format

```toml
[ask]
formatter = "verbose"  # default formatter for tbd ask
```

## Available Settings

| Key | Values | Description |
|-----|--------|-------------|
| `ask.formatter` | `default`, `verbose`, `full`, `thread`, `conversations`, `json`, or any drop-in name | Default output formatter for `tbd ask` |

## CLI Commands

```bash
# Show all config
tbd config

# Show config file path
tbd config path

# Get a value
tbd config get ask.formatter

# Set a value
tbd config set ask.formatter verbose
```

## Precedence

CLI flags always override config:

```
CLI flag > config file > hardcoded default
```

Examples:
- `tbd ask "query"` with `ask.formatter = "verbose"` → uses verbose
- `tbd ask --json "query"` with `ask.formatter = "verbose"` → uses json (CLI wins)
- `tbd ask --format thread "query"` → uses thread (CLI wins)
