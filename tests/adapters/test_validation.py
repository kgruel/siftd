"""Tests for adapter signature validation (H16)."""

from types import SimpleNamespace

from siftd.adapters.validation import validate_adapter
from siftd.doctor.checks.drop_ins_valid import DropInsValidCheck


def _valid_ns(**overrides):
    """Build a minimal valid adapter namespace, overriding named attributes."""
    ns = SimpleNamespace(
        ADAPTER_INTERFACE_VERSION=1,
        NAME="test",
        DEFAULT_LOCATIONS=[],
        DEDUP_STRATEGY="file",
        HARNESS_SOURCE="test",
        discover=lambda locations=None: [],
        can_handle=lambda source: True,
        parse=lambda source: [],
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


class TestValidateAdapterSignatures:
    def test_valid_adapter_passes(self):
        assert validate_adapter(_valid_ns()) is None

    def test_discover_missing_locations(self):
        ns = _valid_ns(discover=lambda: [])
        error = validate_adapter(ns)
        assert error and "discover()" in error and "locations" in error

    def test_can_handle_missing_source(self):
        ns = _valid_ns(can_handle=lambda: True)
        error = validate_adapter(ns)
        assert error and "can_handle()" in error and "source" in error

    def test_parse_missing_source(self):
        ns = _valid_ns(parse=lambda: [])
        error = validate_adapter(ns)
        assert error and "parse()" in error and "source" in error

    def test_extra_kwargs_allowed(self):
        """Extra optional parameters on adapter functions must not trigger an error."""
        ns = _valid_ns(
            discover=lambda locations=None, extra=None: [],
            can_handle=lambda source, debug=False: True,
            parse=lambda source, options=None: [],
        )
        assert validate_adapter(ns) is None

    def test_error_message_names_function(self):
        """Error message must name the offending function clearly."""
        error = validate_adapter(_valid_ns(parse=lambda: []))
        assert error is not None
        assert "parse()" in error


class TestSupportTier:
    def test_absent_tier_is_valid(self):
        assert validate_adapter(_valid_ns()) is None

    def test_valid_tiers_pass(self):
        for tier in ("core", "contrib", "frozen"):
            assert validate_adapter(_valid_ns(SUPPORT_TIER=tier)) is None

    def test_invalid_tier_rejected(self):
        error = validate_adapter(_valid_ns(SUPPORT_TIER="experimental"))
        assert error and "SUPPORT_TIER" in error and "experimental" in error

    def test_all_builtins_declare_valid_tier(self):
        from siftd.adapters.registry import load_builtin_adapters
        from siftd.adapters.validation import VALID_SUPPORT_TIERS

        for plugin in load_builtin_adapters():
            tier = getattr(plugin.module, "SUPPORT_TIER", None)
            assert tier in VALID_SUPPORT_TIERS, f"{plugin.name}: SUPPORT_TIER={tier!r}"


class TestDropInsValidSignatures:
    """Doctor check reports signature errors in drop-in adapter files."""

    _BASE = (
        "ADAPTER_INTERFACE_VERSION = 1\n"
        "NAME = 'test'\n"
        "DEFAULT_LOCATIONS = []\n"
        "DEDUP_STRATEGY = 'file'\n"
        "HARNESS_SOURCE = 'test'\n"
    )

    def _write_adapter(self, tmp_path, funcs: str) -> None:
        (tmp_path / "my_adapter.py").write_text(self._BASE + funcs)

    def test_discover_missing_locations_reported(self, tmp_path):
        self._write_adapter(
            tmp_path,
            "def discover(): return []\n"
            "def can_handle(source): return False\n"
            "def parse(source): return []\n",
        )
        findings = DropInsValidCheck()._check_adapters(tmp_path)
        assert findings
        assert "discover()" in findings[0].message
        assert "locations" in findings[0].message

    def test_can_handle_missing_source_reported(self, tmp_path):
        self._write_adapter(
            tmp_path,
            "def discover(locations=None): return []\n"
            "def can_handle(): return False\n"
            "def parse(source): return []\n",
        )
        findings = DropInsValidCheck()._check_adapters(tmp_path)
        assert findings
        assert "can_handle()" in findings[0].message
        assert "source" in findings[0].message

    def test_parse_missing_source_reported(self, tmp_path):
        self._write_adapter(
            tmp_path,
            "def discover(locations=None): return []\n"
            "def can_handle(source): return False\n"
            "def parse(): return []\n",
        )
        findings = DropInsValidCheck()._check_adapters(tmp_path)
        assert findings
        assert "parse()" in findings[0].message
        assert "source" in findings[0].message

    def test_extra_kwargs_allowed(self, tmp_path):
        """Extra optional params on all three functions → no finding."""
        self._write_adapter(
            tmp_path,
            "def discover(locations=None, extra=None): return []\n"
            "def can_handle(source, debug=False): return False\n"
            "def parse(source, options=None): return []\n",
        )
        assert not DropInsValidCheck()._check_adapters(tmp_path)

    def test_valid_adapter_no_findings(self, tmp_path):
        self._write_adapter(
            tmp_path,
            "def discover(locations=None): return []\n"
            "def can_handle(source): return False\n"
            "def parse(source): return []\n",
        )
        assert not DropInsValidCheck()._check_adapters(tmp_path)
