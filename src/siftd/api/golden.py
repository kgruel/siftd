"""Golden assembly dump: a source's conversations and ledger as canonical JSON.

The differential-testing oracle for the Rust rewrite. One file per source,
format ``golden_version`` 1: UTF-8, keys sorted recursively, indent 1, non-ASCII
kept, written by :func:`golden_json`. Comparison on the consuming side is by
parsed JSON value, never by bytes.

The parse path is not changed here: a version-2 adapter is run through
``adapt(module).assemble(source)`` so the ledger comes from the same parse as
the conversations; a version-1 adapter through ``parse(source)`` with
``"ledger": null``.

Two conventions make a dump reproducible on any machine, both the ones the
adapter golden fixtures already use (``tests/_golden.py``): the process zone is
pinned to UTC while parsing, and a fixture is read from the spelling
``tests/fixtures/<path under the fixtures root>``, relative to a working
directory in which that path resolves. Aider builds the conversation id from the
path and its workspace from the file's directory, so only that spelling is the
same on every machine.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from siftd.adapters.assembly import Ledger
from siftd.adapters.contract import decodes
from siftd.adapters.registry import load_builtin_adapters
from siftd.domain.source import Source
from siftd.errors import UserInputError

GOLDEN_VERSION = 1

#: The zone every dump is parsed under (naive log timestamps resolve against it).
GOLDEN_TZ = "UTC"

#: Where a fixture appears to live while it is parsed: ``<prefix>/<path under the fixtures root>``.
FIXTURE_PREFIX = Path("tests/fixtures")


# --- JSON ----------------------------------------------------------------------


def golden_json(obj: Any) -> str:
    """The canonical text of a golden document (sorted keys, indent 1, non-ASCII kept)."""
    return json.dumps(obj, sort_keys=True, indent=1, ensure_ascii=False) + "\n"


def ledger_to_json(ledger: Ledger) -> dict[str, Any]:
    """A ledger as its golden object: counts, and ``Type:disposition`` / ``disposition:reason`` keys."""
    return {
        "units": ledger.units,
        "records": ledger.records,
        "synthesized": ledger.synthesized,
        "empty_conversations": ledger.empty_conversations,
        "dispositions": {f"{kind}:{disposition.value}": n for (kind, disposition), n in ledger.dispositions.items()},
        "reasons": {f"{disposition.value}:{reason}": n for (disposition, reason), n in ledger.reasons.items()},
    }


# --- Environment ---------------------------------------------------------------


def siftd_commit() -> str:
    """The git sha of the working tree this code runs from, or ``"unknown"``."""
    try:
        done = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return done.stdout.strip() or "unknown"


@contextmanager
def _pinned_tz(zone: str) -> Iterator[None]:
    """Pin the process zone for the block (``time.tzset`` makes the change take effect)."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = zone
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


@contextmanager
def _fixture_view(root: Path) -> Iterator[None]:
    """Make ``root`` appear at ``tests/fixtures`` under the working directory for the block.

    A scratch directory holds a symlink, and the process works from it, so a
    fixture is parsed from the relative spelling the repository's own golden
    tests use on every machine, whatever the real location of ``root`` is.
    """
    with tempfile.TemporaryDirectory() as tmp:
        view = Path(tmp)
        (view / FIXTURE_PREFIX).parent.mkdir(parents=True)
        (view / FIXTURE_PREFIX).symlink_to(root, target_is_directory=True)
        previous = Path.cwd()
        os.chdir(view)
        try:
            yield
        finally:
            os.chdir(previous)


def _builtin_adapters() -> dict[str, Any]:
    """The built-in adapters by NAME, each as consumers use it (wrapped)."""
    return {plugin.name: plugin.module for plugin in load_builtin_adapters()}


# --- One source ----------------------------------------------------------------


def assemble_source(adapter: Any, source: Source) -> dict[str, Any]:
    """The golden body of one source: ``conversations`` and ``ledger``."""
    with _pinned_tz(GOLDEN_TZ):
        if decodes(adapter):
            assembly = adapter.assemble(source)
            conversations = list(assembly.conversations)
            ledger: dict[str, Any] | None = ledger_to_json(assembly.ledger)
        else:
            conversations = list(adapter.parse(source))
            ledger = None
    return {"conversations": [dataclasses.asdict(c) for c in conversations], "ledger": ledger}


def golden_document(adapter: Any, source: Source, *, label: str, commit: str) -> dict[str, Any]:
    """The whole golden file for one source, ``label`` being how it is named in the file."""
    return {
        "golden_version": GOLDEN_VERSION,
        "adapter": adapter.NAME,
        "source": label,
        "siftd_commit": commit,
        **assemble_source(adapter, source),
    }


def _pick_adapter(path: Path, adapters: dict[str, Any], requested: str | None) -> Any:
    """The adapter for ``path``: the one asked for, one its path names, or the only one that can handle it."""
    if requested is not None:
        if requested not in adapters:
            raise UserInputError(f"Unknown adapter {requested!r}. Built-in adapters: {', '.join(sorted(adapters))}")
        return adapters[requested]
    named = [name for name in sorted(adapters) if name in path.parts or path.name.startswith(f"{name}_")]
    if len(named) == 1:
        return adapters[named[0]]
    source = Source(kind="file", location=path)
    able = [name for name in sorted(adapters) if adapters[name].can_handle(source)]
    if len(able) == 1:
        return adapters[able[0]]
    if not able:
        raise UserInputError(f"No built-in adapter can handle {path}. Name one with --adapter.")
    raise UserInputError(f"{path} could be read by {', '.join(able)}. Name one with --adapter.")


def dump_source(
    path: str | Path,
    *,
    adapter: str | None = None,
    root: str | Path | None = None,
    commit: str | None = None,
) -> dict[str, Any]:
    """The golden document for the one source at ``path``.

    ``root`` is a fixtures root: the document's ``source`` is then the path
    relative to it and the source is read as ``tests/fixtures/<that path>``, as
    :func:`dump_fixtures` does. Without it ``source`` is the path as given and
    anything the adapter builds from the path is this machine's. A ``.sql`` file
    is materialized as the database the adapter reads.
    """
    file = Path(path)
    if not file.is_file():
        raise UserInputError(f"Not a file: {file}")
    file = file.resolve()
    chosen = _pick_adapter(file, _builtin_adapters(), adapter)
    commit = commit or siftd_commit()
    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        if root is None:
            return golden_document(chosen, _source_for(file, {}, scratch), label=str(path), commit=commit)
        fixtures = Path(root).resolve()
        try:
            label = file.relative_to(fixtures).as_posix()
        except ValueError:
            raise UserInputError(f"{file} is not under the fixtures root {fixtures}") from None
        located = FIXTURE_PREFIX / label
        with _fixture_view(fixtures):
            source = _source_for(located, {"identity_path": located.as_posix()}, scratch)
            return golden_document(chosen, source, label=label, commit=commit)


def _source_for(file: Path, metadata: dict[str, Any], scratch: Path) -> Source:
    """A Source for ``file``; a ``.sql`` file becomes the SQLite database it builds in ``scratch``."""
    if file.suffix == ".sql":
        db = scratch / "opencode.db"
        db.unlink(missing_ok=True)
        conn = sqlite3.connect(str(db))
        try:
            conn.executescript(file.read_text(encoding="utf-8"))
        finally:
            conn.close()
        return Source(kind="sqlite", location=db, metadata=metadata)
    return Source(kind="file", location=file, metadata=metadata)


# --- A fixtures tree -------------------------------------------------------------


@dataclass
class DumpedFile:
    """One golden file written by :func:`dump_fixtures`."""

    adapter: str
    case: str
    source: str
    path: Path
    conversations: int
    records: int | None  # None for a version-1 adapter (no ledger)
    discovered: bool  # False when the case's input was taken from the case directory, not discover()


@dataclass
class FixtureDump:
    """What :func:`dump_fixtures` wrote, and what it found nothing for."""

    commit: str
    files: list[DumpedFile] = field(default_factory=list)
    #: Adapters whose ``discover()`` yielded no source for some case directory (input taken from the case instead).
    undiscovered: dict[str, list[str]] = field(default_factory=dict)
    #: Adapters with no fixtures at all.
    empty: list[str] = field(default_factory=list)

    @property
    def adapters(self) -> list[str]:
        return sorted({f.adapter for f in self.files})


def _case_input(case_dir: Path) -> Path:
    """The one input file of a case directory that discover() does not reach (as the golden tests read it)."""
    setup = case_dir / "setup.sql"
    if setup.is_file():
        return setup
    candidates = sorted(f for f in case_dir.iterdir() if f.is_file() and f.name != "expected.json")
    if len(candidates) != 1:
        raise UserInputError(
            f"{case_dir}: expected exactly one input file besides expected.json, found {len(candidates)}"
        )
    return candidates[0]


def dump_fixtures(fixtures: str | Path, out: str | Path, *, commit: str | None = None) -> FixtureDump:
    """Write one golden JSON per fixture source under ``fixtures``, at ``out/<adapter>/<case>.json``.

    Sources are what each built-in adapter discovers under
    ``<fixtures>/adapters/<adapter>/<case>/`` (one case at a time, so a case's
    name is its directory's), plus the top-level ``<adapter>_minimal.*`` files.
    A case directory whose layout ``discover()`` does not recognize (the fixture
    is a bare ``input.jsonl``, not the real log tree) is read the way the golden
    tests read it, and is recorded in :attr:`FixtureDump.undiscovered`.
    """
    root = Path(fixtures).resolve()
    out_dir = Path(out).resolve()
    if not (root / "adapters").is_dir():
        raise UserInputError(f"Not a fixtures directory (no adapters/ inside): {root}")
    adapters = _builtin_adapters()
    stray = sorted(d.name for d in (root / "adapters").iterdir() if d.is_dir() and d.name not in adapters)
    if stray:
        raise UserInputError(f"Fixture directories with no built-in adapter: {', '.join(stray)}")
    commit = commit or siftd_commit()
    result = FixtureDump(commit=commit)

    def write(adapter: Any, case: str, source: Source, label: str, discovered: bool) -> None:
        document = golden_document(adapter, source, label=label, commit=commit)
        target = out_dir / adapter.NAME / f"{case}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(golden_json(document), encoding="utf-8", newline="\n")
        ledger = document["ledger"]
        result.files.append(DumpedFile(
            adapter=adapter.NAME, case=case, source=label, path=target,
            conversations=len(document["conversations"]),
            records=None if ledger is None else ledger["records"],
            discovered=discovered,
        ))

    def source_at(file: Path, scratch: Path) -> tuple[Source, str]:
        label = file.relative_to(FIXTURE_PREFIX).as_posix()
        return _source_for(file, {"identity_path": file.as_posix()}, scratch), label

    with tempfile.TemporaryDirectory() as tmp, _fixture_view(root):
        scratch = Path(tmp)
        for name in sorted(adapters):
            adapter = adapters[name]
            adapter_dir = FIXTURE_PREFIX / "adapters" / name
            before = len(result.files)
            cases = sorted(d for d in adapter_dir.iterdir() if d.is_dir()) if adapter_dir.is_dir() else []
            for n, case_dir in enumerate(cases):
                found = sorted(Path(s.location) for s in adapter.discover(locations=[str(case_dir)]))
                discovered = bool(found)
                if not found:
                    result.undiscovered.setdefault(name, []).append(case_dir.name)
                    found = [_case_input(case_dir)]
                for file in found:
                    work = scratch / f"{name}-{n}"
                    work.mkdir(exist_ok=True)
                    source, label = source_at(file, work)
                    case = case_dir.name if len(found) == 1 else f"{case_dir.name}__{file.stem}"
                    write(adapter, case, source, label, discovered)
            for file in sorted(FIXTURE_PREFIX.glob(f"{name}_minimal.*")):
                source, label = source_at(file, scratch)
                write(adapter, file.stem, source, label, True)
            if len(result.files) == before:
                result.empty.append(name)
    return result
