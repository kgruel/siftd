"""The adapter contract: versions, optional hooks, derivation, drop-ins, template.

Interface version 2 (read + decoder) is the declared interface; version 1
(parse) is legacy and must keep working. The framework derives parse(), the
harness and the peek hooks for a version-2 adapter in one place
(siftd.adapters.contract), so a built-in no longer writes them.
"""

import json
import textwrap
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from siftd.adapters import aider, claude_code, gemini_cli, opencode, template, vscode
from siftd.adapters.assembly import AssemblyError
from siftd.adapters.contract import DecodingAdapter, adapt, decodes
from siftd.adapters.records import Disposition
from siftd.adapters.registry import load_builtin_adapters, load_dropin_adapters
from siftd.adapters.validation import (
    DECODING_VERSION,
    LEGACY_VERSION,
    SUPPORTED_VERSIONS,
    OPTIONAL_ATTRS,
    required_names,
    validate_adapter,
)
from siftd.domain import Source
from siftd.ingestion import ingest_all
from siftd.storage.sqlite import create_database

TEMPLATE_LINES = [
    {"type": "session", "id": "s1", "cwd": "/work", "timestamp": "2025-01-01T00:00:00Z"},
    {"type": "user", "id": "u1", "timestamp": "2025-01-01T00:00:01Z", "text": "hello"},
    {
        "type": "assistant", "id": "a1", "timestamp": "2025-01-01T00:00:02Z", "model": "m", "text": "hi",
        "tool_calls": [{"id": "c1", "name": "read", "input": {"path": "x"}}],
        "usage": {"input_tokens": 3, "output_tokens": 4},
    },
    {"type": "tool_result", "timestamp": "2025-01-01T00:00:03Z", "call_id": "c1", "output": "ok"},
    {"type": "note", "text": "bookkeeping"},
    {"type": "brand_new_type"},
    {"type": "tool_result", "timestamp": "2025-01-01T00:00:04Z", "call_id": "never-called", "output": "x"},
]


def _namespace(version, **overrides):
    """A minimal valid adapter of ``version``, with ``overrides`` applied (None deletes)."""
    ns = SimpleNamespace(
        ADAPTER_INTERFACE_VERSION=version,
        NAME="t",
        DEFAULT_LOCATIONS=[],
        DEDUP_STRATEGY="file",
        HARNESS_SOURCE="test",
        discover=lambda locations=None: [],
        can_handle=lambda source: True,
    )
    if version == LEGACY_VERSION:
        ns.parse = lambda source: []
    else:
        ns.read = lambda source: []
        ns.decoder = lambda source: SimpleNamespace(decode=lambda unit: [])
    for key, value in overrides.items():
        if value is None:
            delattr(ns, key)
        else:
            setattr(ns, key, value)
    return ns


def _write_logs(directory: Path, lines=TEMPLATE_LINES, name="session.jsonl") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    return path


def _v2_dropin(tmp_path: Path, logs: Path) -> Path:
    """The shipped template as a drop-in reading ``logs``: what a new author starts from."""
    adapters = tmp_path / "adapters"
    adapters.mkdir()
    source = Path(template.__file__).read_text()
    (adapters / "my_harness.py").write_text(source.replace('["~/.my_harness/logs"]', f"[{str(logs)!r}]"))
    return adapters


class TestVersions:
    def test_the_supported_set_is_legacy_and_decoding(self):
        assert SUPPORTED_VERSIONS == {LEGACY_VERSION, DECODING_VERSION} == {1, 2}

    @pytest.mark.parametrize("version", [1, 2])
    def test_a_supported_version_is_accepted(self, version):
        assert validate_adapter(_namespace(version)) is None

    @pytest.mark.parametrize("version", [0, 3, 999])
    def test_an_unsupported_version_is_rejected_naming_the_supported_ones(self, version):
        error = validate_adapter(_namespace(LEGACY_VERSION, ADAPTER_INTERFACE_VERSION=version))
        assert error and "incompatible interface version" in error and "1, 2" in error

    def test_version_1_requires_parse(self):
        error = validate_adapter(_namespace(1, parse=None))
        assert error and "'parse'" in error

    def test_version_1_is_not_satisfied_by_read_and_decoder(self):
        ns = _namespace(1, parse=None, read=lambda source: [], decoder=lambda source: None)
        assert "'parse'" in validate_adapter(ns)

    @pytest.mark.parametrize("missing", ["read", "decoder"])
    def test_version_2_requires_read_and_decoder(self, missing):
        error = validate_adapter(_namespace(2, **{missing: None}))
        assert error and f"'{missing}'" in error

    def test_version_2_is_not_satisfied_by_parse(self):
        ns = _namespace(2, read=None, decoder=None, parse=lambda source: [])
        assert "missing required function" in validate_adapter(ns)

    def test_version_2_reader_must_accept_a_source(self):
        error = validate_adapter(_namespace(2, read=lambda: []))
        assert error and "read()" in error and "source" in error

    def test_the_required_names_are_validations(self):
        assert "parse" in required_names(1) and "read" not in required_names(1)
        assert {"read", "decoder"} <= set(required_names(2)) and "parse" not in required_names(2)
        for version in SUPPORTED_VERSIONS:
            assert {"NAME", "discover", "can_handle", "ADAPTER_INTERFACE_VERSION"} <= set(required_names(version))


class TestOptionalHooks:
    GOOD = {
        "SUPPORTS_LIVE_REGISTRATION": True,
        "SUBAGENT_PATH_MARKER": "/subagents/",
        "PEEK_GLOB_PATTERNS": ["**/*.jsonl"],
        "TOOL_HINT_KEYS": {"shell.execute": ["command"]},
        "HARNESS_LOG_FORMAT": "jsonl",
        "HARNESS_DISPLAY_NAME": "T",
        "TOOL_ALIASES": {"Bash": "shell.execute"},
        "SESSION_ID_IN_PATH": False,
        "SUPPORTS_PEEK": False,
        "PEEK_LAST_OF_MANY": True,
        "change_paths": lambda source: [],
        "default_session": lambda source: None,
        "peek_read": lambda source: [],
        "peek_enrich": lambda path, scan: scan,
    }

    @pytest.mark.parametrize("name", sorted(GOOD))
    def test_a_declared_hook_of_the_right_shape_is_accepted(self, name):
        assert validate_adapter(_namespace(2, **{name: self.GOOD[name]})) is None

    @pytest.mark.parametrize(
        "name,value",
        [
            ("SUPPORTS_LIVE_REGISTRATION", "yes"),
            ("SUBAGENT_PATH_MARKER", 1),
            ("PEEK_GLOB_PATTERNS", "**/*.jsonl"),
            ("PEEK_GLOB_PATTERNS", [1]),
            ("TOOL_HINT_KEYS", ["command"]),
            ("TOOL_HINT_KEYS", {"shell.execute": "command"}),
            ("TOOL_ALIASES", {"Bash": 1}),
            ("HARNESS_LOG_FORMAT", 1),
            ("HARNESS_DISPLAY_NAME", 1),
            ("SUPPORTS_PEEK", 1),
            ("PEEK_LAST_OF_MANY", "yes"),
            ("change_paths", "opencode.db"),
            ("change_paths", lambda: []),
            ("default_session", lambda: None),
            ("peek_read", lambda: []),
            ("peek_scan", "x"),
        ],
    )
    def test_a_misdeclared_hook_is_rejected_by_name(self, name, value):
        error = validate_adapter(_namespace(2, **{name: value}))
        assert error and name in error

    def test_every_optional_attribute_has_a_good_example(self):
        assert set(OPTIONAL_ATTRS) - {"SUPPORT_TIER"} <= set(self.GOOD)

    def test_hooks_are_validated_on_a_legacy_adapter_too(self):
        error = validate_adapter(_namespace(1, SUPPORTS_LIVE_REGISTRATION="yes"))
        assert error and "SUPPORTS_LIVE_REGISTRATION" in error


class TestOneDefinitionOfTheRequiredInterface:
    def test_the_doctor_ast_check_reads_validations_names(self, tmp_path):
        from siftd.doctor.checks.drop_ins_valid import DropInsValidCheck

        base = "".join(
            f"{name} = {value!r}\n"
            for name, value in [("NAME", "t"), ("DEFAULT_LOCATIONS", []), ("DEDUP_STRATEGY", "file"), ("HARNESS_SOURCE", "t")]
        )
        funcs = "def discover(locations=None): return []\ndef can_handle(source): return False\n"
        (tmp_path / "legacy.py").write_text(base + "ADAPTER_INTERFACE_VERSION = 1\n" + funcs + "def parse(source): return []\n")
        (tmp_path / "decoding.py").write_text(
            base + "ADAPTER_INTERFACE_VERSION = 2\n" + funcs + "def read(source): return []\ndef decoder(source): return None\n"
        )
        assert DropInsValidCheck()._check_adapters(tmp_path) == []

    def test_a_file_with_neither_interface_names_the_closest_ones_gap(self, tmp_path):
        from siftd.doctor.checks.drop_ins_valid import DropInsValidCheck

        (tmp_path / "bare.py").write_text(
            "ADAPTER_INTERFACE_VERSION = 2\nNAME = 't'\nDEFAULT_LOCATIONS = []\nDEDUP_STRATEGY = 'file'\n"
            "HARNESS_SOURCE = 't'\ndef discover(locations=None): return []\ndef can_handle(source): return False\n"
            "def read(source): return []\n"
        )
        (finding,) = DropInsValidCheck()._check_adapters(tmp_path)
        assert "missing 'decoder'" in finding.message and "parse" not in finding.message

    def test_every_builtin_defines_what_its_version_requires(self):
        for plugin in load_builtin_adapters():
            version = plugin.module.ADAPTER_INTERFACE_VERSION
            missing = [n for n in required_names(version) if not hasattr(plugin.module, n)]
            assert not missing, f"{plugin.name} (v{version}) lacks {missing}"


# Built-ins that still define a derivable hook themselves, and why. Shrink-only:
# a hook leaves this list when the contract can express it; none may join.
EXPLICIT_HOOKS = {
    ("aider", "peek_tail"): "markdown has no records to tail: raw lines",
}
DERIVED = ("parse", "assemble", "HARNESS", "peek_scan", "peek_exchanges", "peek_tail")


class TestDerivation:
    def test_a_builtin_that_decodes_writes_none_of_what_the_framework_derives(self):
        """parse, the harness and the peek hooks are written once, in contract.py."""
        written = set()
        for plugin in load_builtin_adapters():
            module = plugin.module._module if isinstance(plugin.module, DecodingAdapter) else plugin.module
            if not decodes(module):
                continue
            written |= {(plugin.name, name) for name in DERIVED if name in vars(module)}
        assert written == set(EXPLICIT_HOOKS)

    def test_the_explicit_hook_list_only_shrinks(self):
        assert set(EXPLICIT_HOOKS) == {("aider", "peek_tail")}

    def test_gemini_cli_stays_on_the_legacy_interface(self):
        assert gemini_cli.ADAPTER_INTERFACE_VERSION == LEGACY_VERSION
        assert adapt(gemini_cli) is gemini_cli and hasattr(gemini_cli, "parse")

    def test_every_other_builtin_decodes(self):
        decoding = {p.name for p in load_builtin_adapters() if decodes(p.module)}
        assert decoding == {
            "aider", "antigravity_cli", "claude_code", "codex_cli", "copilot_cli", "opencode", "pi_agent", "vscode",
        }

    def test_adapt_is_idempotent_and_leaves_a_non_module_alone(self):
        wrapped = adapt(claude_code)
        assert isinstance(wrapped, DecodingAdapter) and adapt(wrapped) is wrapped
        namespace = _namespace(2)
        assert adapt(namespace) is namespace

    @pytest.mark.parametrize("name", ["claude_code", "aider", "vscode"])
    def test_the_wrapper_keeps_the_modules_description(self, name):
        """Docs generated from the registry read __doc__; the wrapper must not replace it with its own."""
        module = {"claude_code": claude_code, "aider": aider, "vscode": vscode}[name]
        assert adapt(module).__doc__ == module.__doc__ and module.__doc__

    def test_the_wrapper_reads_the_module_it_wraps(self):
        wrapped = adapt(claude_code)
        assert wrapped.NAME == "claude_code" and wrapped.DEDUP_STRATEGY == "file"
        assert wrapped.HARNESS.name == "claude_code" and wrapped.HARNESS.display_name == "Claude Code"
        assert wrapped.HARNESS.log_format == "jsonl" and wrapped.HARNESS.source == "anthropic"

    def test_parse_is_the_conversations_of_assemble(self, tmp_path):
        path = _write_logs(tmp_path / "logs")
        module = _v2_module(tmp_path)
        source = Source(kind="file", location=path)
        assembly = module.assemble(source)
        assert [c.external_id for c in module.parse(source)] == [c.external_id for c in assembly.conversations]
        assert assembly.ledger.disposed == assembly.ledger.records

    def test_peek_is_derived_unless_the_adapter_opts_out(self):
        assert hasattr(adapt(claude_code), "peek_scan") and hasattr(adapt(vscode), "peek_exchanges")
        assert not hasattr(adapt(opencode), "peek_scan") and not hasattr(adapt(opencode), "peek_tail")

    def test_an_explicit_peek_hook_wins_over_the_derived_one(self):
        assert adapt(aider).peek_tail is aider.peek_tail
        assert adapt(aider).peek_scan is not getattr(aider, "peek_scan", None)

    def test_a_peek_reader_overrides_the_ingest_reader_for_peek_only(self, tmp_path):
        module = _v2_module(tmp_path, peek_read=lambda source: [{"type": "user", "id": "p", "timestamp": "2025-01-01T00:00:00Z", "text": "from peek_read"}])
        path = _write_logs(tmp_path / "logs")
        (exchange,) = module.peek_exchanges(path, 5)
        assert exchange.prompt_text == "from peek_read"
        (conversation,) = module.parse(Source(kind="file", location=path))
        assert conversation.prompts[0].external_id == "u1"

    def test_peek_enrich_refines_the_derived_scan(self, tmp_path):
        seen = []

        def enrich(path, scan):
            seen.append(path)
            scan.attributes["enriched"] = "yes"
            return scan

        module = _v2_module(tmp_path, peek_enrich=enrich)
        path = _write_logs(tmp_path / "logs")
        assert module.peek_scan(path).attributes["enriched"] == "yes" and seen == [path]

    def test_peek_last_of_many_shows_the_live_conversation(self, tmp_path):
        at = "2025-01-01T00:00:00Z"
        two = [
            {"type": "session", "id": "first"}, {"type": "user", "timestamp": at, "text": "one"},
            {"type": "session", "id": "second"}, {"type": "user", "timestamp": at, "text": "two"},
        ]
        path = _write_logs(tmp_path / "logs", two)
        with pytest.raises(AssemblyError):
            _v2_module(tmp_path, DEDUP_STRATEGY="session").peek_scan(path)
        module = _v2_module(tmp_path, DEDUP_STRATEGY="session", PEEK_LAST_OF_MANY=True)
        assert module.peek_scan(path).session_id.endswith("second")


def _v2_module(tmp_path, **overrides) -> DecodingAdapter:
    """The shipped template, loaded as a module, with ``overrides`` set on it, as adapted."""
    module = ModuleType("template_under_test")
    exec(compile(Path(template.__file__).read_text(), "template.py", "exec"), module.__dict__)  # noqa: S102
    for key, value in overrides.items():
        setattr(module, key, value)
    return adapt(module)


class TestTheTemplate:
    def test_it_loads_validates_and_is_a_decoding_adapter(self, tmp_path):
        logs = tmp_path / "logs"
        failures = []
        (plugin,) = load_dropin_adapters(_v2_dropin(tmp_path, logs), failures_out=failures)
        assert failures == [] and plugin.name == "my_harness"
        assert decodes(plugin.module) and isinstance(plugin.module, DecodingAdapter)
        assert validate_adapter(template) is None
        assert template.ADAPTER_INTERFACE_VERSION == DECODING_VERSION

    def test_it_decodes_a_tiny_fixture_and_accounts_for_every_record(self, tmp_path):
        logs = tmp_path / "logs"
        path = _write_logs(logs)
        (plugin,) = load_dropin_adapters(_v2_dropin(tmp_path, logs))
        assembly = plugin.module.assemble(Source(kind="file", location=path))

        (conversation,) = assembly.conversations
        assert conversation.external_id == "my_harness::s1" and conversation.workspace_path == "/work"
        (prompt,) = conversation.prompts
        (response,) = prompt.responses
        assert response.model == "m" and (response.usage.input_tokens, response.usage.output_tokens) == (3, 4)
        (call,) = response.tool_calls
        assert (call.tool_name, call.status, call.result) == ("read", "success", {"output": "ok"})

        ledger = assembly.ledger
        assert ledger.count(Disposition.UNKNOWN) == 1  # brand_new_type
        assert ledger.count(Disposition.ORPHAN) == 1  # a result whose call never appears
        assert ledger.count(Disposition.IGNORED) == 1  # note
        assert ledger.disposed == ledger.records

    def test_a_source_that_states_no_session_is_named_by_its_file(self, tmp_path):
        logs = tmp_path / "logs"
        path = _write_logs(logs, [TEMPLATE_LINES[1], TEMPLATE_LINES[2]], name="loose.jsonl")
        (plugin,) = load_dropin_adapters(_v2_dropin(tmp_path, logs))
        (conversation,) = plugin.module.parse(Source(kind="file", location=path))
        assert conversation.external_id == "my_harness::loose"

    def test_peek_comes_with_it(self, tmp_path):
        logs = tmp_path / "logs"
        path = _write_logs(logs)
        (plugin,) = load_dropin_adapters(_v2_dropin(tmp_path, logs))
        scan = plugin.module.peek_scan(path)
        assert scan.session_id.endswith("s1") and scan.exchange_count == 1
        (exchange,) = plugin.module.peek_exchanges(path, 5)
        assert exchange.prompt_text == "hello" and exchange.tool_calls == [("read", 1)]


class TestDropInsIngest:
    def test_a_v2_dropin_ingests_and_its_ledger_reaches_the_stats(self, tmp_path):
        logs = tmp_path / "logs"
        _write_logs(logs)
        (plugin,) = load_dropin_adapters(_v2_dropin(tmp_path, logs))
        conn = create_database(tmp_path / "v2.db")
        stats = ingest_all(conn, [plugin.module])
        assert stats.files_ingested == 1 and stats.conversations == 1 and stats.tool_calls == 1
        counts = stats.by_harness["my_harness"]
        assert (counts["unknown"], counts["orphan"], counts["ignored"]) == (1, 1, 1)

    def test_the_registry_hands_out_a_decoding_dropin_with_its_derived_interface(self, tmp_path):
        from siftd.adapters.registry import load_all_adapters

        adapters = _v2_dropin(tmp_path, tmp_path / "logs")
        (plugin,) = [p for p in load_all_adapters(dropin_path=adapters) if p.name == "my_harness"]
        assert isinstance(plugin.module, DecodingAdapter)
        assert callable(plugin.module.parse) and callable(plugin.module.peek_scan)

    def test_a_v2_dropin_is_ingested_when_handed_to_ingest_unwrapped(self, tmp_path):
        """ingest_all adapts what it is given, so a bare module works as the registry's does."""
        logs = tmp_path / "logs"
        _write_logs(logs)
        adapters = _v2_dropin(tmp_path, logs)
        from siftd.plugin_discovery import load_dropin_modules

        (plugin,) = load_dropin_modules(
            adapters, module_name_prefix="bare_", validate=validate_adapter, get_name=lambda m: m.NAME
        )
        assert isinstance(plugin.module, ModuleType)
        stats = ingest_all(create_database(tmp_path / "bare.db"), [plugin.module])
        assert stats.conversations == 1

    def test_a_v1_dropin_still_loads_and_ingests_with_no_ledger(self, tmp_path):
        logs = tmp_path / "logs"
        _write_logs(logs)
        adapters = tmp_path / "adapters"
        adapters.mkdir()
        (adapters / "legacy.py").write_text(
            textwrap.dedent(
                f"""
                from pathlib import Path
                from siftd.adapters.sdk import build_harness
                from siftd.domain import ContentBlock, Conversation, Prompt, Response, Source

                ADAPTER_INTERFACE_VERSION = 1
                NAME = "legacy"
                DEFAULT_LOCATIONS = [{str(logs)!r}]
                DEDUP_STRATEGY = "file"
                HARNESS_SOURCE = "test"

                def discover(locations=None):
                    for path in sorted(Path({str(logs)!r}).glob("*.jsonl")):
                        yield Source(kind="file", location=path)

                def can_handle(source):
                    return source.kind == "file"

                def parse(source):
                    conversation = Conversation(
                        external_id="legacy::s", harness=build_harness(NAME, HARNESS_SOURCE, "jsonl"),
                        started_at="2025-01-01T00:00:00Z",
                    )
                    prompt = Prompt(timestamp="2025-01-01T00:00:01Z")
                    prompt.content.append(ContentBlock(block_type="text", content={{"text": "hi"}}))
                    prompt.responses.append(Response(timestamp="2025-01-01T00:00:02Z"))
                    conversation.prompts.append(prompt)
                    return [conversation]
                """
            )
        )
        failures = []
        (plugin,) = load_dropin_adapters(adapters, failures_out=failures)
        assert failures == [] and plugin.module.ADAPTER_INTERFACE_VERSION == LEGACY_VERSION
        assert not decodes(plugin.module) and not hasattr(plugin.module, "assemble")

        stats = ingest_all(create_database(tmp_path / "v1.db"), [plugin.module])
        assert stats.files_ingested == 1 and stats.conversations == 1
        counts = stats.by_harness["legacy"]
        assert (counts["unknown"], counts["orphan"], counts["ignored"]) == (0, 0, 0)

    def test_a_v1_dropin_that_imports_the_old_version_constant_still_loads(self, tmp_path):
        """Before version 2, drop-ins took their version from validation.ADAPTER_INTERFACE_VERSION."""
        adapters = tmp_path / "adapters"
        adapters.mkdir()
        (adapters / "oldstyle.py").write_text(
            textwrap.dedent(
                """
                from siftd.adapters.validation import ADAPTER_INTERFACE_VERSION

                NAME = "oldstyle"
                DEFAULT_LOCATIONS = []
                DEDUP_STRATEGY = "file"
                HARNESS_SOURCE = "test"

                def discover(locations=None):
                    return []

                def can_handle(source):
                    return False

                def parse(source):
                    return []
                """
            )
        )
        failures = []
        (plugin,) = load_dropin_adapters(adapters, failures_out=failures)
        assert failures == [] and plugin.module.ADAPTER_INTERFACE_VERSION == LEGACY_VERSION
