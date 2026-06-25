from siftd.doctor.checks import CheckContext, CheckCost, Finding


class ConfigValidCheck:
    """Validates configuration file syntax and known keys."""

    name = "config-valid"
    description = "Configuration file syntax and values"
    has_fix = False
    requires_db = False
    requires_embed_db = False
    cost: CheckCost = "fast"

    def run(self, ctx: CheckContext) -> list[Finding]:
        from siftd.paths import config_file

        path = config_file()
        if not path.exists():
            return []

        findings = []

        try:
            import tomlkit
            import tomlkit.exceptions

            content = path.read_text()
            doc = tomlkit.parse(content)
        except tomlkit.exceptions.TOMLKitError as e:
            return [
                Finding(
                    check=self.name,
                    severity="error",
                    message=f"Invalid TOML syntax in config file: {e}",
                    fix_available=False,
                    context={"path": str(path), "error": str(e)},
                )
            ]
        except OSError as e:
            return [
                Finding(
                    check=self.name,
                    severity="error",
                    message=f"Cannot read config file: {e}",
                    fix_available=False,
                    context={"path": str(path), "error": str(e)},
                )
            ]

        search_config = doc.get("search", {})
        if isinstance(search_config, dict):
            formatter = search_config.get("formatter")
            if formatter is not None:
                findings.extend(self._validate_formatter(str(formatter)))

        ui_config = doc.get("ui", {})
        if isinstance(ui_config, dict):
            theme = ui_config.get("theme")
            if theme is not None:
                findings.extend(self._validate_theme(str(theme)))

        return findings

    def _validate_formatter(self, formatter_name: str) -> list[Finding]:
        """Validate that the formatter name is registered."""
        from siftd.output.format_registry import list_format_names

        valid_names = list_format_names()
        if formatter_name not in valid_names:
            return [
                Finding(
                    check=self.name,
                    severity="warning",
                    message=f"Unknown formatter '{formatter_name}' in config (valid: {', '.join(sorted(valid_names))})",
                    fix_available=False,
                    context={"formatter": formatter_name, "valid_formatters": valid_names},
                )
            ]
        return []

    def _validate_theme(self, theme_name: str) -> list[Finding]:
        """Validate that the ui.theme name is a known terminal theme."""
        from siftd.output.theme import THEME_NAMES

        # Normalize exactly as theme_for_name() does (.strip().lower()) so the check
        # mirrors the resolver — otherwise `ui.theme = "Nord"` renders fine but the
        # doctor spuriously flags it, the inverse of the check's purpose.
        if theme_name.strip().lower() not in THEME_NAMES:
            return [
                Finding(
                    check=self.name,
                    severity="warning",
                    message=f"Unknown ui.theme '{theme_name}' in config (valid: {', '.join(THEME_NAMES)})",
                    fix_available=False,
                    context={"theme": theme_name, "valid_themes": list(THEME_NAMES)},
                )
            ]
        return []
