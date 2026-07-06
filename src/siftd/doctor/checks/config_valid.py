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

        embed_config = doc.get("embed", {})
        if isinstance(embed_config, dict):
            findings.extend(self._validate_embed(embed_config))

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

    def _validate_embed(self, embed_config: dict) -> list[Finding]:
        """Validate the [embed] table: backend membership, dimensions sanity, and
        api_key-ref resolvability *shape* (no network calls)."""
        findings: list[Finding] = []

        backend = embed_config.get("backend")
        if backend is not None:
            from siftd.embeddings.presets import preset_names

            valid = [*preset_names(), "fastembed", "off"]
            name = str(backend).strip().lower()
            if name and name not in valid:
                findings.append(
                    Finding(
                        check=self.name,
                        severity="warning",
                        message=f"Unknown embed.backend '{backend}' (valid: {', '.join(valid)})",
                        fix_available=False,
                        context={"backend": str(backend), "valid_backends": valid},
                    )
                )

        dimensions = embed_config.get("dimensions")
        if dimensions is not None:
            try:
                dim = int(dimensions)
                ok = dim > 0
            except (ValueError, TypeError):
                ok = False
            if not ok:
                findings.append(
                    Finding(
                        check=self.name,
                        severity="warning",
                        message=f"embed.dimensions must be a positive integer, got {dimensions!r}",
                        fix_available=False,
                        context={"dimensions": str(dimensions)},
                    )
                )

        api_key = embed_config.get("api_key")
        if api_key:
            from siftd.credentials import TokenRefError, resolve_token_ref

            try:
                resolve_token_ref(str(api_key))
            except TokenRefError as e:
                findings.append(
                    Finding(
                        check=self.name,
                        severity="warning",
                        message=f"embed.api_key is unresolvable: {e}",
                        fix_available=False,
                        context={"error": str(e)},
                    )
                )

        return findings

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
