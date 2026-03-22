"""Tests for siftd.plugin_discovery module."""

from types import ModuleType

from siftd.plugin_discovery import (
    PluginInfo,
    load_all_extensions,
    load_dropin_modules,
    load_entrypoint_modules,
    validate_dropin_ast,
    validate_dropin_module,
    validate_required_interface,
)


def _ok(mod, origin):
    return None


def _fail(mod, origin):
    return "invalid"


def _mod(name: str = "m", **kw) -> ModuleType:
    m = ModuleType(name)
    for k, v in kw.items():
        setattr(m, k, v)
    return m


class _FakeEPs:
    """Mock entry_points() return with a single group."""

    def __init__(self, eps):
        self._eps = eps

    def select(self, group):
        return self._eps


class _EP:
    """Mock entry point."""

    def __init__(self, name, mod=None, *, error=None):
        self.name = name
        self._mod = mod
        self._error = error

    def load(self):
        if self._error:
            raise self._error
        return self._mod


# --- validate_required_interface ---


class TestValidateRequiredInterface:
    def test_valid(self):
        assert validate_required_interface(
            _mod(VERSION=1, fn=lambda: None), "t", {"VERSION": int}, ["fn"],
        ) is None

    def test_missing_attr(self):
        assert "missing 'V'" in validate_required_interface(_mod(), "t", {"V": int}, [])

    def test_wrong_type(self):
        assert "wrong type" in validate_required_interface(
            _mod(V="x"), "t", {"V": int}, [],
        )

    def test_missing_callable(self):
        assert "missing function" in validate_required_interface(_mod(), "t", {}, ["fn"])

    def test_not_callable(self):
        assert "not callable" in validate_required_interface(
            _mod(fn="x"), "t", {}, ["fn"],
        )


# --- load_dropin_modules ---


class TestLoadDropinModules:
    def test_missing_dir(self, tmp_path):
        assert load_dropin_modules(tmp_path / "no", "p_", _ok) == []

    def test_skips_underscored(self, tmp_path):
        (tmp_path / "_priv.py").write_text("x=1")
        assert load_dropin_modules(tmp_path, "p_", _ok) == []

    def test_loads_valid(self, tmp_path):
        (tmp_path / "good.py").write_text("V=42")
        r = load_dropin_modules(tmp_path, "p_", _ok)
        assert len(r) == 1 and r[0].name == "good" and r[0].origin == "dropin"

    def test_custom_get_name(self, tmp_path):
        (tmp_path / "m.py").write_text("N='X'")
        r = load_dropin_modules(tmp_path, "p_", _ok, get_name=lambda m: m.N)
        assert r[0].name == "X"

    def test_validation_skips(self, tmp_path):
        (tmp_path / "bad.py").write_text("x=1")
        assert load_dropin_modules(tmp_path, "p_", _fail) == []

    def test_import_error_skips(self, tmp_path):
        (tmp_path / "err.py").write_text("raise RuntimeError('boom')")
        assert load_dropin_modules(tmp_path, "p_", _ok) == []


# --- validate_dropin_module ---


class TestValidateDropinModule:
    def test_valid(self, tmp_path):
        (tmp_path / "p.py").write_text("V=1")
        mod, errs = validate_dropin_module(tmp_path / "p.py", "p_", _ok)
        assert mod is not None and errs == []

    def test_errors_with_colon(self, tmp_path):
        (tmp_path / "p.py").write_text("x=1")
        _, errs = validate_dropin_module(
            tmp_path / "p.py", "p_",
            lambda m, o: f"{o}: missing 'A', missing 'B'",
        )
        assert len(errs) == 2 and "missing 'A'" in errs[0]

    def test_error_no_colon(self, tmp_path):
        (tmp_path / "p.py").write_text("x=1")
        _, errs = validate_dropin_module(tmp_path / "p.py", "p_", _fail)
        assert errs == ["invalid"]

    def test_import_error(self, tmp_path):
        (tmp_path / "p.py").write_text("raise RuntimeError")
        _, errs = validate_dropin_module(tmp_path / "p.py", "p_", _ok)
        assert "import failed" in errs[0]


# --- validate_dropin_ast ---


class TestValidateDropinAst:
    def test_valid(self, tmp_path):
        (tmp_path / "g.py").write_text("V=1\ndef f(): pass\nclass C: pass\n")
        assert validate_dropin_ast(tmp_path / "g.py", ["V", "f", "C"]) == []

    def test_missing(self, tmp_path):
        (tmp_path / "p.py").write_text("V=1\n")
        errs = validate_dropin_ast(tmp_path / "p.py", ["V", "f"])
        assert len(errs) == 1 and "missing 'f'" in errs[0]

    def test_syntax_error(self, tmp_path):
        (tmp_path / "b.py").write_text("def (\n")
        assert "syntax error" in validate_dropin_ast(tmp_path / "b.py", ["f"])[0]

    def test_read_error(self, tmp_path):
        assert "read failed" in validate_dropin_ast(tmp_path / "no.py", ["f"])[0]

    def test_annotated_assign(self, tmp_path):
        (tmp_path / "a.py").write_text("V: int = 1\n")
        assert validate_dropin_ast(tmp_path / "a.py", ["V"]) == []


# --- load_all_extensions ---


class TestLoadAllExtensions:
    def test_builtins_only(self, tmp_path):
        b = PluginInfo(name="b", origin="builtin", module=_mod())
        r = load_all_extensions(
            builtins=[b], dropin_path=tmp_path / "no",
            dropin_prefix="p_", entrypoint_group="siftd.none", validate=_ok,
        )
        assert len(r) == 1 and r[0].name == "b"

    def test_dropin_overrides_builtin(self, tmp_path):
        (tmp_path / "x.py").write_text("v=1")
        b = PluginInfo(name="x", origin="builtin", module=_mod())
        r = load_all_extensions(
            builtins=[b], dropin_path=tmp_path,
            dropin_prefix="p_", entrypoint_group="siftd.none", validate=_ok,
        )
        assert len(r) == 1 and r[0].origin == "dropin"


# --- load_entrypoint_modules ---


class TestLoadEntrypointModules:
    def test_no_group(self):
        assert load_entrypoint_modules("siftd.test.none_xyz", _ok) == []

    def test_valid(self, monkeypatch):
        m = _mod("ep", VALUE=1)
        monkeypatch.setattr(
            "importlib.metadata.entry_points",
            lambda: _FakeEPs([_EP("my_ep", m)]),
        )
        r = load_entrypoint_modules("g", _ok)
        assert len(r) == 1 and r[0].name == "my_ep" and r[0].origin == "entrypoint"

    def test_validation_failure(self, monkeypatch):
        monkeypatch.setattr(
            "importlib.metadata.entry_points",
            lambda: _FakeEPs([_EP("bad", _mod())]),
        )
        assert load_entrypoint_modules("g", _fail) == []

    def test_load_error(self, monkeypatch):
        monkeypatch.setattr(
            "importlib.metadata.entry_points",
            lambda: _FakeEPs([_EP("err", error=RuntimeError("boom"))]),
        )
        assert load_entrypoint_modules("g", _ok) == []

    def test_custom_get_name(self, monkeypatch):
        m = _mod("ep", D="Custom")
        monkeypatch.setattr(
            "importlib.metadata.entry_points",
            lambda: _FakeEPs([_EP("ep1", m)]),
        )
        r = load_entrypoint_modules("g", _ok, get_name=lambda m: m.D)
        assert r[0].name == "Custom"
