"""Legacy formatter registry — deprecated.

The old FormatterRegistry class has been replaced by the output format
registry in format_registry.py, which uses module-level render functions
instead of class-based formatters.

For validation, use siftd.output.validation.validate_formatter directly.
For drop-in loading, use siftd.output.format_registry.load_all_formats.
"""
